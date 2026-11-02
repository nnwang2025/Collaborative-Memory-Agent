"""Deterministic authoring script for the frozen Phase 4 benchmark snapshot.

Run this only to create a **new** snapshot directory.  Evaluation never calls
the authoring path; it loads the frozen files and verifies their hashes.

The synthetic projects are invented.  Names, IDs, dates and values are stable
so the same snapshot can be regenerated on any machine with the same Python
version.  Labels are authored alongside the corpus and frozen before any run.
They are marked as pending independent human review; no human review is claimed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.evidence import Evidence
from src.evidence_store import EvidenceStore
from src.memory.decisions import SCHEMA as DECISION_SCHEMA

V4_ROOT = Path(__file__).resolve().parent
DEFAULT_DATASET = V4_ROOT / "dataset"
DATASET_VERSION = "cma-v4-dataset-v1"
TASK_SET_VERSION = "cma-v4-tasks-v1"
SCORING_VERSION = "cma-v4-scoring-v1"
COLLECTED_AT = datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc)
BASE = datetime(2025, 1, 6, 9, 0, tzinfo=timezone.utc)

PROJECTS = [
    {"key": "atlas", "project_id": "v4-atlas-observability",
     "name": "Atlas 可观测性平台", "owner": "林澄", "reviewer": "赵岚",
     "channel": "#atlas-obs", "repo": "https://git.example.test/atlas-observability.git"},
    {"key": "zephyr", "project_id": "v4-zephyr-indexer",
     "name": "Zephyr 索引服务", "owner": "周泊", "reviewer": "唐宁",
     "channel": "#zephyr-index", "repo": "https://git.example.test/zephyr-indexer.git"},
    {"key": "orbit", "project_id": "v4-orbit-billing",
     "name": "Orbit 计费系统", "owner": "陈墨", "reviewer": "顾遥",
     "channel": "#orbit-billing", "repo": "https://git.example.test/orbit-billing.git"},
]

# key, project, component, problem, metric, old, new, conflicting value,
# root cause, fix, decision title, environment
STORY_SPECS = [
    ("atlas-ingest", "atlas", "ingest-gateway", "写入放大", "batch_size", "500", "2000", "500",
     "上游批次过小，固定开销占比过高", "把批次窗口从 500 调到 2000 并加入背压", "写入批次策略", "prod"),
    ("atlas-query", "atlas", "query-router", "慢查询", "shard_timeout_seconds", "2", "5", "2",
     "跨分片查询默认超时过短", "将分片超时从 2 秒调整到 5 秒并补齐指标", "查询超时策略", "prod"),
    ("atlas-retention", "atlas", "retention-worker", "保留策略不一致", "retention_days", "7", "14", "7",
     "值班记录仍沿用旧保留窗口", "把保留窗口从 7 天调整为 14 天，并标注未确认版本", "日志保留策略", "prod"),
    ("atlas-alert", "atlas", "alert-deduper", "重复告警", "dedupe_window_minutes", "5", "15", "5",
     "告警去重窗口过短导致重复通知", "把去重窗口从 5 分钟调大到 15 分钟", "告警去重策略", "prod"),
    ("atlas-rollout", "atlas", "rollout-controller", "灰度比例异常", "canary_percent", "5", "10", "5",
     "灰度控制器读取了旧配置", "灰度和回滚检查改为读取新配置", "灰度发布策略", "prod"),
    ("atlas-tenant", "atlas", "tenant-quota", "租户突发流量", "quota_burst_multiplier", "1", "2", "1",
     "租户配额没有突发余量", "允许 2 倍突发配额并增加审计", "租户配额策略", "prod"),
    ("zephyr-build", "zephyr", "index-builder", "索引构建变慢", "index_shards", "8", "12", "8",
     "构建分片过少导致单分片过大", "把索引构建分片从 8 调整到 12", "索引分片策略", "prod"),
    ("zephyr-warmup", "zephyr", "warmup-loader", "预热覆盖不全", "warmup_workers", "4", "8", "4",
     "预热并发不足导致尾部数据未加载", "把预热 worker 从 4 增加到 8", "预热并发策略", "prod"),
    ("zephyr-ttl", "zephyr", "cache-proxy", "缓存命中率下降", "cache_ttl_seconds", "300", "600", "300",
     "缓存 TTL 过短导致频繁回源", "把 TTL 从 300 秒调整为 600 秒", "缓存 TTL 策略", "prod"),
    ("zephyr-retry", "zephyr", "search-client", "重试次数不足", "retry_limit", "1", "2", "1",
     "瞬断时没有第二次重试机会", "把重试上限从 1 次调整为 2 次", "检索重试策略", "prod"),
    ("zephyr-compaction", "zephyr", "segment-compactor", "合并任务延迟", "compaction_threshold_mb", "128", "256", "128",
     "合并阈值过低导致小文件过多", "把合并阈值从 128 MB 调整为 256 MB", "段合并策略", "prod"),
    ("zephyr-auth", "zephyr", "token-validator", "时钟偏移误判", "clock_skew_seconds", "30", "60", "30",
     "校验器没有考虑跨机房时钟漂移", "把允许偏移从 30 秒调整为 60 秒", "令牌校验策略", "prod"),
    ("orbit-pricing", "orbit", "pricing-engine", "金额舍入错误", "rounding_scale", "2", "4", "2",
     "中间计算过早舍入", "中间结果保留 4 位小数，出账时再舍入", "计价舍入策略", "prod"),
    ("orbit-invoice", "orbit", "invoice-renderer", "账单渲染超时", "batch_limit", "100", "250", "100",
     "单批账单数量过少导致请求过多", "把渲染批量从 100 调整到 250", "账单批量策略", "prod"),
    ("orbit-ledger", "orbit", "ledger-writer", "账本写入延迟", "flush_interval_ms", "200", "500", "200",
     "刷盘间隔过短导致写入抖动", "把刷盘间隔从 200ms 调整为 500ms", "账本刷盘策略", "prod"),
    ("orbit-refund", "orbit", "refund-worker", "退款重试耗尽", "refund_retry_limit", "3", "5", "3",
     "退款重试上限没有覆盖支付网关抖动", "把退款重试上限从 3 次调整为 5 次", "退款重试策略", "prod"),
    ("orbit-tax", "orbit", "tax-resolver", "税率缓存过期", "tax_cache_hours", "12", "24", "12",
     "税率缓存刷新周期过短", "把税率缓存从 12 小时调整为 24 小时", "税率缓存策略", "prod"),
    ("orbit-export", "orbit", "export-job", "导出分页过小", "page_size", "1000", "5000", "1000",
     "导出分页过小导致任务数过多", "把导出分页从 1000 调整为 5000", "导出分页策略", "prod"),
]

ROLE_ORDER = [
    "chat_discussion", "chat_proposal", "chat_conflict", "chat_late_note", "chat_distractor",
    "issue_open", "issue_triage", "issue_fix", "issue_duplicate",
    "commit_initial", "commit_fix", "commit_followup", "commit_revert",
    "doc_v1", "doc_v2", "doc_runbook", "doc_review",
    "log_error", "log_recovery", "log_warning",
    "meeting_review", "meeting_followup",
    "code_config",
]

PENDING_STORIES = {"atlas-retention", "atlas-tenant", "zephyr-warmup", "zephyr-auth", "orbit-ledger"}


def _at(day_offset: int, hour: int = 9) -> datetime:
    return BASE + timedelta(days=day_offset, hours=hour)


def _sha(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def _id(prefix: str, *parts: str) -> str:
    return prefix + "_" + _sha(*parts)[:32]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sql(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _project(key: str) -> dict:
    return next(project for project in PROJECTS if project["key"] == key)


def _story(index: int) -> dict:
    key, project_key, component, problem, metric, old, new, conflict, cause, fix, title, env = STORY_SPECS[index]
    project = _project(project_key)
    issue_number = 1400 + index * 37
    return {
        "index": index, "key": key, "project": project, "component": component,
        "problem": problem, "metric": metric, "old": old, "new": new,
        "conflict": conflict, "cause": cause, "fix": fix, "title": title,
        "env": env, "issue_id": f"issue-{project_key.upper()}-{issue_number}",
        "issue_url": f"https://issues.example.test/{project_key}/{issue_number}",
        "doc_id": f"doc-{project_key}-{index:02d}",
        "meeting_id": f"meeting-{project_key}-{index:02d}",
        "log_id": f"log-{project_key}-{index:02d}",
        "chat_base": f"https://chat.example.test/{project_key}/{index:02d}",
        "commit_shas": [_sha("commit", key, role)[:40] for role in
                        ("initial", "fix", "followup", "revert")],
        "day0": index * 9,
    }


def _times(story: dict) -> dict:
    d = story["day0"]
    return {
        "chat_discussion": _at(d, 9), "chat_proposal": _at(d + 4, 10),
        "chat_conflict": _at(d + 4, 11), "chat_late_note": _at(d + 13, 15),
        "chat_distractor": _at(d + 5, 12),
        "issue_open": _at(d + 2, 9), "issue_triage": _at(d + 8, 14),
        "issue_fix": _at(d + 10, 16), "issue_duplicate": _at(d + 12, 10),
        "commit_initial": _at(d + 3, 11), "commit_fix": _at(d + 11, 10),
        "commit_followup": _at(d + 12, 9), "commit_revert": _at(d + 9, 17),
        "doc_v1": _at(d + 1, 8), "doc_v2": _at(d + 11, 9),
        "doc_runbook": _at(d + 12, 11), "doc_review": _at(d + 7, 15),
        "log_error": _at(d + 10, 8), "log_recovery": _at(d + 12, 8),
        "log_warning": _at(d + 13, 7),
        "meeting_review": _at(d + 6, 15), "meeting_followup": _at(d + 17, 15),
        "code_config": _at(d + 11, 12),
        "proposal_b": _at(d + 6, 16), "confirmed_b": _at(d + 8, 18),
        "proposal_c": _at(d + 15, 10), "confirmed_c": _at(d + 17, 18),
        "pending_proposal": _at(d + 20, 10),
    }


def _content(story: dict, role: str, times: dict) -> tuple[str, str, str, dict, str | None]:
    """Return content, source_type, authority, assertions, lifecycle."""
    project = story["project"]
    owner, reviewer, channel = project["owner"], project["reviewer"], project["channel"]
    component, metric = story["component"], story["metric"]
    old, new, conflict = story["old"], story["new"], story["conflict"]
    issue, doc, meeting, log_id = story["issue_id"], story["doc_id"], story["meeting_id"], story["log_id"]
    sha = story["commit_shas"]
    assertions = {metric: new}
    authority = "source_evidence"
    lifecycle = None
    if role == "chat_discussion":
        text = (f"{channel} {times[role]:%Y-%m-%d %H:%M} {owner}: 最近 {component} 的{story['problem']}又出现了，"
                f"{metric} 当前是 {old}，有人知道原因吗？")
        assertions, authority = {metric: old}, "discussion"
    elif role == "chat_proposal":
        text = (f"{channel} {times[role]:%Y-%m-%d %H:%M} {owner}: 我建议把 {component} 的 {metric} 从 {old} "
                f"调到 {new}；初步判断根因是{story['cause']}。")
        authority = "discussion"
    elif role == "chat_conflict":
        text = (f"{channel} {times[role]:%Y-%m-%d %H:%M} {reviewer}: 我在 {story['env']} 的值班记录里看到 "
                f"{metric}={conflict}，和上面讨论的 {new} 不一致，两份记录都要保留。")
        assertions, authority = {metric: conflict}, "discussion"
    elif role == "chat_late_note":
        text = (f"{channel} {times[role]:%Y-%m-%d %H:%M} {owner}: 补充说明，事件发生在 "
                f"{times['chat_proposal']:%Y-%m-%d}，这条聊天是 {times[role]:%Y-%m-%d} 才归档的；"
                "采集时间晚于事件时间。")
        assertions = {}
    elif role == "chat_distractor":
        text = f"{channel} {times[role]:%Y-%m-%d %H:%M} 有人在讨论午饭和周末值班，与 {component} 无关。"
        assertions = {}
    elif role == "issue_open":
        text = (f"Issue {issue} [{component}] {story['problem']}：{metric}={old}，影响 {story['env']}。"
                f"提出人 {owner}。")
        assertions, authority = {metric: old}, "source_evidence"
    elif role == "issue_triage":
        text = (f"Issue {issue} 评审记录：根因是{story['cause']}；建议把 {metric} 调整到 {new}。"
                f"评审人 {reviewer}。")
        authority = "reviewed_decision"
    elif role == "issue_fix":
        text = (f"Issue {issue} 修复验证：提交 {sha[1]} 之后 {metric}={new}，验证通过。"
                f"关联提交 {sha[1]}。")
        authority = "execution"
    elif role == "issue_duplicate":
        text = f"Issue {issue} 关闭为重复：与历史问题相关，保留原始记录，不改变 {metric} 的结论。"
        assertions = {metric: old}
    elif role.startswith("commit_"):
        kind = role.split("_", 1)[1]
        commit_sha = sha[["initial", "fix", "followup", "revert"].index(kind)]
        if kind == "initial":
            subject = f"{component}: 引入 {metric}={old}"
            assertions = {metric: old}
        elif kind == "fix":
            subject = f"{component}: 将 {metric} 调整为 {new}（Fixes {issue}）"
        elif kind == "followup":
            subject = f"{component}: 补充监控与回滚说明，不改变 {metric} 取值"
        else:
            subject = f"{component}: 回滚一次失败实验，恢复 {metric}={old}"
            assertions = {metric: old}
            if story["index"] % 3 == 1:
                lifecycle = "orphaned"
        text = (f"commit {commit_sha}\n作者 {owner}\n{subject}\n事件时间 "
                f"{times[role]:%Y-%m-%dT%H:%M:%S+00:00}\n关联 Issue: {issue}")
        authority = "execution"
    elif role == "doc_v1":
        text = (f"设计草案 {doc} v1：{component} 方案建议 {metric}={old}。"
                f"状态：未评审草案，事件时间 {times[role]:%Y-%m-%d}。")
        assertions, authority = {metric: old}, "source_evidence"
    elif role == "doc_v2":
        text = (f"设计文档 {doc} v2（已评审）：{component} 将 {metric} 从 {old} 调整为 {new}。"
                f"理由：{story['cause']}。评审人 {reviewer}；该文档是原始证据，不等同于系统确认版本。")
        authority = "source_evidence"
    elif role == "doc_runbook":
        text = (f"运行手册 {doc}-runbook：{component} 故障恢复步骤，检查 {metric}={new}，"
                "确认背压与回滚路径，并记录实际执行状态未知。")
    elif role == "doc_review":
        text = (f"评审材料 {doc}-review：{reviewer} 同意把 {metric} 调到 {new}；"
                "这是评审意见，不是正式系统确认。")
        authority = "meeting_record"
    elif role == "log_error":
        text = (f"日志 {log_id} ERROR {component} {story['problem']}：{metric}={conflict}，"
                f"请求失败率上升。事件时间 {times[role]:%Y-%m-%dT%H:%M:%S+00:00}。")
        assertions, authority = {metric: conflict}, "execution"
    elif role == "log_recovery":
        text = (f"日志 {log_id} INFO 调整后 {metric}={new}，{component} 服务恢复；"
                f"关联提交 {sha[1]}。")
        authority = "execution"
    elif role == "log_warning":
        text = f"日志 {log_id} WARN 采集延迟，{component} 的 {metric} 记录可能晚于事件时间到达。"
        assertions = {}
        lifecycle = "deleted"  # retained as a tombstone, not in the active current view
    elif role == "meeting_review":
        text = (f"会议 {meeting} 评审 {component}：讨论 {metric} 从 {old} 到 {new}；"
                "结论为候选方案，需系统审核。参会人 " + owner + "、" + reviewer + "。")
        authority = "meeting_record"
    elif role == "meeting_followup":
        text = (f"会议 {meeting}-followup 跟进：{component} 的 {metric}={new} 已确认并记录；"
                "实际执行状态未知，仍需部署证据。")
        authority = "meeting_record"
    elif role == "code_config":
        text = f"配置文件 {component}.yaml：{metric}: {new}\n# 关联 Issue {issue}，变更提交 {sha[1]}"
        authority = "execution"
    else:
        raise ValueError("Unknown role " + role)
    return text, _source_type(role), authority, assertions, lifecycle


def _source_type(role: str) -> str:
    if role.startswith("chat_"):
        return "chat"
    if role.startswith("issue_"):
        return "issue"
    if role.startswith("commit_"):
        return "git_commit"
    if role.startswith("doc_"):
        return "document"
    if role.startswith("log_"):
        return "log"
    if role.startswith("meeting_"):
        return "meeting"
    if role == "code_config":
        return "code"
    return "unknown"


def _make_record(story: dict, role: str) -> Evidence:
    project = story["project"]
    times = _times(story)
    content, source_type, authority, assertions, lifecycle = _content(story, role, times)
    now = times[role]
    ingested = COLLECTED_AT + timedelta(minutes=story["index"] * 7 + ROLE_ORDER.index(role))
    if role == "chat_late_note":
        ingested = COLLECTED_AT + timedelta(days=17, hours=story["index"] % 8)
    if source_type == "git_commit":
        commit_sha = story["commit_shas"][["initial", "fix", "followup", "revert"].index(role.split("_", 1)[1])]
        original_record_id = commit_sha
        source_uri = project["repo"]
        locator = {"commit_sha": commit_sha, "repository_uri": project["repo"]}
    elif source_type == "document":
        variant = {"doc_v1": "design", "doc_v2": "design",
                   "doc_runbook": "runbook", "doc_review": "review"}[role]
        document_id = story["doc_id"] if variant == "design" else f"{story['doc_id']}-{variant}"
        original_record_id = document_id
        source_uri = f"https://docs.example.test/{project['key']}/{document_id}.md"
        locator = {"path": f"docs/{document_id}.md",
                   "version": "v1" if role == "doc_v1" else "current"}
    elif source_type == "issue":
        original_record_id = f"{story['issue_id']}#{role}"
        source_uri = f"{story['issue_url']}/{role}"
        locator = {"issue_id": story["issue_id"], "comment": role}
    elif source_type == "chat":
        original_record_id = f"chat-{story['key']}-{role}"
        source_uri = f"{story['chat_base']}/{role}"
        locator = {"channel": project["channel"], "message": role}
    elif source_type == "log":
        original_record_id = f"{story['log_id']}-{role}"
        source_uri = f"https://logs.example.test/{project['key']}/{story['log_id']}/{role}"
        locator = {"log_id": story["log_id"], "stream": role}
    elif source_type == "meeting":
        original_record_id = f"{story['meeting_id']}-{role}"
        source_uri = f"https://meetings.example.test/{project['key']}/{story['meeting_id']}/{role}"
        locator = {"meeting_id": story["meeting_id"], "section": role}
    else:
        original_record_id = f"{story['key']}-{role}"
        source_uri = f"https://code.example.test/{project['key']}/{story['key']}/{role}.yaml"
        locator = {"path": f"config/{story['component']}.yaml"}
    metadata = {
        "benchmark": DATASET_VERSION,
        "scenario_key": story["key"],
        "record_role": role,
        "record_authority": authority,
        "assertions": assertions,
        "metric_name": story["metric"],
        "environment": story["env"],
        "related_issue": story["issue_id"],
        "related_commit": story["commit_shas"][1],
        "event_time_status": "recorded",
        "collection_note": "ingested_at is the benchmark collection time, not the event time",
    }
    if lifecycle:
        metadata["benchmark_lifecycle"] = lifecycle
    return Evidence.create(
        project_id=project["project_id"], dataset_kind="live",
        source_type=source_type, source_uri=source_uri,
        original_record_id=original_record_id, content=content,
        event_time=now, ingested_at=ingested, locator=locator, metadata=metadata)


def _records() -> list[Evidence]:
    records = []
    for index in range(len(STORY_SPECS)):
        story = _story(index)
        for role in ROLE_ORDER:
            records.append(_make_record(story, role))
    return records


def _decision_index(story: dict) -> dict:
    times = _times(story)
    project = story["project"]
    decision_id = _id("dec", "v4", story["key"])
    proposal_b = _id("prop", "v4", story["key"], "b")
    proposal_c = _id("prop", "v4", story["key"], "c")
    pending = _id("prop", "v4", story["key"], "pending")
    version_b = _id("dv", "v4", story["key"], "b")
    version_c = _id("dv", "v4", story["key"], "c")
    return {
        "decision_id": decision_id, "proposal_b": proposal_b, "proposal_c": proposal_c,
        "pending": pending, "version_b": version_b, "version_c": version_c,
        "times": times, "title": story["title"], "project": project,
        "pending_only": story["key"] in PENDING_STORIES,
        "statement_b": f"{story['title']}：{story['metric']} 采用 {story['old']}",
        "statement_c": (f"{story['title']}：{story['metric']} 调整为 {story['new']}，"
                        f"替代采用 {story['old']} 的旧版本"),
    }


def _decision_sql(records_by_key: dict[tuple[str, str], Evidence]) -> str:
    lines = ["BEGIN TRANSACTION;"]
    for statement in DECISION_SCHEMA:
        lines.append(statement.strip().rstrip(";") + ";")
    history_seq = 0
    for index in range(len(STORY_SPECS)):
        story = _story(index)
        decisions = _decision_index(story)
        times, project = decisions["times"], decisions["project"]
        role_refs_b = [(records_by_key[(story["key"], "doc_v1")], "supports"),
                       (records_by_key[(story["key"], "issue_triage")], "supports")]
        role_refs_c = [(records_by_key[(story["key"], "doc_v2")], "supports"),
                       (records_by_key[(story["key"], "commit_fix")], "supports")]
        proposal_refs_b = [{"evidence_id": record.evidence_id, "content_hash": record.content_hash,
                            "relation_type": relation} for record, relation in role_refs_b]
        proposal_refs_c = [{"evidence_id": record.evidence_id, "content_hash": record.content_hash,
                            "relation_type": relation} for record, relation in role_refs_c]
        context = {"termination_reason": "sufficient", "validation_level": "identity_and_extract_only",
                   "gaps": [], "conflicts": []}
        risks = ["identity_only_not_semantically_verified", "synthetic_benchmark_authoring"]
        created_b = times["proposal_b"].isoformat()
        created_c = times["proposal_c"].isoformat()
        if decisions["pending_only"]:
            current = None
            revision = 0
            lines.append("INSERT INTO decisions VALUES(%s);" % ",".join(map(_sql, (
                decisions["decision_id"], project["project_id"], "live", story["title"],
                times["pending_proposal"].isoformat(), current, revision))))
            pending_ref = [(records_by_key[(story["key"], "issue_triage")], "supports")]
            pending_refs = [{"evidence_id": record.evidence_id, "content_hash": record.content_hash,
                             "relation_type": relation} for record, relation in pending_ref]
            dedupe = _sha("proposal", story["key"], "pending", _json(pending_refs))
            lines.append("INSERT INTO review_proposals VALUES(%s);" % ",".join(map(_sql, (
                decisions["pending"], decisions["decision_id"],
                f"{story['title']}：{story['metric']} 拟调整为 {story['new']}", None,
                _json(pending_refs), "frozen-authoring-run", "pending",
                times["pending_proposal"].isoformat(), None, None, None,
                _json(risks), 0, dedupe, _json(context)))))
            observation_key = _sha("observation", story["key"], "pending")
            lines.append("INSERT INTO proposal_observations VALUES(%s);" % ",".join(map(_sql, (
                decisions["pending"], observation_key, "frozen-authoring-run",
                _json(context), _json(risks), times["pending_proposal"].isoformat()))))
            continue
        lines.append("INSERT INTO decisions VALUES(%s);" % ",".join(map(_sql, (
            decisions["decision_id"], project["project_id"], "live", story["title"],
            created_b, decisions["version_c"], 2))))
        lines.append("INSERT INTO review_proposals VALUES(%s);" % ",".join(map(_sql, (
            decisions["proposal_b"], decisions["decision_id"], decisions["statement_b"],
            times["chat_proposal"].isoformat(), _json(proposal_refs_b), "frozen-authoring-run",
            "accepted", created_b, times["confirmed_b"].isoformat(), project["reviewer"],
            "Synthetic benchmark authoring record; not a human annotation", _json(risks), 0,
            _sha("proposal", story["key"], "b", _json(proposal_refs_b)),
            _json(context)))))
        lines.append("INSERT INTO review_proposals VALUES(%s);" % ",".join(map(_sql, (
            decisions["proposal_c"], decisions["decision_id"], decisions["statement_c"],
            times["commit_fix"].isoformat(), _json(proposal_refs_c), "frozen-authoring-run",
            "accepted", created_c, times["confirmed_c"].isoformat(), project["reviewer"],
            "Synthetic benchmark authoring record; not a human annotation", _json(risks), 1,
            _sha("proposal", story["key"], "c", _json(proposal_refs_c)),
            _json(context)))))
        for version_id, proposal_id, statement, event_time, reviewed_at, supersedes, revision in (
                (decisions["version_b"], decisions["proposal_b"], decisions["statement_b"],
                 times["chat_proposal"].isoformat(), times["confirmed_b"].isoformat(), None, 1),
                (decisions["version_c"], decisions["proposal_c"], decisions["statement_c"],
                 times["commit_fix"].isoformat(), times["confirmed_c"].isoformat(),
                 decisions["version_b"], 2)):
            status = "confirmed" if version_id == decisions["version_c"] else "superseded"
            lines.append("INSERT INTO decision_versions VALUES(%s);" % ",".join(map(_sql, (
                version_id, decisions["decision_id"], revision, statement, status,
                event_time, reviewed_at, supersedes, project["reviewer"], reviewed_at,
                proposal_id))))
        for record, relation in role_refs_b:
            lines.append("INSERT INTO decision_evidence VALUES(%s);" % ",".join(map(_sql, (
                decisions["version_b"], record.evidence_id, record.content_hash, relation))))
        for record, relation in role_refs_c:
            lines.append("INSERT INTO decision_evidence VALUES(%s);" % ",".join(map(_sql, (
                decisions["version_c"], record.evidence_id, record.content_hash, relation))))
        for proposal_id, version_id, before, after, note in (
                (decisions["proposal_b"], decisions["version_b"], 0, 1, "confirm initial version"),
                (decisions["proposal_c"], decisions["version_c"], 1, 2, "confirm revised version")):
            history_seq += 1
            reviewed_at = times["confirmed_b"].isoformat() if before == 0 else times["confirmed_c"].isoformat()
            lines.append("INSERT INTO decision_review_history VALUES(%s);" % ",".join(map(_sql, (
                history_seq, proposal_id, decisions["decision_id"], "accept", before, after,
                None if before == 0 else decisions["version_b"], version_id,
                project["reviewer"], reviewed_at, f"Synthetic authoring note: {note}"))))
        observation_b = _sha("observation", story["key"], "b")
        observation_c = _sha("observation", story["key"], "c")
        lines.append("INSERT INTO proposal_observations VALUES(%s);" % ",".join(map(_sql, (
            decisions["proposal_b"], observation_b, "frozen-authoring-run", _json(context),
            _json(risks), created_b))))
        lines.append("INSERT INTO proposal_observations VALUES(%s);" % ",".join(map(_sql, (
            decisions["proposal_c"], observation_c, "frozen-authoring-run", _json(context),
            _json(risks), created_c))))
        lines.append("INSERT INTO conflict_links VALUES(%s);" % ",".join(map(_sql, (
            _id("conf", "v4", story["key"]), decisions["decision_id"],
            decisions["version_b"], decisions["version_c"], None, None,
            "版本变更：旧确认版本被新版本替代，保留审核历史。", "reviewed_acceptance",
            project["reviewer"], times["confirmed_c"].isoformat(),
            "Synthetic authoring record", times["confirmed_c"].isoformat()))))
    lines.append("COMMIT;")
    return "\n".join(lines) + "\n"


def _ref(records: dict[tuple[str, str], Evidence], story_key: str, role: str) -> dict:
    record = records[(story_key, role)]
    return {"evidence_id": record.evidence_id, "content_hash": record.content_hash}


def _fact(fact_id: str, fact_type: str, statement: str, *, values=None, marker_groups=None,
          required=True) -> dict:
    value = {"fact_id": fact_id, "fact_type": fact_type, "statement": statement, "required": required}
    if values is not None:
        value["values"] = values
    if marker_groups is not None:
        value["marker_groups"] = marker_groups
    return value


def _base_gold(task_id, category, story, records, *, difficulty, question,
               required_groups, facts, rubric=None, **extra) -> dict:
    gold = {
        "task_id": task_id,
        "category": category,
        "question": question,
        "project_id": story["project"]["project_id"],
        "dataset_kind": "live",
        "known_at": None,
        "required_evidence_groups": required_groups,
        "expected_facts": facts,
        "acceptable_alternative_evidence": [
            {"group_id": group["group_id"], "any_of": group["any_of"][1:],
             "note": "Any referenced record is acceptable for this group; the group's "
                     "required fact must still be supported."}
            for group in required_groups if len(group.get("any_of", [])) > 1],
        "expected_decision_state": None,
        "expected_conflicts": [],
        "forbidden_inferences": [],
        "expected_abstention": {"required": False, "expected_termination": ["sufficient"],
                                "must_disclose": []},
        "expected_limitations": [],
        "scoring_rubric": rubric or {
            "deterministic": ["evidence_recall", "citation_validity",
                              "structured_fact_accuracy", "termination"],
            "manual": ["task_success", "semantic_support", "conflict_interpretation"],
        },
        "difficulty": difficulty,
        "task_set_version": TASK_SET_VERSION,
        "annotation_status": "synthetic_authored_pending_independent_human_review",
        "human_review_rules": [
            "Check that required facts are supported by cited Evidence, not merely present as strings.",
            "Check that alternative Evidence is genuinely equivalent for this question.",
            "Do not mark task_success from deterministic metrics alone.",
        ],
    }
    gold.update(extra)
    return gold


def _build_tasks(records: dict[tuple[str, str], Evidence]) -> tuple[dict, dict]:
    public_tasks, gold_tasks = [], []
    task_index = {"single_source": 0, "cross_source": 0, "multi_step": 0,
                  "decision_history": 0, "conflict": 0, "insufficient": 0,
                  "budget": 0, "tool_failure": 0, "multi_turn": 0}
    selections = {
        "single_source": [0, 3, 6, 9, 12],
        "cross_source": [1, 4, 7, 10, 13],
        "multi_step": [2, 5, 8, 11, 14],
        "decision_history": [0, 3, 6, 9, 12],
        "conflict": [1, 4, 7, 10, 13],
        "insufficient": [2, 5, 7, 11, 14],
        "budget": [0, 3, 6, 9, 12],
        "tool_failure": [1, 4, 7, 10, 13],
        "multi_turn": [2, 5, 8, 11, 14],
    }
    for category, indices in selections.items():
        for index in indices:
            story = _story(index)
            task_index[category] += 1
            task_id = f"v4-{category.replace('_', '-')}-{task_index[category]:02d}"
            base = _base_gold(task_id, category, story, records,
                              difficulty=("easy" if category == "single_source" else
                                          "medium" if category in {"cross_source", "multi_step",
                                                                   "decision_history", "conflict",
                                                                   "multi_turn"} else "hard"),
                              question="", required_groups=[], facts=[])
            if category == "single_source":
                question = f"{story['component']} 的{story['problem']}根因是什么？请引用日志。"
                groups = [{"group_id": "log_error", "any_of": [_ref(records, story["key"], "log_error")]},
                          {"group_id": "issue_triage", "any_of": [_ref(records, story["key"], "issue_triage")]}]
                facts = [
                    _fact("component", "component", "组件名称", values=[story["component"]]),
                    _fact("log_id", "issue_id", "日志记录标识", values=[story["log_id"]]),
                    _fact("root_cause", "text", "根因解释",
                          marker_groups=[[story["cause"][:8], story["cause"]]]),
                    _fact("issue_id", "issue_id", "可选关联 Issue",
                          values=[story["issue_id"]], required=False),
                ]
                gold = _base_gold(task_id, category, story, records, difficulty="easy",
                                  question=question, required_groups=groups, facts=facts)
            elif category == "cross_source":
                question = (f"{story['component']} 为什么采用 {story['new']}？"
                            f"讨论理由与哪个提交相互印证？")
                groups = [{"group_id": "chat_proposal", "any_of": [_ref(records, story["key"], "chat_proposal")]},
                          {"group_id": "commit_fix", "any_of": [_ref(records, story["key"], "commit_fix")]}]
                facts = [
                    _fact("metric_value", "version", "配置值", values=[story["new"]]),
                    _fact("commit", "commit", "修复提交", values=[story["commit_shas"][1],
                                                                 story["commit_shas"][1][:8]]),
                    _fact("reason", "text", "采用理由", marker_groups=[[story["cause"][:8]]]),
                    _fact("meeting_id", "issue_id", "可选会议记录",
                          values=[story["meeting_id"]], required=False),
                ]
                gold = _base_gold(task_id, category, story, records, difficulty="medium",
                                  question=question, required_groups=groups, facts=facts)
            elif category == "multi_step":
                question = (f"从 Issue {story['issue_id']} 追查 {story['component']} 的修复提交"
                            "与验证日志。")
                groups = [{"group_id": "issue_fix", "any_of": [_ref(records, story["key"], "issue_fix")]},
                          {"group_id": "commit_fix", "any_of": [_ref(records, story["key"], "commit_fix")]},
                          {"group_id": "log_recovery", "any_of": [_ref(records, story["key"], "log_recovery")]}]
                facts = [
                    _fact("issue_id", "issue_id", "Issue 标识", values=[story["issue_id"]]),
                    _fact("commit", "commit", "修复提交", values=[story["commit_shas"][1],
                                                                 story["commit_shas"][1][:8]]),
                    _fact("metric_value", "version", "验证值", values=[story["new"]]),
                    _fact("log_id", "issue_id", "可选恢复日志",
                          values=[story["log_id"]], required=False),
                ]
                gold = _base_gold(task_id, category, story, records, difficulty="medium",
                                  question=question, required_groups=groups, facts=facts)
            elif category == "decision_history":
                decision = _decision_index(story)
                known_at = (decision["times"]["confirmed_b"] + timedelta(days=3)).isoformat()
                question = (f"截至 {known_at[:10]}，系统已确认的 {story['title']} 是什么？"
                            f"当时是否已采用 {story['new']}？")
                groups = [{"group_id": "doc_v2", "any_of": [_ref(records, story["key"], "doc_v2")]},
                          {"group_id": "issue_triage", "any_of": [_ref(records, story["key"], "issue_triage")]}]
                facts = [
                    _fact("historical_value", "decision_state", "截止时点已确认值",
                          values=[story["old"], decision["version_b"]]),
                    _fact("not_yet_new", "decision_state", "截止时点尚未确认新值",
                          values=[f"尚未采用 {story['new']}", f"未确认 {story['new']}", "未确认"]),
                    _fact("reviewer", "person", "可选历史确认人",
                          values=[story["project"]["reviewer"]], required=False),
                ]
                gold = _base_gold(
                    task_id, category, story, records, difficulty="medium",
                    question=question, required_groups=groups, facts=facts,
                    known_at=known_at,
                    forbidden_inferences=[[f"已确认 {story['new']}",
                                           f"{story['new']} 已正式确认",
                                           f"已正式采用 {story['new']}"]],
                    expected_decision_state={
                        "decision_id": decision["decision_id"], "known_at": known_at,
                        "expected_current_version_id": decision["version_b"],
                        "expected_version_ids": [decision["version_b"], decision["version_c"]],
                        "must_not_claim_confirmed": [decision["version_c"]],
                    })
            elif category == "conflict":
                question = (f"{story['component']} 在 {story['env']} 的 {story['metric']} "
                            f"到底是 {story['new']} 还是 {story['conflict']}？两份记录是否冲突？")
                groups = [{"group_id": "chat_conflict",
                           "any_of": [_ref(records, story["key"], "chat_conflict"),
                                      _ref(records, story["key"], "log_error")]},
                          {"group_id": "doc_v2", "any_of": [_ref(records, story["key"], "doc_v2")]}]
                facts = [
                    _fact("value_new", "version", "设计文档值", values=[story["new"]]),
                    _fact("value_conflict", "version", "冲突记录值", values=[story["conflict"]]),
                    _fact("issue_id", "issue_id", "可选关联 Issue",
                          values=[story["issue_id"]], required=False),
                ]
                gold = _base_gold(
                    task_id, category, story, records, difficulty="medium",
                    question=question, required_groups=groups, facts=facts,
                    forbidden_inferences=[["以文档为准", "文档是正确", "已裁决"]],
                    expected_conflicts=[{"field": story["metric"],
                                         "values": [story["new"], story["conflict"]],
                                         "must_disclose_both": True}],
                    expected_limitations=["不得自行裁决哪一方正确；必须保留双方记录。"])
            elif category == "insufficient":
                decision = _decision_index(story)
                question = (f"{story['component']} 的 {story['metric']}={story['new']} "
                            f"是谁正式批准的？是否已有当前确认版本？")
                groups = [{"group_id": "issue_triage", "any_of": [_ref(records, story["key"], "issue_triage")]}]
                facts = [
                    _fact("no_confirmation", "text", "说明没有正式批准/确认版本",
                          marker_groups=[["未确认", "待审核", "没有正式批准", "尚无确认版本"]]),
                    _fact("component", "component", "可选组件名称",
                          values=[story["component"]], required=False),
                ]
                gold = _base_gold(
                    task_id, category, story, records, difficulty="hard",
                    question=question, required_groups=groups, facts=facts,
                    forbidden_inferences=[["已正式批准", f"{story['new']} 已确认",
                                           "当前确认版本是该提案"]],
                    expected_abstention={"required": True,
                                         "expected_termination": ["insufficient", "conflict", "tool_failure"],
                                         "must_disclose": [["未确认", "待审核", "没有正式批准", "尚无确认版本"]]},
                    expected_decision_state={
                        "decision_id": decision["decision_id"], "known_at": None,
                        "expected_current_version_id": None,
                        "expected_version_ids": [],
                        "pending_proposal_id": decision["pending"],
                        "must_not_claim_confirmed": [decision["pending"]],
                    },
                    missing_fact=f"{story['component']}:{story['metric']}:formal_approval")
            elif category == "budget":
                question = (f"在受限预算下调查 {story['component']} 的{story['problem']}，"
                            "说明已完成与未完成部分。")
                groups = [{"group_id": "issue_triage", "any_of": [_ref(records, story["key"], "issue_triage")]}]
                facts = [
                    _fact("budget_limitation", "text", "说明预算受限与未完成部分",
                          marker_groups=[["预算"], ["未完成", "未确认", "停止", "无法完成"]]),
                    _fact("component", "component", "可选组件名称",
                          values=[story["component"]], required=False),
                ]
                gold = _base_gold(
                    task_id, category, story, records, difficulty="hard",
                    question=question, required_groups=groups, facts=facts,
                    forbidden_inferences=[["已完成全部调查", "结论完整"]],
                    budget_override={"max_tokens": 1, "max_model_rounds": 1, "max_tool_calls": 1},
                    expected_limitations=["预算耗尽时必须明确说明未完成，不得伪造结论。"],
                    expected_termination=["budget"])
            elif category == "tool_failure":
                if index % 2 == 0:
                    failed_skill, alternative_role = "problem", "doc_v2"
                    tool_clause = "故障"
                else:
                    failed_skill, alternative_role = "document", "issue_triage"
                    tool_clause = "文档"
                question = (f"{story['component']} 的{story['problem']}{tool_clause}如何恢复？"
                            "相关工具不可用时请换来源并说明缺口。")
                groups = [{"group_id": "alternative", "any_of": [_ref(records, story["key"], alternative_role)]}]
                facts = [
                    _fact("metric_value", "version", "配置值", values=[story["new"]]),
                    _fact("gap", "text", "说明工具失败或证据缺口",
                          marker_groups=[["工具", "失败", "缺口", "不可用"]]),
                    _fact("issue_id", "issue_id", "可选关联 Issue",
                          values=[story["issue_id"]], required=False),
                ]
                gold = _base_gold(
                    task_id, category, story, records, difficulty="hard",
                    question=question, required_groups=groups, facts=facts,
                    forbidden_inferences=[["工具没有失败", "不存在缺口"]],
                    injected_failure={"skill": failed_skill, "error": "timeout"},
                    expected_limitations=["必须说明工具失败或缺失来源。"])
            else:  # multi_turn
                question = (f"先确认 {story['component']} 的 {story['title']} 当前配置。"
                            "然后解释为什么后来从旧值改成新值。")
                groups = [{"group_id": "chat_proposal", "any_of": [_ref(records, story["key"], "chat_proposal")]},
                          {"group_id": "commit_fix", "any_of": [_ref(records, story["key"], "commit_fix")]},
                          {"group_id": "doc_v2", "any_of": [_ref(records, story["key"], "doc_v2")]}]
                facts = [
                    _fact("new_value", "version", "改变后的值", values=[story["new"]]),
                    _fact("reason", "text", "改变原因", marker_groups=[[story["cause"][:8]]]),
                    _fact("commit", "commit", "可选修复提交",
                          values=[story["commit_shas"][1],
                                  story["commit_shas"][1][:8]], required=False),
                ]
                gold = _base_gold(
                    task_id, category, story, records, difficulty="medium",
                    question=question, required_groups=groups, facts=facts,
                    session_id=f"session-v4-{story['key']}",
                    turns=[
                        {"question": f"{story['component']} 的 {story['title']} 当前配置是什么？",
                         "evaluation_target": ["new_value"]},
                        {"question": (f"那后来为什么从 {story['old']} 改成 {story['new']}？"
                                      "请引用讨论和提交。"),
                         "evaluation_target": ["reason"]},
                    ])
            public = {
                "task_id": task_id, "category": category, "project_id": story["project"]["project_id"],
                "dataset_kind": "live", "split": "test", "difficulty": gold["difficulty"],
                "question": gold["question"], "task_set_version": TASK_SET_VERSION,
            }
            if gold.get("session_id"):
                public["session_id"] = gold["session_id"]
                public["turns"] = [turn["question"] for turn in gold["turns"]]
            public_tasks.append(public)
            gold_tasks.append(gold)
    public = {"task_set_version": TASK_SET_VERSION, "tasks": public_tasks}
    gold = {"task_set_version": TASK_SET_VERSION, "scoring_version": SCORING_VERSION,
            "annotation_status": "synthetic_authored_pending_independent_human_review",
            "labels": {item["task_id"]: item for item in gold_tasks}}
    return public, gold


def build(output: Path = DEFAULT_DATASET, *, overwrite: bool = False) -> dict:
    output = Path(output)
    if output.exists() and not overwrite:
        raise FileExistsError(f"Snapshot directory exists: {output}")
    if output.exists():
        shutil.rmtree(output)
    (output / "tasks").mkdir(parents=True, exist_ok=True)
    records = _records()
    records_by_key = {}
    for record in records:
        records_by_key[(record.metadata["scenario_key"], record.metadata["record_role"])] = record
    records_path = output / "records.json"
    records_path.write_text(json.dumps([record.model_dump(mode="json") for record in records],
                                       ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    decision_path = output / "decisions.sql"
    decision_path.write_text(_decision_sql(records_by_key), encoding="utf-8")
    public, gold = _build_tasks(records_by_key)
    public_path, gold_path = output / "tasks/public.json", output / "tasks/gold.json"
    public_path.write_text(json.dumps(public, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    gold_path.write_text(json.dumps(gold, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    files = {}
    for relative in ("records.json", "decisions.sql", "tasks/public.json", "tasks/gold.json"):
        blob = (output / relative).read_bytes()
        files[relative] = {"bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest()}
    snapshot_id = "cma-v4-" + hashlib.sha256(records_path.read_bytes() + b"\0" +
                                              decision_path.read_bytes()).hexdigest()[:16]
    task_bytes = public_path.read_bytes() + b"\0" + gold_path.read_bytes()
    manifest = {
        "dataset_version": DATASET_VERSION,
        "task_set_version": TASK_SET_VERSION,
        "scoring_version": SCORING_VERSION,
        "snapshot_id": snapshot_id,
        "task_set_sha256": hashlib.sha256(task_bytes).hexdigest(),
        "record_count": len(records),
        "evidence_identity_count": len({record.evidence_id for record in records}),
        "project_count": len(PROJECTS),
        "files": files,
        "generated_by": "benchmark.v4.build",
        "annotation_status": "synthetic_authored_pending_independent_human_review",
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                          encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--overwrite", action="store_true",
                        help="replace an existing snapshot directory (authoring only)")
    args = parser.parse_args()
    print(json.dumps(build(args.output, overwrite=args.overwrite), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
