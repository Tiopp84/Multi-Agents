"""Translate Telegram updates and run events without accessing bot credentials."""

import re


HELP = """Lệnh hỗ trợ:
/projects
/run <project_id> <yêu cầu>
/status <run_id>
/pm [tin nhắn] — mở chat với Supervisor
/pm off — thoát chat với Supervisor
/spec <run_id> <yêu cầu mới>
/approve <run_id>
/reject <run_id> <góp ý>
/log <run_id>
/continue <run_id>
/stop <run_id>"""


def incoming(update: dict, owner: str):
    callback = update.get("callback_query")
    message = callback.get("message", {}) if callback else update.get("message", {})
    chat = message.get("chat", {})
    if not owner or chat.get("type") != "private" or str(chat.get("id")) != owner:
        return None
    if callback:
        if str(callback.get("from", {}).get("id")) != owner:
            return None
        match = re.fullmatch(r"(approve|reject|continue|stop):([0-9]{1,18})", callback.get("data", ""))
        return (match[1], int(match[2]), "") if match else ("help", "", "")
    text = message.get("text", "").strip()
    parts = text.split(maxsplit=2)
    if not parts:
        return None
    action = parts[0].split("@", 1)[0].removeprefix("/")
    if not parts[0].startswith("/"):
        return ("text", "", text)
    if action == "pm":
        body = text.split(maxsplit=1)
        return ("pm", "", body[1] if len(body) > 1 else "")
    if action == "run":
        return action, "", " ".join(parts[1:])
    return action, parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else ""


def notification(event: dict, owner: str):
    payload = event["payload"]
    run = event["run_id"]
    title = f"Run {run} · v{event['spec_version']}"
    kind = event["type"]
    if kind == "approval.requested":
        design = payload.get("design")
        if design:
            summary = "\n".join(
                f"{entry['signature']} — {entry['behavior']}" for entry in design.get("public_api", [])
            )
        elif payload.get('plan'):
            plan = payload['plan']
            summary = plan['summary'] + '\n' + '\n'.join(f"{t['id']} · {t['role']} · {t['title']} · ~{t['estimate_minutes']} phút" for t in plan['tasks'])
        else:
            summary = payload.get("result", {}).get("stdout", "")
        text = f"{title}\nCần quyết định: {payload['gate']}\n{summary}"
        if payload.get("design_feedback"):
            text = f"{title}\nThiết kế cập nhật theo góp ý: {payload['design_feedback'][:500]}\n{summary}"
        gate = payload["gate"]
    elif kind == 'task.done':
        text = f"{title}\n{event.get('agent')}: {payload.get('node', '')}\n{payload.get('summary', '')}"
        gate = ''
    elif kind == "run.finished":
        report = payload.get("report") or {}
        result = report.get("test_result") or {}
        if payload.get("supervisor_reply"):
            text = f"{title}\n[PM] {payload['supervisor_reply']}"
        else:
            text = f"{title}\nKết thúc: {payload['status']}\n{report.get('summary', '')}\n{result.get('stdout', '')}"
        gate = ""
    else:
        text = f"{title}\n{kind}: {payload.get('reason', '')}"
        gate = ""
    return {"chat_id": owner, "event_id": event["id"], "gate": gate, "text": text[:3800]}


def revision_help(run_id: str):
    return (
        "Bạn muốn sửa phần nào của kế hoạch? Gửi góp ý theo mẫu:\n"
        f"/reject {run_id} <góp ý>\n"
        f"Ví dụ: /reject {run_id} Thêm ca kiểm thử khi một số bằng 0.\n"
        f"Nếu đổi ý, gửi /approve {run_id}."
    )


def run_status(state):
    return f"Run {state['run_id']} · v{state['spec_version']} · {state['status']}"


def replay_text(events):
    return "\n".join(
        f"#{event['id']} {event['type']} {event['agent'] or ''} {event['payload'].get('node', '')}"
        for event in events[-20:]
    ) or "Chưa có sự kiện."
