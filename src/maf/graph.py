from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Literal, NotRequired, Protocol, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, model_validator

from maf.sandbox.runner import collect_python_files
from maf.sandbox.docker_runner import is_test
from maf.schemas import CodeChange, Design, GeneratedFile, IntakeReply, Plan, Report, TestResult, TestSuite


class Team(Protocol):
    def plan(self, request: str, **context) -> Plan | IntakeReply: ...
    def design(self, plan: Plan, feedback: str, previous_design: Design | None = None, **context) -> Design: ...
    def implement(self, design: Design, **context) -> CodeChange: ...
    def fix(self, design: Design, code: CodeChange, result: TestResult, **context) -> CodeChange: ...
    def write_tests(self, design: Design, **context) -> TestSuite: ...
    def report(self, plan: Plan, code: CodeChange, tests: TestSuite, result: TestResult, **context) -> Report: ...


class TestRunner(Protocol):
    def __call__(
        self, run_id: str, spec_version: int, code: CodeChange, tests: TestSuite, *, project_id: str
    ) -> TestResult: ...


class RunState(TypedDict):
    run_id: str
    project_id: str
    project_context: NotRequired[dict]
    request: str
    spec_version: int
    plan: NotRequired[dict | None]
    design: NotRequired[dict | None]
    design_feedback: NotRequired[str]
    code: NotRequired[dict | None]
    tests: NotRequired[dict | None]
    result: NotRequired[dict | None]
    report: NotRequired[dict | None]
    fix_rounds: NotRequired[int]
    supervisor_reply: NotRequired[str | None]
    status: NotRequired[Literal["running", "waiting_approval", "done", "cancelled", "responded", "needs_input"]]


class GateDecision(BaseModel):
    action: str
    feedback: str = ""
    request: str = ""

    @model_validator(mode="after")
    def require_request_for_spec_change(self):
        if self.action == "change_spec" and not self.request.strip():
            raise ValueError("A spec change requires an updated request")
        return self


class DesignDecision(GateDecision):
    action: Literal["approve", "reject", "change_spec"]


class EscalationDecision(GateDecision):
    action: Literal["continue", "stop", "change_spec"]


def build_graph(
    team: Team, run_tests: TestRunner, checkpointer: BaseCheckpointSaver,
    load_project: Callable[[RunState], dict],
):
    def context(project: dict):
        return {"project": project}

    def plan(state: RunState):
        project = load_project(state)
        outcome = team.plan(state["request"], **context(project))
        reset = {
            "project_context": project,
            "design": None,
            "design_feedback": "",
            "code": None,
            "tests": None,
            "result": None,
            "report": None,
            "fix_rounds": 0,
        }
        if isinstance(outcome, IntakeReply):
            return {
                **reset,
                "plan": None,
                "supervisor_reply": outcome.message,
                "status": "needs_input" if outcome.action == "clarify" else "responded",
            }
        return {
            **reset,
            "plan": outcome.model_dump(mode="json"),
            "supervisor_reply": None,
            "status": "running",
        }

    def design(state: RunState):
        previous = state.get("design")
        design = team.design(
            Plan.model_validate(state["plan"]),
            state["design_feedback"],
            Design.model_validate(previous) if previous else None,
            **context(state["project_context"]),
        )
        return {"design": design.model_dump(mode="json"), "status": "waiting_approval"}

    def change_spec(state: RunState, request: str):
        return Command(
            update={"request": request, "spec_version": state["spec_version"] + 1},
            goto="pm_plan",
        )

    def gate_design(state: RunState) -> Command[Literal["dev_implement", "architect_design", "pm_plan"]]:
        decision = DesignDecision.model_validate(interrupt({
            "gate": "design",
            "spec_version": state["spec_version"],
            "design": state["design"],
            "design_feedback": state["design_feedback"],
        }))
        if decision.action == "change_spec":
            return change_spec(state, decision.request)
        if decision.action == "reject":
            return Command(
                update={"design_feedback": decision.feedback, "status": "running"},
                goto="architect_design",
            )
        return Command(update={"status": "running"}, goto="dev_implement")

    def implement(state: RunState):
        return {"code": team.implement(Design.model_validate(state["design"]), **context(state["project_context"])).model_dump(mode="json")}

    def write_tests(state: RunState):
        return {"tests": team.write_tests(Design.model_validate(state["design"]), **context(state["project_context"])).model_dump(mode="json")}

    def verify(state: RunState):
        code = CodeChange.model_validate(state["code"])
        tests = TestSuite.model_validate(state["tests"])
        project = state["project_context"]
        source, regression = {}, {}
        for path, body in project["files"].items():
            if path.endswith(".py"):
                target = regression if is_test(PurePosixPath(path)) else source
                target[path] = body

        def merge(base, changes):
            updated = {**base, **{str(path): body for path, body in collect_python_files(changes).items()}}
            return [GeneratedFile(path=path, content=body) for path, body in updated.items()]

        code = code.model_copy(update={"files": merge(source, code.files)})
        tests = tests.model_copy(update={"files": merge(regression, tests.files)})
        result = run_tests(
            state["run_id"],
            state["spec_version"],
            code,
            tests,
            project_id=state["project_id"],
        )
        return {"result": result.model_dump(mode="json")}

    def route_verification(state: RunState) -> Literal["pm_report", "dev_fix", "prepare_escalation"]:
        if state["result"]["exit_code"] is None or state["result"]["exit_code"] == 0:
            return "pm_report"
        if state["fix_rounds"] < 3:
            return "dev_fix"
        return "prepare_escalation"

    def fix(state: RunState):
        code = team.fix(
            Design.model_validate(state["design"]),
            CodeChange.model_validate(state["code"]),
            TestResult.model_validate(state["result"]),
            **context(state["project_context"]),
        )
        return {"code": code.model_dump(mode="json"), "fix_rounds": state["fix_rounds"] + 1}

    def prepare_escalation(state: RunState):
        return {"status": "waiting_approval"}

    def gate_escalate(state: RunState) -> Command[Literal["dev_fix", "pm_plan", "__end__"]]:
        decision = EscalationDecision.model_validate(interrupt({
            "gate": "escalation",
            "spec_version": state["spec_version"],
            "result": state["result"],
        }))
        if decision.action == "change_spec":
            return change_spec(state, decision.request)
        if decision.action == "continue":
            return Command(update={"fix_rounds": 0, "status": "running"}, goto="dev_fix")
        return Command(update={"status": "cancelled"}, goto=END)

    def report(state: RunState):
        result = TestResult.model_validate(state["result"])
        summary = team.report(
            Plan.model_validate(state["plan"]),
            CodeChange.model_validate(state["code"]),
            TestSuite.model_validate(state["tests"]),
            result,
            **context(state["project_context"]),
        )
        return {
            "report": summary.model_copy(update={"test_result": result}).model_dump(mode="json"),
            "status": "done",
        }

    graph = StateGraph(RunState)
    graph.add_node("pm_plan", plan)
    graph.add_node("architect_design", design)
    graph.add_node("gate_design", gate_design)
    graph.add_node("dev_implement", implement)
    graph.add_node("qa_write_tests", write_tests)
    graph.add_node("qa_run", verify)
    graph.add_node("dev_fix", fix)
    graph.add_node("prepare_escalation", prepare_escalation)
    graph.add_node("gate_escalate", gate_escalate)
    graph.add_node("pm_report", report)
    graph.add_edge(START, "pm_plan")
    graph.add_conditional_edges(
        "pm_plan",
        lambda state: "architect_design" if state["plan"] is not None else END,
        ["architect_design", END],
    )
    graph.add_edge("architect_design", "gate_design")
    graph.add_edge("dev_implement", "qa_write_tests")
    graph.add_edge("qa_write_tests", "qa_run")
    graph.add_edge("dev_fix", "qa_run")
    graph.add_conditional_edges("qa_run", route_verification)
    graph.add_edge("prepare_escalation", "gate_escalate")
    graph.add_edge("pm_report", END)
    return graph.compile(checkpointer=checkpointer)
