/* LocalAgent UI — 基础工具。全局命名空间 window.LA；无外部依赖。 */
(function () {
  'use strict';
  var LA = window.LA = window.LA || {};

  LA.$ = function (s, r) { return (r || document).querySelector(s); };
  LA.$$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };

  LA.esc = function (s) {
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  };

  /** el('div', {class:'x', on:{click:fn}, text:'..', html:'(仅用于受信的图标 svg)'}, child1, 'text', ...) */
  LA.el = function (tag, attrs) {
    var n = document.createElement(tag);
    if (attrs) {
      for (var k in attrs) {
        if (!Object.prototype.hasOwnProperty.call(attrs, k)) continue;
        var v = attrs[k];
        if (v == null || v === false) continue;
        if (k === 'class') n.className = v;
        else if (k === 'text') n.textContent = v;
        else if (k === 'html') n.innerHTML = v;
        else if (k === 'on') { for (var ev in v) n.addEventListener(ev, v[ev]); }
        else if (k === 'style' && typeof v === 'object') { for (var s in v) n.style[s] = v[s]; }
        else if (k === 'dataset') { for (var d in v) n.dataset[d] = v[d]; }
        else if (k in n && k !== 'list' && k !== 'form') { try { n[k] = v; } catch (e) { n.setAttribute(k, v); } }
        else n.setAttribute(k, v === true ? '' : v);
      }
    }
    for (var i = 2; i < arguments.length; i++) LA.append(n, arguments[i]);
    return n;
  };
  LA.append = function (parent, c) {
    if (c == null || c === false) return;
    if (Array.isArray(c)) { c.forEach(function (x) { LA.append(parent, x); }); return; }
    parent.appendChild(typeof c === 'string' || typeof c === 'number' ? document.createTextNode(String(c)) : c);
  };
  LA.clear = function (n) { while (n.firstChild) n.removeChild(n.firstChild); return n; };

  /* ---------- 图标（stroke 风格，自绘） ---------- */
  var P = {
    plus: 'M12 5v14M5 12h14', menu: 'M4 7h16M4 12h16M4 17h16', sidebar: 'M4 5h16v14H4zM9 5v14', panel: 'M4 5h16v14H4zM15 5v14',
    settings: 'M12 15a3 3 0 100-6 3 3 0 000 6zM19.4 15a1.7 1.7 0 00.3 1.8l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.8-.3 1.7 1.7 0 00-1 1.5V21a2 2 0 11-4 0v-.1a1.7 1.7 0 00-1.1-1.5 1.7 1.7 0 00-1.8.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.7 1.7 0 00.3-1.8 1.7 1.7 0 00-1.5-1H3a2 2 0 110-4h.1a1.7 1.7 0 001.5-1.1 1.7 1.7 0 00-.3-1.8l-.1-.1a2 2 0 112.8-2.8l.1.1a1.7 1.7 0 001.8.3H9a1.7 1.7 0 001-1.5V3a2 2 0 114 0v.1a1.7 1.7 0 001 1.5 1.7 1.7 0 001.8-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.7 1.7 0 00-.3 1.8V9a1.7 1.7 0 001.5 1H21a2 2 0 110 4h-.1a1.7 1.7 0 00-1.5 1z',
    brain: 'M9 4a3 3 0 00-3 3 3 3 0 00-2 5 3 3 0 002 5 3 3 0 003 3V4zM15 4a3 3 0 013 3 3 3 0 012 5 3 3 0 01-2 5 3 3 0 01-3 3V4z',
    puzzle: 'M10 4a2 2 0 114 0v1h4v4h-1a2 2 0 100 4h1v4h-4v-1a2 2 0 10-4 0v1H6v-4h1a2 2 0 100-4H6V5h4z',
    folder: 'M3 6a2 2 0 012-2h4l2 2h8a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2z', file: 'M6 3h8l5 5v13H6zM14 3v5h5',
    send: 'M12 19V5M5 12l7-7 7 7', stop: 'M7 7h10v10H7z', clip: 'M20 11l-8.5 8.5a5 5 0 01-7-7L13 4a3.5 3.5 0 015 5l-8.5 8.5a2 2 0 01-3-3L14 7',
    chev: 'M9 6l6 6-6 6', chevd: 'M6 9l6 6 6-6', check: 'M5 12l5 5 9-10', x: 'M6 6l12 12M18 6L6 18', copy: 'M9 9h10v11H9zM5 15V4h10',
    refresh: 'M20 12a8 8 0 11-2.3-5.7M20 4v5h-5', edit: 'M4 20h4L19 9l-4-4L4 16zM13 7l4 4', trash: 'M5 7h14M10 7V4h4v3M7 7l1 13h8l1-13M10 11v6M14 11v6',
    pin: 'M9 4h6l-1 6 3 3H7l3-3zM12 13v7', terminal: 'M4 5h16v14H4zM8 10l3 2-3 2M13 15h4', search: 'M11 4a7 7 0 100 14 7 7 0 000-14zM20 20l-4-4',
    globe: 'M12 3a9 9 0 100 18 9 9 0 000-18zM3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18', image: 'M4 5h16v14H4zM8 10a1.5 1.5 0 100-3 1.5 1.5 0 000 3zM4 17l5-5 4 4 3-3 4 4',
    alert: 'M12 4l9 16H3zM12 10v4M12 17v.5', shield: 'M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z', undo: 'M9 14L4 9l5-5M4 9h10a6 6 0 010 12h-3',
    sun: 'M12 8a4 4 0 100 8 4 4 0 000-8zM12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5L19 19M5 19l1.5-1.5M17.5 6.5L19 5',
    list: 'M9 6h11M9 12h11M9 18h11M4 6h.01M4 12h.01M4 18h.01', help: 'M12 3a9 9 0 100 18 9 9 0 000-18zM9.5 9.5a2.5 2.5 0 114 2c-.8.6-1.5 1-1.5 2M12 17v.5',
    down: 'M12 5v14M6 13l6 6 6-6', up: 'M12 19V5M6 11l6-6 6 6', eye: 'M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12zM12 9a3 3 0 100 6 3 3 0 000-6z',
    cpu: 'M7 7h10v10H7zM9 3v4M15 3v4M9 17v4M15 17v4M3 9h4M3 15h4M17 9h4M17 15h4', download: 'M12 4v11M7 11l5 5 5-5M5 20h14', upload: 'M12 16V5M7 9l5-5 5 5M5 20h14',
    lock: 'M6 11h12v9H6zM8 11V8a4 4 0 118 0v3', play: 'M8 5l11 7-11 7z', zap: 'M13 3L5 14h6l-1 7 8-11h-6z', dots: 'M5 12h.01M12 12h.01M19 12h.01'
  };
  LA.icon = function (name, size) {
    var s = size ? ' style="width:' + size + 'px;height:' + size + 'px"' : '';
    return '<svg class="ic" viewBox="0 0 24 24"' + s + ' aria-hidden="true"><path d="' + (P[name] || '') + '"/></svg>';
  };
  LA.iconEl = function (name, size) { var s = document.createElement('span'); s.style.display = 'inline-flex'; s.innerHTML = LA.icon(name, size); return s; };
  LA.SPARK = '<svg viewBox="0 0 32 32" aria-hidden="true"><path d="M16 5l2.4 8.6L27 16l-8.6 2.4L16 27l-2.4-8.6L5 16l8.6-2.4z"/></svg>';

  /* ---------- 格式化 ---------- */
  LA.fmtN = function (n) { n = +n || 0; return n >= 10000 ? (n / 1000).toFixed(n >= 100000 ? 0 : 1) + 'k' : String(Math.round(n)); };
  LA.fmtSize = function (n) { n = +n || 0; if (n < 1024) return n + ' B'; if (n < 1048576) return (n / 1024).toFixed(1) + ' KB'; if (n < 1073741824) return (n / 1048576).toFixed(1) + ' MB'; return (n / 1073741824).toFixed(2) + ' GB'; };
  LA.fmtTime = function (ts) { var d = new Date(ts * 1000); var p = function (x) { return (x < 10 ? '0' : '') + x; }; return p(d.getHours()) + ':' + p(d.getMinutes()); };
  LA.baseName = function (p) { p = String(p || '').replace(/[\\/]+$/, ''); var i = Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\')); return i >= 0 ? p.slice(i + 1) : p; };
  LA.debounce = function (fn, ms) { var t; return function () { var a = arguments, c = this; clearTimeout(t); t = setTimeout(function () { fn.apply(c, a); }, ms); }; };

  /* ---------- toast ---------- */
  LA.toast = function (msg, kind, ms) {
    var root = LA.$('#toastRoot');
    var t = LA.el('div', { class: 'toast' + (kind ? ' ' + kind : ''), text: msg });
    root.appendChild(t);
    setTimeout(function () { t.style.transition = 'opacity .25s'; t.style.opacity = '0'; setTimeout(function () { t.remove(); }, 260); }, ms || (kind === 'err' ? 5000 : 2200));
  };

  /* ---------- modal ---------- */
  var modalStack = [];
  LA.modal = function (opts) {
    var root = LA.$('#modalRoot');
    var back = LA.el('div', { class: 'modal-back' });
    var box = LA.el('div', { class: 'modal ' + (opts.cls || ''), role: 'dialog', 'aria-modal': 'true' });
    if (opts.width) box.style.setProperty('--mw', opts.width + 'px');
    var closeBtn = LA.el('button', { class: 'icon-btn', title: '关闭', html: LA.icon('x') });
    box.appendChild(LA.el('div', { class: 'modal-h' }, LA.el('h3', { text: opts.title || '' }), closeBtn));
    var body = LA.el('div', { class: opts.bare ? 'set-body' : 'modal-b' });
    LA.append(body, opts.body);
    box.appendChild(body);
    var foot = null;
    if (opts.footer && opts.footer.length) { foot = LA.el('div', { class: 'modal-f' }); LA.append(foot, opts.footer); box.appendChild(foot); }
    back.appendChild(box);
    var m = { el: back, box: box, body: body, foot: foot, closed: false };
    m.close = function (result) {
      if (m.closed) return; m.closed = true;
      back.remove();
      modalStack = modalStack.filter(function (x) { return x !== m; });
      if (opts.onClose) opts.onClose(result);
    };
    closeBtn.addEventListener('click', function () { m.close(); });
    back.addEventListener('mousedown', function (e) { if (e.target === back && !opts.sticky) m.close(); });
    root.appendChild(back);
    modalStack.push(m);
    var f = LA.$('input,textarea,select', body);
    if (f && opts.focus !== false) setTimeout(function () { try { f.focus(); } catch (e) {} }, 30);
    return m;
  };
  LA.closeTopModal = function () { var m = modalStack[modalStack.length - 1]; if (m) { m.close(); return true; } return false; };
  LA.hasModal = function () { return modalStack.length > 0; };

  LA.confirm = function (msg, o) {
    o = o || {};
    return new Promise(function (resolve) {
      var done = false;
      var ok = LA.el('button', { class: 'btn ' + (o.danger ? 'danger' : 'primary'), text: o.okText || '确定' });
      var no = LA.el('button', { class: 'btn', text: o.cancelText || '取消' });
      var m = LA.modal({ title: o.title || '请确认', width: 420, body: LA.el('div', { style: { whiteSpace: 'pre-wrap', lineHeight: '1.6' }, text: msg }), footer: [no, ok], focus: false,
        onClose: function () { if (!done) resolve(false); } });
      ok.addEventListener('click', function () { done = true; m.close(); resolve(true); });
      no.addEventListener('click', function () { m.close(); });
      setTimeout(function () { ok.focus(); }, 30);
    });
  };
  LA.prompt = function (title, value, o) {
    o = o || {};
    return new Promise(function (resolve) {
      var done = false;
      var inp = LA.el('input', { class: 'inp wide', value: value || '', placeholder: o.placeholder || '' });
      var ok = LA.el('button', { class: 'btn primary', text: '确定' });
      var m = LA.modal({ title: title, width: 440, body: inp, footer: [LA.el('button', { class: 'btn', text: '取消', on: { click: function () { m.close(); } } }), ok],
        onClose: function () { if (!done) resolve(null); } });
      var go = function () { done = true; m.close(); resolve(inp.value); };
      ok.addEventListener('click', go);
      inp.addEventListener('keydown', function (e) { if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) go(); });
      setTimeout(function () { inp.focus(); inp.select(); }, 30);
    });
  };

  /* ---------- 下拉菜单（点击外部关闭） ---------- */
  LA.menu = function (anchor, items, onPick, selected) {
    var old = LA.$('.menu.floating'); if (old) old.remove();
    var m = LA.el('div', { class: 'menu floating' });
    items.forEach(function (it) {
      var b = LA.el('button', { class: 'menu-item' + (it.value === selected ? ' sel' : ''), on: { click: function () { m.remove(); onPick(it.value); } } },
        LA.el('b', { text: (it.value === selected ? '✓ ' : '') + it.label }), it.desc ? LA.el('small', { text: it.desc }) : null);
      m.appendChild(b);
    });
    anchor.parentNode.style.position = 'relative';
    anchor.parentNode.appendChild(m);
    var off = function (e) { if (!m.contains(e.target) && e.target !== anchor) { m.remove(); document.removeEventListener('mousedown', off, true); } };
    setTimeout(function () { document.addEventListener('mousedown', off, true); }, 0);
    return m;
  };

  LA.copyText = function (text) {
    var done = function () { LA.toast('已复制'); };
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, function () { LA.legacyCopy(text); done(); });
    else { LA.legacyCopy(text); done(); }
  };
  LA.legacyCopy = function (text) {
    var t = LA.el('textarea', { value: text, style: { position: 'fixed', opacity: '0' } });
    document.body.appendChild(t); t.select(); try { document.execCommand('copy'); } catch (e) {} t.remove();
  };
  LA.download = function (name, text, mime) {
    var a = LA.el('a', { href: URL.createObjectURL(new Blob([text], { type: mime || 'application/octet-stream' })), download: name });
    document.body.appendChild(a); a.click(); setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 500);
  };
})();
