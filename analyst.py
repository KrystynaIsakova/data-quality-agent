"""Terminal entry point for the Analytics Orchestrator: python analyst.py"""
import sys

from dq_agent.orchestrator import Orchestrator, OrchestratorReply
from dq_agent.subagent import AgentError, StartupError


def print_reply(reply: OrchestratorReply) -> None:
    print(f"\nAgent: {reply.text}\n")
    # Exact values from the KPI tools, independent of the model's wording.
    for kpi in reply.kpi_results:
        unit = kpi["unit"]
        if kpi["by"] is None:
            print(f"[{kpi['metric']}] {kpi['value']} {unit}  (n={kpi['n']})")
            continue
        print(f"[{kpi['metric']} by {kpi['by']}]" + (" (truncated)" if kpi["truncated"] else ""))
        for row in kpi["rows"]:
            print(f"  {row[kpi['by']]}: {row['value']} {unit}  (n={row['n']})")
    if reply.warnings:
        print("Data-quality warnings:")
        for w in reply.warnings:
            target = f"{w['table']}.{w['column']}" if w["column"] else w["table"]
            print(f"  [{w['severity']}] {target}: {w['rule']} — {w['actual']}")
    print()


def main() -> int:
    try:
        orchestrator = Orchestrator.create()
    except StartupError as error:
        print(error)
        return 1

    metrics = ", ".join(orchestrator.layer.get("metrics", {}))
    print("Analytics Orchestrator (Gemini + KPI tools + Data Quality Subagent, read-only)")
    print(f"Metrics: {metrics}")
    print("Example: what is the completion rate by specialization?")
    print("Type 'exit' to stop.\n")

    try:
        while True:
            try:
                question = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if question.lower() in {"exit", "quit"}:
                break
            if not question:
                continue
            try:
                print_reply(orchestrator.ask(question))
            except AgentError as error:  # keep the session alive
                print(f"\nError: {error}\n")
    finally:
        orchestrator.close()
    print("Agent: Goodbye!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
