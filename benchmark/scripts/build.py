"""Author a small, auditable frozen corpus. Never called by the evaluation runner."""
from datetime import datetime
from contextlib import closing
import json
import os
from pathlib import Path
import random
import sqlite3
import subprocess
import tempfile
from unittest.mock import patch

from src.evidence import Evidence, EvidenceScope, sha256_text
from src.evidence_store import EvidenceStore
from src.ingest import ingest_git_history
from src.investigation import EvidenceRef, InvestigationResult
from src.memory.decisions import DecisionStore

SEED = 20261022
COLLECTED = "2025-06-01T00:00:00+00:00"


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def build(root):
    root = Path(root)
    if any((root / name).exists() for name in ("corpus", "cases", "labels", "manifest.json")):
        raise FileExistsError("Frozen dataset exists; build a new snapshot directory instead")
    rng = random.Random(SEED)
    records, keys = [], {}
    def add(key, project, kind, text, event="2025-01-01T00:00:00+00:00", assertions=None, **meta):
        record = Evidence.create(project_id=project, dataset_kind="live", source_type=kind,
            source_uri=f"benchmark://{project}/{kind}", original_record_id=key, content=text,
            event_time=event, ingested_at=COLLECTED, locator={"record_key": key},
            metadata={"synthetic": True, "assertions": assertions or {}, **meta})
        records.append(record.model_dump(mode="json")); keys[key] = records[-1]
    a, z, d = "bench-atlas", "bench-zephyr", "bench-orbit-dev"
    add("a-order-chat", a, "chat", "Atlas 顺序队列讨论：采用分区归属方案 B，原因是同一账户的写入必须有序。", assertions={"decision_reason": "account_order"})
    add("a-order-issue", a, "issue", "Atlas 调度方案评审批准 B：固定分区归属；代价是热点账户暂不跨分区迁移。")
    add("a-order-proposal", a, "document", "Atlas 后续调度方案 C 提议：使用租约迁移。评审要求迁移前保留 fencing token。", "2025-02-10T00:00:00+00:00")
    add("a-tls-log", a, "log", "Atlas E42 故障日志：旧证书仍在连接池中；刷新证书后未重建池，因此出现 TLS 校验失败。", "2025-02-02T00:00:00+00:00", {"root_cause": "stale_pool_certificate"})
    add("a-incident", a, "issue", "Atlas 回滚调查入口：失败任务与 A-17 相关；需查 A-17 运行记录才能确认恢复门槛。", "2025-04-01T00:00:00+00:00", related_record="a-runbook-A17")
    add("a-runbook-A17", a, "document", "A-17 运行记录：积压持续两分钟才触发回滚，短暂峰值不回滚；后续提交加入持续时间门槛。", "2025-04-03T00:00:00+00:00")
    add("a-ack-1", a, "document", "Atlas prod 消息确认说明，2025-05-01：ack_mode=exactly_once。", "2025-05-01T00:00:00+00:00", {"environment": "prod", "ack_mode": "exactly_once"})
    add("a-ack-2", a, "document", "Atlas prod 同日运维记录：ack_mode=at_least_once；尚未完成对账。", "2025-05-01T00:00:00+00:00", {"environment": "prod", "ack_mode": "at_least_once"})
    add("a-recovery", a, "document", "Atlas E42 应急手册：重建连接池并重新加载证书，不能仅刷新磁盘文件。", "2025-05-03T00:00:00+00:00")
    add("z-retry", z, "document", "Zephyr 重试策略：最多两次重试，第三次会超出租约窗口，因此要求交给下一轮任务。", "2025-04-01T00:00:00+00:00")
    add("z-warm-issue", z, "issue", "Zephyr 预热覆盖问题：旧任务可能把新缓存值覆盖；Z-cachefix 要求比较 generation 后才能写入。", "2025-01-12T00:00:00+00:00")
    add("z-incident", z, "issue", "Zephyr 泄漏调查入口：定位到 Z-42；需要追查 Z-42 关闭顺序记录，而不是仅提高内存限制。", "2025-04-01T00:00:00+00:00", related_record="z-runbook-Z42")
    add("z-runbook-Z42", z, "document", "Z-42 关闭顺序记录：先停止预热生产者，再释放句柄；反序会让新任务继续占用已释放池。", "2025-04-02T00:00:00+00:00")
    add("z-ttl-1", z, "document", "Zephyr prod 缓存说明，2025-05-01：ttl_seconds=60。", "2025-05-01T00:00:00+00:00", {"environment": "prod", "ttl_seconds": 60})
    add("z-ttl-2", z, "document", "Zephyr prod 值班记录，同日：ttl_seconds=300；与设计记录未核对。", "2025-05-01T00:00:00+00:00", {"environment": "prod", "ttl_seconds": 300})
    add("z-retention", z, "issue", "Zephyr 清理策略建议：提出七天保留窗口；没有审批或执行确认记录。", "2025-05-01T00:00:00+00:00", {"approval": "pending"})
    add("z-pool-log", z, "log", "Zephyr P9 池耗尽记录：预热句柄未归还；恢复时先停止预热并回收句柄，新增容量不是根治。", "2025-05-02T00:00:00+00:00")
    add("z-cache-chat", z, "chat", "Zephyr 缓存方案讨论批准 B：读本地副本，优先可用性；跨节点短暂不一致需要另行处理。")
    add("z-cache-C", z, "document", "Zephyr 缓存方案 C 评审材料：读取版本水位，减少旧值回写；评审通过并不证明生产已切换。", "2025-02-01T00:00:00+00:00")
    add("d-config", d, "document", "Orbit 开发样本：批次上限八条，以便人工定位单条失败。")
    add("d-unknown", d, "issue", "Orbit 开发样本：压缩实验尚未运行，没有吞吐量结论。")
    with tempfile.TemporaryDirectory(prefix="cma-benchmark-build-") as directory:
        work = Path(directory)
        for project, messages in [(a, ["Atlas initial partition config", "Atlas account ordering uses partition ownership B", "Atlas A-17 rollback waits for persistent backlog"]),
                                  (z, ["Zephyr initial cache config", "Zephyr Z-cachefix compares generation before cache write", "Zephyr Z-42 stops producer before closing handles"])]:
            repo = work / project; repo.mkdir()
            env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
            def git(*args):
                return subprocess.run(["git", "-C", str(repo), *args], env=env, capture_output=True, check=True, timeout=10)
            git("init", "--object-format=sha1", "--initial-branch=main")
            git("config", "user.name", "Benchmark Author")
            git("config", "user.email", "benchmark@example.invalid")
            git("config", "commit.gpgsign", "false")
            git("config", "core.autocrlf", "false")
            empty_hooks = repo / ".empty-hooks"; empty_hooks.mkdir()
            git("config", "core.hooksPath", str(empty_hooks))
            for index, message in enumerate(messages):
                date = ["2025-01-01", "2025-01-13", "2025-04-04"][index] + "T12:00:00+00:00"
                env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
                (repo / "config.txt").write_text(f"{project} revision={index}\n", encoding="utf-8")
                git("add", "config.txt"); git("commit", "-m", message)
            imported = ingest_git_history(repo, project_id=project, repository_uri=f"git+benchmark://{project}/repo")
            imported.reverse()
            for index, raw in enumerate(imported):
                raw["ingested_at"] = COLLECTED
                raw["metadata"]["synthetic"] = True
                record = Evidence.model_validate(raw).model_dump(mode="json")
                keys[f"{project}-git-{index}"] = record; records.append(record)
        evidence_db = work / "evidence.sqlite3"
        EvidenceStore(evidence_db).append(records)
        moment = ["2025-01-10T00:00:00+00:00"]
        store = DecisionStore(work / "decisions.sqlite3", evidence_db_path=evidence_db,
                              clock=lambda: datetime.fromisoformat(moment[0]))
        decisions = {}
        def submit(topic, project, key, statement):
            evidence = keys[key]
            result = InvestigationResult(run_id="frozen-authoring-run", scope=EvidenceScope(project_id=project, dataset_kind="live"),
                answer="", termination_reason="sufficient", evidence_refs=[EvidenceRef(evidence_id=evidence["evidence_id"], content_hash=evidence["content_hash"])])
            return store.propose(topic["decision_id"], result, statement, event_time=evidence["event_time"])
        def review(proposal, scope, revision):
            store.review(proposal["proposal_id"], scope, operation="accept", reviewer_id="synthetic-scenario-author",
                expected_revision=revision, note="Scripted corpus history, not a human benchmark annotation", acknowledge_risks=True)
        with patch("src.memory.decisions.identifier", side_effect=lambda prefix: prefix + "_" + f"{rng.getrandbits(128):032x}"):
            for project, title, bkey, ckey, bstatement, cstatement in [
                (a, "Atlas 调度方案", "a-order-issue", "a-order-proposal", "采用固定分区归属 B", "采用带 fencing token 的租约迁移 C"),
                (z, "Zephyr 缓存方案", "z-cache-chat", "z-cache-C", "采用本地副本 B", "采用版本水位读取 C")]:
                scope = EvidenceScope(project_id=project, dataset_kind="live")
                moment[0] = "2025-01-10T00:00:00+00:00"
                topic = store.create(title, scope)
                review(submit(topic, project, bkey, bstatement), scope, 0)
                moment[0] = "2025-02-15T00:00:00+00:00"
                proposal = submit(topic, project, ckey, cstatement)
                moment[0] = "2025-03-20T00:00:00+00:00"
                review(proposal, scope, 1)
                decisions[project] = store.snapshot(topic["decision_id"], scope)
            topic = store.create("Zephyr 清理策略", EvidenceScope(project_id=z, dataset_kind="live"))
            submit(topic, z, "z-retention", "七天保留窗口")
            decisions["retention"] = store.snapshot(topic["decision_id"], EvidenceScope(project_id=z, dataset_kind="live"))
        with closing(sqlite3.connect(store.path)) as db:
            sql = "\n".join(db.iterdump()) + "\n"
        (root / "corpus").mkdir(parents=True)
        (root / "corpus" / "decisions.sql").write_text(sql, encoding="utf-8")
    dump(root / "corpus" / "evidence.json", records)
    relations = [{"kind": "conflict", "project_id": a, "field": "ack_mode", "keys": ["a-ack-1", "a-ack-2"]},
                 {"kind": "conflict", "project_id": z, "field": "ttl_seconds", "keys": ["z-ttl-1", "z-ttl-2"]}]
    for relation in relations:
        relation["evidence_ids"] = [keys[k]["evidence_id"] for k in relation.pop("keys")]
    dump(root / "corpus" / "relations.json", relations)
    cases, labels = [], []
    def case(cid, category, project, question, gold, expected, notes, split="test", **extra):
        cases.append({"case_id": cid, "category": category, "question": question, "project_id": project,
                      "dataset_kind": "live", "corpus_snapshot_id": "", "split": split})
        labels.append({"case_id": cid, "expected_behavior": notes, "gold_evidence_ids": [keys[k]["evidence_id"] for k in gold],
            "gold_content_hashes": {keys[k]["evidence_id"]: keys[k]["content_hash"] for k in gold},
            "expected_termination": expected, "grading_notes": "Requires independent semantic review; " + notes,
            "annotation_status": "pending_human_review", "reviewers": [], **extra})
    case("single-atlas", "single_source", a, "Atlas E42 的 TLS 故障根因是什么？请引用日志。", ["a-tls-log"], ["sufficient"], "Identify the unchanged pool certificate, not a generic network outage.", required_sources=["log"])
    case("single-zephyr", "single_source", z, "Zephyr 重试策略为什么只允许两次？请查技术文档。", ["z-retry"], ["sufficient"], "Explain the lease window boundary.", required_sources=["document"])
    case("cross-atlas", "cross_source", a, "Atlas 为什么采用固定分区归属？讨论理由与哪个代码提交相互印证？", ["a-order-chat", a + "-git-1"], ["sufficient"], "Connect account ordering discussion with the implementing commit.", required_sources=["chat", "git_commit"])
    case("cross-zephyr", "cross_source", z, "Zephyr 预热覆盖问题如何定位并实施修复？请关联 Issue 和提交。", ["z-warm-issue", z + "-git-1"], ["sufficient"], "Connect stale writer symptom to generation comparison.", required_sources=["issue", "git_commit"])
    case("multi-atlas", "multi_round", a, "调查 Atlas 回滚入口，沿其引用追查恢复门槛及后续修复提交。", ["a-incident", "a-runbook-A17", a + "-git-2"], ["sufficient"], "Follow A-17 and distinguish transient peaks from sustained backlog.", minimum_investigation_steps=2, required_capabilities=["feedback_loop"])
    case("multi-zephyr", "multi_round", z, "调查 Zephyr 泄漏入口，沿线索追查关闭顺序及实际修复提交。", ["z-incident", "z-runbook-Z42", z + "-git-2"], ["sufficient"], "Follow Z-42; memory limit increase is not the fix.", minimum_investigation_steps=2, required_capabilities=["feedback_loop"])
    a_dec, z_dec, retention = decisions[a], decisions[z], decisions["retention"]
    case("decision-atlas", "decision_history", a, "截至 2025-02-20 系统已确认的 Atlas 调度方案是什么？当时 C 是否已替代 B？", ["a-order-issue"], ["sufficient", "insufficient"], "Known-at view returns B; C is pending, not confirmed at this cutoff.", decision_id=a_dec["decision"]["decision_id"], known_at="2025-02-20T00:00:00+00:00", gold_decision_version_ids=[a_dec["versions"][0]["version_id"]], expected_current_version_id=a_dec["versions"][0]["version_id"], required_capabilities=["time_filters"])
    case("decision-zephyr", "decision_history", z, "Zephyr 缓存方案的系统确认版本如何从 B 变为 C？当前确认指针和替代关系是什么？", ["z-cache-chat", "z-cache-C"], ["sufficient", "insufficient"], "Require both versions and C.supersedes=B; system state is not production verification.", decision_id=z_dec["decision"]["decision_id"], gold_decision_version_ids=[v["version_id"] for v in z_dec["versions"]], expected_current_version_id=z_dec["current_confirmed"]["version_id"], expected_supersedes={z_dec["versions"][1]["version_id"]: z_dec["versions"][0]["version_id"]})
    case("conflict-atlas", "conflict", a, "Atlas prod 的消息确认模式究竟是什么？两份同日记录一致吗？", ["a-ack-1", "a-ack-2"], ["conflict", "insufficient"], "Disclose exactly_once versus at_least_once without picking one.", conflict_field="ack_mode")
    case("conflict-zephyr", "conflict", z, "Zephyr prod 缓存 TTL 是多少？设计说明与值班记录是否冲突？", ["z-ttl-1", "z-ttl-2"], ["conflict", "insufficient"], "Disclose 60 versus 300 seconds, do not silently average.", conflict_field="ttl_seconds")
    case("missing-atlas", "insufficient", a, "Atlas 调度方案的系统最新确认版本，能证明现在生产环境已经执行吗？", ["a-order-proposal"], ["insufficient"], "Separate latest system confirmation from missing production execution evidence.", missing_fact="production_execution", decision_id=a_dec["decision"]["decision_id"], expected_current_version_id=a_dec["current_confirmed"]["version_id"])
    case("missing-zephyr", "insufficient", z, "Zephyr 清理策略的七天保留窗口是谁正式批准的？是否已有当前确认版本？", ["z-retention"], ["insufficient"], "There is only an unreviewed retention candidate; no approver or confirmed version exists.", missing_fact="retention_approver", decision_id=retention["decision"]["decision_id"], expected_current_version_id=None, no_confirmed_version=True)
    case("recovery-atlas", "tool_failure", a, "Atlas E42 应急恢复方法是什么？Issue 工具不可用时请换来源并说明缺口。", ["a-recovery"], ["sufficient", "insufficient"], "Attempt failed issue source, then obtain handbook evidence via another source.", injected_failure={"skill": "problem", "error": "TimeoutError"}, required_capabilities=["failure_recovery"])
    case("recovery-zephyr", "tool_failure", z, "Zephyr P9 池耗尽如何恢复？文档工具不可用时请查其他来源。", ["z-pool-log"], ["sufficient", "insufficient"], "Recover from document failure with pool log and do not claim larger capacity fixes the cause.", injected_failure={"skill": "document", "error": "TimeoutError"}, required_capabilities=["failure_recovery"])
    case("dev-config", "single_source", d, "Orbit 的开发批次上限及原因是什么？", ["d-config"], ["sufficient"], "Development only, not a test-set score.", split="dev")
    case("dev-unknown", "insufficient", d, "Orbit 压缩实验吞吐量提升多少？", ["d-unknown"], ["insufficient"], "No measurement exists.", split="dev", missing_fact="compression_throughput")
    # Avoid a self-referential file hash: snapshot name derives from frozen corpus only.
    corpus_hashes = {p.relative_to(root).as_posix(): sha256_text(p.read_text(encoding="utf-8")) for p in sorted((root / "corpus").iterdir())}
    snapshot_id = "cma-v1-" + sha256_text(json.dumps(corpus_hashes, sort_keys=True))[:16]
    for row in cases: row["corpus_snapshot_id"] = snapshot_id
    dump(root / "cases" / "public.json", cases)
    dump(root / "labels" / "gold.json", labels)
    files = {p.relative_to(root).as_posix(): {"sha256": __import__("hashlib").sha256(p.read_bytes()).hexdigest(), "bytes": p.stat().st_size}
             for folder in ("corpus", "cases", "labels") for p in sorted((root / folder).iterdir())}
    dump(root / "manifest.json", {"snapshot_id": snapshot_id, "seed": SEED, "synthetic": True, "files": files,
        "annotation_status": "pending_human_review", "test_cases": 14, "development_cases": 2})
    return snapshot_id


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(build(parser.parse_args().output))
