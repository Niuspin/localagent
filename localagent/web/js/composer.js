/* 输入框：自适应高度、IME 安全回车、附件（图片缩放 / 文件上传）、/ 技能弹层、模式与思考开关、发送/停止。 */
(function () {
  'use strict';
  var LA = window.LA, el = LA.el, icon = LA.icon;
  var input, chipsBox, pop, btnSend, hint, modePill, thinkPill, taintChip, fileInput, composerEl;
  var popItems = [], popSel = 0, popRange = null;
  var busy = false;      // 正在发送请求（防双击）

  var MODES = [
    { value: 'ask', label: '逐项确认', desc: '写文件、运行命令前都会询问（推荐）' },
    { value: 'auto_edit', label: '自动编辑', desc: '工作区内的文件修改自动通过；命令与删除仍询问' },
    { value: 'plan', label: '只读规划', desc: '只读取与搜索，不做任何修改，先给出方案' },
    { value: 'auto', label: '全自动', desc: '除敏感文件与不可信上下文外全部自动执行（谨慎）' }
  ];
  var THINK = [
    { value: 'auto', label: '思考：自动', desc: '由模型模板决定' },
    { value: 'on', label: '思考：开启', desc: '更适合复杂任务，但更慢' },
    { value: 'off', label: '思考：关闭', desc: '响应更快' }
  ];
  var MAX_IMG = 1568, MAX_IMAGES = 6;

  /* ------------------------------------------------------------ 对外接口 */
  LA.composer = {
    setText: function (t) { input.value = t || ''; autosize(); updateSend(); },
    focus: function () { input.focus(); },
    sendText: function (t, o) { return send(t, o); },
    refresh: function () { updatePills(); updateSend(); renderChips(); }
  };

  function autosize() { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 220) + 'px'; }

  /* ------------------------------------------------------------ 状态计算 */
  function canSend() {
    if (!LA.S.state) return { ok: false, why: '' };
    var e = LA.engine();
    if (LA.live) return { ok: false, why: 'running' };
    if (e.status === 'stopped' || e.status === 'error' || e.status === 'stopping') return { ok: false, why: 'engine' };
    var ws = (LA.S.cur && LA.S.cur.workspace) || LA.S.state.workspace;
    if (!ws) return { ok: false, why: 'ws' };
    return { ok: true };
  }
  function hasContent() { return !!(input.value.trim() || LA.S.attachments.length); }
  function updateSend() {
    var run = !!LA.live, c = canSend();
    btnSend.className = 'send' + (run ? ' stop' : '');
    btnSend.innerHTML = icon(run ? 'stop' : 'send', 18);
    btnSend.title = run ? '停止' : '发送';
    btnSend.disabled = busy || (!run && (!hasContent() || !c.ok));
    LA.clear(hint);
    if (!run && LA.S.state) {
      if (c.why === 'engine') {
        var e = LA.engine();
        hint.appendChild(document.createTextNode(e.status === 'stopping' ? '模型正在卸载 · ' : '模型未加载 · '));
        if (e.status !== 'stopping') hint.appendChild(el('a', { text: '加载', on: { click: function () { LA.startEngine(); } } }));
      } else if (c.why === 'ws') {
        hint.appendChild(document.createTextNode('未选择工作区 · '));
        hint.appendChild(el('a', { text: '选择', on: { click: function () { LA.panels.pickWorkspace(); } } }));
      } else if (LA.engine().status === 'starting') hint.textContent = '模型加载中，发送后会自动排队…';
    }
  }
  function updatePills() {
    var mode = LA.cfg('agent.permission_mode', (LA.S.state && LA.S.state.permission_mode) || 'ask');
    var m = MODES.filter(function (x) { return x.value === mode; })[0] || MODES[0];
    modePill.className = 'pill' + (mode === 'auto' ? ' danger' : mode === 'plan' ? ' plan' : '');
    modePill.innerHTML = icon(mode === 'plan' ? 'eye' : mode === 'auto' ? 'zap' : 'shield', 14);
    modePill.appendChild(el('span', { text: m.label }));
    var th = LA.cfg('generation.thinking', 'auto');
    var t = THINK.filter(function (x) { return x.value === th; })[0] || THINK[0];
    thinkPill.innerHTML = icon('brain', 14);
    thinkPill.appendChild(el('span', { text: t.label }));
    var tainted = !!(LA.S.cur && LA.S.cur.tainted);
    taintChip.hidden = !tainted;
    if (tainted) {
      LA.clear(taintChip);
      taintChip.appendChild(el('span', { text: '上下文含不可信内容 · 写入/命令需确认', title: '读取过网页、搜索结果、文档或上传文件后，Agent 的写入、命令、删除都会强制询问' }));
      taintChip.appendChild(el('button', { text: '信任并清除', title: '我已检查过这些内容，解除限制', on: { click: untaint } }));
    }
  }
  function untaint() {
    if (!LA.S.cur) return;
    LA.confirm('这会解除“不可信上下文”限制，之后 Agent 的操作不再因此被强制确认。\n\n请确认你已看过它读取的网页/文档内容，且没有可疑的指令。', { okText: '信任并清除' }).then(function (ok) {
      if (!ok) return;
      LA.post('/api/sessions/' + LA.S.cur.id + '/untaint').then(function () { LA.S.cur.tainted = false; LA.bus.emit('taint', false); LA.toast('已清除', 'ok'); }).catch(LA.fail);
    });
  }

  /* ------------------------------------------------------------ 附件 */
  function renderChips() {
    LA.clear(chipsBox);
    LA.S.chosen.forEach(function (n, i) {
      chipsBox.appendChild(el('span', { class: 'attach-chip skill' }, el('span', { html: icon('puzzle', 14) }), el('span', { text: '/' + n }),
        el('button', { class: 'x', html: icon('x', 12), title: '移除', on: { click: function () { LA.S.chosen.splice(i, 1); renderChips(); } } })));
    });
    LA.S.attachments.forEach(function (a, i) {
      var lead = a.kind === 'image' ? el('img', { src: a.dataUrl, alt: '' }) : el('span', { html: icon(a.uploading ? 'upload' : 'file', 14) });
      chipsBox.appendChild(el('span', { class: 'attach-chip' }, lead, el('span', { text: a.name + (a.uploading ? ' …' : '') }),
        el('button', { class: 'x', html: icon('x', 12), title: '移除', on: { click: function () { LA.S.attachments.splice(i, 1); renderChips(); updateSend(); } } })));
    });
  }

  function readAsDataURL(f) { return new Promise(function (res, rej) { var r = new FileReader(); r.onload = function () { res(r.result); }; r.onerror = rej; r.readAsDataURL(f); }); }
  function downscale(file) {
    return readAsDataURL(file).then(function (url) {
      return new Promise(function (resolve) {
        var img = new Image();
        img.onload = function () {
          var w = img.naturalWidth, h = img.naturalHeight, s = Math.min(1, MAX_IMG / Math.max(w, h));
          if (s >= 1 && file.size < 1.5e6 && /^image\/(png|jpeg|webp)$/.test(file.type)) return resolve(url);
          var c = document.createElement('canvas'); c.width = Math.max(1, Math.round(w * s)); c.height = Math.max(1, Math.round(h * s));
          var g = c.getContext('2d'); g.fillStyle = '#fff'; g.fillRect(0, 0, c.width, c.height); g.drawImage(img, 0, 0, c.width, c.height);
          resolve(c.toDataURL('image/jpeg', 0.85));
        };
        img.onerror = function () { resolve(null); };
        img.src = url;
      });
    });
  }
  function addFiles(files) {
    files = Array.prototype.slice.call(files || []);
    if (!files.length) return;
    files.forEach(function (f) {
      if (/^image\/(png|jpe?g|webp|gif|bmp)$/i.test(f.type)) {
        if (!LA.engine().vision) { LA.toast('当前模型不支持图片输入；图片会以文件形式放入工作区 uploads/', null, 3500); return uploadFile(f); }
        if (LA.S.attachments.filter(function (a) { return a.kind === 'image'; }).length >= MAX_IMAGES) { LA.toast('最多添加 ' + MAX_IMAGES + ' 张图片'); return; }
        downscale(f).then(function (u) {
          if (!u) return LA.toast('无法读取图片: ' + f.name, 'err');
          LA.S.attachments.push({ kind: 'image', name: f.name || '粘贴的图片', dataUrl: u }); renderChips(); updateSend();
        });
      } else uploadFile(f);
    });
  }
  function uploadFile(f) {
    var a = { kind: 'file', name: f.name, uploading: true }; LA.S.attachments.push(a); renderChips(); updateSend();
    LA.ensureSession().then(function (s) {
      var fd = new FormData(); fd.append('file', f, f.name);
      return LA.api('POST', '/api/sessions/' + s.id + '/upload', fd);
    }).then(function (r) { a.uploading = false; a.path = r.path; a.name = LA.baseName(r.path); renderChips(); updateSend(); })
      .catch(function (e) { var i = LA.S.attachments.indexOf(a); if (i >= 0) LA.S.attachments.splice(i, 1); renderChips(); updateSend(); LA.fail(e); });
  }

  /* ------------------------------------------------------------ / 技能弹层 */
  function slashToken() {
    var pos = input.selectionStart, v = input.value, i = pos;
    while (i > 0 && !/\s/.test(v[i - 1])) i--;
    var tok = v.slice(i, pos);
    if (tok[0] !== '/' || /[\/\\]/.test(tok.slice(1))) return null;
    return { q: tok.slice(1).toLowerCase(), start: i, end: pos };
  }
  function updatePop() {
    var t = slashToken();
    if (!t) return closePop();
    var list = (LA.S.skills || []).filter(function (s) { return s.enabled && LA.S.chosen.indexOf(s.name) < 0 && (s.name.toLowerCase().indexOf(t.q) >= 0 || (t.q.length > 1 && (s.description || '').toLowerCase().indexOf(t.q) >= 0)); }).slice(0, 8);
    popRange = t; popItems = list; popSel = Math.min(popSel, Math.max(0, list.length - 1));
    LA.clear(pop);
    if (!list.length) {
      if (!(LA.S.skills || []).some(function (s) { return s.enabled; })) pop.appendChild(el('div', { class: 'dr-empty', text: '还没有启用的技能。到 侧栏→技能 添加或启用。' }));
      else return closePop();
    }
    list.forEach(function (s, i) {
      pop.appendChild(el('button', { class: 'slash-item' + (i === popSel ? ' sel' : ''), on: { mousedown: function (e) { e.preventDefault(); pick(i); } } },
        el('b', { text: '/' + s.name }), el('small', { text: s.description || '' })));
    });
    pop.hidden = false;
  }
  function closePop() { pop.hidden = true; popItems = []; popRange = null; }
  function pick(i) {
    var s = popItems[i]; if (!s || !popRange) return;
    var v = input.value, before = v.slice(0, popRange.start), after = v.slice(popRange.end).replace(/^ /, '');
    input.value = before + after; input.selectionStart = input.selectionEnd = before.length;
    LA.S.chosen.push(s.name); closePop(); renderChips(); autosize(); updateSend(); input.focus();
  }

  /* ------------------------------------------------------------ 发送 */
  function send(textOverride, o) {
    o = o || {};
    if (LA.live) return Promise.resolve();
    var text = textOverride != null ? String(textOverride).trim() : input.value.trim();
    var atts = textOverride != null && !o.keepAttachments ? [] : LA.S.attachments;
    if (atts.some(function (a) { return a.uploading; })) { LA.toast('附件还在上传…'); return Promise.resolve(); }
    var images = atts.filter(function (a) { return a.kind === 'image'; }).map(function (a) { return a.dataUrl; });
    var files = atts.filter(function (a) { return a.kind === 'file' && a.path; }).map(function (a) { return a.path; });
    var skills = textOverride != null ? [] : LA.S.chosen.slice();
    if (!text && !images.length && !files.length) return Promise.resolve();
    var c = canSend();
    if (!c.ok) { updateSend(); return Promise.resolve(); }
    busy = true; updateSend();
    return LA.ensureSession().then(function (s) {
      return LA.post('/api/sessions/' + s.id + '/messages', { text: text, images: images, files: files, skills: skills }).then(function (r) { return { s: s, r: r }; });
    }).then(function (x) {
      if (textOverride == null) { input.value = ''; LA.S.attachments = []; LA.S.chosen = []; renderChips(); autosize(); }
      closePop();
      if (LA.chat.isEmpty()) { LA.clear(LA.$('#col')); }
      LA.chat.appendUser({ id: 'u' + Date.now(), role: 'user', ts: Date.now() / 1000, text: text, images: images, files: files, skills: skills });
      LA.chat.startRun(x.r.run_id, x.s.id);
      LA.refreshSessions();
    }).catch(LA.fail).then(function () { busy = false; updateSend(); });
  }

  /* ------------------------------------------------------------ 初始化 */
  LA.composer.init = function () {
    input = LA.$('#input'); chipsBox = LA.$('#attachChips'); pop = LA.$('#slashPop'); btnSend = LA.$('#btnSend'); hint = LA.$('#sendHint');
    modePill = LA.$('#modePill'); thinkPill = LA.$('#thinkPill'); taintChip = LA.$('#taintChip'); fileInput = LA.$('#fileInput'); composerEl = LA.$('#composer');
    LA.$('#btnAttach').innerHTML = icon('clip', 19);

    input.addEventListener('input', function () { autosize(); updateSend(); updatePop(); });
    input.addEventListener('click', updatePop);
    input.addEventListener('blur', function () { setTimeout(closePop, 120); });
    input.addEventListener('keydown', function (e) {
      if (!pop.hidden && popItems.length) {
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); popSel = (popSel + (e.key === 'ArrowDown' ? 1 : popItems.length - 1)) % popItems.length; updatePop(); return; }
        if ((e.key === 'Enter' || e.key === 'Tab') && !e.isComposing) { e.preventDefault(); pick(popSel); return; }
      }
      if (e.key === 'Escape' && !pop.hidden) { e.preventDefault(); e.stopPropagation(); closePop(); return; }
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); if (!btnSend.disabled && !LA.live) send(); }
      if (e.key === 'Backspace' && !input.value && LA.S.chosen.length) { LA.S.chosen.pop(); renderChips(); }
    });
    input.addEventListener('paste', function (e) {
      var items = (e.clipboardData && e.clipboardData.files) || [];
      if (items.length) { e.preventDefault(); addFiles(items); }
    });
    btnSend.addEventListener('click', function () { if (LA.live) LA.chat.cancel(); else send(); });
    LA.$('#btnAttach').addEventListener('click', function () { fileInput.click(); });
    fileInput.addEventListener('change', function () { addFiles(fileInput.files); fileInput.value = ''; });

    // 拖放
    var mask = LA.$('#dropMask'), depth = 0;
    function hasFiles(e) { return e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], 'Files') >= 0; }
    document.addEventListener('dragenter', function (e) { if (hasFiles(e)) { depth++; mask.hidden = false; } });
    document.addEventListener('dragleave', function (e) { if (hasFiles(e) && --depth <= 0) { depth = 0; mask.hidden = true; } });
    document.addEventListener('dragover', function (e) { if (hasFiles(e)) e.preventDefault(); });
    document.addEventListener('drop', function (e) { if (!hasFiles(e)) return; e.preventDefault(); depth = 0; mask.hidden = true; addFiles(e.dataTransfer.files); });

    modePill.addEventListener('click', function () {
      LA.menu(modePill, MODES, function (v) { LA.cfgSet('agent.permission_mode', v, { immediate: true }); }, LA.cfg('agent.permission_mode', 'ask'));
    });
    thinkPill.addEventListener('click', function () {
      LA.menu(thinkPill, THINK, function (v) { LA.cfgSet('generation.thinking', v, { immediate: true }); }, LA.cfg('generation.thinking', 'auto'));
    });

    ['state', 'config', 'runstate', 'taint', 'sessionmeta', 'session'].forEach(function (n) { LA.bus.on(n, function () { updatePills(); updateSend(); }); });
    updatePills(); updateSend(); autosize();
  };
})();
