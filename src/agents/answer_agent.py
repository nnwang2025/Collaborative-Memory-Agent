import json
import re
import urllib.error
import urllib.request

from src.llm_config import resolve_api_key, resolve_base_url, resolve_model_name


class AnswerAgent:
    """Compose final answers from one or more skill results.

    The configured chat model is used only for Evidence -> Summary. Route,
    retrieval and skill selection stay outside this class.
    """

    SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
    DEFAULT_SILICONFLOW_MODEL = "Qwen/Qwen2.5-14B-Instruct"
    SOURCE_LABELS = {
        "chat": "讨论记录",
        "issue": "Issue/评审记录",
        "git_commit": "提交信息",
        "document": "文档记录",
        "meeting": "会议记录",
        "log": "日志记录",
        "code": "代码记录",
        "memory": "记忆记录",
        "unknown": "来源记录",
    }
    COMMIT_HASH = re.compile(r"\bcommit\s+([0-9a-f]{7,40})\b", re.IGNORECASE)
    DIFF_MARKER = re.compile(r"(?m)^(?:diff --git|@@|--- a/|\+\+\+ b/)")
    RETRIEVAL_ENTITIES = [
        "Embedding Retrieval",
        "BM25",
        "Rerank",
        "Entity Retrieval",
        "Exact Match",
    ]
    TIME_ASSERTION = re.compile(
        r"\b(?:19|20)\d{2}[-/]\d{1,2}(?:[-/]\d{1,2})?\b|\bQ[1-4]\b|"
        r"(?:19|20)\d{2}年|\d{1,2}月|\d{1,2}日|今天|昨天|前天|本周|上周|下周|"
        r"今年|去年|明年|季度|年初|年底|月底|"
        r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December|"
        r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+(?:\d{1,2}(?:,\s*\d{4})?|\d{4})\b",
        re.IGNORECASE,
    )

    def __init__(self):
        # Attribute names are kept for compatibility with existing callers and
        # tests; the shared resolver supports DeepSeek official settings first
        # and still honours legacy SiliconFlow/OpenAI variables.
        self.base_url = resolve_base_url()
        self.siliconflow_api_key = resolve_api_key(self.base_url)
        self.siliconflow_model = resolve_model_name(self.base_url)

    @staticmethod
    def _decision_memory_lines(record, label):
        lines = [f"- {label}: {record['version_id']} status={record['status']} "
                 f"event_time={record['event_time']} decision_known_at={record.get('decision_known_at') or record.get('reviewed_at')} "
                 f"recorded_at={record.get('recorded_at')} knowledge_cutoff={record.get('knowledge_cutoff')}: {record['statement']}"]
        if record.get("evidence"):
            links = ", ".join(
                f"{item['evidence_id']}@{item['content_hash']}[{item['relation_type']}; "
                f"event_time={item.get('event_time')}; ingested_at={item.get('ingested_at')}; "
                f"{item.get('provenance_status', 'unverified')}]"
                for item in record["evidence"])
            lines.append(f"  Evidence: {links}")
        return lines

    def render_decision_memories(self, records):
        # Records from different queries stay separate and are never rewritten:
        # a historical cutoff view and the current view answer different questions.
        if not records:
            return ""
        historical = [record for record in records if record.get("knowledge_cutoff")]
        current = [record for record in records if not record.get("knowledge_cutoff")]
        lines = ["", "【内部 Decision Memory（与原始 Evidence 区分）】"]
        if historical:
            lines.append("【历史截止视图（按问题截止时点查询；原始查询结果未被改动）】")
            for record in historical:
                lines.extend(self._decision_memory_lines(
                    record, f"截至 {record['knowledge_cutoff']} 的历史截止视图"
                            "（当时系统记录的最新已确认版本）"))
        if current:
            label = "当前视图（查询时点）的系统记录的最新已确认版本"
            label += "（不属于历史截止视图）" if historical else "（查询快照）"
            lines.append("【当前视图（查询时点）】")
            for record in current:
                lines.extend(self._decision_memory_lines(
                    record, label if record["system_current"] else "当前视图的系统历史版本"))
        if historical and current:
            lines.append("说明：两个视图分别来自不同查询时点；system_current 只表示该版本在对应查询快照内是当前版本，"
                         "不表示同一时点存在两个当前版本。历史结论以问题要求的截止时点为准。")
        lines.append("Decision Memory 是人工审核后的系统确认记录；原 Evidence 中的聊天、提案或草案不等于正式确认；"
                     "系统确认也不等于实际部署执行，现实状态未验证（execution_status/world_state 仍为 unknown）。")
        return "\n".join(lines)

    CUTOFF_DATE = re.compile(
        r"(?:截至|截止|as\s+of)\s*[:：]?\s*((?:19|20)\d{2}-\d{1,2}-\d{1,2})", re.IGNORECASE)

    @classmethod
    def _question_cutoff(cls, question):
        match = cls.CUTOFF_DATE.search(str(question or ""))
        return match.group(1) if match else None

    def _decision_snapshot_statements(self, decision_memories, question):
        """Conclusions from Decision Memory version/time metadata, not Evidence claims."""
        from src.investigation import SynthesisStatement
        historical = [m for m in decision_memories if m.get("knowledge_cutoff")]
        if not historical:
            return []
        asked = self._question_cutoff(question)
        matched = ([m for m in historical
                    if str(m.get("knowledge_cutoff") or "")[:10] == asked] if asked else historical)
        if not matched and len({str(m.get("knowledge_cutoff"))[:10] for m in historical}) == 1:
            matched = historical
        if not matched:
            return []
        cutoff = str(matched[0].get("knowledge_cutoff"))
        cutoff_date = cutoff[:10]
        statements = []
        confirmed = [m for m in matched if m.get("status") == "confirmed"]
        if confirmed:
            chosen = next((m for m in confirmed if m.get("system_current")), confirmed[0])
            statements.append(SynthesisStatement(
                text=(f"截至 {cutoff_date}（问题指定的截止时点），系统记录中当时已确认的版本是 "
                      f"{chosen['version_id']}：{chosen.get('statement')}（status=confirmed，"
                      f"decision_known_at={chosen.get('decision_known_at') or chosen.get('reviewed_at')}，"
                      f"recorded_at={chosen.get('recorded_at')}）。该结论由 Decision Memory 的版本与审核时间"
                      "元数据支持，不是原始 Evidence 的逐字 Claim。"),
                basis="validated_decision_snapshot"))
        else:
            statements.append(SynthesisStatement(
                text=(f"截至 {cutoff_date}（问题指定的截止时点），Decision Memory 中没有已确认版本。"),
                basis="validated_decision_snapshot"))
        for memory in decision_memories:
            if memory.get("knowledge_cutoff"):
                continue
            known = str(memory.get("decision_known_at") or memory.get("reviewed_at") or "")[:10]
            if not known or known <= cutoff_date:
                continue
            later_status = ("当前视图显示它于 " + known + " 才被记录为已确认"
                            if memory.get("status") == "confirmed"
                            else "当前视图中该版本状态仍为 " + str(memory.get("status")))
            statements.append(SynthesisStatement(
                text=(f"当前视图另有版本 {memory['version_id']}：{memory.get('statement')}；其 "
                      f"decision_known_at={known} 晚于截止时点 {cutoff_date}，截至 {cutoff_date} 属于"
                      f"未确认的后续候选方案，不能据此认定当时已替代已确认版本；{later_status}。"
                      "该记录属于当前视图，不属于历史截止视图。"),
                basis="validated_decision_snapshot"))
        if any(memory.get("execution_status") == "unknown" or memory.get("world_state") == "unknown"
               for memory in decision_memories):
            statements.append(SynthesisStatement(
                text=("Decision Memory 的 execution_status 与 world_state 均为 unknown；"
                      "没有实际部署或执行证据，系统确认不等于现实部署，现实状态未验证。"),
                basis="validated_decision_snapshot"))
        return statements

    @staticmethod
    def _assertion_fields(record):
        assertions = record.metadata.get("assertions")
        if not isinstance(assertions, dict):
            return set()
        return {str(key).lower() for key in assertions}

    @staticmethod
    def _conflict_fields(conflict, records):
        """Use declared structured fields, or derive only from contradictory assertions.

        Model-submitted free text is never parsed into attribution metadata.
        """
        fields = {str(field).strip().lower() for field in conflict.fields if str(field).strip()}
        if fields:
            return fields
        assertions = [record.metadata.get("assertions") for record in records]
        assertions = [value for value in assertions if isinstance(value, dict)]
        for index, left in enumerate(assertions):
            for right in assertions[index + 1:]:
                for key in set(left) & set(right):
                    if key in {"environment", "env", "project_id", "dataset_kind"}:
                        continue
                    first, second = left[key], right[key]
                    if (isinstance(first, (str, int, float, bool))
                            and isinstance(second, (str, int, float, bool)) and first != second):
                        fields.add(str(key).lower())
        return fields

    @staticmethod
    def _claim_context(claim, records):
        parts = [claim.text.lower()]
        parts.extend(record.content.lower() for record in records)
        for record in records:
            subject = record.metadata.get("subject")
            if isinstance(subject, str):
                parts.append(subject.lower())
        return " ".join(parts)

    def _conflict_relation(self, claim, claim_records, conflict, conflict_records):
        """Classify only from verifiable identity/field/subject evidence.

        ``unattributable`` is deliberately conservative: when neither evidence
        identity nor an explicit field/subject can relate a conflict to a claim,
        the claim is retained but marked for verification instead of assumed safe.
        """
        claim_keys = {(record.evidence_id, record.content_hash) for record in claim.refs}
        conflict_keys = {(record.evidence_id, record.content_hash) for record in conflict_records}
        if claim_keys & conflict_keys:
            return "affects_submitted_claim"
        fields = self._conflict_fields(conflict, conflict_records)
        subject = (conflict.subject or "").strip().lower()
        claim_text = claim.text.lower()
        claim_fields = set()
        for record in claim_records:
            claim_fields.update(self._assertion_fields(record))
        context = self._claim_context(claim, claim_records)
        if fields:
            text_match = any(field in claim_text or field.replace("_", " ") in claim_text
                             for field in fields)
            if text_match or fields & claim_fields:
                if subject and subject not in context:
                    return "independent_of_submitted_claims"
                return "affects_submitted_claim"
            if claim_fields:
                # Explicit structured fields exist on both sides and are disjoint.
                return "independent_of_submitted_claims"
            return "unattributable"
        if subject:
            return ("affects_submitted_claim" if subject in context
                    else "independent_of_submitted_claims")
        return "unattributable"

    def _claim_records(self, ledger, claim):
        """Only records already present in this run's scoped ledger."""
        index = {(entry.evidence.evidence_id, entry.evidence.content_hash): entry.evidence
                 for entry in ledger.entries}
        return [index[key] for key in ((ref.evidence_id, ref.content_hash) for ref in claim.refs)
                if key in index]

    @classmethod
    def _commit_id(cls, records):
        for record in records:
            match = cls.COMMIT_HASH.search(record.content or "")
            if match:
                return match.group(1)
            # Git evidence validates original_record_id as the full commit SHA.
            # Reading it is a ledger field lookup, not an inferred commit identity.
            if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", record.original_record_id or ""):
                return record.original_record_id
        return None

    def build_synthesis(self, ledger, claims, gaps, conflicts, reason, question="",
                        *, decision_memories=()):
        """Deterministic user-facing synthesis over validated claims and ledger refs.

        The claim layer never creates a new fact or a new free-form causal claim.
        Conflict disclosure is a separate, clearly labelled meta-observation over
        refs already validated by the ledger.  The answer groups verbatim extracts
        by source type, links at least two distinct validated sources when
        available, and explicitly separates unconfirmed gaps.  Commit-only
        evidence is labelled as commit-message evidence, not as verified code
        implementation.
        """
        from src.investigation import Synthesis, SynthesisStatement
        verified = [(index, claim) for index, claim in enumerate(claims)
                    if claim.verification_status == "supported_extract"]
        statements = []
        commit_without_diff = False
        missing_ledger_ref = False
        for index, claim in verified:
            records = self._claim_records(ledger, claim)
            if not records:
                missing_ledger_ref = True
                continue
            source_types = sorted({record.source_type for record in records})
            primary = "git_commit" if "git_commit" in source_types else source_types[0]
            label = self.SOURCE_LABELS.get(primary, "来源记录")
            if primary == "git_commit":
                commit_id = self._commit_id(records)
                if commit_id:
                    text = f"提交 {commit_id[:8]} 的提交信息为：“{claim.text}”"
                else:
                    text = f"提交信息：“{claim.text}”"
                if not any(self.DIFF_MARKER.search(record.content or "") for record in records):
                    text += "（仅提交信息；未核验代码 diff）"
                    commit_without_diff = True
            else:
                text = f"{label}：“{claim.text}”"
            statement = SynthesisStatement(
                text=text, claim_indexes=[index], evidence_refs=list(claim.refs),
                source_types=source_types, basis="validated_claim")
            statements.append(statement)

        # Conflict disclosure is a meta-observation over validated ledger
        # references, never a choice between the conflicting sides.  Independent
        # conflicts are omitted from the answer body to avoid noise.
        conflict_index = {(entry.evidence.evidence_id, entry.evidence.content_hash): entry.evidence
                          for entry in ledger.entries}
        for conflict in conflicts:
            if conflict.relation == "independent_of_submitted_claims":
                continue
            records = [conflict_index.get((ref.evidence_id, ref.content_hash))
                       for ref in conflict.refs]
            if any(record is None for record in records):
                missing_ledger_ref = True
                continue
            snippets = []
            for record in records:
                label = self.SOURCE_LABELS.get(record.source_type, "来源记录")
                snippets.append(f"{label}原文：{self._compact(record.content, 240)}")
            if conflict.origin == "structured_assertion":
                source_note = "结构化断言"
                if conflict.fields:
                    source_note += "，字段：" + ", ".join(conflict.fields)
            elif conflict.origin == "evidence_version":
                source_note = "同一证据的多个内容版本"
            else:
                source_note = "模型报告；描述未经核验"
            statements.append(SynthesisStatement(
                text=(f"检测到未裁决冲突（{source_note}；系统不择一裁决）。"
                      "冲突相关原文：" + "；".join(snippets)),
                evidence_refs=list(conflict.refs),
                source_types=sorted({record.source_type for record in records}),
                basis="validated_conflict_disclosure"))

        # Prefer a cross-source pair that includes a commit when one exists:
        # this makes the implementation-evidence link explicit without asserting
        # that the commit message proves the implementation.
        first_by_type = {}
        for statement in statements:
            if statement.basis != "validated_claim":
                continue
            for source_type in statement.source_types:
                first_by_type.setdefault(source_type, statement)
        commit_statement = first_by_type.get("git_commit")
        other_statements = [statement for source_type, statement in first_by_type.items()
                            if source_type != "git_commit"]
        pair = None
        if commit_statement and other_statements:
            pair = (commit_statement, other_statements[0])
        elif len(other_statements) >= 2:
            pair = (other_statements[0], other_statements[1])
        if pair:
            left, right = pair
            left_type = next((value for value in left.source_types if value != "git_commit"),
                             left.source_types[0])
            right_type = next((value for value in right.source_types if value != left_type),
                              right.source_types[0])
            statements.append(SynthesisStatement(
                text=("跨来源核对：" + self.SOURCE_LABELS.get(left_type, "来源记录") +
                      "与" + self.SOURCE_LABELS.get(right_type, "来源记录") +
                      "均已通过原文摘录与引用校验；系统只确认两段原文并存，" +
                      "不作超出原文的因果或实现断言。"),
                claim_indexes=list(dict.fromkeys(left.claim_indexes + right.claim_indexes)),
                evidence_refs=list({(ref.evidence_id, ref.content_hash): ref
                                    for ref in left.evidence_refs + right.evidence_refs}.values()),
                source_types=list(dict.fromkeys(left.source_types + right.source_types)),
                basis="validated_cross_source"))

        # Decision Memory conclusions come first in the body when a historical
        # cutoff query supplied a confirmed version.  They are derived only
        # from version/status/time metadata, never presented as Evidence claims.
        statements = (self._decision_snapshot_statements(decision_memories, question)
                      + statements)

        caveats = ["综合回答由已通过校验的原文摘录确定性组装；未做独立语义蕴含判定。"]
        if commit_without_diff:
            caveats.append("提交来源仅提供提交信息，未包含 diff；不能据此确认代码实际实现。")
        if conflicts:
            caveats.append("存在未裁决冲突；受影响结论未作为确定事实。")
        if any(claim.verification_status == "pending_unattributable_conflict" for claim in claims):
            caveats.append("存在待核实摘录，未计入已确认结论。")
        if missing_ledger_ref:
            caveats.append("部分结论的引用不在本轮 Ledger 中，未纳入综合回答。")
        if not verified:
            caveats.append("本轮没有通过原文摘录与引用校验的结论。")
        return Synthesis(question=str(question or "")[:4000], statements=statements,
                         unconfirmed=list(dict.fromkeys(gaps))[:20], caveats=caveats,
                         conflict_count=len(conflicts))

    def render_synthesis(self, synthesis):
        if synthesis is None:
            return "【综合回答】\n本轮没有可确认的结论。"
        lines = ["【综合回答】"]
        if synthesis.question:
            lines.append(f"针对问题“{synthesis.question}”，本轮可确认的内容如下：")
        else:
            lines.append("本轮可确认的内容如下：")
        if not synthesis.statements:
            if synthesis.conflict_count:
                lines.append("本轮没有可作为事实的已核验结论；但检测到未裁决冲突，")
                lines.append("冲突双方原文与未确认范围见下方区段。")
            else:
                lines.append("本轮没有通过原文摘录与引用校验的结论；不能确认该问题的答案。")
        for statement in synthesis.statements:
            refs = ", ".join(f"{ref.evidence_id}@{ref.content_hash}" for ref in statement.evidence_refs)
            lines.append(f"- {statement.text}" + (f" [{refs}]" if refs else ""))
        if synthesis.unconfirmed:
            preview = "；".join(synthesis.unconfirmed[:3])
            if len(synthesis.unconfirmed) > 3:
                preview += " 等"
            lines.append(f"未确认部分：{preview}（完整见【未解决的缺口】，"
                         f"{len(synthesis.unconfirmed)} 项）。")
        if synthesis.caveats:
            lines.append("说明：" + " ".join(synthesis.caveats))
        return "\n".join(lines)

    def compose_investigation(self, ledger, finish, getter, requires_current_status=False):
        """Whitelist citations and accept only literal source extracts as supported claims.

        This performs identity/refetch and extract checks, NOT semantic entailment.
        Conflicts are scoped to claims using verified evidence identity, structured
        assertion fields or an explicit shared subject.  A conflict with no such
        basis marks a retained claim for verification; it never silently assumes
        that the claim is unaffected.  Free-form legacy summaries never enter the
        answer.
        """
        from src.investigation import (
            Claim, Conflict, classify_assertion_pair, state_evolution_conflict)
        gaps = list(finish.gaps)
        accepted = []
        validated = []
        conflicts = list(ledger.conflicts)
        resolved_conflicts = []
        conflict_signatures = set()
        for conflict in conflicts:
            records = [ledger.resolve(ref, getter) for ref in conflict.refs]
            resolved_conflicts.append((conflict, records))
            conflict_signatures.add(tuple(sorted((r.evidence_id, r.content_hash) for r in conflict.refs)))
        evolution_signatures = {
            tuple(sorted((r.evidence_id, r.content_hash) for r in evolution.refs))
            for evolution in ledger.state_evolutions}
        for conflict in finish.conflicts:
            records = [ledger.resolve(ref, getter) for ref in conflict.refs]
            signature = tuple(sorted((r.evidence_id, r.content_hash) for r in conflict.refs))
            # A model report cannot resurrect a pair the evidence already
            # classifies as an explicit pre/post state evolution.
            if signature in evolution_signatures:
                continue
            analyses = []
            for index, left in enumerate(records):
                for right in records[index + 1:]:
                    analysis = classify_assertion_pair(left, right)
                    if analysis is not None:
                        analyses.append((left, right, analysis))
            pair_count = len(records) * (len(records) - 1) // 2
            if (analyses and len(analyses) == pair_count
                    and all(item[2]["classification"] == "state_evolution"
                            for item in analyses)):
                left, right, analysis = analyses[0]
                ledger.state_evolutions.append(
                    state_evolution_conflict(left, right, analysis))
                evolution_signatures.add(signature)
                continue
            # A duplicate of a structured conflict is one conflict, not two.
            if signature in conflict_signatures:
                continue
            # Never trust attribution metadata supplied by the model itself.
            # An explicit model-reported conflict without a verified state
            # evolution is still retained as competing/undetermined.
            resolved_conflicts.append((Conflict.model_validate({
                "description": conflict.description, "refs": conflict.refs,
                "origin": "model_reported", "fields": [], "subject": None,
                "status": "competing", "relation": "unattributable",
                "affected_claim_indexes": [],
            }), records))
            conflict_signatures.add(signature)
        discarded_by_validation = 0
        for claim in finish.claims:
            records = [ledger.resolve(ref, getter) for ref in claim.refs]
            if claim.kind in {"unknown", "inference"}:
                gaps.append("模型提出的推测或未知项尚未证实。")
                discarded_by_validation += 1
                continue
            if claim.kind == "current":
                requires_current_status = True
                gaps.append("当前有效性未知：尚未独立核实后续状态证据的有效时间与权威性。")
                discarded_by_validation += 1
                continue
            if not records or not all(claim.text in r.content for r in records):
                gaps.append("部分结论不是引用原文，未作为已支持事实输出。")
                discarded_by_validation += 1
                continue
            if any(r.metadata.get("legacy") for r in records):
                gaps.append("包含未验证的 legacy 证据，不能确认为真实历史事实。")
                discarded_by_validation += 1
                continue
            undated_structured = any(
                r.event_time is None and r.metadata.get("structured_import") for r in records)
            if undated_structured or (any(r.event_time is None for r in records)
                                      and self.TIME_ASSERTION.search(claim.text)):
                gaps.append("引用证据的事件时间未知，不能据此确认该时间表述或当时状态。")
            validated.append((Claim.model_validate({
                **claim.model_dump(), "verification_status": "supported_extract"}), records))
        relation_map = []
        claim_relations = [[] for _ in validated]
        for conflict, conflict_records in resolved_conflicts:
            relations = [self._conflict_relation(claim, records, conflict, conflict_records)
                         for claim, records in validated]
            for index, relation in enumerate(relations):
                claim_relations[index].append(relation)
            affected = [index for index, relation in enumerate(relations)
                        if relation == "affects_submitted_claim"]
            if not validated:
                relation = "unattributable"
            elif affected:
                relation = "affects_submitted_claim"
            elif any(relation == "unattributable" for relation in relations):
                relation = "unattributable"
            else:
                relation = "independent_of_submitted_claims"
            relation_map.append(Conflict.model_validate({
                **conflict.model_dump(), "relation": relation, "affected_claim_indexes": affected}))
        discarded_by_conflict = 0
        retained_with_conflict = 0
        for index, (claim, records) in enumerate(validated):
            relations = claim_relations[index]
            if "affects_submitted_claim" in relations:
                discarded_by_conflict += 1
                continue
            pending = "unattributable" in relations
            accepted.append(Claim.model_validate({
                **claim.model_dump(),
                "verification_status": ("pending_unattributable_conflict" if pending
                                        else "supported_extract")}))
            if pending:
                retained_with_conflict += 1
        if requires_current_status:
            gaps.append("当前有效性未知，历史记录不能单独证明当前仍然有效。")
        if discarded_by_conflict:
            gaps.append("存在直接影响结论的证据冲突，受影响结论已暂不作为确定事实输出。")
        if retained_with_conflict:
            gaps.append("存在无法可靠归属到具体结论的证据冲突，保留的原文摘录仍待核实。")
        if not accepted:
            gaps.append("尚无通过原文摘录及引用身份校验的结论。")
        claim_counts = {
            "claims_submitted": len(finish.claims),
            "claims_accepted": len(accepted),
            "claims_discarded_by_validation": discarded_by_validation,
            "claims_discarded_by_conflict": discarded_by_conflict,
            "claims_retained_with_unresolved_conflict": retained_with_conflict,
        }
        return accepted, list(dict.fromkeys(gaps)), relation_map, claim_counts

    def _conflict_ref_lines(self, ledger, conflict):
        index = {(entry.evidence.evidence_id, entry.evidence.content_hash): entry.evidence
                 for entry in ledger.entries}
        lines = []
        for ref in conflict.refs:
            evidence = index.get((ref.evidence_id, ref.content_hash))
            if evidence is None:
                lines.append(f"  引用：{ref.evidence_id}@{ref.content_hash}")
            else:
                lines.append(f"  引用：{ref.evidence_id}@{ref.content_hash} "
                             f"原文摘录：{self._compact(evidence.content, 300)}")
        return lines

    def render_investigation(self, ledger, claims, gaps, conflicts, reason, coverage=None,
                             synthesis=None, question="", state_evolutions=()):
        if synthesis is None:
            synthesis = self.build_synthesis(ledger, claims, gaps, conflicts, reason, question)
        lines = ["【回答】(ReAct)", f"run_id: {ledger.run_id}", f"termination_reason: {reason}",
                 self.render_synthesis(synthesis)]
        if not ledger.entries:
            lines.append("未检索到相关证据，无法确认事实。")
        verified = [claim for claim in claims if claim.verification_status == "supported_extract"]
        pending = [claim for claim in claims if claim.verification_status == "pending_unattributable_conflict"]
        if verified:
            lines.append("【有引用支持的原文摘录（非独立语义核验）】")
            for claim in verified:
                refs = ", ".join(f"{r.evidence_id}@{r.content_hash}" for r in claim.refs)
                lines.append(f"- 历史来源记载：{claim.text} [{refs}]")
        if pending:
            lines.append("【待核实的原文摘录（存在未裁决冲突，未作为确定事实）】")
            for claim in pending:
                refs = ", ".join(f"{r.evidence_id}@{r.content_hash}" for r in claim.refs)
                lines.append(f"- 待核实原文摘录：{claim.text} [{refs}]")
        if state_evolutions:
            lines.append("【历史状态演进（证据明确区分阶段，非竞争性冲突；保留双方，不据此裁决）】")
            for evolution in state_evolutions:
                refs = ", ".join(f"{ref.evidence_id}@{ref.content_hash}"
                                 for ref in evolution.refs)
                lines.append(f"- {evolution.description} [{refs}]")
            lines.append("- 完整原文见 Trace；该区段不改变任何 Claim 的引用身份校验结果。")
        related = [conflict for conflict in conflicts
                   if conflict.relation == "affects_submitted_claim"]
        unattributable = [conflict for conflict in conflicts
                          if conflict.relation == "unattributable"]
        independent = [conflict for conflict in conflicts
                       if conflict.relation == "independent_of_submitted_claims"]
        if related:
            lines.append("【冲突（未裁决，直接影响已提交结论）】")
            for conflict in related:
                lines.append("- 受影响结论未输出；冲突描述（未经核验）：" + conflict.description)
                lines.extend(self._conflict_ref_lines(ledger, conflict))
        if unattributable:
            lines.append("【冲突（未裁决，影响范围无法自动判定）】")
            for conflict in unattributable:
                lines.append("- 相关原文摘录仅标记待核实；冲突描述（未经核验）：" + conflict.description)
                lines.extend(self._conflict_ref_lines(ledger, conflict))
        if independent:
            lines.append("【附录：与本轮已提交结论无直接关联的冲突（保留，不据此裁决）】")
            for conflict in independent:
                fields = ", ".join(conflict.fields) if conflict.fields else "unknown"
                lines.append(f"- fields={fields}")
                lines.extend(self._conflict_ref_lines(ledger, conflict))
        # User-facing evidence summary is limited to evidence actually cited by
        # output claims; the full scoped ledger remains in the trace/result.
        cited_keys = {(ref.evidence_id, ref.content_hash)
                      for claim in claims for ref in claim.refs}
        cited = [entry for entry in ledger.entries
                 if (entry.evidence.evidence_id, entry.evidence.content_hash) in cited_keys]
        lines.append("【证据（仅列出已被输出结论引用的来源；完整证据见 Trace）】")
        if not cited:
            lines.append("- 无已引用证据。")
        for entry in cited:
            e = entry.evidence
            lines.append(f"- {e.evidence_id}@{e.content_hash} source={e.source_uri or e.metadata.get('legacy_source_label')} "
                         f"event_time={e.event_time} ingested_at={e.ingested_at} "
                         f"legacy_unverified={bool(e.metadata.get('legacy'))}")
        if coverage:
            lines.append("【任务覆盖（确定性存在性检查，不是语义完整性证明）】")
            for target in coverage.get("targets", []):
                lines.append(f"- {target['label']}: {target['status']} — {target['basis']}")
            for level in coverage.get("authority_levels", []):
                lines.append(f"- 权威层级 {level['level']}: observed={level['observed']}")
            lines.append("- 说明：工具次数和 Evidence 数量不作为任务完成依据；语义支持仍待核验。")
        lines.append("【未解决的缺口】")
        lines.extend(f"- {gap}" for gap in gaps)
        return "\n".join(lines)

    def _clean(self, text):
        return str(text or "").replace("\u2011", "-")

    def _compact(self, text, limit=240):
        content = " ".join(self._clean(text).split())
        return content[:limit] + "..." if len(content) > limit else content

    def _contains_any(self, text, words):
        lower = self._clean(text).lower()
        return any(word.lower() in lower for word in words)

    def _question_type(self, question):
        if self._is_optimization_question(question):
            return "optimization"
        if self._contains_any(question, ["谁修改", "修改过", "谁处理", "谁改"]):
            return "owner_change"
        if self._contains_any(question, ["怎么修", "如何修", "修复", "bug", "故障"]):
            return "fix"
        if self._contains_any(question, ["怎么被调用", "调用链", "执行流程", "被谁调用"]):
            return "call_chain"
        if self._contains_any(question, ["为什么", "原因", "采用", "方案", "技术选型", "决策", "没有采用"]):
            return "decision"
        return "general"

    def _matched_retrieval_entity(self, question):
        q = self._clean(question).lower()
        for entity in self.RETRIEVAL_ENTITIES:
            if entity.lower() in q:
                return entity
        if "embedding" in q:
            return "Embedding Retrieval"
        if "entity" in q and "retrieval" in q:
            return "Entity Retrieval"
        if "exact" in q:
            return "Exact Match"
        return None

    def _is_optimization_question(self, question):
        return self._contains_any(question, ["做过哪些优化", "优化了什么", "改进了什么", "调整了什么"])

    def _top_relevant(self, question, evidence):
        q = self._clean(question)
        hints = []
        for word in [
            "agent",
            "rag",
            "bm25",
            "embedding",
            "rerank",
            "memory",
            "langgraph",
            "skill",
            "混合检索",
            "长期记忆",
            "实体抽取",
            "prompt",
            "memoryskill",
            "memory_skill",
        ]:
            if word.lower() in q.lower():
                hints.append(word)

        ranked = []
        for item in evidence:
            content = self._clean(item.get("content", ""))
            score = item.get("score", 0)
            bonus = sum(20 for hint in hints if hint.lower() in content.lower())
            if "decision_memory" in item.get("source", ""):
                bonus += 15
            if "commit" in content.lower() or re.search(r"\bcma\d{4}\b", content.lower()):
                bonus += 5
            ranked.append((bonus + score, item))
        return [item for _, item in sorted(ranked, key=lambda pair: pair[0], reverse=True)]

    def _evidence_for_prompt(self, evidence):
        lines = []
        for index, item in enumerate(evidence[:8], start=1):
            source = self._clean(item.get("source", "unknown"))
            skill = self._clean(item.get("skill", "unknown"))
            score = item.get("score", 0)
            content = self._compact(item.get("content", ""), limit=700)
            lines.append(f"{index}. evidence_id={item.get('evidence_id', '?')}; "
                         f"content_hash={item.get('content_hash', '?')}; source={source}; "
                         f"source_uri={item.get('source_uri')}; project={item.get('project_id')}; "
                         f"dataset={item.get('dataset_kind')}; "
                         f"legacy_unverified={bool(item.get('metadata', {}).get('legacy'))}; "
                         f"event_time={item.get('event_time')}; locator={item.get('locator', {})}; "
                         f"skill={skill}; relevance={score}; content={content}")
        return "\n".join(lines)

    def _build_llm_prompt(self, question, evidence):
        qtype = self._question_type(question)
        style_map = {
            "optimization": "如果问题是“做过哪些优化”，总结优化对象、优化原因、具体动作、影响范围、涉及文件。",
            "decision": "如果问题是“为什么采用xx”，总结技术决策原因、替代方案和取舍。",
            "fix": "如果问题是“怎么修复”，按“问题现象、根因、修复方案”总结。",
            "owner_change": "如果问题是“谁修改过”，直接输出人名、Commit、文件。",
            "call_chain": "如果问题是“调用链”，输出清晰调用路径。",
            "general": "输出简洁中文总结，突出与研发协作长期记忆相关的结论。",
        }
        return (
            "你是研发协作记忆系统的总结模块。\n"
            "只能根据给定 evidence 回答，禁止编造不存在的信息。\n"
            "如果证据不足，输出：当前证据不足。\n"
            f"问题类型：{qtype}\n"
            f"总结风格：{style_map[qtype]}\n\n"
            f"用户问题：{self._clean(question)}\n\n"
            "evidence:\n"
            f"{self._evidence_for_prompt(evidence)}\n\n"
            "请只输出【回答】部分的正文，不要输出标题，不要重复列证据。"
        )

    def _summarize_with_siliconflow(self, question, evidence):
        if not self.siliconflow_api_key or not evidence:
            return None

        payload = {
            "model": self.siliconflow_model or self.DEFAULT_SILICONFLOW_MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": "你是研发协作记忆系统的总结模块，只能根据 evidence 总结。",
                },
                {"role": "user", "content": self._build_llm_prompt(question, evidence)},
            ],
            "temperature": 0.1,
            "max_tokens": 512,
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.siliconflow_api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError):
            return None

        choices = data.get("choices") or []
        if not choices:
            return None
        content = choices[0].get("message", {}).get("content", "")
        content = self._clean(content).strip()
        return content or None

    def _decision_items(self, evidence):
        return [item for item in evidence if "decision_memory" in item.get("source", "")]

    def _parse_decision_content(self, content):
        text = self._clean(content)
        fields = {}
        for key in ("title", "decision", "alternatives", "reason", "future_plan", "related_entities"):
            match = re.search(rf"{key}:\s*(.*?)(?:\s*\|\s*[a-z_]+:|$)", text, re.IGNORECASE)
            if match:
                fields[key] = match.group(1).strip()
        return fields

    def _score_decision_for_question(self, question, item):
        q = self._clean(question).lower()
        metadata = item.get("metadata") or {}
        text = " ".join([
            self._clean(metadata.get("title", "")),
            self._clean(metadata.get("decision", "")),
            self._clean(" ".join(metadata.get("related_entities", []) if isinstance(metadata.get("related_entities"), list) else [])),
            self._clean(item.get("content", "")),
        ]).lower()

        score = 0
        for token in ["混合检索", "bm25", "embedding", "rerank", "agent", "rag", "memory", "长期记忆", "decision memory"]:
            if token in q and token in text:
                score += 10
        for char in set(q):
            if "\u4e00" <= char <= "\u9fff" and char in text:
                score += 1
        return score + item.get("score", 0)

    def _select_decision_item(self, question, evidence):
        items = self._decision_items(evidence)
        if not items:
            return None
        return max(items, key=lambda item: self._score_decision_for_question(question, item))

    def _decision_summary_from_item(self, item):
        metadata = item.get("metadata") or {}
        parsed = self._parse_decision_content(item.get("content", ""))

        decision = metadata.get("decision") or parsed.get("decision", "")
        alternatives = metadata.get("alternatives") or parsed.get("alternatives", "")
        reason = metadata.get("reason") or parsed.get("reason", "")
        future_plan = metadata.get("future_plan") or parsed.get("future_plan", "")

        if isinstance(reason, str):
            reasons = [part.strip() for part in re.split(r"[；;、]", reason) if part.strip()]
        else:
            reasons = [self._clean(part).strip() for part in reason if self._clean(part).strip()]

        alternative_text = alternatives.strip() if isinstance(alternatives, str) else "、".join(self._clean(part) for part in alternatives)
        if not decision and not reasons:
            return None

        lines = []
        if decision:
            lines.append(f"{decision}。")
        if reasons:
            lines.append("")
            lines.append("采用该方案的原因：")
            for index, item_reason in enumerate(reasons, start=1):
                lines.append(f"{index}. {item_reason}")
        if alternative_text:
            lines.append("")
            lines.append(f"相比 {alternative_text}：")
            if self._contains_any(decision + " " + " ".join(reasons), ["混合检索", "BM25", "Embedding", "Rerank"]):
                lines.append("更适合研发协作场景中的实体 + 语义混合问题。")
            else:
                lines.append("当前方案更符合 Collaborative Memory Agent 的 MVP 目标和可解释性要求。")
        if future_plan:
            lines.append("")
            lines.append(f"后续计划：{future_plan}")
        return "\n".join(lines)

    def _extract_decisions(self, evidence):
        decisions = []
        for item in self._decision_items(evidence):
            metadata = item.get("metadata") or {}
            decision = metadata.get("decision")
            if not decision:
                decision = self._parse_decision_content(item.get("content", "")).get("decision")
            if decision:
                decisions.append(self._clean(decision).strip())
        return list(dict.fromkeys(decisions))

    def _fallback_optimization_summary(self, question, evidence):
        entity = self._matched_retrieval_entity(question)
        if not entity:
            return None

        text = "\n".join(self._clean(item.get("content", "")) for item in evidence)
        files = re.findall(r"(?:src|docs|data)/[a-zA-Z0-9_./-]+\.(?:py|md|json)", text)

        if entity == "Embedding Retrieval":
            points = [
                "用于处理模糊自然语言和语义相似问题。",
                "与 Exact Match、BM25、Entity Retrieval 一起组成混合检索，避免纯向量检索漏掉文件名、函数名、Issue ID 等精确实体。",
                "通过 Rerank 在召回后结合实体重合度、来源可信度和上下文相关性重新排序。",
                "补充 mock 数据、技术文档和 Skill 逻辑，使 Embedding Retrieval 的检索与回答更稳定。",
            ]
        elif entity == "BM25":
            points = [
                "保留 BM25 用于关键词、中文短文本和 Commit Message 召回。",
                "与 Exact Match、Embedding Retrieval、Entity Retrieval 组合，弥补纯语义检索对精确词的遗漏。",
                "通过 Rerank 对 BM25 与其他召回结果统一排序，提升最终证据质量。",
            ]
        elif entity == "Rerank":
            points = [
                "用于对多路召回结果进行最终重排。",
                "排序时综合实体重合度、来源可信度和上下文相关性。",
                "配合 Decision Memory 和实体命中权重，减少无关聊天或低相关片段进入最终答案。",
            ]
        elif entity == "Entity Retrieval":
            points = [
                "根据 Agent、Skill、模块、函数、Issue、Commit 和技术关键词召回证据。",
                "当问题出现实体名称时提高实体命中权重。",
                "优先召回 decision_memory、docs、issues、commits 中与实体相关的记录。",
            ]
        else:
            points = [
                "用于精确命中文件名、函数名、Issue ID、Commit Hash 和错误片段。",
                "与 BM25、Embedding Retrieval、Entity Retrieval 组合，覆盖精确实体和模糊语义两类问题。",
            ]

        lines = [f"{entity} 的优化主要包括："]
        lines.extend(f"{index}. {point}" for index, point in enumerate(points, start=1))
        if files:
            lines.append("")
            lines.append("涉及文件：" + "、".join(dict.fromkeys(files)))
        return "\n".join(lines)

    def _fallback_decision_summary(self, question, evidence):
        decisions = self._extract_decisions(evidence)
        if self._contains_any(question, ["哪些", "历史上", "所有", "列表"]) and decisions:
            return "历史上沉淀的关键技术决策包括：" + "；".join(decisions) + "。"

        selected = self._select_decision_item(question, evidence)
        if selected:
            summary = self._decision_summary_from_item(selected)
            if summary:
                return summary

        return "根据命中的证据，相关方案主要围绕 Agent 架构、混合检索、长期记忆和回答证据化展开。"

    def _fallback_owner_change_summary(self, question, evidence):
        text = "\n".join(self._clean(item.get("content", "")) for item in self._top_relevant(question, evidence)[:3])
        commit_match = re.search(r"\b(cma\d{4})\b", text.lower())
        author_match = re.search(r"\b(lin|zhou|chen|wang|li)\b", text.lower())
        files = re.findall(r"(?:src|docs|data)/[a-zA-Z0-9_./-]+\.(?:py|md|json)", text)

        parts = [f"{author_match.group(1) if author_match else '相关开发者'} 修改过该项目模块"]
        if commit_match:
            parts.append(f"commit 是 {commit_match.group(1)}")
        if files:
            parts.append("涉及文件包括 " + "、".join(dict.fromkeys(files)))
        return "，".join(parts) + "。"

    def _fallback_fix_summary(self, question, evidence):
        text = "\n".join(self._clean(item.get("content", "")) for item in self._top_relevant(question, evidence)[:4])
        files = re.findall(r"(?:src|docs|data)/[a-zA-Z0-9_./-]+\.(?:py|md|json)", text)
        phenomenon = "问题现象：项目检索、路由或回答质量在某些研发协作问题上不稳定。"
        root = "根因：需要结合 Issue、日志和 Commit 证据确认具体模块。"
        fix = "修复方式：根据命中的记录调整 Skill 逻辑、mock 数据、检索权重或回答模板。"

        if "route_score" in text or "Coordinator" in text:
            phenomenon = "问题现象：问题路由不够稳定，容易把研发记忆问题分到不合适的 Skill。"
            root = "根因：硬编码关键词缺少加权和 Top 2 聚合策略。"
            fix = "修复方式：在 Coordinator Agent 中引入 route_score，并在分差较小时同时调用 Top 2 Skill。"
        elif "Rerank" in text or "BM25" in text or "Embedding" in text:
            phenomenon = "问题现象：检索结果会混入无关聊天或低相关片段。"
            root = "根因：多路召回后缺少足够明确的重排和证据聚焦。"
            fix = "修复方式：提高 Decision Memory、实体命中和项目文档的权重，并用 Rerank 聚合证据。"

        file_part = "涉及文件：" + ("、".join(dict.fromkeys(files)) if files else "见下方来源文件。")
        return "\n".join([phenomenon, root, fix, file_part])

    def _fallback_call_chain_summary(self, evidence):
        text = "\n".join(self._clean(item.get("content", "")) for item in evidence)
        if "CoordinatorAgent.ask" in text and "MemorySkill.run" in text:
            return (
                "memory_skill 的调用路径是：main.py 创建 CoordinatorAgent；"
                "CoordinatorAgent.ask(question) 调用 route(question) 和 route_scores(question) 判断应走 memory；"
                "随后通过 SkillRegistry.get('memory') 取得 MemorySkill 实例；"
                "最终执行 MemorySkill.run(question) 完成长期记忆检索。"
            )
        return "根据 AST 调用链分析，相关函数的调用路径见下方关键证据。"

    def _fallback_summary(self, question, skill_names, evidence):
        qtype = self._question_type(question)
        if qtype == "optimization":
            summary = self._fallback_optimization_summary(question, evidence)
            if summary:
                return summary
        if qtype == "owner_change":
            return self._fallback_owner_change_summary(question, evidence)
        if qtype == "fix":
            return self._fallback_fix_summary(question, evidence)
        if qtype == "call_chain":
            return self._fallback_call_chain_summary(evidence)
        if qtype == "decision":
            return self._fallback_decision_summary(question, evidence)
        if "memory" in skill_names and any("decision_memory" in item.get("source", "") for item in evidence):
            return self._fallback_decision_summary(question, evidence)
        return "根据命中的证据，已整理出与 Collaborative Memory Agent 相关的结论和来源；详细片段见下方关键证据。"

    def _summary(self, question, skill_names, evidence):
        if not evidence:
            return "当前证据不足，无法确认。"
        llm_summary = self._summarize_with_siliconflow(question, evidence)
        if llm_summary:
            return llm_summary
        # Offline templates used to invent fixes and owners from topic words.
        # Quote retrieved material instead of asserting an unsupported conclusion.
        return "当前证据不足以自动确认完整结论。以下为检索片段，需结合原始记录核实：\n" + "\n".join(
            f"- [{item.get('source', 'unknown')}] {self._compact(item.get('content', ''), 300)}"
            for item in evidence[:3]
        )

    def compose(self, question, skill_names, skill_results, route_scores=None):
        if isinstance(skill_names, str):
            skill_names = [skill_names]
        if isinstance(skill_results, dict) and "evidence" in skill_results:
            skill_results = {skill_names[0]: skill_results}

        route_scores = route_scores or {}
        evidence = []
        sources = set()
        entities = set()
        decision_memories = []

        for skill_name in skill_names:
            result = skill_results.get(skill_name, {})
            decision_memories.extend(result.get("decision_memories", []))
            for item in result.get("evidence", []):
                copied = dict(item)
                copied["skill"] = skill_name
                evidence.append(copied)
            sources.update(result.get("sources", []))
            entities.update(result.get("entities", []))

        partitions = {(item["project_id"], item["dataset_kind"]) for item in evidence if "evidence_id" in item}
        if len(partitions) > 1:
            raise ValueError("Cannot compose evidence from mixed projects or datasets")
        evidence = self._top_relevant(question, evidence)[:8]
        summary = self._summary(question, skill_names, evidence)
        sources = {item.get("source", "unknown") for item in evidence}

        evidence_lines = []
        for item in evidence:
            source = item.get("source", "unknown")
            score = item.get("score", 0)
            skill_name = item.get("skill", "unknown")
            content = self._compact(item.get("content", ""))
            evidence_lines.append(f"- [{skill_name} relevance={score} evidence_id={item.get('evidence_id', '?')} "
                                  f"hash={item.get('content_hash', '?')} "
                                  f"provenance={'unverified_legacy' if item.get('metadata', {}).get('legacy') else 'recorded_source'}] {source}: {content}")

        source_lines = [f"- {source}" for source in sorted(sources)]
        entity_lines = [f"- {entity}" for entity in sorted(entities)]
        max_score = max(route_scores.values()) if route_scores else 0
        confidence = "未校准（路由分数不代表事实置信度）" if evidence else "低（证据不足）"
        route_lines = [f"- {skill}: {score}" for skill, score in sorted(route_scores.items())]
        route_lines.append(f"- 最终调用: {', '.join(skill_names)}")

        return "\n".join([
            "【回答】",
            summary,
            "",
            "【关键证据】",
            "\n".join(evidence_lines) if evidence_lines else "- 暂无命中证据。",
            "",
            "【来源文件】",
            "\n".join(source_lines) if source_lines else "- 暂无来源文件。",
            "",
            "【关联实体】",
            "\n".join(entity_lines) if entity_lines else "- 暂未识别到明确实体。",
            "",
            "【置信度】",
            confidence,
            "",
            "【路由结果】",
            "\n".join(route_lines) if route_lines else "- 暂无路由分数。",
        ]) + self.render_decision_memories(decision_memories)
