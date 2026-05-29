from src.agents.coordinator_agent import CoordinatorAgent


def main():
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ModuleNotFoundError:
        pass

    agent = CoordinatorAgent()

    print("Collaborative Memory Agent")
    print("输入问题开始检索，输入 exit 退出。")

    while True:
        question = input("\nEnter question: ").strip()
        if question.lower() in {"exit", "quit"}:
            break
        if not question:
            continue
        print(agent.ask(question))


if __name__ == "__main__":
    main()
