/* 入口：初始化各模块、顶栏、全局状态轮询、主题、快捷键、会话恢复。 */
(function () {
  'use strict';
  var LA = window.LA, icon = LA.icon;
  var appEl;

  /* ------------------------------------------------------------ 外观 */
  function applyUi() {
    var theme = LA.cfg('ui.theme', 'auto'), fs = +LA.cfg('ui.font_size', 15) || 15;
    document.documentElement.dataset.theme = ['auto', 'light', 'dark'].indexOf(theme) >= 0 ? theme : 'auto';
    document.documentElement.style.setProperty('--fs', Math.max(12, Math.min(20, fs)) + 'px');
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
    LA.$('#engText').textContent = e.status === 'ready' ? shortModel(e.model) + (e.vision ? ' · 视觉' : '') : ({ stopped: '未加载', starting: '加载中…', stopping: '卸载中…', error: '引擎出错' })[e.status] || e.status;
    LA.$('#chipEngine').title = e.status === 'ready' ? '已加载 ' + e.model + '，ctx ' + e.ctx + (e.tps ? '，' + e.tps + ' tok/s' : '') + '。点击打开设置' : '模型与引擎设置';
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
    LA.$('#btnDrawer').title = '工作区面板（文件 / 变更 / 任务）';
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
      pollTimer = setTimeout(poll, s === 'starting' || s === 'stopping' ? 3000 : 10000);
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
  function connectEvents() {
    LA.sse('/api/events', {
      engine: function (e) { if (LA.S.state) { LA.S.state.engine = e; LA.bus.emit('state', LA.S.state); } },
      notice: function (n) { LA.toast(n.text, n.level === 'warn' ? 'err' : null, 4500); }
    }, { onerror: function (final) { if (final) setTimeout(connectEvents, 5000); } });
  }

  /* ------------------------------------------------------------ 快捷键 */
  function wireKeys() {
    document.addEventListener('keydown', function (e) {
      var mod = e.ctrlKey || e.metaKey;
      if (mod && e.shiftKey && (e.key === 'O' || e.key === 'o')) { e.preventDefault(); LA.newSession(); }
      else if (mod && !e.shiftKey && (e.key === 'b' || e.key === 'B')) { e.preventDefault(); toggleSide(); }
      else if (e.key === 'Escape' && !e.defaultPrevented) { if (LA.closeTopModal()) e.preventDefault(); else appEl.classList.remove('menu-open'); }
    });
    window.addEventListener('beforeunload', function () { if (LA.flushConfig) LA.flushConfig(); });
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
    try { if (localStorage.getItem('la.collapsed') === '1') appEl.classList.add('collapsed'); } catch (e) {}
    LA.chat.init(); LA.composer.init(); LA.sidebar.init(); LA.panels.initDrawer(); wireTop(); wireKeys();
    ['state', 'session', 'sessionmeta', 'context', 'runstate', 'workspace'].forEach(function (n) { LA.bus.on(n, updateTop); });
    LA.bus.on('state', watchEngine);
    LA.bus.on('config', applyUi);
    LA.bus.on('context', function (c) { if (LA.S.cur) LA.S.cur.context = c; updateTop(); });

    Promise.all([LA.refreshConfig(), LA.refreshState(), LA.refreshSessions(), LA.refreshSkills()]).then(function () {
      applyUi(); lastStatus = LA.engine().status;
      var last = ''; try { last = localStorage.getItem('la.lastSession') || ''; } catch (e) {}
      var has = last && LA.S.sessions.some(function (s) { return s.id === last; });
      var p = has ? LA.openSession(last) : (LA.setSession(null), Promise.resolve());
      return p.then(function () {
        try { if (localStorage.getItem('la.drawer') === '1' && window.innerWidth > 860) LA.panels.toggleDrawer(true); } catch (e) {}
        updateTop(); poll(); connectEvents();
        // 引擎未加载且有模型：给出明确引导（不自动加载，避免意外占满显存）
      });
    }).catch(function (e) { if (e && e.message !== 'unauthorized') { LA.toast('无法连接到后端：' + e.message, 'err', 8000); } });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
