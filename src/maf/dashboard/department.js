const names = {architect: 'BA', developer: 'Dev', qa: 'QA'};
const statuses = {pending: 'Chờ', working: 'Đang làm', done: 'Xong', blocked: 'Cần xử lý'};
const node = (tag, text = '', className = '') => {
  const element = document.createElement(tag);
  element.textContent = text;
  element.className = className;
  return element;
};

export function renderDepartment(run, command, busy) {
  const section = node('section', '', 'department');
  section.append(node('p', `Lượt gọi model ${run.calls}/${run.max_calls} · tối đa ${run.calls_per_minute} lượt/phút · từng task lần lượt`, 'muted'));
  const buttons = node('div', '', 'dialog-actions');
  function action(label, route, body = {}) {
    const button = node('button', label, 'secondary');
    button.type = 'button';
    button.disabled = busy;
    button.onclick = () => command(`/runs/${run.run_id}/${route}`, body);
    buttons.append(button);
  }
  if (run.phase === 'approval') {
    const gate = run.pending_approvals[0];
    action('Duyệt kế hoạch', 'approve', {gate_id: gate.id});
  }
  if (['running', 'planning'].includes(run.status)) action('Tạm dừng sau bước hiện tại', 'pause');
  if (['paused', 'blocked'].includes(run.status) && !run.pending_request) action('Tiếp tục và cấp thêm lượt', 'continue');
  if (!['done', 'cancelled'].includes(run.status)) action('Dừng', 'stop');
  section.append(buttons);
  if (run.pause_requested) section.append(node('p', 'Đã nhận yêu cầu tạm dừng hoặc thay đổi. Đang chờ bước hiện tại kết thúc.', 'muted'));
  if (run.plan) {
    section.append(node('h3', 'Kế hoạch dự án'), node('p', run.plan.summary));
    const tasks = run.tasks;
    const ordered = [];
    const done = new Set();
    while (ordered.length < tasks.length) {
      const next = tasks.find(task => !done.has(task.id) && task.depends_on.every(id => done.has(id)));
      if (!next) break;
      ordered.push(next);
      done.add(next.id);
    }
    const durations = ordered.map(task => Math.max(task.estimate_minutes, Math.ceil(task.max_calls / run.calls_per_minute)));
    const total = durations.reduce((sum, duration) => sum + duration, 0) || 1;
    section.append(node('p', `Gantt ước lượng · tổng ${total} phút theo thứ tự thực thi. Thời gian thực tế phụ thuộc model, lệnh và hạn mức.`, 'muted'));
    const gantt = node('div', '', 'gantt');
    let elapsed = 0;
    ordered.forEach((task, index) => {
      const row = node('details', '', 'gantt-task');
      const summary = node('summary');
      const title = node('span', `${task.id} · ${names[task.role]} · ${task.title}`);
      const track = node('span', '', 'gantt-track');
      const bar = node('span', '', 'gantt-bar ' + task.status);
      bar.style.marginLeft = `${elapsed / total * 100}%`;
      bar.style.width = `${durations[index] / total * 100}%`;
      track.append(bar);
      summary.append(title, node('small', `${statuses[task.status]} · ~${durations[index]} phút · ${task.calls}/${task.max_calls} lượt`), track);
      row.append(summary, node('p', task.instructions), node('p', 'Phụ thuộc · ' + (task.depends_on.join(', ') || 'Có thể bắt đầu')));
      const criteria = node('ul');
      task.acceptance.forEach(text => criteria.append(node('li', text)));
      row.append(criteria);
      if (task.summary) row.append(node('p', task.summary));
      if (task.started_at) row.append(node('small', 'Bắt đầu ' + new Date(task.started_at * 1000).toLocaleString('vi-VN')));
      gantt.append(row);
      elapsed += durations[index];
    });
    section.append(gantt);
  }
  if (!['done', 'cancelled'].includes(run.status)) {
    const change = node('details');
    const input = node('textarea');
    input.className = 'department-change';
    input.rows = 3;
    input.placeholder = 'Thay đổi yêu cầu hoặc góp ý cho PM';
    input.setAttribute('aria-label', 'Thay đổi yêu cầu cho PM');
    const submit = node('button', 'Gửi PM lập lại kế hoạch', 'secondary');
    submit.type = 'button';
    submit.disabled = busy;
    submit.onclick = () => {
      if (!input.value.trim()) return input.focus();
      command(`/runs/${run.run_id}/spec`, {request: input.value.trim()});
    };
    change.append(node('summary', 'Trao đổi thay đổi yêu cầu'), input, submit);
    section.append(change);
  }
  if (run.failures.length) section.append(node('pre', JSON.stringify(run.failures, null, 2)));
  return section;
}
