"""Terminal entry point: python main.py"""
import sys

from dq_agent.session import AgentError, StartupError, start_session


def main() -> int:
    try:
        session = start_session()
    except StartupError as error:
        print(error)
        return 1

    for problem in session.rule_problems:
        print(f"Warning: {problem}")
    print("Data Quality Agent (Gemini + PostgreSQL, read-only)")
    print(f"Tables: {', '.join(session.schema.tables())}")
    print("Example: check duplicates and missing values in enrollments")
    print("Type 'exit' to stop.\n")

    try:
        while True:
            try:
                request = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if request.lower() in {"exit", "quit"}:
                break
            if not request:
                continue

            try:
                reply = session.ask(request)
            except AgentError as error:  # keep the session alive
                print(f"\nError: {error}\n")
                continue

            print(f"\nAgent: {reply.text}\n")
            if reply.questions:
                print("Questions for the business:")
                for q in reply.questions:
                    print(f"  - {q}")
                print()
            if session.report_path:
                print(f"Report saved: reports/{session.report_path.name}\n")
    finally:
        session.close()
    print("Agent: Goodbye!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
