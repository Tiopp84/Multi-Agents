"""Local HTTP gateway backed by SQLite checkpoints and event replay."""

import asyncio
import hmac
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, closing, contextmanager
from pathlib import Path
from threading import BoundedSemaphore, Event, Lock, Thread
from time import monotonic
from typing import Annotated
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import BaseModel, Field, StringConstraints, ValidationError

from maf.app import build_application_graph
from maf import department
from maf.model_config import AgentSettings, ModelConfig, PM_TOOLS, WORK_TOOLS, RoleName
from maf.events.outbox import deliver_events, signature
from maf.events.store import EventStore, NOTIFICATIONS
from maf.model_team import ModelTeam
from maf.pm_chat import PMAnswer, PMCreateRun, PMGetRun, PMListRuns, PMChangeRun, PMControlRun, PMGetProject, summarize_run
from maf.projects import Project, create_project, project_baseline
from maf.runtime import DuplicateDecision, RunRuntime
from maf.telegram import HELP, incoming, notification, replay_text, revision_help, run_status


logger = logging.getLogger(__name__)
PM_TIMEOUT = 120
bearer = HTTPBearer(auto_error=False)
RequestText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
ProjectID = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]


class RunRequest(BaseModel):
    request: RequestText
    project_id: ProjectID
    max_calls: int = Field(default=200, ge=10, le=2000)
    calls_per_minute: int = Field(default=6, ge=1, le=60)


class ProjectRequest(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    workspace: RequestText
    folder: str = Field(default="", max_length=120)
    brief: str = Field(default="", max_length=4000)


class Feedback(BaseModel):
    feedback: str = ""
    gate_id: str | None = None


class SpecRequest(BaseModel):
    request: RequestText
    gate_id: str | None = None


class SignedEvent(BaseModel):
    payload: str
    signature: str


def create_app(root: Path | None = None, data_dir: Path | None = None) -> FastAPI:
    root = root or Path(__file__).resolve().parents[2]
    load_dotenv(root / ".env")
    data_dir = data_dir or root / ".maf"
    data_dir.mkdir(parents=True, exist_ok=True)
    workspace_root = Path(os.environ.get("MAF_WORKSPACE_ROOT", str(Path.home()))).expanduser().resolve()
    token = os.environ.get("MAF_GATEWAY_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Set MAF_GATEWAY_TOKEN before starting the Gateway")

    def authenticate(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        if credentials is None or not hmac.compare_digest(
            credentials.credentials.encode(), token.encode()
        ):
            raise HTTPException(401, "Invalid bearer token", headers={"WWW-Authenticate": "Bearer"})

    owner = os.environ.get("MAF_TELEGRAM_CHAT_ID", "").strip()
    webhook = os.environ.get("MAF_N8N_EVENT_URL", "").strip()
    webhook_secret = os.environ.get("MAF_N8N_WEBHOOK_SECRET", "").strip()
    if webhook and (not webhook_secret or not owner):
        raise RuntimeError("Set MAF_N8N_WEBHOOK_SECRET and MAF_TELEGRAM_CHAT_ID for n8n delivery")

    pm_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="maf-pm")
    pm_slots = BoundedSemaphore(2)

    @asynccontextmanager
    async def lifespan(app):
        stop = Event()
        department_thread = Thread(target=department_worker, args=(stop,), daemon=True, name='maf-department')
        department_thread.start()
        if webhook:
            worker = Thread(
                target=deliver_events, args=(data_dir / "events.sqlite", webhook, webhook_secret, stop),
                daemon=True,
            )
            worker.start()
        try:
            yield
        finally:
            stop.set()
            department_thread.join(timeout=2)
            pm_pool.shutdown(wait=False)

    app = FastAPI(title="Multi-agent Gateway", lifespan=lifespan)
    api = APIRouter(dependencies=[Depends(authenticate)])
    dashboard = Path(__file__).with_name("dashboard")
    app.mount("/dashboard/assets", StaticFiles(directory=dashboard), name="dashboard-assets")

    @app.get("/dashboard", include_in_schema=False)
    def dashboard_page():
        return FileResponse(dashboard / "index.html", headers={
            "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; base-uri 'none'",
            "Cache-Control": "no-store",
        })
    active_runs: set[str] = set()
    active_projects: set[str] = set()
    active_lock = Lock()
    agent_config_lock = Lock()
    project_registry_lock = Lock()
    workspace_picker_lock = Lock()

    def agent_payload(config: ModelConfig, role: RoleName):
        profile = config.profile(role)
        settings = profile.model_dump(mode="json")
        settings["tools"] = sorted(settings["tools"])
        settings["routes"] = [route.model_dump() for route in getattr(config.roles, role)]
        return settings

    @api.get("/agents")
    def list_agents():
        config = ModelConfig.load(root)
        return {
            "agents": {role: agent_payload(config, role) for role, _ in config.roles},
            "backends": {name: backend.kind for name, backend in config.backends.items()},
            "tools": {role: PM_TOOLS if role == "supervisor" else WORK_TOOLS for role, _ in config.roles},
        }

    @api.put("/agents/{role}")
    def update_agent(role: RoleName, settings: AgentSettings):
        try:
            with agent_config_lock:
                config = ModelConfig.save_agent(root, role, settings)
        except ValidationError as error:
            raise HTTPException(422, error.errors(include_url=False, include_context=False, include_input=False)) from error
        return agent_payload(config, role)

    @contextmanager
    def event_store():
        with closing(sqlite3.connect(data_dir / "events.sqlite")) as connection:
            yield EventStore(connection)

    def workspace_path(value: str):
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise HTTPException(422, "Workspace phải là đường dẫn tuyệt đối trên máy Gateway.")
        path = path.resolve()
        if not path.is_relative_to(workspace_root) or not path.is_dir():
            raise HTTPException(422, "Chọn thư mục có sẵn trong vùng workspace của Gateway.")
        if any(part.startswith(".") for part in path.relative_to(workspace_root).parts):
            raise HTTPException(422, "Chọn một thư mục workspace hiển thị.")
        return path

    @api.post("/workspace-picker")
    def open_workspace_picker(path: str | None = None):
        folder = workspace_path(path or str(workspace_root))
        picker = shutil.which("zenity")
        if not picker or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            raise HTTPException(503, "Gateway cần chạy trong phiên desktop có Zenity để mở hộp thoại chọn thư mục.")
        if not workspace_picker_lock.acquire(blocking=False):
            raise HTTPException(409, "Hộp thoại hệ điều hành đang mở.")
        try:
            result = subprocess.run(
                [picker, "--file-selection", "--directory", "--title=Chọn workspace project",
                 f"--filename={folder}{os.sep}"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180,
            )
        except subprocess.TimeoutExpired as error:
            raise HTTPException(408, "Đã hết thời gian chọn thư mục.") from error
        except OSError as error:
            raise HTTPException(503, "Gateway không mở được hộp thoại chọn thư mục.") from error
        finally:
            workspace_picker_lock.release()
        if result.returncode == 1 and "display" not in result.stderr.casefold():
            return {"path": None}
        if result.returncode != 0 or not result.stdout:
            raise HTTPException(503, "Gateway không mở được hộp thoại trong phiên desktop hiện tại.")
        return {"path": str(workspace_path(result.stdout.removesuffix("\n")))}

    @api.get("/projects")
    def list_projects():
        with event_store() as events:
            return [
                Project(pid, name, Path(folder), brief).describe()
                for pid, name, folder, brief in events.connection.execute(
                    "SELECT id, name, workspace, brief FROM projects ORDER BY name COLLATE NOCASE, id"
                )
            ]

    def find_project(project_id: str):
        with event_store() as events:
            row = events.connection.execute(
                "SELECT id, name, workspace, brief FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        if not row:
            raise HTTPException(404, "Không tìm thấy project.")
        return Project(row[0], row[1], Path(row[2]), row[3])

    @api.post("/projects", status_code=201)
    def register_project(body: ProjectRequest):
        with project_registry_lock:
            return initialize_project(body)

    def initialize_project(body: ProjectRequest):
        workspace = workspace_path(body.workspace)
        folder = body.folder.strip()
        if folder:
            if folder.startswith(".") or "/" in folder or "\\" in folder or "\x00" in folder:
                raise HTTPException(422, "Tên thư mục con phải là một tên thư mục hợp lệ.")
            workspace = workspace / folder
        for registered in list_projects():
            other = Path(registered["workspace"])
            if workspace.is_relative_to(other) or other.is_relative_to(workspace):
                raise HTTPException(409, "Chọn workspace riêng, không trùng hoặc lồng với project đã đăng ký.")
        if folder:
            try:
                workspace.mkdir()
            except FileExistsError as error:
                raise HTTPException(409, "Thư mục đã tồn tại. Chọn trực tiếp nếu muốn sử dụng.") from error
            except OSError as error:
                raise HTTPException(422, "Gateway không tạo được thư mục con.") from error
        try:
            with event_store() as events:
                project = create_project(events.connection, workspace, body.name, body.brief.strip())
        except (OSError, sqlite3.IntegrityError) as error:
            if folder:
                workspace.rmdir()
            if isinstance(error, (FileExistsError, sqlite3.IntegrityError)):
                raise HTTPException(409, "Thư mục này đã có dữ liệu MAF.") from error
            raise HTTPException(422, "Gateway không ghi được dữ liệu project vào workspace.") from error
        return project.describe()

    def project_for_run(run_id: str):
        with event_store() as events:
            project_id = events.run_project(run_id)
        if not project_id:
            raise HTTPException(404, "Run không thuộc project hiện tại.")
        return find_project(project_id)

    @contextmanager
    def runtime(project: Project):
        try:
            storage = project.storage
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        checkpoint_path = storage / "checkpoints.sqlite"
        if checkpoint_path.is_symlink():
            raise HTTPException(409, "Checkpoint project phải là file trong workspace.")
        with (
            event_store() as events,
            SqliteSaver.from_conn_string(str(checkpoint_path)) as checkpoint,
        ):
            events.attach_project(storage / "events.sqlite")
            graph = build_application_graph(root, project, checkpoint)
            yield RunRuntime(graph, events)

    @contextmanager
    def execute(run_id: str, project: Project):
        with active_lock:
            if run_id in active_runs:
                raise HTTPException(409, "This run is already processing a command")
            if project.id in active_projects:
                raise HTTPException(409, "Project đang xử lý một lệnh khác. Thử lại sau khi lệnh đó kết thúc.")
            active_runs.add(run_id)
            active_projects.add(project.id)
        try:
            with runtime(project) as service:
                yield service
        except HTTPException:
            raise
        except Exception as error:
            logger.exception("Run execution failed: %s", run_id)
            raise HTTPException(
                502, {"run_id": run_id, "message": "Run execution failed. See replay or server logs."}
            ) from error
        finally:
            with active_lock:
                active_runs.discard(run_id)
                active_projects.discard(project.id)

    def department_worker(stop):
        recovered = set()
        while not stop.is_set():
            try:
                for item in list_projects():
                    if stop.is_set():
                        break
                    try:
                        project = find_project(item['id'])
                        states = department.all_runs(project)
                        # One project workspace is written by one active run at a time.
                        selected_run = next((value['run_id'] for value in states if value['status'] in {'running', 'planning'}
                                             or value.get('pending_request')), None)
                        for state in states:
                            run_id = state['run_id']
                            if run_id not in recovered:
                                recovered.add(run_id)
                                if state.get('in_flight'):
                                    state.update(status='blocked', in_flight=False,
                                                 failures=[{'node': 'recovery', 'error': 'Gateway dừng khi đang thực thi. Kiểm tra file/log trước khi tiếp tục.'}])
                                    department.save(project, state)
                                    department.emit(project, data_dir / 'events.sqlite', state, 'blocked', {'reason': state['failures'][0]['error']})
                            if stop.is_set():
                                break
                            if run_id == selected_run:
                                department.build_worker(project, data_dir / 'events.sqlite', root, stop).invoke({'run_id': run_id})
                    except Exception:
                        logger.exception('Department project failed: %s', item['id'])
            except Exception:
                logger.exception('Department worker iteration failed')
            stop.wait(0.5)

    def department_state(project, run_id):
        state = department.load(project, run_id)
        if state:
            return {**state, 'tasks': [{key: value for key, value in task.items() if key != 'history'} for task in state['tasks']], 'next': []}
        return None

    def snapshot(service: RunRuntime, run_id: str):
        state = service.state(run_id)
        if not state.values:
            raise HTTPException(404, "Run not found")
        return state

    def describe(service: RunRuntime, run_id: str):
        project = project_for_run(run_id)
        current = department_state(project, run_id)
        if current:
            return current
        state = snapshot(service, run_id)
        gates = [{"id": item.id, **item.value} for task in state.tasks for item in task.interrupts]
        failures = [{"node": task.name, "error": str(task.error)} for task in state.tasks if task.error]
        return {
            **{key: value for key, value in state.values.items() if key != "project_context"},
            "status": "failed" if failures else state.values.get("status", "running"),
            "next": list(state.next),
            "pending_approvals": gates,
            "failures": failures,
        }

    def decide(run_id: str, action: str, gate_id: str | None, **details):
        project = project_for_run(run_id)
        state = department.load(project, run_id)
        if state:
            try:
                department.decide(project, data_dir / 'events.sqlite', state, action, gate_id, **details)
            except ValueError as error:
                raise HTTPException(409, str(error)) from error
            department.emit(project, data_dir / 'events.sqlite', state, 'run.updated', {'status': state['status']})
            return department_state(project, run_id)
        with execute(run_id, project) as service:
            current = describe(service, run_id)
            gates = current["pending_approvals"]
            if len(gates) != 1 or (gate_id is not None and gates[0]["id"] != gate_id):
                raise HTTPException(409, "No matching approval is pending")
            if action in {"approve", "continue"}:
                context = service.state(run_id).values.get("project_context") or {}
                if context.get("baseline_run_id") != project_baseline(project)[0]:
                    raise HTTPException(409, "Project đã có bản QA mới. Cập nhật yêu cầu để thiết kế lại trên bản mới.")
            try:
                service.decide(run_id, {"action": action, **details})
            except ValidationError as error:
                raise HTTPException(409, "Decision is not valid for the current gate") from error
            except DuplicateDecision as error:
                raise HTTPException(409, "Decision was already recorded") from error
            return describe(service, run_id)

    def start_run(run_id: str, body: RunRequest):
        project = find_project(body.project_id)
        department.create(project, data_dir / 'events.sqlite', run_id, body.request, body.max_calls, body.calls_per_minute)
        return department_state(project, run_id)

    @api.get("/runs")
    def list_runs(limit: Annotated[int, Query(ge=1, le=200)] = 100, project_id: str | None = None):
        with event_store() as events:
            return events.dashboard_runs(limit, project_id)

    def stream_batch(after: int):
        with event_store() as events:
            return events.dashboard_changes(after)

    @api.get("/dashboard/stream")
    async def dashboard_stream(request: Request, after: Annotated[int, Query(ge=0)] = 0):
        async def stream():
            cursor = after
            idle = 0
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 30
            while loop.time() < deadline and not await request.is_disconnected():
                changes = await run_in_threadpool(stream_batch, cursor)
                if changes:
                    cursor = changes[-1]["id"]
                    runs = list(dict.fromkeys(change["run_id"] for change in changes))
                    yield f"id: {cursor}\ndata: {json.dumps(runs)}\n\n"
                    idle = 0
                else:
                    if idle % 15 == 0:
                        yield ": heartbeat\n\n"
                    idle += 1
                    await asyncio.sleep(1)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
        })

    @api.post("/runs", status_code=201)
    def start(body: RunRequest):
        return start_run(uuid4().hex, body)

    @api.get("/runs/{run_id}")
    def status(run_id: str):
        with runtime(project_for_run(run_id)) as service:
            return describe(service, run_id)

    @api.get("/runs/{run_id}/events")
    def replay(run_id: str, after: Annotated[int, Query(ge=0)] = 0):
        project_for_run(run_id)
        with event_store() as store:
            events = store.replay(run_id, after)
        if not events:
            status(run_id)
        return events

    @api.post("/runs/{run_id}/approve")
    def approve(run_id: str, body: Feedback):
        return decide(run_id, "approve", body.gate_id, feedback=body.feedback)

    @api.post("/runs/{run_id}/reject")
    def reject(run_id: str, body: Feedback):
        return decide(run_id, "reject", body.gate_id, feedback=body.feedback)

    @api.post("/runs/{run_id}/spec")
    def change_spec(run_id: str, body: SpecRequest):
        return decide(run_id, "change_spec", body.gate_id, request=body.request)

    @api.post("/runs/{run_id}/continue")
    def continue_after_escalation(run_id: str, body: Feedback):
        return decide(run_id, "continue", body.gate_id, feedback=body.feedback)

    @api.post("/runs/{run_id}/pause")
    def pause_run(run_id: str):
        return decide(run_id, 'pause', None)

    @api.post("/runs/{run_id}/stop")
    def stop_after_escalation(run_id: str, body: Feedback):
        return decide(run_id, "stop", body.gate_id, feedback=body.feedback)

    def submit_telegram_run(request: str, project_id: str):
        body = RunRequest(request=request, project_id=project_id)
        key = body.project_id + ":" + body.request
        run_id = uuid4().hex
        with event_store() as events:
            existing = events.reserve_telegram_run(key, run_id)
        if existing != run_id:
            return existing, False
        try:
            start_run(run_id, body)
        except HTTPException:
            with event_store() as events:
                events.release_telegram_run(key, run_id)
            raise
        return run_id, True

    def list_pm_runs(step: PMListRuns):
        query = step.query.strip().casefold()
        with event_store() as events:
            runs = events.dashboard_runs(100, step.project_id)["runs"]
        return [
            {**run, "request": (run["request"] or "")[:300]}
            for run in runs
            if (step.scope == "recent" or run["status"] in {"running", "planning", "paused", "blocked", "waiting_approval"})
            and (query in (run["request"] or "").casefold() or query in run["run_id"])
        ][:10]

    def get_pm_run(step: PMGetRun):
        with runtime(project_for_run(step.run_id)) as service:
            try:
                state = describe(service, step.run_id)
            except HTTPException as error:
                if error.status_code != 404:
                    raise
                return {"error": "No checkpoint for this run yet or run ID not found"}
            return summarize_run(state, service.events.recent_pm_events(step.run_id))

    def generate_pm_answer(question: str):
        deadline = monotonic() + PM_TIMEOUT
        with event_store() as events:
            history = events.pm_history(owner)
        team = ModelTeam.from_config(root)
        projects = [{"id": project["id"], "name": project["name"]} for project in list_projects()]
        tool_results = []
        for _ in range(4):
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("Supervisor model deadline exceeded")
            step = team.pm_step(question, history, tool_results, remaining, projects)
            if isinstance(step, (PMAnswer, PMCreateRun, PMChangeRun, PMControlRun)):
                return step
            if isinstance(step, PMListRuns):
                result = list_pm_runs(step)
            elif isinstance(step, PMGetProject):
                project = find_project(step.project_id)
                result = {'project': project.describe(), 'runs': [department_state(project, state['run_id']) for state in department.all_runs(project)[-8:]]}
            else:
                result = get_pm_run(step)
            tool_results.append({"call": step.model_dump(), "result": result})
        raise RuntimeError("Supervisor did not finish within four steps")

    def ask_pm(question: str):
        question = question[:20000]
        if not pm_slots.acquire(blocking=False):
            return "[PM] Mình đang xử lý các câu hỏi trước. Bạn thử lại sau nhé."
        try:
            future = pm_pool.submit(generate_pm_answer, question)
        except RuntimeError:
            pm_slots.release()
            return "[PM] Gateway đang dừng. Bạn thử lại sau nhé."
        future.add_done_callback(lambda _: pm_slots.release())
        try:
            outcome = future.result(timeout=PM_TIMEOUT + 2)
        except TimeoutError:
            return "[PM] Model đang phản hồi chậm. Bạn thử lại sau nhé."
        except Exception:
            logger.exception("Supervisor chat failed")
            return "[PM] Mình chưa trả lời được lúc này. Bạn thử lại sau nhé."

        if isinstance(outcome, PMCreateRun):
            run_id, created = submit_telegram_run(outcome.request, outcome.project_id)
            answer = f"Đã tạo run {run_id}." if created else f"Yêu cầu này đang có run {run_id}."
        elif isinstance(outcome, PMChangeRun):
            decide(outcome.run_id, 'change_spec', None, request=outcome.request)
            answer = f"Đã ghi nhận thay đổi cho run {outcome.run_id}. PM sẽ lập lại kế hoạch để bạn duyệt sau bước đang thực hiện."
        elif isinstance(outcome, PMControlRun):
            decide(outcome.run_id, outcome.operation, None)
            answer = f"Đã nhận {outcome.operation} cho run {outcome.run_id}."
        else:
            answer = outcome.answer

        try:
            with event_store() as events:
                events.record_pm_turn(owner, question, answer)
        except sqlite3.Error:
            logger.exception("Could not save Supervisor chat")
        return f"[PM] {answer}"

    @api.post("/integrations/telegram/update")
    def telegram_update(update: dict):
        command = incoming(update, owner)
        if command is None:
            return {"text": ""}
        update_id = update.get("update_id")
        if type(update_id) is int:
            with event_store() as events:
                if not events.claim_telegram_update(owner, update_id):
                    return {"text": ""}
        action, target, argument = command
        try:
            if isinstance(target, int):
                with event_store() as events:
                    event = events.get(target)
                if not event or event["type"] != "approval.requested" or not event["payload"].get("gate_id"):
                    raise HTTPException(409, "Cổng duyệt đã hết hiệu lực")
                run_id = event["run_id"]
                gate_id = event["payload"]["gate_id"]
                if action == "reject" and event["payload"]["gate"] in {"design", "plan"}:
                    current = status(run_id)
                    if not any(gate["id"] == gate_id for gate in current["pending_approvals"]):
                        return {"text": ""}
                    project = project_for_run(run_id)
                    with event_store() as events:
                        events.attach_project(project.storage / "events.sqlite")
                        first = events.append(
                            run_id, current["spec_version"], "approval.feedback_requested",
                            {"gate_id": gate_id},
                        )
                    text = revision_help(run_id) if first else ""
                else:
                    decide(run_id, action, gate_id)
                    text = ""
            elif action == "run" and argument:
                parts = argument.split(maxsplit=1)
                if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{32}", parts[0]):
                    text = "Dùng /run PROJECT_ID yêu cầu. Gửi /projects để xem danh sách."
                else:
                    run_id, created = submit_telegram_run(parts[1], parts[0])
                    text = f"Đã nhận yêu cầu. PM đang lập kế hoạch cho run {run_id}." if created else f"Yêu cầu này đang có run {run_id}."
            elif action == "projects":
                text = "\n".join(f"{p['name']} · {p['id']}" for p in list_projects()[:20]) or "Chưa có project. Tạo project trong dashboard."
            elif action == "status" and target:
                text = run_status(status(target))
            elif action == "pm":
                enabled = argument.casefold() != "off"
                with event_store() as events:
                    events.set_pm_open(owner, enabled)
                if not enabled:
                    text = "Đã thoát chat với PM. Dùng /pm để mở lại."
                elif argument:
                    text = ask_pm(argument)
                else:
                    text = "[PM] Mình đây. Bạn muốn bàn việc gì?"
            elif action == "text":
                with event_store() as events:
                    enabled = events.pm_is_open(owner)
                text = ask_pm(argument) if enabled else HELP
            elif action == "log" and target:
                text = replay_text(replay(target, 0))
            elif action == "spec" and target and argument:
                decide(target, "change_spec", None, request=argument)
                text = ""
            elif action == "reject" and target and not argument.strip():
                text = revision_help(target)
            elif action in {"approve", "reject", "continue", "stop"} and target:
                decide(target, action, None, feedback=argument)
                text = ""
            else:
                text = HELP
        except HTTPException as error:
            text = "" if isinstance(target, int) and error.status_code == 409 else (
                f"Gateway {error.status_code}: {error.detail}"
            )
        return {"chat_id": owner, "text": text}

    @api.post("/integrations/n8n/verify")
    def verify_notification(delivery: SignedEvent):
        expected = signature(webhook_secret, delivery.payload)
        if not webhook_secret or not hmac.compare_digest(delivery.signature.encode(), expected.encode()):
            raise HTTPException(401, "Invalid event signature")
        event = json.loads(delivery.payload)
        with event_store() as events:
            if event != events.get(event["id"]) or event["type"] not in NOTIFICATIONS:
                raise HTTPException(404, "Notification not found")
            if events.delivered(event["id"]):
                return {"skip": True}
            if events.obsolete_approval(event):
                events.confirm_delivery(event["id"])
                return {"skip": True}
            if not events.claim_notification(event["id"]):
                raise HTTPException(409, "Notification delivery is already in progress")
        return {"skip": False, **notification(event, owner)}

    @api.post("/integrations/n8n/events/{event_id}/ack")
    def acknowledge_notification(event_id: int):
        with event_store() as events:
            events.confirm_delivery(event_id)
        return {"ok": True}

    app.include_router(api)
    return app
