/* 侧栏：会话列表（置顶/按时间分组）、新建/切换/重命名/置顶/删除；会话搜索与切换的统一入口。 */
(function () {
  'use strict';
  var LA = window.LA, el = LA.el, icon = LA.icon;
  var listEl, searchInp, searchClear, filterQuery = '';
  var searchHits = null, searchTimer = null, searchSeq = 0;

  /* ------------------------------------------------------------ 会话切换 */
  LA.setSession = function (view, opts) {
    opts = opts || {};
    LA.chat.detach();
    LA.S.cur = view;
    if (view) { view.messages = view.messages || []; }
    if (!opts.noRender) {
      if (view) LA.chat.renderSession(view); else LA.chat.renderEmpty();
    }
    try { localStorage.setItem('la.lastSession', view ? view.id : ''); } catch (e) {}
    LA.bus.emit('session', view);
    renderList();
    if (view && view.active_run && !opts.noRender) LA.chat.startRun(view.active_run, view.id);
    var app = document.getElementById('app');
    if (app) app.classList.remove('menu-open');
  };

  LA.openSession = function (id) {
    var app = document.getElementById('app');
    if (LA.S.cur && LA.S.cur.id === id) {
      if (app) app.classList.remove('menu-open');
      return Promise.resolve();
    }
    return LA.get('/api/sessions/' + encodeURIComponent(id)).then(function (v) {
      LA.setSession(v);
    }).catch(function (e) {
      LA.fail(e); LA.refreshSessions();
    });
  };

  LA.newSession = function () {
    LA.setSession(null);
    LA.composer.setText(''); LA.S.attachments = []; LA.S.chosen = []; LA.composer.refresh();
    LA.composer.focus();
  };

  /** 发送/上传前确保有会话（懒创建，避免产生空会话）。 */
  LA.ensureSession = function () {
    if (LA.S.cur) return Promise.resolve(LA.S.cur);
    if (LA._creating) return LA._creating;
    LA._creating = LA.post('/api/sessions', {}).then(function (v) {
      LA._creating = null; v.messages = v.messages || [];
      LA.setSession(v, { noRender: true });
      return v;
    }, function (e) { LA._creating = null; throw e; });
    return LA._creating;
  };

  /* ------------------------------------------------------------ 列表渲染 */
  function groupOf(s, now) {
    if (s.pinned) return 0;
    var d0 = new Date(now); d0.setHours(0, 0, 0, 0);
    var t = d0.getTime() / 1000, u = s.updated;
    if (u >= t) return 1; if (u >= t - 86400) return 2; if (u >= t - 7 * 86400) return 3; return 4;
  }
  var GROUPS = ['已置顶', '今天', '昨天', '过去 7 天', '更早'];

  function renderList() {
    if (!listEl) return;
    var keepScroll = listEl.scrollTop;
    LA.clear(listEl);
    var ss = LA.S.sessions || [];

    if (filterQuery) {
      if (searchHits !== null) {
        ss = ss.filter(function (s) { return Object.prototype.hasOwnProperty.call(searchHits, s.id); });
      } else {
        ss = ss.filter(function (s) {
          return (s.title || '新对话').toLowerCase().indexOf(filterQuery) >= 0;
        });
      }
    }

    if (!ss.length) {
      var emptyMsg = filterQuery ? '未找到相关对话' : '还没有对话';
      listEl.appendChild(el('div', { class: 'sb-empty', text: emptyMsg }));
      return;
    }

    var now = Date.now(), buckets = [[], [], [], [], []];
    ss.forEach(function (s) { buckets[groupOf(s, now)].push(s); });
    buckets.forEach(function (b, gi) {
      if (!b.length) return;
      listEl.appendChild(el('div', { class: 'sb-group', text: GROUPS[gi] }));
      b.forEach(function (s) { listEl.appendChild(item(s)); });
    });
    listEl.scrollTop = keepScroll;
  }

  function item(s) {
    var active = LA.S.cur && LA.S.cur.id === s.id;
    var running = LA.live && LA.live.sid === s.id;
    var row = el('div', {
      class: 'sb-item' + (active ? ' active' : ''),
      tabIndex: 0,
      role: 'button',
      title: s.title || '新对话',
      on: {
        click: function () { LA.openSession(s.id); },
        keydown: function (e) {
          if (e.target === row && (e.key === 'Enter' || e.key === ' ')) {
            e.preventDefault();
            LA.openSession(s.id);
          }
        }
      }
    });

    if (s.pinned) row.appendChild(el('span', { class: 'pin', html: icon('pin', 14) }));
    var snip = (searchHits && searchHits[s.id] && filterQuery) ? searchHits[s.id] : '';
    var titleWrap = el('span', { class: 'sb-twrap' },
      el('span', { class: 't', text: s.title || '新对话' }),
      snip && snip !== (s.title || '新对话') ? el('span', { class: 'sb-snip', text: snip }) : null);
    row.appendChild(titleWrap);
    if (running) row.appendChild(el('span', { class: 'run-dot', title: '运行中' }));

    function act(ic, title, fn) {
      return el('button', {
        class: 'icon-btn',
        type: 'button',
        title: title,
        'aria-label': title,
        html: icon(ic, 14),
        on: {
          click: function (e) {
            e.stopPropagation();
            fn();
          }
        }
      });
    }

    row.appendChild(el('span', { class: 'acts' },
      act('edit', '重命名', function () {
        LA.prompt('重命名对话', s.title).then(function (v) {
          if (v == null || !v.trim()) return;
          LA.patch('/api/sessions/' + s.id, { title: v.trim() }).then(function () {
            if (LA.S.cur && LA.S.cur.id === s.id) {
              LA.S.cur.title = v.trim();
              LA.bus.emit('sessionmeta', LA.S.cur);
            }
            LA.refreshSessions();
          }).catch(LA.fail);
        });
      }),
      act('pin', s.pinned ? '取消置顶' : '置顶', function () {
        LA.patch('/api/sessions/' + s.id, { pinned: !s.pinned }).then(LA.refreshSessions).catch(LA.fail);
      }),
      act('trash', '删除', function () {
        LA.confirm('删除对话「' + (s.title || '新对话') + '」？\n（不会删除工作区里的文件）', { danger: true, okText: '删除' }).then(function (ok) {
          if (!ok) return;
          LA.del('/api/sessions/' + s.id).then(function () {
            var was = LA.S.cur && LA.S.cur.id === s.id;
            return LA.refreshSessions().then(function () { if (was) LA.newSession(); });
          }).catch(LA.fail);
        });
      })
    ));

    return row;
  }

  LA.sidebar = {
    init: function () {
      listEl = LA.$('#sessionList');
      searchInp = LA.$('#sbSearchInput');
      searchClear = LA.$('#sbSearchClear');

      var searchIcon = LA.$('#sbSearchIcon');
      if (searchIcon) searchIcon.innerHTML = icon('search', 14);

      if (searchInp) {
        searchInp.addEventListener('input', function () {
          filterQuery = searchInp.value.trim().toLowerCase();
          if (searchClear) searchClear.hidden = !filterQuery;
          if (searchTimer) { clearTimeout(searchTimer); searchTimer = null; }
          if (!filterQuery || filterQuery.length < 2) {
            searchHits = null;
            renderList();
            return;
          }
          renderList();
          var mySeq = ++searchSeq, qNow = filterQuery;
          searchTimer = setTimeout(function () {
            LA.get('/api/sessions/search?q=' + encodeURIComponent(qNow)).then(function (rows) {
              if (mySeq !== searchSeq || filterQuery !== qNow) return;
              var map = {};
              (rows || []).forEach(function (r) { map[r.id] = r.snippet || ''; });
              searchHits = map;
              renderList();
            }).catch(function () {});
          }, 180);
        });
      }
      if (searchClear) {
        searchClear.addEventListener('click', function () {
          if (searchInp) { searchInp.value = ''; searchInp.focus(); }
          if (searchTimer) { clearTimeout(searchTimer); searchTimer = null; }
          searchSeq++;
          filterQuery = '';
          searchHits = null;
          searchClear.hidden = true;
          renderList();
        });
      }

      var pers = LA.getPersonalization ? LA.getPersonalization() : { assistantName: 'LocalAgent' };
      var bn = LA.$('.brand-name');
      if (bn) bn.textContent = pers.assistantName;
      LA.$('#brandGlyph').innerHTML = LA.appIconHtml ? LA.appIconHtml(30, 'brand-icon-img') : LA.SPARK;
      LA.$('#btnCollapse').innerHTML = icon('sidebar', 18);
      LA.$('#btnExpand').innerHTML = icon('sidebar', 18);
      LA.$('#btnMenu').innerHTML = icon('menu', 20);
      LA.$('#newIcon').innerHTML = icon('plus', 17);

      var fis = LA.$$('.foot-btn .fi');
      if (fis[0]) fis[0].innerHTML = icon('puzzle', 17);
      if (fis[1]) fis[1].innerHTML = icon('brain', 17);
      if (fis[2]) fis[2].innerHTML = icon('settings', 17);

      LA.$('#btnNew').addEventListener('click', function () {
        LA.newSession();
        var app = document.getElementById('app');
        if (app) app.classList.remove('menu-open');
      });
      LA.$('#btnSkills').addEventListener('click', function () { LA.panels.openSkills(); });
      LA.$('#btnMemory').addEventListener('click', function () { LA.panels.openMemory(); });
      LA.$('#btnSettings').addEventListener('click', function () { LA.settings.open(); });

      LA.bus.on('sessions', renderList);
      LA.bus.on('missions', function () {
        if (LA.missionPanel && LA.missionPanel.renderSidebar) LA.missionPanel.renderSidebar(LA.$('#missionList'));
      });
      if (LA.missionPanel && LA.missionPanel.renderSidebar) LA.missionPanel.renderSidebar(LA.$('#missionList'));
      LA.bus.on('runstate', renderList);
      LA.bus.on('sessionmeta', function () { renderList(); });
    }
  };
})();
