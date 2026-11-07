/* API 层：fetch 封装、SSE、全局状态 LA.S、事件总线、配置自动保存。契约见 docs/API.md */
(function () {
  'use strict';
  var LA = window.LA = window.LA || {};

  LA.S = { state: null, config: null, sessions: [], cur: null, skills: [], drawerOpen: false, drawerTab: 'files', attachments: [], chosen: [], restartNeeded: false };

  /* ---------- 事件总线 ---------- */
  var subs = {};
  LA.bus = {
    on: function (n, f) { (subs[n] = subs[n] || []).push(f); },
    emit: function (n, d) { (subs[n] || []).forEach(function (f) { try { f(d); } catch (e) { console.error(e); } }); }
  };

  /* ---------- HTTP ---------- */
  var expired = false;
  LA.sessionExpired = function () {
    if (expired) return; expired = true;
    var b = LA.el('div', { class: 'blocker' }, LA.el('div', null, LA.el('h2', { text: '会话已失效' }), LA.el('p', { text: '请从终端打印的链接重新打开 LocalAgent。' }),
      LA.el('p', { style: { color: 'var(--muted)', fontSize: '.85em' }, text: '（每次启动都会生成新的访问令牌）' })));
    document.body.appendChild(b);
  };
  LA.api = function (method, path, body, opts) {
    var init = { method: method, credentials: 'same-origin', headers: {}, cache: 'no-store' };
    if (body instanceof FormData) init.body = body;
    else if (body !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(body); }
    return fetch(path, init).then(function (r) {
      if (r.status === 401) { LA.sessionExpired(); throw new Error('unauthorized'); }
      var ct = r.headers.get('content-type') || '';
      var p = ct.indexOf('json') >= 0 ? r.json() : r.text();
      return p.then(function (d) {
        if (!r.ok) { var e = new Error((d && d.detail) || ('HTTP ' + r.status)); e.status = r.status; e.data = d; throw e; }
        return d;
      });
    });
  };
  LA.get = function (p) { return LA.api('GET', p); };
  LA.post = function (p, b) { return LA.api('POST', p, b === undefined ? {} : b); };
  LA.patch = function (p, b) { return LA.api('PATCH', p, b); };
  LA.put = function (p, b) { return LA.api('PUT', p, b); };
  LA.del = function (p) { return LA.api('DELETE', p); };
  LA.fail = function (e) { if (e && e.message !== 'unauthorized') LA.toast(e.message || String(e), 'err'); };

  /* ---------- SSE ---------- */
  /** 返回 close()。handlers: {事件名: fn(data, rawEvent)}；opts.onerror(closedForever) */
  LA.sse = function (url, handlers, opts) {
    opts = opts || {};
    var es = new EventSource(url), closed = false, hadError = 0;
    Object.keys(handlers).forEach(function (name) {
      es.addEventListener(name, function (ev) {
        hadError = 0;
        var d = null; try { d = JSON.parse(ev.data); } catch (e) { return; }
        handlers[name](d, ev);
      });
    });
    es.onerror = function () {
      if (closed) return;
      hadError++;
      if (es.readyState === 2 || hadError > 6) { closed = true; es.close(); if (opts.onerror) opts.onerror(true); }
      else if (opts.onerror) opts.onerror(false);
    };
    return function () { closed = true; es.close(); };
  };

  /* ---------- 状态刷新 ---------- */
  LA.refreshState = function () {
    return LA.get('/api/state').then(function (s) { LA.S.state = s; LA.bus.emit('state', s); return s; });
  };
  LA.refreshConfig = function () {
    return LA.get('/api/config').then(function (r) { LA.S.config = r.config; LA.bus.emit('config', r.config); return r.config; });
  };
  LA.refreshSessions = function () {
    return LA.get('/api/sessions').then(function (l) { LA.S.sessions = l; LA.bus.emit('sessions', l); return l; });
  };
  LA.refreshSkills = function () {
    return LA.get('/api/skills').then(function (l) { LA.S.skills = l; return l; });
  };
  LA.engine = function () { return (LA.S.state && LA.S.state.engine) || { status: 'stopped' }; };
  LA.engineReady = function () { return LA.engine().status === 'ready'; };
  LA.cfg = function (path, dflt) {
    var c = LA.S.config; if (!c) return dflt;
    var p = path.split('.');
    for (var i = 0; i < p.length; i++) { if (c == null) return dflt; c = c[p[i]]; }
    return c === undefined ? dflt : c;
  };

  /* ---------- 配置：本地立即生效 + 防抖保存 ---------- */
  var pending = {}, saveTimer = null;
  function deepMerge(a, b) { for (var k in b) { if (b[k] && typeof b[k] === 'object' && !Array.isArray(b[k])) { a[k] = a[k] && typeof a[k] === 'object' ? a[k] : {}; deepMerge(a[k], b[k]); } else a[k] = b[k]; } return a; }
  function nest(path, v) { var o = {}, p = path.split('.'), c = o; for (var i = 0; i < p.length - 1; i++) { c[p[i]] = {}; c = c[p[i]]; } c[p[p.length - 1]] = v; return o; }
  var ENGINE_KEYS = /^engine\./;
  LA.cfgSet = function (path, value, opts) {
    opts = opts || {};
    var part = nest(path, value);
    deepMerge(pending, part);
    if (LA.S.config) deepMerge(LA.S.config, part);
    if (ENGINE_KEYS.test(path) && LA.engine().status === 'ready' && !opts.noRestartHint) { LA.S.restartNeeded = true; LA.bus.emit('restartNeeded', true); }
    clearTimeout(saveTimer);
    saveTimer = setTimeout(LA.flushConfig, opts.immediate ? 0 : 450);
    LA.bus.emit('config', LA.S.config);
  };
  LA.flushConfig = function () {
    clearTimeout(saveTimer);
    var body = pending; pending = {};
    if (!Object.keys(body).length) return Promise.resolve();
    return LA.patch('/api/config', body).then(function (r) {
      LA.S.config = r.config; LA.bus.emit('config', r.config); LA.bus.emit('saved', true);
    }).catch(function (e) { LA.fail(e); return LA.refreshConfig(); });
  };
})();
