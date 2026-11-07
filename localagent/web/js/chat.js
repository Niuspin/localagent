/* 对话视图：渲染会话 JSON、处理运行事件流（流式 / 工具 / 审批 / 提问 / 任务清单）。 */
(function () {
  'use strict';
  var LA = window.LA, el = LA.el, icon = LA.icon;

  var col, scrollEl, toBottom;
  var stick = true;
  var raf = {};            // 待渲染块（rAF 合并）
  LA.live = null;          // {runId, sid, close, view, model}

  var TOOL_ICON = { read_file: 'file', write_file: 'edit', edit_file: 'edit', edit_office: 'edit', write_document: 'file', write_spreadsheet: 'file', write_presentation: 'file',
    list_dir: 'folder', glob: 'search', grep: 'search', run_command: 'terminal', delete_path: 'trash', move_path: 'folder', copy_path: 'copy', make_dir: 'folder',
    web_search: 'globe', web_fetch: 'globe', memory_save: 'brain', memory_search: 'brain', memory_forget: 'brain', use_skill: 'puzzle', read_skill_file: 'puzzle', create_skill: 'puzzle',
    todo_write: 'list', ask_user: 'help' };

  /* ---------------------------------------------------------------- 滚动 */
  function nearBottom() { return scrollEl.scrollHeight - scrollEl.scrollTop - scrollEl.clientHeight < 90; }
  LA.chat = {
    scrollToBottom: function (force) {
      if (force) stick = true;
      if (stick) { scrollEl.scrollTop = scrollEl.scrollHeight; toBottom.hidden = true; }
      else if (LA.live) toBottom.hidden = false;
    },
    init: function () {
      col = LA.$('#col'); scrollEl = LA.$('#scroll'); toBottom = LA.$('#toBottom');
      toBottom.innerHTML = icon('down') + '<span>回到底部</span>';
      scrollEl.addEventListener('scroll', function () { stick = nearBottom(); if (stick) toBottom.hidden = true; });
      toBottom.addEventListener('click', function () { LA.chat.scrollToBottom(true); });
      LA.bus.on('state', function () { LA.chat.updateEmptyCards(); });
    },
    isEmpty: function () { return !!LA.$('.empty', col); }
  };

  function schedule(key, fn) {
    if (raf[key]) { raf[key].fn = fn; return; }
    raf[key] = { fn: fn };
    requestAnimationFrame(function () { var f = raf[key].fn; delete raf[key]; f(); LA.chat.scrollToBottom(); });
  }

  /* ---------------------------------------------------------------- 空状态 */
  function greeting() { var h = new Date().getHours(); return h < 5 ? '夜深了' : h < 11 ? '早上好' : h < 13 ? '中午好' : h < 18 ? '下午好' : '晚上好'; }
  var SUGGEST = ['梳理这个文件夹里有什么，并生成一份 README', '读取工作区里的表格，汇总关键数据并做成 Excel', '搜索最近关于 llama.cpp 的更新并写一份简报', '把 notes 里的 Markdown 合并成一份 Word 文档'];

  LA.chat.renderEmpty = function () {
    LA.clear(col);
    var sug = el('div', { class: 'suggest' }, SUGGEST.map(function (t) {
      return el('button', { text: t, on: { click: function () { LA.composer.setText(t); LA.composer.focus(); } } });
    }));
    col.appendChild(el('div', { class: 'empty' },
      el('h1', null, el('span', { class: 'g', html: LA.SPARK.replace('<svg', '<svg style="width:.7em;height:.7em;fill:currentColor;vertical-align:-.02em"') }), greeting()),
      el('p', { text: '本地模型，本地数据。今天想让我帮你做点什么？' }),
      el('div', { class: 'cards', id: 'emptyCards' }), sug));
    LA.chat.updateEmptyCards();
  };
  LA.chat.updateEmptyCards = function () {
    var box = LA.$('#emptyCards'); if (!box || !LA.S.state) return;
    LA.clear(box);
    var st = LA.S.state, e = st.engine, cur = LA.S.cur;
    var hasWs = !!((cur && cur.workspace) || st.workspace);
    function card(ic, title, body, btn) {
      box.appendChild(el('div', { class: 'card' }, el('span', { class: 'ci', html: icon(ic, 20) }), el('div', null, el('b', { text: title }), body, btn || null)));
    }
    if (!st.models.length) card('cpu', '还没有模型', el('p', { html: '把 <code>.gguf</code> 模型文件放进项目根目录的 <code>models/</code> 文件夹（多模态模型请把配套的 <code>mmproj-*.gguf</code> 放在同一目录），然后刷新。' }),
      el('button', { class: 'btn sm', text: '重新扫描', on: { click: function () { LA.refreshState(); } } }));
    if (!e.binary) card('zap', '没有找到 llama-server', el('p', { html: '请把 llama.cpp 发行包解压到 <code>bin/</code> 目录（含 <code>llama-server</code>），或在 设置→模型与引擎 里指定路径。' }),
      el('button', { class: 'btn sm', text: '打开设置', on: { click: function () { LA.settings.open('engine'); } } }));
    if (!hasWs) card('folder', '选择一个工作区', el('p', { text: 'Agent 只能在你选定的文件夹里读写文件、运行命令。' }),
      el('button', { class: 'btn sm primary', text: '选择工作区', on: { click: function () { LA.panels.pickWorkspace(); } } }));
    if (st.models.length && e.binary && (e.status === 'stopped' || e.status === 'error')) {
      card(e.status === 'error' ? 'alert' : 'play', e.status === 'error' ? '引擎启动失败' : '模型尚未加载',
        e.status === 'error' ? el('p', { text: e.error || '未知错误' }) : el('p', { text: '加载后即可开始对话。首次加载大模型可能需要几十秒。' }),
        el('button', { class: 'btn sm primary', text: '加载模型', on: { click: function () { LA.startEngine(); } } }));
    }
  };

  /* ---------------------------------------------------------------- 会话渲染 */
  LA.chat.renderSession = function (view, opts) {
    opts = opts || {};
    LA.clear(col);
    var msgs = view.messages || [];
    if (view.active_run && msgs.length && msgs[msgs.length - 1].role === 'assistant' && msgs[msgs.length - 1].status === 'running') msgs = msgs.slice(0, -1);
    if (!msgs.length && !view.active_run) { LA.chat.renderEmpty(); return; }
    var wrap = el('div', { class: 'msgs', id: 'msgs' });
    col.appendChild(wrap);
    msgs.forEach(function (m, i) { wrap.appendChild(m.role === 'user' ? renderUser(m, i) : new AstView(m, i).el); });
    stick = true;
    requestAnimationFrame(function () { scrollEl.scrollTop = scrollEl.scrollHeight; });
  };
  function msgsWrap() {
    var w = LA.$('#msgs', col);
    if (!w) { LA.clear(col); w = el('div', { class: 'msgs', id: 'msgs' }); col.appendChild(w); }
    return w;
  }

  function renderUser(m, idx) {
    var d = el('div', { class: 'msg user', dataset: { idx: idx } });
    if (m.images && m.images.length) d.appendChild(el('div', { class: 'u-imgs' }, m.images.map(function (u) { return el('img', { src: u, alt: '图片' }); })));
    var chips = [];
    (m.files || []).forEach(function (f) { chips.push(el('span', { class: 'attach-chip' }, el('span', { html: icon('file', 14) }), el('span', { text: f }))); });
    (m.skills || []).forEach(function (s) { chips.push(el('span', { class: 'attach-chip skill' }, el('span', { html: icon('puzzle', 14) }), el('span', { text: '/' + s }))); });
    if (chips.length) d.appendChild(el('div', { class: 'u-chips' }, chips));
    if (m.text) d.appendChild(el('div', { class: 'bubble', text: m.text }));
    d.appendChild(el('div', { class: 'msg-acts' },
      el('button', { class: 'act', html: icon('edit', 14) + ' 编辑', on: { click: function () { editFrom(idx, m); } } }),
      el('button', { class: 'act', html: icon('copy', 14) + ' 复制', on: { click: function () { LA.copyText(m.text || ''); } } })));
    return d;
  }

  function guardIdle() { if (LA.live) { LA.toast('请先等待当前回答完成或点击停止'); return false; } return true; }
  function editFrom(idx, m) {
    if (!guardIdle()) return;
    LA.post('/api/sessions/' + LA.S.cur.id + '/truncate', { from_index: idx }).then(function (v) {
      LA.setSession(v); LA.composer.setText(m.text || ''); LA.composer.focus();
    }).catch(LA.fail);
  }
  function regenerate(idx) {
    if (!guardIdle()) return;
    var msgs = LA.S.cur.messages, u = idx - 1;
    if (u < 0 || msgs[u].role !== 'user') return;
    var text = msgs[u].text || '';
    LA.post('/api/sessions/' + LA.S.cur.id + '/truncate', { from_index: u }).then(function (v) {
      LA.setSession(v); LA.composer.sendText(text);
    }).catch(LA.fail);
  }

  /* ---------------------------------------------------------------- 助手消息视图 */
  function AstView(model, idx) {
    this.model = model; this.idx = idx; this.views = []; this.lastType = null; this.toolsWrap = null; this.todoEl = null;
    this.body = el('div', { class: 'ast-body' });
    this.usageEl = el('div', { class: 'usage' });
    this.acts = el('div', { class: 'msg-acts' });
    this.el = el('div', { class: 'msg assistant', dataset: { idx: idx } },
      el('div', { class: 'ast-head' }, el('span', { class: 'avatar', html: LA.SPARK })), this.body, this.usageEl, this.acts);
    var self = this;
    (model.blocks || []).forEach(function (b) { self.addBlock(b); });
    if (model.error) this.showError(model.error);
    if (model.status === 'cancelled') this.addNote('已停止', true);
    this.renderFooter();
  }
  AstView.prototype.addBlock = function (b) {
    var v;
    if (b.type === 'tool') {
      if (!this.toolsWrap || this.lastType !== 'tool') { this.toolsWrap = el('div', { class: 'tools' }); this.body.appendChild(this.toolsWrap); }
      v = mkTool(b); this.toolsWrap.appendChild(v.el);
    } else {
      this.toolsWrap = null;
      v = b.type === 'thinking' ? mkThinking(b) : mkText(b);
      this.body.appendChild(v.el);
    }
    this.lastType = b.type; this.views.push(v);
    return v;
  };
  AstView.prototype.addNote = function (text, warn) {
    this.toolsWrap = null; this.lastType = 'note';
    this.body.appendChild(el('div', { class: 'pill-row' }, el('span', { class: 'pill-note' + (warn ? ' warn' : ''), text: text })));
  };
  AstView.prototype.showError = function (msg) { this.body.appendChild(el('div', { class: 'err-box', text: msg })); };
  AstView.prototype.setTodos = function (items) {
    if (!items || !items.length) return;
    if (!this.todoEl) { this.todoEl = el('details', { class: 'todo-card', open: true }); this.body.insertBefore(this.todoEl, this.body.firstChild); }
    var done = items.filter(function (i) { return i.status === 'done'; }).length;
    LA.clear(this.todoEl);
    this.todoEl.appendChild(el('summary', null, el('span', { html: icon('list', 15) }), '任务清单 ' + done + '/' + items.length));
    this.todoEl.appendChild(todoList(items));
  };
  AstView.prototype.renderFooter = function () {
    var m = this.model, u = m.usage, self = this;
    this.usageEl.textContent = u && u.prompt_tokens ? '输入 ' + LA.fmtN(u.prompt_tokens) + ' · 输出 ' + LA.fmtN(u.completion_tokens) + (u.tps ? ' · ' + u.tps + ' tok/s' : '') : '';
    LA.clear(this.acts);
    if (m.status === 'running') return;
    this.acts.appendChild(el('button', { class: 'act', html: icon('copy', 14) + ' 复制', on: { click: function () {
      LA.copyText((m.blocks || []).filter(function (b) { return b.type === 'text'; }).map(function (b) { return b.text; }).join('\n\n')); } } }));
    this.acts.appendChild(el('button', { class: 'act', html: icon('refresh', 14) + ' 重新生成', on: { click: function () { regenerate(self.idx); } } }));
  };

  function todoList(items) {
    var ul = el('ul', { class: 'todo-list' });
    items.forEach(function (i) { ul.appendChild(el('li', { class: i.status }, el('span', { class: 'st', html: i.status === 'done' ? icon('check') : '' }), el('span', { class: 'tx', text: i.text }))); });
    return ul;
  }
  LA.chat.todoList = todoList;

  /* ---------------------------------------------------------------- 块：思考 / 文本 / 工具 */
  function mkThinking(b, live) {
    var sum = el('summary', null, el('span', { class: 'chev', html: icon('chev', 14) }), el('span', { class: 'lbl' }));
    var body = el('div', { class: 'tbody' });
    var d = el('details', { class: 'think' }, sum, body);
    var v = { el: d, type: 'thinking', b: b };
    v.update = function () {
      body.textContent = b.text || '';
      var lbl = LA.$('.lbl', sum);
      if (b.live) { d.classList.add('live'); lbl.textContent = '思考中…'; }
      else { d.classList.remove('live'); lbl.textContent = '已思考' + (b.ms >= 800 ? ' ' + (b.ms / 1000).toFixed(1) + 's' : ''); }
      if (b.live) body.scrollTop = body.scrollHeight;
    };
    if (b.live) d.open = true;
    v.update();
    return v;
  }
  function mkText(b) {
    var d = el('div', { class: 'md' });
    var v = { el: d, type: 'text', b: b };
    v.update = function () { d.innerHTML = LA.renderMarkdown(b.text || ''); d.classList.toggle('cursor', !!b.live); };
    v.update();
    return v;
  }

  function mkTool(b) {
    var open = false;
    var head = el('button', { class: 't-head', 'aria-expanded': 'false' });
    var ti = el('span', { class: 'ti' }), tt = el('span', { class: 'tt' }), ts = el('span', { class: 'ts' }), ch = el('span', { class: 'chev', html: icon('chev', 14) });
    head.append(ti, tt, ts, ch);
    var body = el('div', { class: 't-body' });
    var apBox = el('div');
    var root = el('div', { class: 'tool' }, head, body, apBox);
    var v = { el: root, type: 'tool', b: b };
    var apId = null;
    head.addEventListener('click', function () { open = !open; root.classList.toggle('open', open); head.setAttribute('aria-expanded', open); });

    v.update = function () {
      root.className = 'tool ' + b.status + (open ? ' open' : '');
      ti.innerHTML = icon(TOOL_ICON[b.name] || 'zap', 16);
      tt.textContent = b.title || b.name;
      var s = b.status;
      ts.innerHTML = s === 'running' ? '<span class="spin"></span>' : s === 'ok' ? icon('check', 15) : s === 'error' ? icon('alert', 15) : s === 'denied' ? '已拒绝' : s === 'pending_approval' ? icon('lock', 14) + ' 等待确认' : '';
      if (b.duration_ms && s !== 'running') ts.appendChild(el('span', { text: b.duration_ms >= 1000 ? (b.duration_ms / 1000).toFixed(1) + 's' : b.duration_ms + 'ms' }));
      if (b.status === 'pending_approval' && !open) { open = true; root.classList.add('open'); }
      // 详情
      LA.clear(body);
      var args = b.args && Object.keys(b.args).length ? JSON.stringify(b.args, null, 2) : '';
      if (args) { body.appendChild(el('h6', { text: '参数' })); body.appendChild(el('pre', { text: args.length > 3000 ? args.slice(0, 3000) + '\n…' : args })); }
      if (b.content) { body.appendChild(el('h6', { text: '结果' })); body.appendChild(el('pre', { text: b.content })); }
      var meta = b.meta || {};
      var chips = el('div', { class: 't-meta' });
      if (meta.path) chips.appendChild(el('span', { class: 'meta-chip', text: meta.path }));
      if (meta.sandbox) chips.appendChild(el('span', { class: 'meta-chip', text: meta.sandbox }));
      if (meta.undo_id) chips.appendChild(el('button', { class: 'btn sm', html: icon('undo', 13) + ' 撤销', on: { click: function (e) { e.stopPropagation(); undo(meta.undo_id, chips); } } }));
      if (chips.childNodes.length) body.appendChild(chips);
      // 审批卡片
      if (b.approval) { if (apId !== b.approval.approval_id) { LA.clear(apBox); apBox.appendChild(approvalCard(b)); apId = b.approval.approval_id; } }
      else if (apId) { LA.clear(apBox); apId = null; }
    };
    v.update();
    return v;
  }

  function undo(id, chips, force) {
    LA.post('/api/journal/' + id + '/undo', force ? { force: true } : {}).then(function () {
      LA.toast('已撤销', 'ok'); chips.innerHTML = ''; chips.appendChild(el('span', { class: 'meta-chip', text: '已撤销' })); LA.bus.emit('fschanged');
    }).catch(function (e) {
      if (e.status === 409 && !force) LA.confirm((e.message || '').replace(/^conflict:\s*/, '') + '\n\n仍要强制撤销吗？这会覆盖之后的修改。', { danger: true, okText: '强制撤销' }).then(function (ok) { if (ok) undo(id, chips, true); });
      else LA.fail(e);
    });
  }

  function diffView(text) {
    var d = el('div', { class: 'diff' });
    String(text || '').split('\n').forEach(function (ln) {
      var c = /^(\+\+\+|---)/.test(ln) ? 'hunk' : ln[0] === '+' ? 'add' : ln[0] === '-' ? 'del' : /^@@/.test(ln) ? 'hunk' : '';
      d.appendChild(el('span', { class: 'l ' + c, text: ln || ' ' }));
    });
    return d;
  }

  function approvalCard(b) {
    var a = b.approval, done = false;
    var card = el('div', { class: 'approval', tabIndex: 0, role: 'group', 'aria-label': '操作确认' });
    card.appendChild(el('div', { class: 'reason' }, el('b', { text: '需要你的确认' }), el('span', { class: 'risk ' + a.risk, text: { high: '高风险', medium: '中风险', low: '低风险' }[a.risk] || a.risk }),
      a.reason ? el('div', { text: a.reason, style: { marginTop: '2px', color: 'var(--muted)' } }) : null));
    if (a.tainted) card.appendChild(el('div', { class: 'taint-banner' }, el('span', { html: icon('alert', 16) }), el('span', { text: '当前上下文读入过网页/文档等不可信内容。请确认这一步确实是你想要的，而不是内容里藏的指令。' })));
    var pv = a.preview || {};
    if (pv.kind === 'diff') card.appendChild(diffView(pv.text));
    else if (pv.text) card.appendChild(el('div', { class: 'cmdbox', text: pv.text }));
    function decide(dec) {
      if (done) return; done = true;
      LA.$$('button', card).forEach(function (x) { x.disabled = true; });
      LA.post('/api/approvals/' + a.approval_id, { decision: dec }).catch(function (e) { done = false; LA.$$('button', card).forEach(function (x) { x.disabled = false; }); LA.fail(e); });
    }
    var ok = el('button', { class: 'btn primary', text: '允许一次', on: { click: function () { decide('allow'); } } });
    var sess = el('button', { class: 'btn', text: '本会话允许', title: '本会话内对相同类型的操作不再询问（敏感文件/不可信上下文除外）', on: { click: function () { decide('allow_session'); } } });
    var no = el('button', { class: 'btn danger', text: '拒绝', on: { click: function () { decide('deny'); } } });
    card.appendChild(el('div', { class: 'approve-btns' }, ok, sess, no));
    card.addEventListener('keydown', function (e) {
      if (e.target !== card) return;
      if (e.key === 'Enter') { e.preventDefault(); decide('allow'); } else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); decide('deny'); }
    });
    setTimeout(function () { var ae = document.activeElement; if (!ae || ae === document.body || (ae.id === 'input' && !ae.value)) { try { card.focus({ preventScroll: true }); } catch (e) {} } }, 50);
    return card;
  }

  function questionCard(d, view) {
    var done = false;
    var box = el('div', { class: 'question' }, el('div', { class: 'q', text: d.question }));
    function answer(t) {
      if (done || !t) return; done = true;
      LA.post('/api/questions/' + d.question_id, { answer: t }).then(function () { box.remove(); view.addNote('你的回答: ' + t); }).catch(function (e) { done = false; LA.fail(e); });
    }
    if (d.options && d.options.length) box.appendChild(el('div', { class: 'opts' }, d.options.map(function (o) { return el('button', { class: 'btn sm', text: o, on: { click: function () { answer(o); } } }); })));
    var inp = el('input', { placeholder: '或者自己回答…' });
    inp.addEventListener('keydown', function (e) { if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) answer(inp.value.trim()); });
    box.appendChild(el('div', { class: 'free' }, inp, el('button', { class: 'btn sm primary', text: '回答', on: { click: function () { answer(inp.value.trim()); } } })));
    return box;
  }

  /* ---------------------------------------------------------------- 运行事件流 */
  /** 开始一次运行的视图：为本次 run 创建助手消息并订阅事件。 */
  LA.chat.startRun = function (runId, sid, opts) {
    opts = opts || {};
    var model = { id: 'live', role: 'assistant', status: 'running', blocks: [], usage: null, error: null };
    var wrap = msgsWrap();
    var idx = LA.S.cur.messages.length;
    if (opts.pushModel !== false) LA.S.cur.messages.push(model);
    var view = new AstView(model, idx);
    wrap.appendChild(view.el);
    view.el.classList.add('live');
    LA.chat.scrollToBottom(true);
    var cur = { thinking: null, text: null };
    var live = LA.live = { runId: runId, sid: sid, view: view, model: model, close: null };
    LA.bus.emit('runstate', true);

    function endBlocks() {
      if (cur.thinking) { cur.thinking.b.live = false; cur.thinking.update(); cur.thinking.el.open = false; cur.thinking = null; }
      if (cur.text) { cur.text.b.live = false; cur.text.update(); cur.text = null; }
    }
    function sched(v) { schedule(v.b.__k || (v.b.__k = 'b' + Math.random()), function () { v.update(); }); }
    function findTool(id) { for (var i = view.views.length - 1; i >= 0; i--) { var v = view.views[i]; if (v.type === 'tool' && v.b.id === id) return v; } return null; }

    var H = {
      thinking_delta: function (d) {
        if (cur.text) { cur.text.b.live = false; cur.text.update(); cur.text = null; }
        if (!cur.thinking) { var b = { type: 'thinking', text: '', ms: 0, live: true, t0: Date.now() }; model.blocks.push(b); cur.thinking = view.addBlock(b); }
        cur.thinking.b.text += d.text; cur.thinking.b.ms = Date.now() - cur.thinking.b.t0; sched(cur.thinking);
      },
      text_delta: function (d) {
        if (cur.thinking) { cur.thinking.b.live = false; cur.thinking.update(); cur.thinking.el.open = false; cur.thinking = null; }
        if (!cur.text) { var b = { type: 'text', text: '', live: true }; model.blocks.push(b); cur.text = view.addBlock(b); }
        cur.text.b.text += d.text; sched(cur.text);
      },
      tool_start: function (d) {
        endBlocks();
        var b = { type: 'tool', id: d.id, name: d.name, title: d.title, args: d.args, status: 'running', content: '', meta: {}, duration_ms: 0, approval: null };
        model.blocks.push(b); view.addBlock(b); LA.chat.scrollToBottom();
      },
      approval_request: function (d) {
        var v = findTool(d.tool_id); if (!v) return;
        v.b.status = 'pending_approval'; v.b.approval = { approval_id: d.approval_id, reason: d.reason, risk: d.risk, preview: d.preview, tainted: d.tainted };
        v.update(); LA.chat.scrollToBottom(true);
        if (document.hidden && 'Notification' in window && Notification.permission === 'granted') { try { new Notification('LocalAgent 需要你确认', { body: d.title }); } catch (e) {} }
        document.title = '⏸ 需要确认 · LocalAgent';
      },
      approval_resolved: function (d) {
        var v = findTool(d.tool_id); if (!v) return; v.b.approval = null; v.b.status = d.decision === 'deny' ? 'denied' : 'running'; v.update();
        document.title = 'LocalAgent';
      },
      tool_result: function (d) {
        var v = findTool(d.id); if (!v) return;
        v.b.status = d.status; v.b.content = d.content; v.b.meta = d.meta || {}; v.b.duration_ms = d.duration_ms; v.b.approval = null; v.update();
        if ((d.meta && (d.meta.path || d.meta.undo_id)) || d.name === 'run_command') LA.bus.emit('fschanged');
      },
      question: function (d) { endBlocks(); view.toolsWrap = null; view.lastType = 'note'; view.body.appendChild(questionCard(d, view)); LA.chat.scrollToBottom(true); },
      todo_update: function (d) { LA.S.cur.todos = d.items; view.setTodos(d.items); LA.bus.emit('todos', d.items); },
      usage: function (d) {
        model.usage = { prompt_tokens: d.prompt_tokens, completion_tokens: d.completion_tokens, tps: d.tps };
        view.renderFooter(); LA.S.cur.context = { ctx: d.ctx, used: d.used, pct: d.pct }; LA.bus.emit('context', LA.S.cur.context);
      },
      context_event: function (d) { endBlocks(); view.addNote(d.detail, d.kind === 'trimmed'); if (d.kind === 'grow_done') LA.refreshState(); },
      notice: function (d) { view.addNote(d.text, d.level === 'warn'); },
      taint: function () { LA.S.cur.tainted = true; LA.bus.emit('taint', true); },
      assistant_done: function () { endBlocks(); },
      run_end: function (d) {
        endBlocks(); close();
        model.status = d.status === 'cancelled' ? 'cancelled' : d.status === 'error' ? 'error' : 'done';
        if (d.error) { model.error = d.error; view.showError(d.error); }
        if (d.status === 'cancelled') view.addNote('已停止', true);
        view.el.classList.remove('live'); view.renderFooter();
        LA.live = null; document.title = 'LocalAgent'; LA.bus.emit('runstate', false);
        LA.get('/api/sessions/' + sid).then(function (v) { if (LA.S.cur && LA.S.cur.id === sid) { LA.S.cur.title = v.title; LA.S.cur.context = v.context; LA.S.cur.tainted = v.tainted; LA.S.cur.todos = v.todos; LA.bus.emit('sessionmeta', v); } }).catch(function () {});
        LA.refreshSessions(); LA.bus.emit('fschanged');
        if (d.status === 'done' && document.hidden && 'Notification' in window && Notification.permission === 'granted') { try { new Notification('LocalAgent 已完成'); } catch (e) {} }
      }
    };
    var closeFn = LA.sse('/api/runs/' + runId + '/events?after=0', H, { onerror: function (final) {
      if (final && LA.live && LA.live.runId === runId) { LA.live = null; model.status = 'error'; view.showError('与服务的连接中断'); view.renderFooter(); LA.bus.emit('runstate', false); }
    } });
    function close() { closeFn(); }
    live.close = close;
    return live;
  };

  /** 切换会话时断开事件流（运行在服务端继续）。 */
  LA.chat.detach = function () { if (LA.live) { LA.live.close(); LA.live = null; LA.bus.emit('runstate', false); } };
  LA.chat.cancel = function () { if (LA.live) LA.post('/api/runs/' + LA.live.runId + '/cancel').catch(LA.fail); };
  LA.chat.appendUser = function (m) {
    var wrap = msgsWrap();
    var idx = LA.S.cur.messages.length;
    LA.S.cur.messages.push(m);
    wrap.appendChild(renderUser(m, idx));
    LA.chat.scrollToBottom(true);
  };
})();
