const roleNames = {supervisor: 'Supervisor / PM', architect: 'BA / Architect', developer: 'Developer', qa: 'QA'};
const roleContracts = {
  supervisor: 'Mặc định: phân loại đầu vào, lập kế hoạch, điều phối chat và tổng hợp báo cáo.',
  architect: 'BA phân tích yêu cầu và thiết kế theo từng task đã duyệt.',
  developer: 'Mặc định: viết mã theo thiết kế đã duyệt và sửa mã theo kết quả QA.',
  qa: 'Mặc định: soạn file pytest từ thiết kế.',
};
const toolNames = {get_project: 'Đọc kế hoạch project', change_run: 'Thay đổi yêu cầu và lập lại kế hoạch', control_run: 'Tạm dừng, tiếp tục, dừng run', list_files: 'Liệt kê file workspace', read_file: 'Đọc file', write_file: 'Ghi file', command: 'Chạy lệnh terminal', list_runs: 'Tra danh sách run', get_run: 'Đọc chi tiết run và kết quả QA', create_run: 'Tạo run từ cuộc trò chuyện'};

export function setupAgentConfig(api, getSession) {
  const get = id => document.getElementById(id);
  const dialog = get('agent-dialog');
  const roles = get('agent-role');
  const fields = get('agent-fields');
  const rules = get('agent-rules');
  const status = get('agent-config-status');
  const save = get('save-agent');
  let catalog;
  let generation = 0;

  function make(tag, text = '') {
    const element = document.createElement(tag);
    element.textContent = text;
    return element;
  }

  function button(text, handler, disabled = false) {
    const control = make('button', text);
    control.type = 'button';
    control.className = 'secondary';
    control.disabled = disabled;
    control.onclick = handler;
    return control;
  }

  function field(label, input) {
    const wrapper = make('label', label);
    wrapper.append(input);
    return wrapper;
  }

  function textInput(value, onChange, limit, multiline = false) {
    const input = make(multiline ? 'textarea' : 'input');
    if (multiline) input.rows = 3;
    input.value = value;
    input.maxLength = limit;
    input.oninput = () => onChange(input.value);
    return input;
  }

  function check(label, enabled, onChange) {
    const input = make('input');
    input.type = 'checkbox';
    input.checked = enabled;
    input.onchange = () => onChange(input.checked);
    const checkbox = field(label, input);
    checkbox.prepend(input);
    checkbox.className = 'agent-check';
    return checkbox;
  }

  function render() {
    const role = roles.value;
    const agent = catalog.agents[role];
    get('agent-role-contract').textContent = roleContracts[role];
    rules.value = agent.rules;
    rules.oninput = () => { agent.rules = rules.value; };

    get('agent-skills').replaceChildren(...agent.skills.map((skill, index) => {
      const box = make('div');
      box.className = 'agent-config-row';
      const name = textInput(skill.name, value => { skill.name = value; }, 100);
      const instructions = textInput(skill.instructions, value => { skill.instructions = value; }, 6000, true);
      name.required = instructions.required = true;
      box.append(field('Tên skill', name), field('Hướng dẫn', instructions));
      const actions = make('div');
      actions.className = 'agent-row-actions';
      actions.append(check('Bật skill', skill.enabled, enabled => { skill.enabled = enabled; }),
        button('Xóa skill', () => { agent.skills.splice(index, 1); render(); }));
      box.append(actions);
      return box;
    }));
    get('add-skill').disabled = agent.skills.length >= 10;

    get('agent-routes').replaceChildren(...agent.routes.map((route, index) => {
      const box = make('div');
      box.className = 'agent-config-row';
      const backend = make('select');
      for (const name of Object.keys(catalog.backends)) backend.add(new Option(name, name));
      backend.value = route.backend;
      const model = textInput(route.model ?? '', value => { route.model = value.trim() || null; }, 300);
      model.required = catalog.backends[route.backend] === 'openai_compatible';
      backend.onchange = () => {
        route.backend = backend.value;
        model.required = catalog.backends[route.backend] === 'openai_compatible';
      };
      const inputs = make('div');
      inputs.className = 'agent-route-inputs';
      inputs.append(field('Backend', backend), field('Model ID', model));
      const actions = make('div');
      actions.className = 'agent-row-actions';
      function move(offset) {
        const target = index + offset;
        [agent.routes[index], agent.routes[target]] = [agent.routes[target], agent.routes[index]];
        render();
      }
      actions.append(button('Lên', () => move(-1), index === 0),
        button('Xuống', () => move(1), index === agent.routes.length - 1),
        button('Xóa model', () => { agent.routes.splice(index, 1); render(); }, agent.routes.length === 1));
      box.append(make('strong', index === 0 ? 'Model chính' : 'Fallback ' + index), inputs, actions);
      return box;
    }));
    get('add-route').disabled = agent.routes.length >= 10;

    const tools = catalog.tools[role] ?? [];
    get('agent-tools').replaceChildren(...tools.map(tool => check(toolNames[tool] ?? tool, agent.tools.includes(tool), enabled => {
      agent.tools = enabled ? [...agent.tools, tool] : agent.tools.filter(name => name !== tool);
    })));
    if (!tools.length) {
      get('agent-tools').append(make('p', role === 'qa'
        ? 'Pytest do LangGraph chạy trong Docker ở bước QA.'
        : 'Vai trò này chạy theo các bước và dữ liệu do LangGraph cấp.'));
    }
  }

  get('configure-agents').onclick = async () => {
    const current = getSession();
    const request = ++generation;
    catalog = undefined;
    roles.replaceChildren();
    roles.disabled = fields.disabled = save.disabled = true;
    status.textContent = 'Đang tải cấu hình…';
    dialog.showModal();
    try {
      const config = await api('/agents', {}, current);
      if (request !== generation || !dialog.open) return;
      catalog = config;
      for (const role of Object.keys(catalog.agents)) roles.add(new Option(roleNames[role] ?? role, role));
      render();
      roles.disabled = fields.disabled = save.disabled = false;
      status.textContent = '';
    } catch (error) {
      if (request === generation && dialog.open) status.textContent = error.message;
    }
  };

  roles.onchange = () => { status.textContent = ''; render(); };
  get('add-skill').onclick = () => {
    catalog.agents[roles.value].skills.push({name: '', instructions: '', enabled: true});
    render();
    get('agent-skills').lastElementChild.querySelector('input').focus();
  };
  get('add-route').onclick = () => {
    catalog.agents[roles.value].routes.push({backend: Object.keys(catalog.backends)[0], model: null});
    render();
    get('agent-routes').lastElementChild.querySelector('select').focus();
  };
  get('agent-form').onsubmit = async event => {
    event.preventDefault();
    const request = generation;
    const role = roles.value;
    if (!catalog || save.disabled) return;
    roles.disabled = fields.disabled = save.disabled = true;
    status.textContent = 'Đang lưu…';
    try {
      const updated = await api('/agents/' + encodeURIComponent(role), {
        method: 'PUT',
        body: JSON.stringify(catalog.agents[role]),
      }, getSession());
      if (request !== generation || !dialog.open) return;
      catalog.agents[role] = updated;
      render();
      status.textContent = 'Đã lưu. Cấu hình áp dụng từ lần xử lý kế tiếp.';
    } catch (error) {
      if (request === generation && dialog.open) status.textContent = error.message;
    } finally {
      if (request === generation && dialog.open) roles.disabled = fields.disabled = save.disabled = false;
    }
  };
  const close = () => { if (dialog.open) dialog.close(); };
  get('close-agents').onclick = close;
  return {close};
}
