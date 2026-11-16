/* 入口：初始化各模块、顶栏、全局状态轮询、主题、快捷键、会话恢复。 */
(function () {
  'use strict';
  var LA = window.LA, icon = LA.icon;
  var appEl;

  /* ------------------------------------------------------------ 外观与布局持久化 */
  var THEMES = ['auto', 'light', 'dark', 'paper', 'midnight', 'hc', 'colorblind'];
  var MOTIONS = ['auto', 'full', 'reduced', 'off'];

  LA.viewTransition = function (fn) {
    var m = document.documentElement.dataset.motion || 'auto';
    var sysReduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (document.startViewTransition && m !== 'off' && m !== 'reduced' && !(m === 'auto' && sysReduced)) {
      try {
        var vt = document.startViewTransition(fn);
        if (vt && vt.ready && vt.ready.catch) vt.ready.catch(function () {});
        if (vt && vt.finished && vt.finished.catch) vt.finished.catch(function () {});
        return vt;
      } catch (e) {}
    }
    fn();
  };

  function applyUi() {
    var root = document.documentElement;
    var theme = LA.cfg('ui.theme', 'auto');
    var targetTheme = THEMES.indexOf(theme) >= 0 ? theme : 'auto';
    var fs = +LA.cfg('ui.font_size', 15) || 15;
    var motion = LA.cfg('ui.motion', 'auto');
    var density = LA.cfg('ui.density', 'comfortable');
    var lig = !!LA.cfg('ui.code_ligatures', false);

    var apply = function () {
      root.dataset.theme = targetTheme;
      root.dataset.motion = MOTIONS.indexOf(motion) >= 0 ? motion : 'auto';
      root.dataset.density = density === 'compact' ? 'compact' : 'comfortable';
      root.dataset.ligatures = lig ? '1' : '0';
      root.style.setProperty('--fs', Math.max(12, Math.min(22, fs)) + 'px');
      try {
        var acc = localStorage.getItem('la.accent');
        if (acc) root.style.setProperty('--accent', acc);
        else root.style.removeProperty('--accent');

        var cw = localStorage.getItem('la.colWidth') || 'auto';
        root.dataset.colWidth = cw;
        if (cw && cw !== 'auto') root.style.setProperty('--col-w', cw);
        else root.style.removeProperty('--col-w');

        var sw = parseInt(localStorage.getItem('la.sideW') || '', 10);
        if (sw >= 220 && sw <= 360) root.style.setProperty('--side-w', sw + 'px');
        var dw = parseInt(localStorage.getItem('la.drawerW') || '', 10);
        if (dw >= 280 && dw <= 520) root.style.setProperty('--drawer-w', dw + 'px');
      } catch (e) {}
    };

    if (root.dataset.theme && root.dataset.theme !== targetTheme) LA.viewTransition(apply);
    else apply();
  }

  function wireSplitter(elId, varName, storeKey, minW, maxW, invert) {
    var sp = LA.$(elId); if (!sp) return;
    function setW(px) {
      var w = Math.max(minW, Math.min(maxW, Math.round(px)));
      document.documentElement.style.setProperty(varName, w + 'px');
      try { localStorage.setItem(storeKey, String(w)); } catch (e) {}
    }
    sp.addEventListener('mousedown', function (e) {
      e.preventDefault();
      var startX = e.clientX;
      var curW = parseInt(getComputedStyle(document.documentElement).getPropertyValue(varName), 10) || minW;
      sp.classList.add('dragging');
      function onMove(ev) { setW(curW + (invert ? (startX - ev.clientX) : (ev.clientX - startX))); }
      function onUp() {
        sp.classList.remove('dragging');
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
      }
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
    sp.addEventListener('keydown', function (e) {
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
      e.preventDefault();
      var curW = parseInt(getComputedStyle(document.documentElement).getPropertyValue(varName), 10) || minW;
      var delta = e.key === 'ArrowRight' ? 12 : -12;
      setW(curW + (invert ? -delta : delta));
    });
  }

  function toggleFocus(force) {
    if (!appEl) return;
    var on = force != null ? !!force : !appEl.classList.contains('focus-mode');
    appEl.classList.toggle('focus-mode', on);
    var bf = LA.$('#btnFocus'); if (bf) bf.classList.toggle('on', on);
    try { localStorage.setItem('la.focus', on ? '1' : '0'); } catch (e) {}
  }

  /* ------------------------------------------------------------ 顶栏 */
  function shortModel(n) { return String(n || '').replace(/\.gguf$/i, ''); }
  function updateTop() {
    var st = LA.S.state, cur = LA.S.cur;
    // 标题
    var ti = LA.$('#titleInput');
    if (document.activeElement !== ti) ti.value = cur ? (cur.title === '新对话' ? '' : cur.title) : '';
    ti.disabled = !cur; ti.placeholder = '新对话';
    // 工作区
    var ws = (cur && cur.workspace) || (st && st.workspace) || '';
    LA.$('#wsName').textContent = ws ? LA.baseName(ws) : '未选择工作区';
    LA.$('#chipWs').title = ws || '选择工作区'; LA.$('#chipWs').classList.toggle('warn', !ws);
    // 引擎
    var e = LA.engine();
    LA.$('#engDot').className = 'dot ' + e.status;
    var perf = e.perf || {};
    var perfStr = (e.tps ? '，' + e.tps + ' tok/s' : '') + (perf.prompt_tps ? '（预填充 ' + perf.prompt_tps + ' tok/s）' : '') + (perf.spec_accept_rate ? '，投机采纳 ' + Math.round(perf.spec_accept_rate * 100) + '%' : '');
    LA.$('#engText').textContent = e.status === 'ready' ? shortModel(e.model) + (e.vision ? ' · 视觉' : '') + (e.tps ? ' · ' + e.tps + 't/s' : '') : ({ stopped: '未加载', starting: '加载中…', stopping: '卸载中…', error: '引擎出错' })[e.status] || e.status;
    LA.$('#chipEngine').title = e.status === 'ready' ? '已加载 ' + e.model + '，ctx ' + e.ctx + perfStr + '。点击打开设置' : '模型与引擎设置';
    // 上下文
    var c = cur && cur.context ? cur.context : { ctx: e.ctx || 0, used: 0, pct: 0 };
    var pct = Math.max(0, Math.min(100, +c.pct || 0));
    var fg = LA.$('#ringFg'); fg.style.strokeDashoffset = String(94.25 * (1 - pct / 100)); fg.classList.toggle('hot', pct >= 85);
    LA.$('#ctxText').textContent = Math.round(pct) + '%';
    LA.$('#ctxMeter').title = c.ctx ? '上下文 ' + LA.fmtN(c.used) + ' / ' + LA.fmtN(c.ctx) + ' tokens' + (c.summarized ? '（含较早对话的摘要）' : '') : '模型加载后显示上下文用量';
    LA.$('#btnCompact').disabled = !cur || !!LA.live || !(cur.messages && cur.messages.length);
  }

  function wireTop() {
    LA.$('#btnCollapse').addEventListener('click', toggleSide); LA.$('#btnExpand').addEventListener('click', toggleSide);
    LA.$('#btnMenu').addEventListener('click', function () { appEl.classList.toggle('menu-open'); });
    LA.$('#scrim').addEventListener('click', function () { appEl.classList.remove('menu-open'); });
    LA.$('#wsIcon').innerHTML = icon('folder', 15);
    LA.$('#chipWs').addEventListener('click', function () { LA.panels.pickWorkspace(); });
    LA.$('#chipEngine').addEventListener('click', function () { LA.settings.open('engine'); });
    var ti = LA.$('#titleInput'), orig = '';
    ti.addEventListener('focus', function () { orig = ti.value; });
    ti.addEventListener('keydown', function (e) { if (e.key === 'Enter' && !e.isComposing) ti.blur(); if (e.key === 'Escape') { ti.value = orig; ti.blur(); } });
    ti.addEventListener('blur', function () {
      var v = ti.value.trim(); if (!LA.S.cur || !v || v === orig) { updateTop(); return; }
      LA.patch('/api/sessions/' + LA.S.cur.id, { title: v }).then(function () { LA.S.cur.title = v; LA.refreshSessions(); }).catch(LA.fail);
    });
    LA.$('#btnCompact').addEventListener('click', function () {
      var cur = LA.S.cur; if (!cur || LA.live) return;
      var b = LA.$('#btnCompact'); b.disabled = true; b.textContent = '压缩中…';
      LA.post('/api/sessions/' + cur.id + '/compact').then(function () {
        LA.toast('较早的对话已压缩成摘要', 'ok');
        return LA.get('/api/sessions/' + cur.id).then(function (v) { if (LA.S.cur && LA.S.cur.id === cur.id) { LA.S.cur.context = v.context; updateTop(); } });
      }).catch(LA.fail).then(function () { b.textContent = '压缩'; updateTop(); });
    });
    LA.$('#btnDrawer').title = LA.t('top.drawer', '工作区面板（文件 / 变更 / 任务 / 集群）');
    var bf = LA.$('#btnFocus');
    if (bf) {
      bf.innerHTML = icon('focus', 17);
      bf.title = LA.t('top.focus', '专注模式 (Ctrl+Shift+F)');
      bf.addEventListener('click', function () { toggleFocus(); });
    }
    wireSplitter('#sideSplitter', '--side-w', 'la.sideW', 220, 360, false);
    wireSplitter('#drawerSplitter', '--drawer-w', 'la.drawerW', 280, 520, true);
  }
  function toggleSide() {
    appEl.classList.toggle('collapsed');
    try { localStorage.setItem('la.collapsed', appEl.classList.contains('collapsed') ? '1' : '0'); } catch (e) {}
  }

  /* ------------------------------------------------------------ 状态同步 */
  var pollTimer = null, lastStatus = null;
  function poll() {
    clearTimeout(pollTimer);
    LA.refreshState().catch(function () {}).then(function () {
      var s = LA.engine().status;
      pollTimer = setTimeout(poll, s === 'starting' || s === 'stopping' ? 3000 : 30000);
    });
  }
  function watchEngine() {
    var s = LA.engine().status;
    if (s !== lastStatus) {
      if (s === 'starting') { LA.S.restartNeeded = false; LA.bus.emit('restartNeeded', false); }
      if (lastStatus === 'starting' && s === 'ready') LA.toast('模型已就绪', 'ok');
      if (lastStatus && s === 'error') LA.toast('引擎启动失败：' + (LA.engine().error || '见设置→日志'), 'err', 6000);
      lastStatus = s;
    }
  }
  var debRefreshMissions = LA.debounce(function () { if (LA.refreshMissions) LA.refreshMissions().catch(function () {}); }, 350);
  function connectEvents() {
    LA.sse('/api/events', {
      engine: function (e) { if (LA.S.state) { LA.S.state.engine = e; LA.bus.emit('state', LA.S.state); } },
      config: function (d) { if (d && d.config) { LA.S.config = d.config; LA.bus.emit('config', d.config); } },
      mcp: function (d) { LA.bus.emit('mcp', d); },
      hub: function (d) { LA.bus.emit('hub', d); },
      state: function (d) {
        if (d && d.session_id && LA.S.cur && LA.S.cur.id === d.session_id && 'permission_mode' in d) {
          LA.S.cur.permission_mode = d.permission_mode || null;
          LA.bus.emit('sessionmeta', LA.S.cur);
        }
      },
      notice: function (n) { LA.toast(n.text, n.level === 'warn' ? 'err' : null, 4500); },
      mission: function () { debRefreshMissions(); },
      mission_started: function () { debRefreshMissions(); },
      milestone_done: function () { debRefreshMissions(); },
      mission_blocked: function () { debRefreshMissions(); LA.toast('Mission 有操作等待审批', 'err', 5000); },
      budget_warn: function () { debRefreshMissions(); LA.toast('Mission 预算已达 80%', null, 4500); },
      mission_paused: function () { debRefreshMissions(); },
      mission_resumed: function () { debRefreshMissions(); },
      mission_done: function (d) { debRefreshMissions(); LA.toast('Mission 已结束 (' + ((d && d.status) || '') + ')', d && d.status === 'completed' ? 'ok' : 'err', 5000); },
      mission_rollback: function () { debRefreshMissions(); },
      artifact_created: function (d) { LA.bus.emit('artifact', d); }
    }, { onerror: function (final) { if (final) setTimeout(connectEvents, 5000); } });
  }

  /* ------------------------------------------------------------ 全局命令面板 (Ctrl/Cmd+K) */
  function openPalette() {
    var inp = LA.el('input', { class: 'inp wide', placeholder: '搜索文件、会话、斜杠命令或快捷操作…', spellcheck: false });
    var listEl = LA.el('div', { class: 'pal-list' });
    var sel = 0, visible = [], filesLoaded = false, wsFiles = [];
    var m = LA.modal({ title: '命令面板 (Ctrl+K)', width: 560, body: LA.el('div', { class: 'pal-body' }, inp, listEl), focus: false });

    function baseItems() {
      var items = [
        { kind: 'act', tag: '操作', title: '新建对话', sub: 'Ctrl+Shift+O', run: function () { LA.newSession(); } },
        { kind: 'act', tag: '面板', title: '打开文件抽屉', sub: '查看工作区文件', run: function () { LA.panels.toggleDrawer(true); } },
        { kind: 'act', tag: '产物', title: '打开产物面板与导出', sub: '图表/表格/HTML 沙箱预览、打包与导出', run: function () { if (LA.panels.openArtifact) LA.panels.openArtifact(); } },
        { kind: 'act', tag: '集群', title: '打开 Swarm 集群面板', sub: '查看多代理 DAG 与合并队列', run: function () { if (LA.panels.openSwarm) LA.panels.openSwarm(); } },
        { kind: 'act', tag: '任务', title: '新建或查看长期 Mission', sub: '超长期断点续跑 / MISSION.md / 回滚', run: function () { if (LA.panels.openMission) LA.panels.openMission(); } },
        { kind: 'act', tag: '调研', title: '打开联网调研面板', sub: '多源证据、引用核查与来源摘录', run: function () { if (LA.panels.openResearch) LA.panels.openResearch(); } },
        { kind: 'act', tag: '账本', title: '打开网络账本', sub: '出站请求审计与策略拦截明细', run: function () { if (LA.panels.openNet) LA.panels.openNet(); } },
        { kind: 'act', tag: '视图', title: '切换专注模式', sub: 'Ctrl+Shift+F 隐藏侧栏与检查器', run: function () { toggleFocus(); } },
        { kind: 'act', tag: 'Git', title: '查看 Git 变更与撤销日志', sub: '切换至变更抽屉', run: function () { LA.panels.openChanges(); } },
        { kind: 'act', tag: '设置', title: '打开设置', sub: '模型、引擎、生成与安全配置', run: function () { LA.openSettings(); } },
        { kind: 'act', tag: '记忆', title: '打开长期记忆库', sub: '检索与管理记忆条目', run: function () { LA.panels.openMemory(); } },
        { kind: 'act', tag: '技能', title: '打开技能管理', sub: '启用或创建本地技能', run: function () { LA.panels.openSkills(); } }
      ];
      ['ask', 'auto_edit', 'plan', 'auto'].forEach(function (md) {
        var names = { ask: '逐项确认', auto_edit: '自动编辑', plan: '只读规划', auto: '全自动' };
        items.push({ kind: 'mode', tag: '模式', title: '权限模式: ' + names[md], sub: '/mode ' + md, run: function () {
          if (LA.setSessionMode) LA.setSessionMode(md);
          else LA.cfgSet('agent.permission_mode', md, { immediate: true });
        } });
      });
      (LA.S.sessions || []).slice(0, 12).forEach(function (s) {
        items.push({ kind: 'sess', tag: '会话', title: s.title || '未命名会话', sub: s.workspace || '', run: function () { LA.openSession(s.id); } });
      });
      wsFiles.forEach(function (f) {
        items.push({ kind: 'file', tag: '文件', title: f.path || f.name, sub: '预览代码', run: function () { LA.panels.openFile(f.path || f.name); } });
      });
      return items;
    }

    function render() {
      var q = inp.value.trim().toLowerCase();
      var all = baseItems();
      visible = all.filter(function (it) {
        if (!q) return it.kind !== 'file';
        return it.title.toLowerCase().indexOf(q) >= 0 || (it.sub || '').toLowerCase().indexOf(q) >= 0 || it.tag.toLowerCase().indexOf(q) >= 0;
      }).slice(0, 14);
      if (sel >= visible.length) sel = Math.max(0, visible.length - 1);
      LA.clear(listEl);
      if (!visible.length) {
        listEl.appendChild(LA.el('div', { class: 'dr-empty', text: '没有匹配项' }));
        return;
      }
      visible.forEach(function (it, idx) {
        listEl.appendChild(LA.el('button', {
          class: 'pal-item' + (idx === sel ? ' sel' : ''),
          type: 'button',
          on: { click: function () { m.close(); it.run(); } }
        },
          LA.el('span', { class: 'pal-tag', text: it.tag }),
          LA.el('b', { text: it.title }),
          LA.el('small', { text: it.sub || '' })
        ));
      });
    }

    inp.addEventListener('input', function () {
      sel = 0;
      render();
      if (!filesLoaded && ((LA.S.cur && LA.S.cur.workspace) || (LA.S.state && LA.S.state.workspace))) {
        filesLoaded = true;
        var sid = LA.S.cur ? LA.S.cur.id : '';
        LA.get('/api/workspace/tree?recursive=1&path=&session_id=' + encodeURIComponent(sid)).then(function (r) {
          wsFiles = (r && r.entries) || [];
          render();
        }).catch(function () {});
      }
    });
    inp.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        if (visible.length) sel = (sel + (e.key === 'ArrowDown' ? 1 : visible.length - 1)) % visible.length;
        render();
      } else if (e.key === 'Enter' && !e.isComposing && visible[sel]) {
        e.preventDefault();
        var item = visible[sel];
        m.close();
        item.run();
      }
    });
    render();
    setTimeout(function () { inp.focus(); }, 20);
  }

  /* ------------------------------------------------------------ 快捷键 */
  function wireKeys() {
    document.addEventListener('keydown', function (e) {
      var mod = e.ctrlKey || e.metaKey;
      if (mod && !e.shiftKey && (e.key === 'k' || e.key === 'K')) { e.preventDefault(); if (!LA.hasModal()) openPalette(); }
      else if (mod && e.shiftKey && (e.key === 'O' || e.key === 'o')) { e.preventDefault(); LA.newSession(); }
      else if (mod && e.shiftKey && (e.key === 'F' || e.key === 'f')) { e.preventDefault(); toggleFocus(); }
      else if (mod && !e.shiftKey && (e.key === 'b' || e.key === 'B')) { e.preventDefault(); toggleSide(); }
      else if (e.key === 'Escape' && !e.defaultPrevented) { if (LA.closeTopModal()) e.preventDefault(); else appEl.classList.remove('menu-open'); }
    });
    window.addEventListener('beforeunload', function () {
      clearTimeout(pollTimer);
      if (LA.flushConfig) LA.flushConfig();
    });
    document.addEventListener('visibilitychange', function () { if (!document.hidden) LA.refreshState().catch(function () {}); });
    // 外链一律新标签页打开且不带 referrer
    document.addEventListener('click', function (e) {
      var a = e.target.closest && e.target.closest('a[href]');
      if (a && /^https?:/i.test(a.getAttribute('href')) && !a.target) { a.target = '_blank'; a.rel = 'noopener noreferrer nofollow'; }
    }, true);
  }

  /* ------------------------------------------------------------ 启动 */
  function boot() {
    appEl = document.getElementById('app');
    if (LA.loadIcons) LA.loadIcons();
    try { if (localStorage.getItem('la.collapsed') === '1') appEl.classList.add('collapsed'); } catch (e) {}
    try { if (localStorage.getItem('la.focus') === '1') toggleFocus(true); } catch (e) {}
    LA.chat.init(); LA.composer.init(); LA.sidebar.init(); LA.panels.initDrawer(); wireTop(); wireKeys();
    ['state', 'session', 'sessionmeta', 'context', 'runstate', 'workspace'].forEach(function (n) { LA.bus.on(n, updateTop); });
    LA.bus.on('state', watchEngine);
    LA.bus.on('config', applyUi);
    LA.bus.on('context', function (c) { if (LA.S.cur) LA.S.cur.context = c; updateTop(); });

    Promise.all([LA.refreshConfig(), LA.refreshState(), LA.refreshSessions(), LA.refreshSkills(), LA.refreshMissions()]).then(function () {
      applyUi(); lastStatus = LA.engine().status;
      var last = ''; try { last = localStorage.getItem('la.lastSession') || ''; } catch (e) {}
      var has = last && LA.S.sessions.some(function (s) { return s.id === last; });
      var p = has ? LA.openSession(last) : (LA.setSession(null), Promise.resolve());
      return p.then(function () {
        try { if (localStorage.getItem('la.drawer') === '1' && window.innerWidth > 860) LA.panels.toggleDrawer(true); } catch (e) {}
        updateTop();
        pollTimer = setTimeout(poll, LA.engine().status === 'starting' || LA.engine().status === 'stopping' ? 3000 : 30000);
        connectEvents();
        // 引擎未加载且有模型：给出明确引导（不自动加载，避免意外占满显存）
      });
    }).catch(function (e) { if (e && e.message !== 'unauthorized') { LA.toast('无法连接到后端：' + e.message, 'err', 8000); } });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
