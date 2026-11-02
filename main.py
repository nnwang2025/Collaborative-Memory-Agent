"""Collaborative Memory Agent – CLI entry point.

Three agent backends available:
  1. LangGraph (default) – StateGraph with parallel skill fan-out
  2. ReAct      (–react) – Iterative Think-Act-Observe loop
  3. Linear     (–linear) – Original sequential pipeline
"""

import argparse
import json

from src.evidence import EvidenceScope

from src.agents.coordinator_agent import CoordinatorAgent


def print_banner(backend: str):
    print("=" * 60)
    print(f"  Collaborative Memory Agent  v2.0")
    print(f"  Backend: {backend}")
    print("=" * 60)
    print("输入问题开始检索，输入 exit 退出。")
    print()


def _print_json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _db_path(args):
    from src.evidence_store import default_db_path
    return args.db or str(default_db_path())


def _v2_source_add(args):
    from src.sync.models import SourceSpec
    from src.sync.service import SourceRegistry
    config = json.loads(args.config_json)
    if not isinstance(config, dict):
        raise SystemExit("--config-json must be a JSON object")
    spec = SourceRegistry(_db_path(args)).register(SourceSpec(
        project_id=args.project_id, dataset_kind=args.dataset_kind,
        connector_type=args.connector, source_uri=args.source_uri,
        config=config, display_name=args.name,
    ))
    _print_json({"source_id": spec.source_id, "project_id": spec.project_id,
                 "dataset_kind": spec.dataset_kind, "connector_type": spec.connector_type,
                 "source_uri": spec.source_uri, "config": spec.config})


def _v2_sync(args):
    from src.sync.service import SyncService
    report = SyncService(_db_path(args)).sync(args.source_id, dry_run=args.dry_run)
    _print_json(report.as_dict())


def _v2_source_list(args):
    from src.sync.service import SourceRegistry
    sources = SourceRegistry(_db_path(args)).list(
        project_id=args.project_id, dataset_kind=args.dataset_kind)
    _print_json([{"source_id": s.source_id, "project_id": s.project_id,
                  "dataset_kind": s.dataset_kind, "connector_type": s.connector_type,
                  "source_uri": s.source_uri, "display_name": s.display_name,
                  "config": s.config} for s in sources])


def _v2_source_status(args):
    from src.sync.service import SyncService
    _print_json(SyncService(_db_path(args)).status(args.source_id))


def _v2_source_set(args):
    from src.sync.service import SourceRegistry
    registry = SourceRegistry(_db_path(args))
    getattr(registry, args.action)(args.source_id)
    _print_json({"source_id": args.source_id, "status": registry.status(args.source_id)})


def _v2_index_build(args):
    from src.index.builder import IndexBuilder
    scope = EvidenceScope(project_id=args.project_id, dataset_kind=args.dataset_kind)
    result = IndexBuilder(_db_path(args), index_root=args.index_root).build(
        scope, force=args.force)
    _print_json(result)


def _v2_index_status(args):
    from src.index.builder import IndexBuilder
    scope = EvidenceScope(project_id=args.project_id, dataset_kind=args.dataset_kind)
    _print_json(IndexBuilder(_db_path(args), index_root=args.index_root).status(scope))


def _v2_index_verify(args):
    from src.index.builder import IndexBuilder
    scope = EvidenceScope(project_id=args.project_id, dataset_kind=args.dataset_kind)
    _print_json(IndexBuilder(_db_path(args), index_root=args.index_root).verify(
        scope, version_id=args.version))


def _v2_index_activate(args):
    from src.index.store import IndexStore
    _print_json(IndexStore(_db_path(args)).activate(args.version))


def _v2_retrieve(args):
    from src.retrieval import retrieve_evidence
    scope = EvidenceScope(project_id=args.project_id, dataset_kind=args.dataset_kind)
    hits = retrieve_evidence(args.query, scope=scope, db_path=_db_path(args),
                             index_root=args.index_root, mode=args.mode,
                             top_k=args.top_k)
    _print_json(hits)


def _add_v2_commands(parser):
    commands = parser.add_subparsers(dest="command")

    add = commands.add_parser("v2-source-add", help="Register an ingestion source")
    add.add_argument("--project-id", required=True)
    add.add_argument("--dataset-kind", choices=["fixture", "live"], default="live")
    add.add_argument("--connector", required=True, choices=["git", "directory", "csv", "json"])
    add.add_argument("--source-uri", required=True)
    add.add_argument("--config-json", default="{}")
    add.add_argument("--name")
    add.add_argument("--db", default=argparse.SUPPRESS)
    add.set_defaults(handler=_v2_source_add)

    sync = commands.add_parser("v2-sync", help="Run an incremental/full synchronization")
    sync.add_argument("source_id")
    sync.add_argument("--dry-run", action="store_true")
    sync.add_argument("--db", default=argparse.SUPPRESS)
    sync.set_defaults(handler=_v2_sync)

    listing = commands.add_parser("v2-source-list", help="List registered sources")
    listing.add_argument("--project-id")
    listing.add_argument("--dataset-kind", choices=["fixture", "live"])
    listing.add_argument("--db", default=argparse.SUPPRESS)
    listing.set_defaults(handler=_v2_source_list)

    status = commands.add_parser("v2-source-status", help="Show source, cursor, and run status")
    status.add_argument("source_id")
    status.add_argument("--db", default=argparse.SUPPRESS)
    status.set_defaults(handler=_v2_source_status)

    for action in ("pause", "resume"):
        command = commands.add_parser(f"v2-source-{action}", help=f"{action.title()} a source")
        command.add_argument("source_id")
        command.add_argument("--db", default=argparse.SUPPRESS)
        command.set_defaults(handler=_v2_source_set, action=action)

    build = commands.add_parser("v2-index-build", help="Build and atomically publish a live index")
    build.add_argument("--project-id", required=True)
    build.add_argument("--dataset-kind", choices=["live"], default="live")
    build.add_argument("--index-root")
    build.add_argument("--force", action="store_true")
    build.add_argument("--db", default=argparse.SUPPRESS)
    build.set_defaults(handler=_v2_index_build)

    index_status = commands.add_parser("v2-index-status", help="Show index versions and staleness")
    index_status.add_argument("--project-id", required=True)
    index_status.add_argument("--dataset-kind", choices=["live"], default="live")
    index_status.add_argument("--index-root")
    index_status.add_argument("--db", default=argparse.SUPPRESS)
    index_status.set_defaults(handler=_v2_index_status)

    verify = commands.add_parser("v2-index-verify", help="Verify manifest checksums")
    verify.add_argument("--project-id", required=True)
    verify.add_argument("--dataset-kind", choices=["live"], default="live")
    verify.add_argument("--version")
    verify.add_argument("--index-root")
    verify.add_argument("--db", default=argparse.SUPPRESS)
    verify.set_defaults(handler=_v2_index_verify)

    activate = commands.add_parser("v2-index-activate", help="Switch/rollback the active alias")
    activate.add_argument("version")
    activate.add_argument("--db", default=argparse.SUPPRESS)
    activate.set_defaults(handler=_v2_index_activate)

    retrieve = commands.add_parser("v2-retrieve", help="Query live Evidence (bm25/dense/hybrid)")
    retrieve.add_argument("--project-id", required=True)
    retrieve.add_argument("--dataset-kind", choices=["live"], default="live")
    retrieve.add_argument("--query", required=True)
    retrieve.add_argument("--mode", choices=["bm25", "dense", "hybrid"], default="bm25")
    retrieve.add_argument("--top-k", type=int, default=10)
    retrieve.add_argument("--index-root")
    retrieve.add_argument("--db", default=argparse.SUPPRESS)
    retrieve.set_defaults(handler=_v2_retrieve)


def main():
    # Load .env
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ModuleNotFoundError:
        pass

    # Parse CLI flags
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--react", action="store_true")
    modes.add_argument("--linear", action="store_true")
    modes.add_argument("--langgraph", action="store_true")
    parser.add_argument("--dataset-kind", choices=["fixture", "live"], default="fixture")
    parser.add_argument("--project-id")
    parser.add_argument("--db", type=str, default=None, help="Explicit live Evidence SQLite path")
    parser.add_argument("--trace-jsonl", help="Explicit opt-in export path, for --react only")
    _add_v2_commands(parser)
    args = parser.parse_args()
    if getattr(args, "command", None):
        try:
            args.handler(args)
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            raise SystemExit(f"error: {exc}") from exc
        return
    fields = {"dataset_kind": args.dataset_kind}
    if args.project_id is not None:
        fields["project_id"] = args.project_id
    try:
        scope = EvidenceScope(**fields)
    except ValueError as exc:
        parser.error(str(exc))
    use_react, use_linear = args.react, args.linear

    if use_react:
        print_banner("ReAct Agent (Think-Act-Observe)")
        from src.agents.react_agent import ReActAgent
        agent = ReActAgent(max_iterations=5, scope=scope, db_path=args.db)
        run_react_loop(agent, trace_path=args.trace_jsonl)
    elif use_linear:
        print_banner("Linear Pipeline")
        agent = CoordinatorAgent(use_langgraph=False, scope=scope, db_path=args.db)
        run_simple_loop(agent)
    else:
        print_banner("LangGraph StateGraph (parallel fan-out)")
        agent = CoordinatorAgent(use_langgraph=True, scope=scope, db_path=args.db)
        run_simple_loop(agent)


def run_simple_loop(agent):
    """CLI loop for LangGraph and Linear backends (single ask() call)."""
    while True:
        try:
            question = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break

        if question.lower() in {"exit", "quit", "q"}:
            print("再见！")
            break
        if not question:
            continue

        answer = agent.ask(question)
        print()
        print(answer)


def run_react_loop(agent, trace_path=None):
    """CLI loop for ReAct backend."""
    while True:
        try:
            question = input("\nreact> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break

        if question.lower() in {"exit", "quit", "q"}:
            print("再见！")
            break
        if not question:
            continue

        if trace_path:
            result = agent.run_result(question)
            result.export_jsonl(trace_path)
            answer = result.answer
        else:
            answer = agent.run(question)
        print()
        print(answer)


if __name__ == "__main__":
    main()
