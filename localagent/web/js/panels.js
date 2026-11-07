/* 面板：工作区选择、右侧抽屉（文件/变更/任务）、技能管理、记忆库管理。 */
(function () {
  'use strict';
  var LA = window.LA, el = LA.el, icon = LA.icon;
  var P = LA.panels = {};

  function btn(text, fn, cls, title) { return el('button', { class: 'btn sm ' + (cls || ''), text: text, title: title || '', on: { click: fn } }); }
  function iconBtn(ic, title, fn) { return el('button', { class: 'icon-btn', title: title, 'aria-label': title, html: icon(ic, 15), on: { click: function (e) { e.stopPropagation(); fn(e); } } }); }
  function sid() { return LA.S.cur ? LA.S.cur.id : ''; }
  function sq(extra) { var q = sid() ? 'session_id=' + encodeURIComponent(sid()) : ''; return q ? (extra ? extra + '&' + q : '?' + q) : (extra || ''); }
  function curWs() { return (LA.S.cur && LA.S.cur.workspace) || (LA.S.state && LA.S.state.workspace) || ''; }

  /* ================================================================ 工作区选择 */
  P.pickWorkspace = function () {
    var cur = curWs(), path = cur, parent = '', sep = '/';
    var listBox = el('div', { class: 'browse-list' }), pathInp = el('input', { class: 'inp wide', placeholder: '输入或选择文件夹路径', spellcheck: false });
    var err = el('div', { class: 'hint2', style: { color: 'var(--danger)', minHeight: '1.2em' } });
    var ok = el('button', { class: 'btn primary', text: '选择此文件夹' });
    var m = LA.modal({ title: '选择工作区', width: 560, body: [
      el('div', { class: 'hint2', text: 'Agent 只能读写这个文件夹内的文件，也只能在这里运行命令。不能选择磁盘根目录或用户主目录本身。' }),
      pathInp, listBox, err], footer: [btn('取消', function () { m.close(); }), ok] });
    function load(p) {
      LA.get('/api/fs/browse?path=' + encodeURIComponent(p || '')).then(function (r) {
        path = r.path; parent = r.parent; sep = r.sep || '/'; pathInp.value = r.path || '';
        LA.clear(listBox);
        if (r.parent != null && r.parent !== '') listBox.appendChild(row('..  上一级', function () { load(r.parent); }));
        (r.dirs || []).forEach(function (d) { listBox.appendChild(row(d, function () { load((r.path ? r.path.replace(/[\\/]+$/, '') + sep : '') + d); })); });
        if (!(r.dirs || []).length) listBox.appendChild(el('div', { class: 'dr-empty', text: '（没有子文件夹）' }));
        err.textContent = '';
      }).catch(function (e) { err.textContent = e.message; });
    }
    function row(t, fn) { return el('div', { class: 'browse-row', tabIndex: 0, on: { click: fn, keydown: function (e) { if (e.key === 'Enter') fn(); } } }, el('span', { html: icon('folder', 16) }), el('span', { text: t })); }
    pathInp.addEventListener('keydown', function (e) { if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) load(pathInp.value.trim()); });
    ok.addEventListener('click', function () {
      var p = pathInp.value.trim() || path;
      if (!p) { err.textContent = '请先进入一个文件夹'; return; }
      ok.disabled = true;
      LA.put('/api/workspace', { path: p, session_id: sid() || undefined }).then(function (r) {
        LA.S.state.workspace = r.path; if (LA.S.cur) LA.S.cur.workspace = r.path;
        m.close(); LA.bus.emit('workspace', r.path); LA.bus.emit('state', LA.S.state); LA.refreshSessions(); LA.toast('工作区已设置', 'ok');
      }).catch(function (e) { err.textContent = e.message; ok.disabled = false; });
    });
    load(cur || '');
  };

  /* ================================================================ 右侧抽屉 */
  var drawer, dtab = 'files', previewing = null, expanded = {}, drawBody;

  P.initDrawer = function () {
    drawer = LA.$('#drawer');
    LA.$('#btnDrawer').innerHTML = icon('panel', 19);
    LA.$('#btnDrawer').addEventListener('click', function () { P.toggleDrawer(); });
    var refresh = LA.debounce(function () { if (LA.S.drawerOpen && !previewing) renderDrawer(); }, 400);
    LA.bus.on('fschanged', refresh); LA.bus.on('session', function () { previewing = null; expanded = {}; if (LA.S.drawerOpen) renderDrawer(); });
    LA.bus.on('workspace', function () { expanded = {}; if (LA.S.drawerOpen) renderDrawer(); });
    LA.bus.on('todos', function () { if (LA.S.drawerOpen && dtab === 'tasks') renderDrawer(); });
  };
  P.toggleDrawer = function (force) {
    LA.S.drawerOpen = force != null ? force : !LA.S.drawerOpen;
    drawer.hidden = !LA.S.drawerOpen; LA.$('#btnDrawer').classList.toggle('on', LA.S.drawerOpen);
    try { localStorage.setItem('la.drawer', LA.S.drawerOpen ? '1' : '0'); } catch (e) {}
    if (LA.S.drawerOpen) renderDrawer();
  };
  function renderDrawer() {
    LA.clear(drawer);
    var tabs = el('div', { class: 'dr-tabs' });
    [['files', '文件'], ['changes', '变更'], ['tasks', '任务']].forEach(function (t) {
      tabs.appendChild(el('button', { class: 'dr-tab' + (dtab === t[0] ? ' on' : ''), text: t[1], on: { click: function () { dtab = t[0]; previewing = null; renderDrawer(); } } }));
    });
    tabs.appendChild(el('span', { style: { flex: 1 } }));
    tabs.appendChild(iconBtn('refresh', '刷新', function () { renderDrawer(); }));
    tabs.appendChild(iconBtn('x', '关闭面板', function () { P.toggleDrawer(false); }));
    drawBody = el('div', { class: 'dr-body' });
    drawer.append(tabs, drawBody);
    if (dtab === 'files') (previewing ? drawPreview(previewing) : drawFiles());
    else if (dtab === 'changes') drawChanges(); else drawTasks();
  }

  function drawFiles() {
    var ws = curWs();
    if (!ws) { drawBody.appendChild(el('div', { class: 'dr-empty' }, el('p', { text: '还没有选择工作区' }), btn('选择工作区', P.pickWorkspace, 'primary'))); return; }
    drawBody.appendChild(el('div', { class: 'dr-sec' }, el('span', { text: ws, title: ws, style: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 } }), iconBtn('folder', '更换工作区', P.pickWorkspace)));
    var root = el('div'); drawBody.appendChild(root);
    loadDir('', root, 0);
  }
  function loadDir(rel, box, depth) {
    LA.get('/api/workspace/tree?path=' + encodeURIComponent(rel) + (sid() ? '&session_id=' + encodeURIComponent(sid()) : '')).then(function (r) {
      LA.clear(box);
      var es = (r.entries || []).slice().sort(function (a, b) { return (a.type === b.type ? 0 : a.type === 'dir' ? -1 : 1) || a.name.localeCompare(b.name); });
      if (!es.length && depth === 0) box.appendChild(el('div', { class: 'dr-empty', text: '工作区是空的。让 Agent 创建点什么吧。' }));
      es.forEach(function (e) {
        var p = rel ? rel + '/' + e.name : e.name, isDir = e.type === 'dir', open = !!expanded[p];
        var row = el('div', { class: 'tree-row' + (open ? ' open' : ''), style: { paddingLeft: (6 + depth * 14) + 'px' }, tabIndex: 0, title: p },
          el('span', { class: 'chev', html: isDir ? icon('chev', 12) : '' }), el('span', { html: icon(isDir ? 'folder' : 'file', 15) }), el('span', { class: 'nm', text: e.name }), isDir ? null : el('span', { class: 'sz', text: LA.fmtSize(e.size) }));
        var kids = el('div');
        function act() {
          if (isDir) { expanded[p] = !expanded[p]; row.classList.toggle('open', expanded[p]); if (expanded[p]) loadDir(p, kids, depth + 1); else LA.clear(kids); }
          else { previewing = p; renderDrawer(); }
        }
        row.addEventListener('click', act); row.addEventListener('keydown', function (ev) { if (ev.key === 'Enter') act(); });
        box.appendChild(row); box.appendChild(kids);
        if (isDir && open) loadDir(p, kids, depth + 1);
      });
    }).catch(function (e) { LA.clear(box); box.appendChild(el('div', { class: 'dr-empty', text: e.message })); });
  }
  function drawPreview(p) {
    var box = el('div', { class: 'preview' });
    drawBody.appendChild(box);
    var back = el('button', { class: 'icon-btn', title: '返回', 'aria-label': '返回', html: icon('chev', 15).replace('class="ic"', 'class="ic" style="transform:rotate(180deg)"'), on: { click: function () { previewing = null; renderDrawer(); } } });
    box.appendChild(el('div', { class: 'ph' }, back, el('b', { text: LA.baseName(p), style: { flex: 1, overflow: 'hidden', textOverflow: 'ellipsis' }, title: p })));
    var body = el('div', { text: '加载中…', class: 'hint2' }); box.appendChild(body);
    LA.get('/api/workspace/file?path=' + encodeURIComponent(p) + (sid() ? '&session_id=' + encodeURIComponent(sid()) : '')).then(function (r) {
      LA.clear(body); body.className = '';
      var bar = el('div', { class: 'toolbar' }, el('span', { class: 'hint2', text: LA.fmtSize(r.size) + (r.truncated ? ' · 仅显示开头部分' : '') }),
        btn('引用到对话', function () { LA.S.attachments.push({ kind: 'file', name: LA.baseName(p), path: p }); LA.composer.refresh(); LA.toast('已添加到输入框'); }, 'ghost'));
      if (r.kind === 'text' || r.kind === 'office') {
        bar.appendChild(btn('复制', function () { LA.copyText(r.content || ''); }, 'ghost'));
        body.appendChild(bar); body.appendChild(el('pre', { text: r.content || '（空文件）' }));
      } else if (r.kind === 'image' && r.data_url) { body.appendChild(bar); body.appendChild(el('img', { src: r.data_url, alt: LA.baseName(p) })); }
      else { body.appendChild(bar); body.appendChild(el('div', { class: 'dr-empty', text: '二进制文件，无法预览' })); }
    }).catch(function (e) { body.textContent = e.message; });
  }

  function drawChanges() {
    var jBox = el('div'), tBox = el('div');
    drawBody.append(el('div', { class: 'dr-sec' }, el('span', { text: '本会话的文件变更' })), jBox, el('div', { class: 'dr-sec' }, el('span', { text: '回收站' }),
      btn('清空', function () { LA.confirm('永久清空回收站？此操作无法撤销。', { danger: true, okText: '清空' }).then(function (ok) { if (ok) LA.del('/api/trash').then(drawChangesRefresh).catch(LA.fail); }); }, 'ghost')), tBox);
    function drawChangesRefresh() { LA.clear(drawBody); drawChanges(); }
    LA.get('/api/journal' + (sid() ? '?session_id=' + encodeURIComponent(sid()) : '')).then(function (l) {
      if (!l.length) jBox.appendChild(el('div', { class: 'dr-empty', text: '还没有变更记录' }));
      l.slice().reverse().forEach(function (j) {
        jBox.appendChild(el('div', { class: 'jr-item' + (j.undone ? ' undone' : ''), title: j.path },
          el('span', { class: 'badge', text: { write: '写入', edit: '编辑', delete: '删除', move: '移动', copy: '复制', mkdir: '新建夹', create: '创建' }[j.op] || j.op }),
          el('span', { class: 'tx', text: j.summary || j.path }), el('span', { class: 'tm', text: LA.fmtTime(j.ts) }),
          j.can_undo && !j.undone ? iconBtn('undo', '撤销', function () {
            LA.post('/api/journal/' + j.id + '/undo', {}).then(function () { LA.toast('已撤销', 'ok'); drawChangesRefresh(); }).catch(function (e) {
              if (e.status === 409) LA.confirm((e.message || '').replace(/^conflict:\s*/, '') + '\n\n仍要强制撤销吗？', { danger: true, okText: '强制撤销' }).then(function (ok) { if (ok) LA.post('/api/journal/' + j.id + '/undo', { force: true }).then(drawChangesRefresh).catch(LA.fail); });
              else LA.fail(e);
            });
          }) : null));
      });
    }).catch(function (e) { jBox.textContent = e.message; });
    LA.get('/api/trash').then(function (l) {
      if (!l.length) tBox.appendChild(el('div', { class: 'dr-empty', text: '回收站是空的' }));
      l.forEach(function (t) {
        tBox.appendChild(el('div', { class: 'jr-item', title: t.path }, el('span', { html: icon(t.is_dir ? 'folder' : 'file', 14) }), el('span', { class: 'tx', text: t.path }), el('span', { class: 'tm', text: LA.fmtSize(t.size) }),
          iconBtn('undo', '恢复', function () { LA.post('/api/trash/' + t.id + '/restore').then(function () { LA.toast('已恢复', 'ok'); drawChangesRefresh(); LA.bus.emit('fschanged'); }).catch(LA.fail); })));
      });
    }).catch(function (e) { tBox.textContent = e.message; });
  }

  function drawTasks() {
    var items = (LA.S.cur && LA.S.cur.todos) || [];
    if (!items.length) { drawBody.appendChild(el('div', { class: 'dr-empty', text: 'Agent 处理多步任务时，会在这里列出任务清单。' })); return; }
    var wrap = el('div', { class: 'todo-card', style: { margin: '8px' } }); wrap.appendChild(LA.chat.todoList(items)); drawBody.appendChild(wrap);
  }

  /* ================================================================ 技能 */
  P.openSkills = function () {
    var listBox = el('div'), m;
    var fileInp = el('input', { type: 'file', accept: '.zip', hidden: true });
    function load() {
      LA.refreshSkills().then(function (l) {
        LA.clear(listBox);
        if (!l.length) listBox.appendChild(el('div', { class: 'dr-empty', text: '还没有技能' }));
        l.forEach(function (s) {
          var c = el('input', { type: 'checkbox' }); c.checked = s.enabled;
          c.addEventListener('change', function () {
            var go = function () { return LA.post('/api/skills/' + encodeURIComponent(s.name) + '/enable', { enabled: c.checked }).then(load).catch(function (e) { c.checked = !c.checked; LA.fail(e); }); };
            if (c.checked && s.warnings && s.warnings.length) LA.confirm('此技能有以下安全提示：\n\n• ' + s.warnings.join('\n• ') + '\n\n技能内容会进入模型上下文，其中的脚本可能被 Agent 执行（仍需你批准）。确认启用？', { okText: '启用' }).then(function (ok) { if (ok) go(); else c.checked = false; });
            else go();
          });
          listBox.appendChild(el('div', { class: 'list-row' }, c,
            el('div', { class: 'main' }, el('b', { text: s.name }), el('span', { class: 'badge ' + (s.source === 'builtin' ? '' : 'user'), text: s.source === 'builtin' ? '内置' : '自定义' }),
              el('p', { text: s.description || '（无描述）' }),
              s.warnings && s.warnings.length ? el('div', { class: 'warns', text: '⚠ ' + s.warnings.join('；') }) : null),
            iconBtn('eye', s.source === 'builtin' ? '查看' : '查看/编辑', function () { editSkill(s.name, load); }),
            s.source === 'user' ? iconBtn('trash', '删除', function () { LA.confirm('删除技能「' + s.name + '」及其文件？', { danger: true, okText: '删除' }).then(function (ok) { if (ok) LA.del('/api/skills/' + encodeURIComponent(s.name)).then(load).catch(LA.fail); }); }) : null));
        });
      }).catch(LA.fail);
    }
    fileInp.addEventListener('change', function () {
      var f = fileInp.files[0]; if (!f) return; var fd = new FormData(); fd.append('file', f, f.name); fileInp.value = '';
      LA.api('POST', '/api/skills/install', fd).then(function (s) { afterInstall(s); load(); }).catch(LA.fail);
    });
    function afterInstall(s) {
      LA.toast('已安装「' + s.name + '」，默认未启用——请检查内容后再启用', 'ok', 4500);
      if (s.warnings && s.warnings.length) LA.toast('安全提示：' + s.warnings.join('；'), 'err', 7000);
    }
    m = LA.modal({ title: '技能', width: 640, onClose: function () { LA.refreshSkills(); },
      body: [el('div', { class: 'hint2', text: '技能是一份 SKILL.md 说明书（可附带脚本/资料）。启用后模型能看到名称与简介，需要时再按需读取全文；也可在输入框用 / 指定。新安装的技能默认不启用。' }),
        el('div', { class: 'toolbar', style: { marginTop: '10px' } },
          btn('安装 .zip', function () { fileInp.click(); }, 'primary'),
          btn('从文件夹安装', function () { LA.prompt('技能文件夹的绝对路径', '', { placeholder: '例如 /home/me/skills/pdf-tools' }).then(function (p) { if (p && p.trim()) LA.post('/api/skills/install', { path: p.trim() }).then(function (s) { afterInstall(s); load(); }).catch(LA.fail); }); }),
          btn('新建技能', function () { editSkill(null, load); }), fileInp), listBox] });
    load();
  };

  function editSkill(name, done) {
    var nm = el('input', { class: 'inp wide', placeholder: 'kebab-case 名称，如 meeting-notes', disabled: !!name });
    var desc = el('input', { class: 'inp wide', placeholder: '一句话说明何时使用（模型据此决定是否调用）' });
    var body = el('textarea', { class: 'inp', rows: 14, placeholder: '# 做法\n\n1. …', spellcheck: false });
    var info = el('div', { class: 'hint2' }), save = btn('保存', function () {}, 'primary'), m, readonly = false;
    function field(l, c) { return el('div', { class: 'field stack' }, el('label', { text: l }), c); }
    m = LA.modal({ title: name ? '技能：' + name : '新建技能', width: 680, body: [info, field('名称', nm), field('描述', desc), field('SKILL.md 正文', body)], footer: [btn('取消', function () { m.close(); }), save] });
    function go(s) { desc.value = s.description || ''; body.value = s.body || ''; readonly = s.source === 'builtin'; if (readonly) { desc.readOnly = true; body.readOnly = true; save.hidden = true; info.textContent = '内置技能只读。想修改请新建一个同功能的自定义技能。'; } else if (s.files && s.files.length > 1) info.textContent = '包含文件：' + s.files.join('、'); }
    if (name) { nm.value = name; LA.get('/api/skills/' + encodeURIComponent(name)).then(go).catch(LA.fail); }
    save.addEventListener('click', function () {
      save.disabled = true;
      var req = name ? LA.put('/api/skills/' + encodeURIComponent(name), { description: desc.value.trim(), body: body.value })
        : LA.post('/api/skills/create', { name: nm.value.trim(), description: desc.value.trim(), body: body.value });
      req.then(function () { m.close(); LA.toast('已保存', 'ok'); done(); }).catch(function (e) { save.disabled = false; LA.fail(e); });
    });
  }

  /* ================================================================ 记忆库 */
  var KINDS = { fact: '事实', preference: '偏好', project: '项目', episode: '经历' };
  P.openMemory = function () {
    var state = { q: '', kind: '', pending: false };
    var listBox = el('div'), q = el('input', { class: 'inp', placeholder: '搜索记忆…', style: { flex: 1, minWidth: '140px' } });
    var kindSel = el('select', { class: 'inp' }, el('option', { value: '', text: '全部类型' }));
    Object.keys(KINDS).forEach(function (k) { kindSel.appendChild(el('option', { value: k, text: KINDS[k] })); });
    var tabs = el('div', { class: 'tabs2' }), count = el('span', { class: 'hint2' });
    var fileInp = el('input', { type: 'file', accept: '.json,application/json', hidden: true });
    function drawTabs() {
      LA.clear(tabs);
      [[false, '全部'], [true, '待审核']].forEach(function (t) { tabs.appendChild(el('button', { class: state.pending === t[0] ? 'on' : '', text: t[1], on: { click: function () { state.pending = t[0]; drawTabs(); load(); } } })); });
    }
    function load() {
      var url = '/api/memory?q=' + encodeURIComponent(state.q) + '&kind=' + encodeURIComponent(state.kind) + '&pending=' + (state.pending ? 1 : 0);
      LA.get(url).then(function (r) {
        LA.clear(listBox); count.textContent = r.total + ' 条';
        if (!r.items.length) listBox.appendChild(el('div', { class: 'dr-empty', text: state.pending ? '没有待审核的记忆' : '记忆库是空的。你可以手动添加，或对 Agent 说“记住……”。' }));
        r.items.forEach(function (it) { listBox.appendChild(memRow(it)); });
      }).catch(LA.fail);
    }
    function memRow(it) {
      var meta = el('div', null, el('span', { class: 'badge', text: KINDS[it.kind] || it.kind }), el('span', { class: 'badge', text: it.scope === 'workspace' ? '当前工作区' : '全局' }),
        it.source === 'agent' ? el('span', { class: 'badge', text: 'Agent 写入' }) : null, !it.trusted ? el('span', { class: 'badge warn', text: '待审核' }) : null,
        it.pinned ? el('span', { class: 'badge moe', text: '置顶' }) : null, (it.tags || []).map(function (t) { return el('span', { class: 'tag', text: '#' + t }); }));
      return el('div', { class: 'list-row' }, el('div', { class: 'main' }, el('div', { class: 'mem-text', text: it.text }), meta),
        !it.trusted ? btn('批准', function () { LA.post('/api/memory/' + it.id + '/approve').then(load).catch(LA.fail); }, 'primary') : null,
        iconBtn('pin', it.pinned ? '取消置顶' : '置顶', function () { LA.patch('/api/memory/' + it.id, { pinned: !it.pinned }).then(load).catch(LA.fail); }),
        iconBtn('edit', '编辑', function () { editMem(it, load); }),
        iconBtn('trash', '删除', function () { LA.confirm('删除这条记忆？', { danger: true, okText: '删除' }).then(function (ok) { if (ok) LA.del('/api/memory/' + it.id).then(load).catch(LA.fail); }); }));
    }
    q.addEventListener('input', LA.debounce(function () { state.q = q.value.trim(); load(); }, 250));
    kindSel.addEventListener('change', function () { state.kind = kindSel.value; load(); });
    fileInp.addEventListener('change', function () {
      var f = fileInp.files[0]; if (!f) return; fileInp.value = '';
      f.text().then(function (t) { var o = JSON.parse(t); return LA.post('/api/memory/import', { items: Array.isArray(o) ? o : o.items }); })
        .then(function (r) { LA.toast('已导入 ' + (r.imported != null ? r.imported : '') + ' 条', 'ok'); load(); }).catch(function (e) { LA.toast('导入失败：' + e.message, 'err'); });
    });
    LA.modal({ title: '记忆库', width: 700, body: [
      el('div', { class: 'hint2', text: '跨对话保存的长期记忆，保存在本机 data/ 的 SQLite 中。相关记忆会在你提问时自动召回。Agent 在读过网页/文档后写入的记忆需要你批准才会生效。' }),
      el('div', { class: 'toolbar', style: { marginTop: '10px' } }, q, kindSel, btn('添加', function () { editMem(null, load); }, 'primary'),
        btn('导出', function () { LA.get('/api/memory/export').then(function (r) { LA.download('memory.json', JSON.stringify(r, null, 2), 'application/json'); }).catch(LA.fail); }, 'ghost'),
        btn('导入', function () { fileInp.click(); }, 'ghost'), fileInp),
      el('div', { style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between' } }, tabs, count), listBox] });
    drawTabs(); load();
  };

  function editMem(it, done) {
    var text = el('textarea', { class: 'inp', rows: 5, placeholder: '要记住的内容，一条一个要点' }); text.value = it ? it.text : '';
    var kind = el('select', { class: 'inp' }); Object.keys(KINDS).forEach(function (k) { kind.appendChild(el('option', { value: k, text: KINDS[k] })); }); kind.value = it ? it.kind : 'fact';
    var scope = el('select', { class: 'inp' }, el('option', { value: 'global', text: '全局' }), el('option', { value: 'workspace', text: '仅当前工作区' })); scope.value = it ? it.scope : 'global';
    var tags = el('input', { class: 'inp wide', placeholder: '标签，用逗号分隔（可选）', value: it ? (it.tags || []).join(', ') : '' });
    var pin = el('input', { type: 'checkbox' }); pin.checked = !!(it && it.pinned);
    var save = btn('保存', function () {}, 'primary'), m;
    function field(l, c) { return el('div', { class: 'field stack' }, el('label', { text: l }), c); }
    m = LA.modal({ title: it ? '编辑记忆' : '添加记忆', width: 520, body: [field('内容', text), el('div', { class: 'toolbar' }, kind, scope, el('label', { class: 'switch' }, pin, el('span', { text: '置顶（始终召回）' }))), field('标签', tags)], footer: [btn('取消', function () { m.close(); }), save] });
    save.addEventListener('click', function () {
      var body = { text: text.value.trim(), kind: kind.value, scope: scope.value, pinned: pin.checked, tags: tags.value.split(/[,，]/).map(function (s) { return s.trim(); }).filter(Boolean) };
      if (!body.text) return LA.toast('内容不能为空');
      save.disabled = true;
      (it ? LA.patch('/api/memory/' + it.id, body) : LA.post('/api/memory', body)).then(function () { m.close(); done(); }).catch(function (e) { save.disabled = false; LA.fail(e); });
    });
  }
})();
