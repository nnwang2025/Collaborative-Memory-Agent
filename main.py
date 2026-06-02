"""Collaborative Memory Agent – CLI entry point.

Three agent backends available:
  1. LangGraph (default) – StateGraph with parallel skill fan-out
  2. ReAct      (–react) – Iterative Think-Act-Observe loop
  3. Linear     (–linear) – Original sequential pipeline
"""

import sys

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
    use_react = "--react" in sys.argv
    use_linear = "--linear" in sys.argv

    if use_react:
        print_banner("ReAct Agent (Think-Act-Observe)")
        from src.agents.react_agent import ReActAgent
        agent = ReActAgent(max_iterations=5)
        run_react_loop(agent)
    elif use_linear:
        print_banner("Linear Pipeline")
        agent = CoordinatorAgent(use_langgraph=False)
        run_simple_loop(agent)
    else:
        print_banner("LangGraph StateGraph (parallel fan-out)")
        agent = CoordinatorAgent(use_langgraph=True)
        run_simple_loop(agent)


def run_simple_loop(agent):
    """CLI loop for LangGraph and Linear backends (single ask() call)."""
    while True:
        try:
            question = input("\n❯ ").strip()
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


def run_react_loop(agent):
    """CLI loop for ReAct backend."""
    while True:
        try:
            question = input("\n🧠❯ ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break

        if question.lower() in {"exit", "quit", "q"}:
            print("再见！")
            break
        if not question:
            continue

        answer = agent.run(question)
        print()
        print(answer)


if __name__ == "__main__":
    main()
