import {renderDepartment} from "./department.js";
import {createOffice} from "./office.js";
import {setupAgentConfig} from "./agents.js";

const get = id => document.getElementById(id);
const el = (tag, text = '', className = '') => {
  const node = document.createElement(tag);
  node.className = className;
  node.textContent = text;
  return node;
};
const states = {planning: 'PM đang lập kế hoạch', paused: 'Tạm dừng', blocked: 'Cần xử lý', running: 'Đang chạy', waiting_approval: 'Chờ duyệt', done: 'Hoàn tất', failed: 'Lỗi', cancelled: 'Đã dừng', responded: 'PM đã trả lời', needs_input: 'Cần làm rõ'};
const agents = {supervisor: 'Supervisor', architect: 'BA', developer: 'Developer', qa: 'QA'};
const nodes = {
  pm_plan: 'Lập kế hoạch', architect_design: 'Thiết kế', gate_design: 'Duyệt thiết kế',
  dev_implement: 'Viết mã', qa_write_tests: 'Soạn test', qa_run: 'Chạy pytest',
  dev_fix: 'Sửa lỗi', pm_report: 'Báo cáo', gate_escalate: 'Xử lý lỗi QA', prepare_escalation: 'Chuẩn bị quyết định QA',
};
const eventNames = {
  'run.created': 'Tạo run', 'run.finished': 'Kết thúc run', 'task.started': 'Bắt đầu',
  'task.done': 'Hoàn thành', 'test.result': 'Kết quả pytest', 'approval.requested': 'Yêu cầu duyệt',
  'approval.resolved': 'Nhận quyết định', 'approval.feedback_requested': 'Yêu cầu góp ý',
  'agent.message': 'Trao đổi', 'tool.started': 'Gọi công cụ', 'tool.finished': 'Kết quả công cụ', 'command.started': 'Chạy lệnh', 'command.output': 'Output trực tiếp', 'command.finished': 'Lệnh kết thúc', 'llm.call': 'Gọi model', 'run.updated': 'Cập nhật', 'spec.change_requested': 'Yêu cầu thay đổi', 'run.paused': 'Tạm dừng', 'artifact.created': 'Tạo đầu ra', 'spec.updated': 'Đổi yêu cầu', error: 'Lỗi', blocked: 'Tạm dừng',
};
const time = new Intl.DateTimeFormat('vi-VN', {dateStyle: 'short', timeStyle: 'medium'});
const office = createOffice(nodes, time);
let runs = [];
let projects = [];
let selected;
let session;
let events = [];
let eventCursor = 0;
let detailRevision = 0;
let commandPending = false;
let runDetail;
const agentConfig = setupAgentConfig(json, () => session);

function connection(text, live = false) {
  const node = get('connection');
  node.textContent = text;
  node.dataset.live = String(live);
}

async function api(path, options = {}, current = session) {
  const response = await fetch(path, {
    ...options,
    signal: current.controller.signal,
    headers: {Authorization: 'Bearer ' + current.token, 'Content-Type': 'application/json'},
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const detail = body.detail?.message ?? body.detail;
    const errors = Array.isArray(detail) ? detail.map(item => item.msg).filter(Boolean).join(' · ') : '';
    const error = new Error(typeof detail === 'string' ? detail : errors || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return response;
}

async function json(path, options, current) {
  return (await api(path, options, current)).json();
}

function badge(status) {
  return el('span', states[status] ?? status, 'badge ' + status);
}

function renderRuns() {
  get('metrics').replaceChildren(...[
    ['Gần nhất', runs.length], ['Đang chạy', runs.filter(r => r.status === 'running').length],
    ['Chờ duyệt', runs.filter(r => r.status === 'waiting_approval').length],
    ['Hoàn tất', runs.filter(r => r.status === 'done').length],
  ].map(([key, value]) => {
    const metric = el('div', '', 'metric');
    metric.append(el('strong', String(value)), el('span', key));
    return metric;
  }));
  const query = get('search').value.trim().toLocaleLowerCase('vi');
  const status = get('filter').value;
  const visible = runs.filter(run => (!status || run.status === status)
    && (run.run_id + ' ' + (run.request ?? '')).toLocaleLowerCase('vi').includes(query));
  get('run-count').textContent = `${visible.length}/${runs.length}`;
  get('runs').replaceChildren(...visible.map(run => {
    const button = el('button', '', 'run');
    button.type = 'button';
    button.setAttribute('aria-pressed', String(run.run_id === selected));
    const subtitle = [projectName(run.project_id), run.run_id.slice(0, 8), `v${run.spec_version}`, agents[run.agent], time.format(new Date(run.updated_at))].filter(Boolean);
    button.append(el('strong', run.request || run.run_id), badge(run.status), el('small', subtitle.join(' · '), 'mono'));
    button.onclick = () => select(run.run_id);
    return button;
  }));
  if (!visible.length) get('runs').append(el('p', 'Không có run phù hợp.', 'empty muted'));
}

function expand(label, content) {
  const wrapper = el('details');
  wrapper.append(el('summary', label), el('pre', content));
  return wrapper;
}

function renderDetail(run) {
  const top = el('div', '', 'detail-top');
  top.append(el('h2', run.request), badge(run.status));
  const finishedTasks = new Set(events.filter(event =>
    ['task.done', 'error', 'approval.requested'].includes(event.type)).map(event => event.task_id));
  const active = events.findLast(event => event.type === 'task.started' && !finishedTasks.has(event.task_id));
  const current = run.status === 'running'
    ? active?.payload.node ?? run.next?.[0]
    : run.pending_approvals?.[0]?.gate === 'design' ? 'gate_design'
      : run.pending_approvals?.length ? 'gate_escalate' : undefined;
  const lastMeta = current ? nodes[current] ?? current : '';
  const detail = get('details');
  const feedbackValue = detail.querySelector('textarea')?.value ?? '';
  const specValue = detail.querySelector('.spec-request')?.value ?? '';
  const previousGate = detail.querySelector('.approval')?.dataset.gate;
  detail.replaceChildren(top, el('p', [projectName(run.project_id), run.run_id, `v${run.spec_version}`, lastMeta].filter(Boolean).join(' · '), 'meta mono muted'));
  if (run.phase) {
    const oldDraft = get('details').dataset.changeDraft ?? '';
    detail.append(renderDepartment(run, runCommand, commandPending));
    const editor = detail.querySelector('.department-change');
    if (editor) {
      editor.value = oldDraft;
      editor.oninput = () => { detail.dataset.changeDraft = editor.value; };
    }
    if (run.report?.summary) detail.append(el('p', run.report.summary, 'meta'));
    if (run.supervisor_reply) detail.append(el('p', run.supervisor_reply, 'meta'));
    return;
  }
  if (run.supervisor_reply) detail.append(el('p', run.supervisor_reply, 'meta'));
  if (run.report?.summary) detail.append(el('p', run.report.summary, 'meta'));
  if (run.result) {
    const qa = el('div', '', 'qa');
    qa.append(el('h3', run.result.exit_code === 0 ? 'Pytest đạt' : `Pytest · mã thoát ${run.result.exit_code ?? '—'}`));
    if (run.result.workspace) qa.append(el('p', run.result.workspace, 'meta mono'));
    if (run.result.stdout) qa.append(expand('Output pytest', run.result.stdout));
    detail.append(qa);
  }
  const artifacts = [...(run.code?.files ?? []), ...(run.tests?.files ?? [])];
  if (artifacts.length) {
    const files = el('div', '', 'files');
    files.append(el('h3', 'File do agent tạo'));
    for (const file of artifacts) files.append(expand(file.path, file.content));
    detail.append(files);
  }
  if (run.failures?.length) detail.append(expand('Chi tiết lỗi', JSON.stringify(run.failures, null, 2)));
  const gate = run.pending_approvals?.[0];
  if (gate) detail.append(approval(run, gate, previousGate === gate.id ? feedbackValue : '',
    previousGate === gate.id ? specValue : ''));
}

function approval(run, gate, feedbackValue, specValue) {
  const runId = run.run_id;
  const box = el('div', '', 'approval');
  box.dataset.gate = gate.id;
  const isDesign = gate.gate === 'design';
  box.append(el('h3', isDesign ? 'Chờ duyệt thiết kế' : 'Chờ quyết định QA'));
  if (gate.design) box.append(expand('Xem thiết kế', JSON.stringify(gate.design, null, 2)));
  const feedback = el('textarea');
  feedback.rows = 2;
  feedback.placeholder = 'Góp ý khi yêu cầu sửa';
  feedback.setAttribute('aria-label', 'Góp ý cho Architect');
  feedback.value = feedbackValue;
  if (isDesign) box.append(feedback);
  const actions = el('div', '', 'dialog-actions');
  for (const [action, label, main] of isDesign
    ? [['approve', 'Duyệt', true], ['reject', 'Yêu cầu sửa', false]]
    : [['continue', 'Tiếp tục', true], ['stop', 'Dừng', false]]) {
    const button = el('button', label, main ? 'primary' : 'secondary');
    button.type = 'button';
    button.disabled = commandPending;
    button.onclick = () => {
      if (action === 'reject' && !feedback.value.trim()) {
        get('notice').textContent = 'Nhập góp ý cho Architect trước khi yêu cầu sửa.';
        feedback.focus();
        return;
      }
      runCommand(`/runs/${runId}/${action}`, {gate_id: gate.id, feedback: feedback.value});
    };
    actions.append(button);
  }
  box.append(actions);
  if (run.project_id) {
    const request = el('textarea', '', 'spec-request');
    request.rows = 3;
    request.value = specValue;
    request.setAttribute('aria-label', 'Yêu cầu cập nhật');
    const spec = el('details');
    spec.open = Boolean(specValue);
    const update = el('button', 'Lập lại kế hoạch', 'secondary');
    update.type = 'button';
    update.disabled = commandPending;
    update.onclick = () => {
      if (!request.value.trim()) return request.focus();
      runCommand(`/runs/${runId}/spec`, {gate_id: gate.id, request: request.value.trim()});
    };
    spec.append(el('summary', 'Cập nhật yêu cầu'), request, update);
    box.append(spec);
  }
  return box;
}

function appendEvents(newEvents) {
  for (const event of newEvents) {
    const payload = event.payload;
    const label = event.type === 'handoff'
      ? `${agents[payload.from] ?? payload.from} → ${agents[payload.to] ?? payload.to}`
      : [eventNames[event.type] ?? event.type, agents[event.agent], nodes[payload.node] ?? payload.node].filter(Boolean).join(' · ');
    const preview = event.type === 'agent.message' ? `${label} → ${agents[payload.to] ?? 'owner'} · ${payload.text}`
      : event.type === 'tool.started' ? `${label} · ${payload.action} ${payload.path ?? payload.command ?? ''}` : label;
    const row = expand(preview, JSON.stringify(payload, null, 2));
    row.className = 'event';
    row.querySelector('summary').append(el('time', `#${event.id} · ${time.format(new Date(event.ts))}`, 'mono'));
    get('timeline').prepend(row);
  }
  get('event-count').textContent = `${events.length} sự kiện`;
}

async function refreshDetail(current = session) {
  const runId = selected;
  if (!runId || !current) return;
  const revision = ++detailRevision;
  const route = '/runs/' + encodeURIComponent(runId);
  try {
    const [run, updates] = await Promise.all([
      json(route, {}, current), json(route + '/events?after=' + eventCursor, {}, current),
    ]);
    if (revision !== detailRevision || session !== current) return;
    events.push(...updates);
    eventCursor = updates.at(-1)?.id ?? eventCursor;
    runDetail = run;
    renderDetail(run);
    office.update(run, events);
    appendEvents(updates);
    get('timeline-section').hidden = false;
  } catch (error) {
    if (revision === detailRevision && session === current) get('notice').textContent = error.message;
  }
}

function select(runId) {
  if (selected === runId) return;
  selected = runId;
  delete get('details').dataset.changeDraft;
  events = [];
  office.update(undefined, events);
  eventCursor = 0;
  runDetail = undefined;
  get('timeline').replaceChildren();
  get('timeline-section').hidden = true;
  get('details').replaceChildren(el('p', runId ? 'Đang tải run…' : 'Chọn một run để xem thông tin.', 'muted'));
  get('notice').textContent = '';
  renderRuns();
  refreshDetail();
}

async function refreshRuns(current = session) {
  const filter = get('project-filter').value;
  const query = filter === 'all' ? '' : '?project_id=' + encodeURIComponent(filter);
  const result = await json('/runs' + query, {}, current);
  if (session !== current || filter !== get('project-filter').value) return result;
  runs = result.runs;
  if (!runs.some(run => run.run_id === selected) && (selected || runs.length)) select(runs[0]?.run_id);
  else renderRuns();
  return result;
}

function projectName(id) {
  return id ? projects.find(project => project.id === id)?.name ?? id : '';
}

function setProjects(items, filter = get('project-filter').value) {
  projects = items;
  const choices = items.map(project => new Option(project.name, project.id));
  get('project-filter').replaceChildren(new Option('Tất cả project', 'all'), ...choices);
  get('run-project').replaceChildren(new Option('Chọn project', ''), ...choices.map(option => option.cloneNode(true)));
  get('project-filter').value = ['all', ...items.map(project => project.id)].includes(filter) ? filter : 'all';
  showProjectLocation();
}

function showProjectLocation() {
  const project = projects.find(item => item.id === get('project-filter').value);
  get('project-location').textContent = project
    ? project.workspace + ' · ' + project.id
    : projects.length ? '' : 'Tạo project để chọn workspace cho run mới.';
}

async function listen(current, initialCursor) {
  let cursor = initialCursor;
  while (session === current) {
    try {
      const response = await api('/dashboard/stream?after=' + cursor, {}, current);
      connection('Cập nhật trực tiếp', true);
      const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
      let buffer = '';
      while (session === current) {
        const {done, value} = await reader.read();
        if (done) break;
        buffer += value;
        const frames = buffer.split('\n\n');
        buffer = frames.pop();
        const changed = new Set();
        for (const frame of frames) {
          const match = /^id: (\d+)\ndata: (.*)$/m.exec(frame);
          if (match) {
            for (const id of JSON.parse(match[2])) changed.add(id);
            cursor = Number(match[1]);
          }
        }
        if (changed.size) {
          await refreshRuns(current);
          if (changed.has(selected)) await refreshDetail(current);
        }
      }
      // Rotate the stream normally so it never holds a server shutdown open indefinitely.
      continue;
    } catch (error) {
      if (session !== current) return;
      if (error.status === 401) {
        disconnect();
        get('login-error').textContent = 'Token không còn hợp lệ.';
        return;
      }
    }
    if (session !== current) return;
    connection('Đang kết nối lại…');
    await new Promise(resolve => setTimeout(resolve, 1800));
    try {
      const latest = await refreshRuns(current);
      await refreshDetail(current);
      cursor = latest.cursor;
    } catch {
      // Keep the last usable view while the server is offline.
    }
  }
}

async function runCommand(path, body) {
  if (commandPending || !session) return;
  const current = session;
  commandPending = true;
  get('new-run').disabled = true;
  for (const button of document.querySelectorAll('.approval button')) button.disabled = true;
  get('notice').textContent = 'Gateway đang xử lý yêu cầu…';
  try {
    const result = await json(path, {method: 'POST', body: JSON.stringify(body)}, current);
    if (session !== current) return;
    if (path === '/runs' && get('project-filter').value !== 'all') {
      get('project-filter').value = result.project_id;
      showProjectLocation();
    }
    await refreshRuns(current);
    if (session !== current) return;
    select(result.run_id);
    await refreshDetail(current);
    get('notice').textContent = '';
  } catch (error) {
    if (session === current) {
      get('notice').textContent = error.message;
      await refreshDetail(current);
    }
  } finally {
    commandPending = false;
    get('new-run').disabled = !projects.length;
    if (session === current && runDetail) renderDetail(runDetail);
  }
}

function disconnect() {
  session?.controller.abort();
  agentConfig.close();
  get('project-dialog').close();
  get('run-dialog').close();
  setProjects([], 'all');
  session = undefined;
  selected = undefined;
  events = [];
  office.update(undefined, events);
  runs = [];
  runDetail = undefined;
  get('console').hidden = true;
  get('lock').hidden = true;
  get('login').hidden = false;
  get('token').value = '';
  get('timeline').replaceChildren();
  get('timeline-section').hidden = true;
  get('details').replaceChildren(el('p', 'Chọn một run để xem thông tin.', 'muted'));
  get('notice').textContent = '';
  connection('Chưa kết nối');
  get('token').focus();
}

get('login-form').addEventListener('submit', async event => {
  event.preventDefault();
  const token = get('token').value.trim();
  if (!token) return;
  const current = {token, controller: new AbortController()};
  session?.controller.abort();
  session = current;
  get('login-error').textContent = '';
  try {
    const items = await json('/projects', {}, current);
    if (session !== current) return;
    setProjects(items);
    get('new-run').disabled = !items.length;
    const initial = await refreshRuns(current);
    if (session !== current) return;
    get('token').value = '';
    get('login').hidden = true;
    get('lock').hidden = false;
    get('console').hidden = false;
    listen(current, initial.cursor);
  } catch (error) {
    if (session !== current) return;
    disconnect();
    get('login-error').textContent = error.status === 401 ? 'Token Gateway không hợp lệ.' : error.message;
  }
});

get('lock').onclick = disconnect;
get('search').oninput = renderRuns;
get('filter').onchange = renderRuns;
get('project-filter').onchange = () => {
  showProjectLocation();
  refreshRuns().catch(error => { get('notice').textContent = error.message; });
};
get('new-run').onclick = () => {
  const filter = get('project-filter').value;
  get('run-project').value = projects.some(project => project.id === filter) ? filter : '';
  get('run-dialog').showModal();
};
get('cancel-run').onclick = () => get('run-dialog').close();
get('run-form').addEventListener('submit', event => {
  event.preventDefault();
  const request = get('request').value.trim();
  if (!request) return;
  get('run-dialog').close();
  get('request').value = '';
  runCommand('/runs', {request, project_id: get('run-project').value, max_calls: Number(get('run-calls').value), calls_per_minute: Number(get('run-rate').value)});
});

let selectedWorkspace;
let folderRevision = 0;

get('new-project').onclick = () => {
  folderRevision++;
  selectedWorkspace = undefined;
  get('project-form').reset();
  get('project-workspace').textContent = 'Chưa chọn thư mục';
  get('browse-workspace').disabled = false;
  get('save-project').disabled = true;
  get('project-status').textContent = '';
  get('project-dialog').showModal();
};
get('cancel-project').onclick = () => get('project-dialog').close();
get('browse-workspace').onclick = async () => {
  const revision = ++folderRevision;
  const dialog = get('project-dialog');
  const picker = get('browse-workspace');
  const save = get('save-project');
  const status = get('project-status');
  picker.disabled = save.disabled = true;
  status.textContent = 'Đang chờ chọn thư mục trong cửa sổ hệ điều hành…';
  try {
    const query = selectedWorkspace ? '?path=' + encodeURIComponent(selectedWorkspace) : '';
    const result = await json('/workspace-picker' + query, {method: 'POST'});
    if (revision !== folderRevision || !dialog.open) return;
    if (result.path) {
      selectedWorkspace = result.path;
      get('project-workspace').textContent = selectedWorkspace;
    }
    status.textContent = '';
  } catch (error) {
    if (revision === folderRevision && dialog.open) status.textContent = error.message;
  } finally {
    if (revision === folderRevision && dialog.open) {
      picker.disabled = false;
      save.disabled = !selectedWorkspace;
    }
  }
};
get('project-form').onsubmit = async event => {
  event.preventDefault();
  const current = session;
  const dialog = get('project-dialog');
  const save = get('save-project');
  const status = get('project-status');
  if (!selectedWorkspace || save.disabled) return;
  const revision = folderRevision;
  const picker = get('browse-workspace');
  picker.disabled = save.disabled = true;
  status.textContent = 'Đang tạo project…';
  try {
    const project = await json('/projects', {method: 'POST', body: JSON.stringify({
      name: get('project-name').value,
      workspace: selectedWorkspace,
      folder: get('project-folder').value,
      brief: get('project-brief').value,
    })}, current);
    if (session !== current) return;
    setProjects([...projects, project], project.id);
    get('new-run').disabled = commandPending;
    if (revision === folderRevision) dialog.close();
    await refreshRuns(current);
  } catch (error) {
    if (session !== current) return;
    (dialog.open && revision === folderRevision ? status : get('notice')).textContent = error.message;
  } finally {
    if (revision === folderRevision && dialog.open) {
      picker.disabled = false;
      save.disabled = !selectedWorkspace;
    }
  }
};
