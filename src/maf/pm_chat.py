"""Typed tools and run context for the Supervisor agent."""

from typing import Annotated, Literal

from pydantic import Field, create_model

from maf.schemas import StructuredOutput


class PMAnswer(StructuredOutput):
    action: Literal["answer"]
    answer: str = Field(min_length=1, max_length=3000, pattern=r"\S")


class PMListRuns(StructuredOutput):
    action: Literal["list_runs"]
    scope: Literal["active", "recent"] = "active"
    query: str = Field(default="", max_length=200)
    project_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")


class PMGetRun(StructuredOutput):
    action: Literal["get_run"]
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class PMCreateRun(StructuredOutput):
    project_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    action: Literal["create_run"]
    request: str = Field(min_length=1, max_length=20000, pattern=r"\S")


class PMChangeRun(StructuredOutput):
    action: Literal['change_run']
    run_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    request: str = Field(min_length=1, max_length=6000)


class PMControlRun(StructuredOutput):
    action: Literal['control_run']
    run_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    operation: Literal['pause', 'continue', 'stop']


class PMGetProject(StructuredOutput):
    action: Literal['get_project']
    project_id: str = Field(pattern=r'^[0-9a-f]{32}$')


PM_ACTIONS = {
    "list_runs": PMListRuns,
    "get_run": PMGetRun,
    "create_run": PMCreateRun,
    "get_project": PMGetProject,
    "change_run": PMChangeRun,
    "control_run": PMControlRun,
}


def pm_step_schema(tools):
    outcome = PMAnswer
    for name, action in PM_ACTIONS.items():
        if name in tools:
            outcome = outcome | action
    if tools:
        outcome = Annotated[outcome, Field(discriminator="action")]
    return create_model("PMStep", __base__=StructuredOutput, outcome=(outcome, ...))


def summarize_run(state: dict, events: list[dict]) -> dict:
    timeline = []
    for event in events:
        entry = {key: event[key] for key in ("id", "ts", "spec_version", "type", "agent")}
        payload = event["payload"]
        for key in ("node", "gate", "action", "exit_code", "status"):
            if key in payload:
                entry[key] = payload[key]
        if payload.get("reason"):
            entry["reason"] = str(payload["reason"])[:300]
        timeline.append(entry)

    dispatched = {event["task_id"]: event for event in events if event["type"] == "task.started"}
    for event in events:
        if event["type"] in {"task.done", "error", "approval.requested"}:
            dispatched.pop(event["task_id"], None)
    result = state.get("result")
    report = state.get("report")
    return {
        "plan": state.get("plan"),
        "tasks": state.get("tasks", []),
        "calls": state.get("calls"),
        "run_id": state["run_id"],
        "project_id": state.get("project_id"),
        "spec_version": state["spec_version"],
        "request": state["request"][:1200],
        "status": state["status"],
        "current_tasks": [
            {"node": event["payload"].get("node"), "agent": event["agent"]}
            for event in dispatched.values()
        ] if state["status"] == "running" else [],
        "next": state["next"],
        "pending_gates": [gate["gate"] for gate in state["pending_approvals"]],
        "fix_rounds": state.get("fix_rounds"),
        "test_result": {"exit_code": result["exit_code"], "stdout": result["stdout"][-1600:]} if result else None,
        "final_summary": report["summary"][:700] if report else None,
        "recent_events": timeline,
    }
