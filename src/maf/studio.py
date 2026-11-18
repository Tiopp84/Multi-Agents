"""Studio entry point into the same durable department used by the Gateway."""

import json
import os
from pathlib import Path
from typing import NotRequired, TypedDict
from urllib.parse import quote
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from langgraph.graph import END, START, StateGraph


class StudioState(TypedDict):
    project_id: str
    request: NotRequired[str]
    run_id: NotRequired[str]
    status: NotRequired[str]
    phase: NotRequired[str]
    tasks: NotRequired[list[dict]]
    plan: NotRequired[dict | None]
    pending_approvals: NotRequired[list[dict]]
    failures: NotRequired[list[dict]]


def dispatch(state: StudioState):
    load_dotenv(Path(__file__).resolve().parents[2] / '.env')
    base = os.environ.get('MAF_GATEWAY_URL', 'http://127.0.0.1:8000').rstrip('/')
    headers = {'Authorization': 'Bearer ' + os.environ['MAF_GATEWAY_TOKEN'], 'Content-Type': 'application/json'}
    if state.get('run_id'):
        request = Request(base + '/runs/' + quote(state['run_id'], safe=''), headers=headers)
    else:
        if not state.get('request', '').strip():
            raise ValueError('Provide project_id and request, or project_id and an existing run_id.')
        body = json.dumps({'project_id': state['project_id'], 'request': state['request']}).encode()
        request = Request(base + '/runs', data=body, headers=headers)
    with urlopen(request, timeout=10) as response:
        result = json.load(response)
    if result['project_id'] != state['project_id']:
        raise ValueError('Run belongs to a different project.')
    return {key: result[key] for key in StudioState.__annotations__ if key in result}


builder = StateGraph(StudioState)
builder.add_node('supervisor_dispatch', dispatch)
builder.add_edge(START, 'supervisor_dispatch')
builder.add_edge('supervisor_dispatch', END)
graph = builder.compile()
