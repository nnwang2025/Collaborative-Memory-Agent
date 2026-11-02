"""Collaborative Memory Agent – CLI entry point.

Three agent backends available:
  1. LangGraph (default) – StateGraph with parallel skill fan-out
  2. ReAct      (–react) – Iterative Think-Act-Observe loop
  3. Linear     (–linear) – Original sequential pipeline
"""

import argparse

from src.evidence import EvidenceScope

from src.agents.coordinator_agent import CoordinatorAgent


def print_banner(backend: str):
    print("=" * 60)
    print(f"  Collaborative Memory Agent  v2.0")
    print(f"  Backend: {backend}")
    print("=" * 60)
    print("输入问题开始检索，输入 exit 退出。")
    print()


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
    args = parser.parse_args()
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
