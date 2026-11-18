const roles = {
  supervisor: {label: 'Supervisor', focus: 'Kế hoạch và báo cáo', icon: '📋'},
  architect: {label: 'BA', focus: 'Phân tích và thiết kế', icon: '📐'},
  developer: {label: 'Developer', focus: 'Hiện thực và sửa lỗi', icon: '💻'},
  qa: {label: 'QA', focus: 'Kiểm thử', icon: '🧪'},
};

const statuses = {
  idle: 'Chưa làm việc', running: 'Đang làm', waiting: 'Chờ duyệt',
  done: 'Đã hoàn thành', failed: 'Có lỗi', stopped: 'Đã dừng',
};

function make(tag, text = '', className = '') {
  const node = document.createElement(tag);
  node.className = className;
  node.textContent = text;
  return node;
}

function isRelated(event, role) {
  return event.agent === role
    || (event.type === 'handoff' && [event.payload.from, event.payload.to].includes(role));
}

function roleStatus(role, run, events) {
  const task = events.findLast(event => event.agent === role
    && ['task.started', 'task.done', 'error'].includes(event.type));
  if (run?.phase) {
    if (role === 'supervisor') {
      if (run.phase === 'planning') return ['running', task];
      if (run.phase === 'approval') return ['waiting', task];
      if (run.status === 'blocked') return ['failed', task];
      return [run.status === 'done' ? 'done' : run.status === 'cancelled' ? 'stopped' : 'idle', task];
    }
    const assigned = run.tasks.filter(item => item.role === role);
    if (assigned.some(item => item.status === 'blocked')) return ['failed', task];
    if (assigned.some(item => item.status === 'working')) return [run.status === 'paused' ? 'waiting' : 'running', task];
    if (assigned.length && assigned.every(item => item.status === 'done')) return ['done', task];
    return ['idle', task];
  }
  const gate = run?.pending_approvals?.[0]?.gate;
  if ((role === 'architect' && gate === 'design') || (role === 'qa' && gate === 'escalation')) {
    return ['waiting', task];
  }
  if (!task) return ['idle', null];
  if (task.type === 'error') return ['failed', task];
  if (task.type === 'task.started') {
    if (run?.status === 'running') return ['running', task];
    if (run?.status === 'failed') return ['failed', task];
    if (run?.status === 'cancelled') return ['stopped', task];
  }
  return ['done', task];
}

export function createOffice(labels, time) {
  const stations = document.getElementById('stations');
  const handoff = document.getElementById('handoff');
  const detail = document.getElementById('agent-detail');
  const scope = document.getElementById('office-run');
  const buttons = new Map();
  let selected;
  let selectedRunId;

  for (const [role, info] of Object.entries(roles)) {
    const button = make('button', '', 'station');
    button.type = 'button';
    button.dataset.role = role;
    const status = make('span', '', 'station-status');
    const scene = make('span', '', 'station-scene');
    scene.setAttribute('aria-hidden', 'true');
    scene.append(make('span', info.icon, 'station-avatar'), make('span', '', 'station-desk'));
    button.append(status, scene, make('strong', info.label), make('small', info.focus));
    buttons.set(role, {button, status});
    stations.append(button);
  }

  function update(run, events) {
    if (selectedRunId !== run?.run_id) {
      selected = undefined;
      selectedRunId = run?.run_id;
    }
    scope.textContent = run ? `${run.run_id.slice(0, 8)} · v${run.spec_version}` : 'Chưa chọn run';
    const transfer = events.findLast(event => event.type === 'handoff');
    for (const [role, {button, status}] of buttons) {
      const [state, task] = roleStatus(role, run, events);
      button.dataset.state = state;
      button.dataset.transfer = transfer?.payload.from === role ? 'from' : transfer?.payload.to === role ? 'to' : '';
      button.setAttribute('aria-pressed', String(selected === role));
      status.textContent = statuses[state];
      button.title = task?.payload.node ? labels[task.payload.node] ?? task.payload.node : roles[role].focus;
      button.onclick = () => {
        selected = role;
        update(run, events);
      };
    }
    handoff.replaceChildren(make('span', 'Bàn giao gần nhất', 'handoff-label'));
    if (transfer) {
      const {from, to, node} = transfer.payload;
      handoff.append(
        make('strong', roles[from]?.label ?? from),
        make('span', '⟶', 'handoff-arrow'),
        make('strong', roles[to]?.label ?? to),
        make('span', labels[node] ?? node ?? '', 'muted handoff-task'),
        make('time', time.format(new Date(transfer.ts)), 'mono muted'),
      );
    } else {
      handoff.append(make('span', 'Chưa ghi nhận bàn giao.', 'muted'));
    }
    if (!selected) {
      detail.replaceChildren(make('h3', 'Công việc của agent'),
        make('p', 'Chọn một chỗ ngồi để xem việc đang làm và lịch sử.', 'muted'));
      return;
    }
    const info = roles[selected];
    const [state, task] = roleStatus(selected, run, events);
    detail.replaceChildren(make('h3', info.label),
      make('p', [statuses[state], labels[task?.payload.node] ?? task?.payload.node].filter(Boolean).join(' · '), 'agent-current'));
    const history = events.filter(event => isRelated(event, selected)).slice(-8).reverse();
    if (!history.length) {
      detail.append(make('p', 'Chưa có hoạt động trong run này.', 'muted'));
      return;
    }
    const list = make('ol', '', 'agent-history');
    for (const event of history) {
      const {payload} = event;
      let label;
      if (event.type === 'handoff') {
        label = `${roles[payload.from]?.label ?? payload.from} → ${roles[payload.to]?.label ?? payload.to}`;
      } else {
        const kind = {
          'task.started': 'Bắt đầu', 'task.done': 'Hoàn thành', 'artifact.created': 'Tạo đầu ra',
          'agent.message': 'Trao đổi', 'tool.started': 'Gọi công cụ', 'tool.finished': 'Kết quả công cụ', 'command.started': 'Chạy lệnh', 'command.output': 'Output trực tiếp', 'command.finished': 'Lệnh kết thúc', 'llm.call': 'Gọi model', 'blocked': 'Cần xử lý', 'test.result': 'Kết quả pytest', error: 'Lỗi',
        }[event.type] ?? event.type;
        label = [kind, labels[payload.node] ?? payload.node ?? payload.kind].filter(Boolean).join(' · ');
      }
      const item = make('li');
      const entry = make('details');
      entry.append(make('summary', label), make('pre', JSON.stringify(payload, null, 2)));
      item.append(entry, make('time', time.format(new Date(event.ts)), 'mono muted'));
      list.append(item);
    }
    detail.append(list);
  }

  update(undefined, []);
  return {update};
}
