/* 设置 → 扩展：MCP 服务器。界面只有“一行一个服务器 + 展开详情”，所有安全决策在后端（沙箱默认开启，按服务器单独放宽）。 */
(function () {
  'use strict';
  var LA = window.LA = window.LA || {}, el = LA.el, MASK = '••••••';
  var STATUS = { running: '运行中', idle: '未启动', error: '出错', disabled: '已停用' };

  function check(label, value, onchange, hint) {
    var c = el('input', { type: 'checkbox' }); c.checked = !!value;
    c.addEventListener('change', function () { onchange(c.checked, c); });
    return el('label', { class: 'switch', title: hint || '' }, c, el('span', { text: label }));
  }
  function envText(v) { return Object.keys(v.env_masked || {}).map(function (k) { return k + '=' + MASK; }).join('\n'); }
  function parseEnv(text, old) {
    var out = {}, bad = null;
    text.split('\n').forEach(function (ln) {
      ln = ln.trim(); if (!ln) return;
      var i = ln.indexOf('='); if (i < 1) { bad = ln; return; }
      var k = ln.slice(0, i).trim(), val = ln.slice(i + 1);
      out[k] = (val === MASK && old && old[k]) ? null : val;       // null = 沿用原值（界面从不回显密钥）
    });
    return bad ? { error: '环境变量格式应为 KEY=VALUE：' + bad } : { env: out };
  }

  LA.mcpTab = function (pane, H) {
    var listBox = el('div', { class: 'mcp-list' }), data = null, open = {};
    pane.appendChild(H.sec('外部工具（MCP 服务器）'));
    pane.appendChild(el('div', { class: 'hint2', text: '把本机的 MCP 服务器接入为工具。每个服务器默认在沙箱里运行：只能写自己的私有目录、看不到你的主目录和密钥、不能联网；沙箱不可用时会拒绝启动。它返回的内容按“不可信”处理，调用前默认需要你确认。可在每个服务器的详情里单独放宽。' }));
    var banner = el('div'); pane.appendChild(banner); pane.appendChild(listBox);

    function reload() { return LA.get('/api/mcp').then(function (r) { data = r; draw(); }).catch(LA.fail); }
    function save(name, patch) { return LA.put('/api/mcp/' + encodeURIComponent(name), patch).then(function (v) { return reload().then(function () { return v; }); }).catch(function (e) { LA.fail(e); return reload(); }); }

    function draw() {
      LA.clear(banner); LA.clear(listBox);
      var sb = data.sandbox || {};
      if (!sb.available) banner.appendChild(H.banner('warn', '这台设备没有可用的沙箱，默认沙箱的服务器将无法启动。你信任的服务器可以在详情里关闭沙箱。'));
      if (!data.servers.length) listBox.appendChild(el('div', { class: 'hint2', text: '还没有服务器。在下面填写名称和启动命令即可添加，例如  npx -y @modelcontextprotocol/server-filesystem /path' }));
      data.servers.forEach(function (v) { listBox.appendChild(row(v)); });
      listBox.appendChild(addForm());
    }

    function row(v) {
      var head = el('div', { class: 'mcp-head' },
        el('span', { class: 'dot ' + (v.status === 'running' ? 'ready' : v.status === 'error' ? 'error' : '') }),
        el('b', { text: v.name }),
        el('span', { class: 'hint2', text: STATUS[v.status] + (v.tools.length ? ' · ' + v.tools.length + ' 个工具' : '') }),
        el('span', { style: { flex: 1 } }),
        check('启用', v.enabled, function (on) { save(v.name, { enabled: on }); }),
        check('沙箱', v.sandbox === 'auto', function (on, c) {
          if (on) { save(v.name, { sandbox: 'auto' }); return; }
          c.checked = true;
          LA.confirm('关闭沙箱后，“' + v.name + '”将以你的权限直接运行，能读写你可访问的任何文件。只对你信任的服务器这样做。', { title: '关闭沙箱', danger: true, okText: '关闭沙箱' })
            .then(function (ok) { if (ok) save(v.name, { sandbox: 'off' }); });
        }, '默认开启'),
        H.btn(open[v.name] ? '收起' : '详情', function () { open[v.name] = !open[v.name]; draw(); }, 'ghost'));
      var r = el('div', { class: 'mcp-row' }, head);
      if (v.error) r.appendChild(el('div', { class: 'mcp-err', text: v.error }));
      if (open[v.name]) r.appendChild(details(v));
      return r;
    }

    function details(v) {
      var cmd = el('input', { class: 'inp wide', value: v.command_line, spellcheck: false });
      var env = el('textarea', { class: 'inp wide', rows: 3, spellcheck: false, placeholder: 'KEY=VALUE，每行一个（值保存后不再显示）' }); env.value = envText(v);
      var ro = el('textarea', { class: 'inp wide', rows: 2, spellcheck: false, placeholder: '额外只读目录，每行一个绝对路径' }); ro.value = (v.readonly_paths || []).join('\n');
      var logs = el('div');
      var d = el('div', { class: 'mcp-detail' },
        H.F('启动命令', cmd),
        H.F('环境变量', env, '只在此服务器生效，保存在本机 data/mcp.json（仅你可读）。'),
        el('div', { class: 'mcp-opts' },
          check('允许联网', v.network, function (on) { save(v.name, { network: on }); }, '默认关闭。开启后自动模式也会逐次确认。'),
          check('可访问当前工作区', v.workspace, function (on) { save(v.name, { workspace: on }); }, '把工作区以可写方式挂进沙箱。'),
          check('信任返回内容', v.trust_results, function (on) { save(v.name, { trust_results: on }); }, '不把它的返回当作不可信内容（不触发污染保护）。')),
        H.F('额外只读目录', ro, '运行时装在主目录或 /opt 下时需要；不能包含主目录整体或 LocalAgent 数据目录。'),
        el('div', { class: 'toolbar' },
          H.btn('保存', function () {
            var p = parseEnv(env.value, v.env_masked);
            if (p.error) { LA.toast(p.error, 'err'); return; }
            save(v.name, { command: cmd.value, env: p.env, readonly_paths: ro.value.split('\n').map(function (x) { return x.trim(); }).filter(Boolean) });
          }, 'primary'),
          H.btn('启动并读取工具', function (e) {
            var b = e.currentTarget; b.disabled = true; b.textContent = '启动中…';
            LA.post('/api/mcp/' + encodeURIComponent(v.name) + '/start').then(reload).catch(function (er) { LA.fail(er); reload(); });
          }),
          H.btn('停止', function () { LA.post('/api/mcp/' + encodeURIComponent(v.name) + '/stop').then(reload).catch(LA.fail); }, 'ghost'),
          H.btn('日志', function () { LA.clear(logs); logs.appendChild(el('div', { class: 'logbox', text: v.stderr || '（暂无输出）' })); }, 'ghost'),
          H.btn('删除', function () {
            LA.confirm('删除服务器“' + v.name + '”及其私有数据目录？', { danger: true, okText: '删除' }).then(function (ok) {
              if (ok) LA.del('/api/mcp/' + encodeURIComponent(v.name)).then(reload).catch(LA.fail);
            });
          }, 'danger')),
        logs);
      if (v.warnings && v.warnings.length) d.appendChild(el('div', { class: 'mcp-err', text: v.warnings.join('\n') }));
      if (v.tools.length) d.appendChild(el('div', { class: 'hint2', text: '工具：' + v.tools.map(function (t) { return t.tool; }).join('、') }));
      return d;
    }

    function addForm() {
      var name = el('input', { class: 'inp', placeholder: '名称（如 files）', spellcheck: false, style: { width: '9em' } });
      var cmd = el('input', { class: 'inp wide', placeholder: '启动命令（如 npx -y @modelcontextprotocol/server-filesystem /data）', spellcheck: false });
      return el('div', { class: 'mcp-add' }, name, cmd, H.btn('添加', function () {
        var n = name.value.trim(), c = cmd.value.trim();
        if (!n || !c) { LA.toast('请填写名称和启动命令', 'err'); return; }
        save(n, { command: c }).then(function (v) { if (v && v.name) { open[v.name] = true; draw(); } });
      }, 'primary'));
    }
    reload();
  };
})();
