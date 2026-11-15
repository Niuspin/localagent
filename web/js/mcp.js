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
  function kvText(masked) { return Object.keys(masked || {}).map(function (k) { return k + '=' + MASK; }).join('\n'); }
  function parseKV(text, old, label) {
    var out = {}, bad = null;
    text.split('\n').forEach(function (ln) {
      ln = ln.trim(); if (!ln) return;
      var i = ln.indexOf('='); if (i < 1) { bad = ln; return; }
      var k = ln.slice(0, i).trim(), val = ln.slice(i + 1);
      out[k] = (val === MASK && old && old[k]) ? null : val;
    });
    return bad ? { error: label + ' 格式应为 KEY=VALUE：' + bad } : { map: out };
  }

  LA.mcpTab = function (pane, H) {
    var listBox = el('div', { class: 'mcp-list' }), data = null, open = {};
    pane.appendChild(H.sec('外部工具（MCP 服务器）'));
    pane.appendChild(el('div', { class: 'hint2', text: '支持本机 stdio 与远程/本地 HTTP (SSE) MCP 服务器。stdio 默认在 OS 沙箱中隔离运行；返回内容默认按“不可信”处理。' }));
    var banner = el('div'); pane.appendChild(banner); pane.appendChild(listBox);

    function reload() { return LA.get('/api/mcp').then(function (r) { data = r; draw(); }).catch(LA.fail); }
    function save(name, patch) { return LA.put('/api/mcp/' + encodeURIComponent(name), patch).then(function (v) { return reload().then(function () { return v; }); }).catch(function (e) { LA.fail(e); return reload(); }); }
    if (!LA._mcpBound) {
      LA._mcpBound = true;
      LA.bus.on('mcp', function () { if (LA._mcpReload && LA._mcpBox && LA._mcpBox.isConnected) LA._mcpReload(); });
    }
    LA._mcpBox = listBox;
    LA._mcpReload = reload;

    function previewModal(title, obj) {
      var pre = el('pre', { class: 'logbox', text: typeof obj === 'string' ? obj : JSON.stringify(obj, null, 2) });
      LA.modal({ title: title, width: 620, body: [pre] });
    }

    function draw() {
      if (!listBox.isConnected) return;
      LA.clear(banner); LA.clear(listBox);
      var sb = data.sandbox || {};
      if (!sb.available) banner.appendChild(H.banner('warn', '这台设备没有可用的沙箱，默认沙箱的 stdio 服务器将无法启动。你信任的服务器可以在详情里关闭沙箱。'));
      if (!data.servers.length) listBox.appendChild(el('div', { class: 'hint2', text: '还没有服务器。在下面填写名称和启动命令（或 HTTP URL）即可添加。' }));
      data.servers.forEach(function (v) { listBox.appendChild(row(v)); });
      listBox.appendChild(addForm());
    }

    function row(v) {
      var isHttp = v.transport === 'http';
      var cntInfo = [v.tools.length ? v.tools.length + ' 工具' : '', (v.resources || []).length ? v.resources.length + ' 资源' : '', (v.prompts || []).length ? v.prompts.length + ' 模板' : ''].filter(Boolean).join(' / ');
      var head = el('div', { class: 'mcp-head' },
        el('span', { class: 'dot ' + (v.status === 'running' ? 'ready' : v.status === 'error' ? 'error' : '') }),
        el('b', { text: v.name }),
        isHttp ? el('span', { class: 'badge', text: 'HTTP' }) : null,
        el('span', { class: 'hint2', text: STATUS[v.status] + (cntInfo ? ' · ' + cntInfo : '') }),
        el('span', { style: { flex: 1 } }),
        check('启用', v.enabled, function (on) { save(v.name, { enabled: on }); }),
        !isHttp ? check('沙箱', v.sandbox === 'auto', function (on, c) {
          if (on) { save(v.name, { sandbox: 'auto' }); return; }
          c.checked = true;
          LA.confirm('关闭沙箱后，“' + v.name + '”将以你的权限直接运行，能读写你可访问的任何文件。只对你信任的服务器这样做。', { title: '关闭沙箱', danger: true, okText: '关闭沙箱' })
            .then(function (ok) { if (ok) save(v.name, { sandbox: 'off' }); });
        }, '默认开启') : null,
        H.btn(open[v.name] ? '收起' : '详情', function () { open[v.name] = !open[v.name]; draw(); }, 'ghost'));
      var r = el('div', { class: 'mcp-row' }, head);
      if (v.error) r.appendChild(el('div', { class: 'mcp-err', text: v.error }));
      if (open[v.name]) r.appendChild(details(v));
      return r;
    }

    function details(v) {
      var isHttp = v.transport === 'http';
      var primaryInp = el('input', { class: 'inp wide', value: isHttp ? (v.url || '') : v.command_line, spellcheck: false, placeholder: isHttp ? 'https://example.com/mcp' : 'command args...' });
      var kvInp = el('textarea', { class: 'inp wide', rows: 3, spellcheck: false, placeholder: 'KEY=VALUE，每行一个（值保存后不再显示）' });
      kvInp.value = kvText(isHttp ? v.headers_masked : v.env_masked);
      var ro = !isHttp ? el('textarea', { class: 'inp wide', rows: 2, spellcheck: false, placeholder: '额外只读目录，每行一个绝对路径' }) : null;
      if (ro) ro.value = (v.readonly_paths || []).join('\n');
      var logs = el('div');
      var d = el('div', { class: 'mcp-detail' });
      if (isHttp) {
        d.appendChild(H.F('服务器 URL', primaryInp));
        d.appendChild(H.F('HTTP 请求头', kvInp, '如 Authorization=Bearer xxx，保存在本机 data/mcp.json，不回显值。'));
        d.appendChild(el('div', { class: 'mcp-opts' },
          check('信任返回内容', v.trust_results, function (on) { save(v.name, { trust_results: on }); }, '不把它的返回当作不可信内容（不触发污染保护）。')));
      } else {
        d.appendChild(H.F('启动命令', primaryInp));
        d.appendChild(H.F('环境变量', kvInp, '只在此服务器生效，保存在本机 data/mcp.json（仅你可读）。'));
        d.appendChild(el('div', { class: 'mcp-opts' },
          check('允许联网', v.network, function (on) { save(v.name, { network: on }); }, '默认关闭。开启后自动模式也会逐次确认。'),
          check('可访问当前工作区', v.workspace, function (on) { save(v.name, { workspace: on }); }, '把工作区以可写方式挂进沙箱。'),
          check('信任返回内容', v.trust_results, function (on) { save(v.name, { trust_results: on }); }, '不把它的返回当作不可信内容（不触发污染保护）。')));
        d.appendChild(H.F('额外只读目录', ro, '运行时装在主目录或 /opt 下时需要；不能包含主目录整体或 LocalAgent 数据目录。'));
      }
      d.appendChild(el('div', { class: 'toolbar' },
        H.btn('保存', function () {
          var p = parseKV(kvInp.value, isHttp ? v.headers_masked : v.env_masked, isHttp ? 'HTTP 请求头' : '环境变量');
          if (p.error) { LA.toast(p.error, 'err'); return; }
          if (isHttp) save(v.name, { transport: 'http', url: primaryInp.value.trim(), headers: p.map });
          else save(v.name, { command: primaryInp.value, env: p.map, readonly_paths: ro.value.split('\n').map(function (x) { return x.trim(); }).filter(Boolean) });
        }, 'primary'),
        H.btn(isHttp ? '连接并同步能力' : '启动并读取工具', function (e) {
          var b = e.currentTarget; b.disabled = true; b.textContent = '连接中…';
          LA.post('/api/mcp/' + encodeURIComponent(v.name) + '/start').then(reload).catch(function (er) { LA.fail(er); reload(); });
        }),
        H.btn('停止', function () { LA.post('/api/mcp/' + encodeURIComponent(v.name) + '/stop').then(reload).catch(LA.fail); }, 'ghost'),
        !isHttp ? H.btn('日志', function () { LA.clear(logs); logs.appendChild(el('div', { class: 'logbox', text: v.stderr || '（暂无输出）' })); }, 'ghost') : null,
        H.btn('删除', function () {
          LA.confirm('删除服务器“' + v.name + '”及其私有数据目录？', { danger: true, okText: '删除' }).then(function (ok) {
            if (ok) LA.del('/api/mcp/' + encodeURIComponent(v.name)).then(reload).catch(LA.fail);
          });
        }, 'danger')));
      d.appendChild(logs);
      if (v.warnings && v.warnings.length) d.appendChild(el('div', { class: 'mcp-err', text: v.warnings.join('\n') }));
      if (v.tools.length) d.appendChild(el('div', { class: 'hint2', text: '工具：' + v.tools.map(function (t) { return t.tool; }).join('、') }));
      if (v.resources && v.resources.length) {
        var rRow = el('div', { class: 'toolbar', style: { flexWrap: 'wrap', marginTop: '6px' } }, el('span', { class: 'hint2', text: '资源：' }));
        v.resources.slice(0, 12).forEach(function (resItem) {
          rRow.appendChild(H.btn(resItem.name || resItem.uri, function () {
            LA.post('/api/mcp/' + encodeURIComponent(v.name) + '/resources/read', { uri: resItem.uri })
              .then(function (out) { previewModal('资源：' + (resItem.name || resItem.uri), out); }).catch(LA.fail);
          }, 'ghost'));
        });
        d.appendChild(rRow);
      }
      if (v.prompts && v.prompts.length) {
        var pRow = el('div', { class: 'toolbar', style: { flexWrap: 'wrap', marginTop: '6px' } }, el('span', { class: 'hint2', text: '提示模板：' }));
        v.prompts.slice(0, 12).forEach(function (prItem) {
          pRow.appendChild(H.btn(prItem.name, function () {
            LA.post('/api/mcp/' + encodeURIComponent(v.name) + '/prompts/get', { name: prItem.name })
              .then(function (out) { previewModal('模板：' + prItem.name, out); }).catch(LA.fail);
          }, 'ghost'));
        });
        d.appendChild(pRow);
      }
      return d;
    }

    function addForm() {
      var name = el('input', { class: 'inp', placeholder: '名称（如 files）', spellcheck: false, style: { width: '9em' } });
      var cmd = el('input', { class: 'inp wide', placeholder: '启动命令（如 npx -y @modelcontextprotocol/server-filesystem /data）', spellcheck: false });
      var trSel = el('select', { class: 'inp', title: '传输协议' },
        el('option', { value: 'stdio', text: 'stdio' }),
        el('option', { value: 'http', text: 'HTTP' })
      );
      trSel.addEventListener('change', function () {
        cmd.placeholder = trSel.value === 'http'
          ? 'HTTP/SSE MCP URL（如 http://127.0.0.1:8080/mcp）'
          : '启动命令（如 npx -y @modelcontextprotocol/server-filesystem /data）';
      });
      return el('div', { class: 'mcp-add' }, name, cmd, trSel, H.btn('添加', function () {
        var n = name.value.trim(), c = cmd.value.trim(), tr = trSel.value;
        if (!n || !c) { LA.toast(tr === 'http' ? '请填写名称和 URL' : '请填写名称和启动命令', 'err'); return; }
        var patch = tr === 'http' ? { transport: 'http', url: c } : { transport: 'stdio', command: c };
        save(n, patch).then(function (v) { if (v && v.name) { open[v.name] = true; draw(); } });
      }, 'primary'));
    }
    reload();
  };
})();
