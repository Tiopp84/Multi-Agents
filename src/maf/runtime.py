from langgraph.types import Command

from maf.events.store import EventStore
from maf.graph import DesignDecision, EscalationDecision


NODE_AGENTS = {
    "pm_plan": "supervisor",
    "architect_design": "architect",
    "dev_implement": "developer",
    "dev_fix": "developer",
    "qa_write_tests": "qa",
    "qa_run": "qa",
    "pm_report": "supervisor",
}
ARTIFACTS = {
    "architect_design": "design",
    "dev_implement": "code",
    "dev_fix": "code",
    "qa_write_tests": "tests",
}


class DuplicateDecision(ValueError):
    pass


class RunRuntime:
    """Execute a single run command at a time in the local runtime."""

    def __init__(self, graph, events: EventStore):
        self.graph = graph
        self.events = events

    @staticmethod
    def config(run_id: str):
        return {"configurable": {"thread_id": run_id}}

    def state(self, run_id: str):
        return self.graph.get_state(self.config(run_id))

    def start(self, run_id: str, request: str, project_id: str):
        if not run_id or not request.strip():
            raise ValueError("Run ID and request are required")
        if self.state(run_id).values:
            raise ValueError(f"Run already exists: {run_id}")
        payload = {"request": request}
        payload["project_id"] = project_id
        self.events.append(run_id, 1, "run.created", payload)
        return self._execute(run_id, 1, {"run_id": run_id, "spec_version": 1, **payload})

    def decide(self, run_id: str, decision: dict):
        snapshot = self.state(run_id)
        interrupts = [item for task in snapshot.tasks for item in task.interrupts]
        if len(interrupts) != 1:
            raise ValueError("Run has no pending approval")
        pending = interrupts[0]
        gate = pending.value["gate"]
        decision_type = DesignDecision if gate == "design" else EscalationDecision
        validated = decision_type.model_validate(decision)
        version = snapshot.values["spec_version"]
        recorded = self.events.append(
            run_id, version, "approval.resolved",
            {"gate": gate, "gate_id": pending.id, **validated.model_dump()},
        )
        if not recorded:
            raise DuplicateDecision("Decision was already recorded")
        if validated.action == "change_spec":
            version += 1
            self.events.append(run_id, version, "spec.updated", {"request": validated.request})
        return self._execute(
            run_id, version, Command(resume={pending.id: validated.model_dump()}),
            previous_agent=self.events.last_completed_agent(run_id, version),
        )

    def _execute(self, run_id, version, graph_input, *, previous_agent=None):
        config = self.config(run_id)

        def emit(kind, payload, **context):
            self.events.append(run_id, version, kind, payload, **context)

        for task in self.graph.stream(graph_input, config, stream_mode="tasks"):
            node = task["name"]
            context = {"agent": NODE_AGENTS.get(node), "task_id": task["id"]}
            if "input" in task:
                agent = context["agent"]
                if agent and previous_agent and agent != previous_agent:
                    emit("handoff", {"from": previous_agent, "to": agent, "node": node}, **context)
                if agent:
                    previous_agent = agent
                emit("task.started", {"node": node}, **context)
                continue
            if task["error"]:
                emit("error", {"node": node, "reason": str(task["error"])}, **context)
                continue
            if task["interrupts"]:
                for interrupt in task["interrupts"]:
                    value = interrupt["value"]
                    self.events.append(
                        run_id, value["spec_version"], "approval.requested",
                        {**value, "gate_id": interrupt["id"]}, **context
                    )
                continue
            result = task["result"]
            if node in ARTIFACTS:
                key = ARTIFACTS[node]
                emit("artifact.created", {"kind": key, "artifact": result[key]}, **context)
            if node == "qa_run":
                emit("test.result", result["result"], **context)
            emit("task.done", {"node": node}, **context)

        state = self.graph.get_state(config).values
        if state["status"] in {"done", "cancelled", "responded", "needs_input"}:
            payload = {"status": state["status"], "report": state.get("report")}
            if state.get("supervisor_reply"):
                payload["supervisor_reply"] = state["supervisor_reply"]
            emit("run.finished", payload)
        return state
