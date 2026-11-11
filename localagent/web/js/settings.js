/* 设置：模型与引擎 / 生成 / 安全 / 搜索 / 外观。所有改动即时本地生效并防抖保存（见 LA.cfgSet）。 */
(function () {
  'use strict';
  var LA = window.LA, el = LA.el, icon = LA.icon;
  var modal = null, pane = null, nav = null, tab = 'engine', timers = [], savedEl = null, bannerSlot = null, engineRedraw = null;

  var TABS = [['engine', '模型与引擎'], ['gen', '生成'], ['security', '安全'], ['search', '搜索'], ['mcp', '扩展'], ['ui', '外观']];
  var SEARCH_ENGINES = {
    duckduckgo: { name: 'DuckDuckGo', note: '无需密钥' }, bing: { name: 'Bing', note: '无需密钥' }, baidu: { name: '百度', note: '无需密钥' }, yahoo: { name: 'Yahoo', note: '无需密钥' },
    brave: { name: 'Brave Search', note: '需要 API Key', key: 'brave' }, google_cse: { name: 'Google 可编程搜索', note: '需要 API Key 与 CX', key: 'google_cse' },
    searxng: { name: 'SearXNG', note: '需要实例地址' }, tavily: { name: 'Tavily', note: '需要 API Key', key: 'tavily' }, serper: { name: 'Serper (Google)', note: '需要 API Key', key: 'serper' }
  };

  /* ------------------------------------------------------------ 控件工厂 */
  function F(label, ctl, hint, cls) {
    return el('div', { class: 'field' + (cls ? ' ' + cls : '') }, el('label', { text: label }), el('div', { class: 'ctl' }, ctl), hint ? el('div', { class: 'hint2', text: hint }) : null);
  }
  function sel(path, opts, parse, onchange) {
    var s = el('select', { class: 'inp' });
    opts.forEach(function (o) { s.appendChild(el('option', { value: String(o[0]), text: o[1] })); });
    s.value = String(LA.cfg(path, opts[0][0]));
    if (s.value !== String(LA.cfg(path, opts[0][0]))) s.value = String(opts[0][0]);
    s.addEventListener('change', function () { var v = parse ? parse(s.value) : s.value; LA.cfgSet(path, v, { immediate: true }); if (onchange) onchange(v); });
    return s;
  }
  function num(path, o) {
    o = o || {};
    var i = el('input', { class: 'inp num', type: 'number', value: LA.cfg(path, ''), step: o.step || 1 });
    if (o.min != null) i.min = o.min; if (o.max != null) i.max = o.max;
    i.addEventListener('change', function () {
      var v = o.float ? parseFloat(i.value) : parseInt(i.value, 10);
      if (isNaN(v)) { i.value = LA.cfg(path, ''); return; }
      if (o.min != null) v = Math.max(o.min, v); if (o.max != null) v = Math.min(o.max, v);
      i.value = v; LA.cfgSet(path, v);
    });
    return i;
  }
  /** "auto" 或整数 */
  function autoNum(path, ph) {
    var cur = LA.cfg(path, 'auto');
    var i = el('input', { class: 'inp num', value: cur === 'auto' ? '' : cur, placeholder: ph || '自动' });
    i.addEventListener('change', function () {
      var t = i.value.trim().toLowerCase();
      if (!t || t === 'auto') { i.value = ''; return LA.cfgSet(path, 'auto'); }
      var v = parseInt(t, 10); if (isNaN(v) || v < 0) { i.value = cur === 'auto' ? '' : cur; return LA.toast('请输入非负整数，或留空表示自动'); }
      i.value = v; LA.cfgSet(path, v);
    });
    return i;
  }
  function sw(path, text) {
    var c = el('input', { type: 'checkbox' }); c.checked = !!LA.cfg(path, false);
    c.addEventListener('change', function () { LA.cfgSet(path, c.checked, { immediate: true }); });
    return el('label', { class: 'switch' }, c, el('span', { text: text || '' }));
  }
  function txt(path, o) {
    o = o || {};
    var i = o.area ? el('textarea', { class: 'inp wide', rows: o.rows || 3 }) : el('input', { class: 'inp' + (o.wide ? ' wide' : ''), placeholder: o.ph || '', spellcheck: false });
    i.value = LA.cfg(path, '') || '';
    i.addEventListener('change', function () { LA.cfgSet(path, i.value.trim()); });
    return i;
  }
  function sec(title) { return el('h4', { text: title }); }
  function btn(text, fn, cls) { return el('button', { class: 'btn sm ' + (cls || ''), text: text, on: { click: fn } }); }
  function banner(kind, text, action) { return el('div', { class: 'banner ' + kind }, el('span', { text: text }), action || null); }

  function later(fn, ms) { var t = setInterval(fn, ms); timers.push(t); return t; }
  function stopTimers() { timers.forEach(clearInterval); timers = []; }

  /* ------------------------------------------------------------ 打开 / 切换 */
  LA.settings = {
    open: function (t) {
      if (modal && !modal.closed) { show(t || tab); return; }
      tab = t || tab || 'engine';
      nav = el('div', { class: 'set-nav' });
      savedEl = el('div', { class: 'hint2', style: { marginTop: 'auto', padding: '8px 12px', opacity: 0, transition: 'opacity .3s' }, text: '✓ 已自动保存' });
      pane = el('div', { class: 'set-pane' });
      bannerSlot = el('div');
      var right = el('div', { style: { flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column' } }, bannerSlot, pane);
      modal = LA.modal({ title: '设置', cls: 'settings', bare: true, body: [nav, right], focus: false, onClose: function () { stopTimers(); LA.flushConfig(); modal = null; } });
      TABS.forEach(function (t) { nav.appendChild(el('button', { text: t[1], dataset: { tab: t[0] }, on: { click: function () { show(t[0]); } } })); });
      nav.appendChild(savedEl);
      renderBanner();
      show(tab);
    },
    refresh: function () { if (modal && !modal.closed && (tab === 'engine')) {/* 引擎页自己轮询刷新 */} }
  };
  function show(t) {
    tab = t; stopTimers(); engineRedraw = null;
    LA.$$('button', nav).forEach(function (b) { b.classList.toggle('on', b.dataset.tab === t); });
    LA.clear(pane); pane.scrollTop = 0;
    ({ engine: tabEngine, gen: tabGen, security: tabSecurity, search: tabSearch, mcp: tabMcp, ui: tabUi })[t]();
    renderBanner();
  }
  function renderBanner() {
    if (!bannerSlot) return;
    LA.clear(bannerSlot);
    if (LA.S.restartNeeded && LA.engineReady()) {
      bannerSlot.appendChild(el('div', { style: { padding: '0 22px' } }, banner('warn', '引擎相关设置已更改，需要重启引擎才能生效。', btn('立即重启', function () { restartEngine(); }, 'primary'))));
    }
  }
  LA.bus.on('restartNeeded', renderBanner);
  LA.bus.on('saved', function () { if (savedEl) { savedEl.style.opacity = 1; setTimeout(function () { if (savedEl) savedEl.style.opacity = 0; }, 1400); } });

  /* ------------------------------------------------------------ 引擎控制（全局复用） */
  LA.startEngine = function (body) {
    return LA.flushConfig().then(function () { return LA.post('/api/engine/start', body || {}); }).then(function () { LA.S.restartNeeded = false; renderBanner(); return LA.refreshState(); }).catch(LA.fail);
  };
  function restartEngine() {
    return LA.flushConfig().then(function () { return LA.post('/api/engine/restart', {}); }).then(function () { LA.S.restartNeeded = false; renderBanner(); return LA.refreshState(); }).catch(LA.fail);
  }
  function stopEngine() { return LA.post('/api/engine/stop').then(LA.refreshState).catch(LA.fail); }

  /* ------------------------------------------------------------ 模型与引擎 */
  var STATUS = { stopped: '未加载', starting: '加载中…', ready: '运行中', error: '出错', stopping: '卸载中…' };
  function tabEngine() {
    var st = LA.S.state || { models: [], engine: {}, hw: {} };
    var e = st.engine || {};
    var statusBox = el('div'), logBox = el('div', { class: 'logbox' }), planBox = el('div');

    function drawStatus() {
      var s = LA.S.state, e = s.engine || {}, models = s.models || [];
      LA.clear(statusBox);
      if (!e.binary) statusBox.appendChild(banner('warn', '没有找到 llama-server。把 llama.cpp 发行包解压到项目的 bin/ 目录，或在下方“高级”里填写可执行文件路径。'));
      if (!models.length) statusBox.appendChild(banner('info', 'models/ 目录里还没有 .gguf 文件。放入后点击“重新扫描”。'));
      var line = el('div', { class: 'toolbar' },
        el('span', { class: 'dot ' + e.status }), el('b', { text: STATUS[e.status] || e.status }),
        e.model ? el('span', { style: { color: 'var(--muted)' }, text: e.model + (e.ctx ? ' · ctx ' + LA.fmtN(e.ctx) : '') + (e.vision ? ' · 视觉' : '') + (e.tps ? ' · ' + e.tps + ' tok/s' : '') }) : null);
      statusBox.appendChild(line);
      if (e.error) statusBox.appendChild(banner('danger', e.error));
      var acts = el('div', { class: 'toolbar' });
      if (e.status === 'ready' || e.status === 'starting') {
        acts.appendChild(btn('重启', restartEngine)); acts.appendChild(btn('停止', stopEngine));
      } else acts.appendChild(btn('加载模型', function () { LA.startEngine(); }, 'primary'));
      acts.appendChild(btn('重新扫描', function () { LA.refreshState().then(function () { show('engine'); LA.toast('已重新扫描'); }); }, 'ghost'));
      statusBox.appendChild(acts);
      if (e.binary_version) statusBox.appendChild(el('div', { class: 'hint2', style: { marginTop: '4px' }, text: 'llama-server ' + e.binary_version + ' · ' + e.binary }));
    }
    function drawLog() {
      LA.get('/api/engine/logs?n=120').then(function (r) {
        var atEnd = logBox.scrollTop + logBox.clientHeight >= logBox.scrollHeight - 12;
        logBox.textContent = (r.lines || []).join('\n') || '（暂无日志）';
        if (atEnd) logBox.scrollTop = logBox.scrollHeight;
      }).catch(function () {});
    }

    pane.appendChild(sec('引擎状态'));
    pane.appendChild(statusBox);
    drawStatus();
    var hw = st.hw || {};
    var hwText = [hw.os, hw.cpu_threads ? hw.cpu_threads + ' 线程' : '', hw.ram_gb ? '内存 ' + hw.ram_gb + ' GB（可用 ' + (hw.ram_free_gb || '?') + '）' : '']
      .concat((hw.gpus || []).map(function (g) { return g.name + ' ' + g.vram_gb + ' GB'; })).filter(Boolean).join(' · ');
    if (hwText) pane.appendChild(el('div', { class: 'hint2', text: '硬件：' + hwText }));

    pane.appendChild(sec('模型'));
    var models = st.models || [];
    var modelSel = el('select', { class: 'inp', style: { maxWidth: '100%' } });
    modelSel.appendChild(el('option', { value: '', text: models.length ? '（自动选择第一个）' : '（没有可用模型）' }));
    models.forEach(function (m) {
      modelSel.appendChild(el('option', { value: m.file, text: m.file + '  ·  ' + m.size_gb + ' GB' + (m.moe ? '  ·  MoE ' + m.experts_used + '/' + m.experts : '') + (m.vision ? '  ·  视觉' : '') }));
    });
    modelSel.value = LA.cfg('engine.model', '') || '';
    var mmSel = el('select', { class: 'inp' });
    function fillMm() {
      LA.clear(mmSel);
      var m = models.filter(function (x) { return x.file === (modelSel.value || (models[0] && models[0].file)); })[0];
      mmSel.appendChild(el('option', { value: 'auto', text: '自动配对' + (m && m.mmproj ? '（' + m.mmproj + '）' : m ? '（未找到）' : '') }));
      mmSel.appendChild(el('option', { value: 'none', text: '不使用（纯文本）' }));
      (st.mmprojs || []).forEach(function (f) { mmSel.appendChild(el('option', { value: f, text: f })); });
      var cur = LA.cfg('engine.mmproj', 'auto'); mmSel.value = cur; if (mmSel.value !== cur) mmSel.value = 'auto';
    }
    fillMm();
    modelSel.addEventListener('change', function () { LA.cfgSet('engine.model', modelSel.value, { immediate: true }); fillMm(); showInfo(); });
    mmSel.addEventListener('change', function () { LA.cfgSet('engine.mmproj', mmSel.value, { immediate: true }); });
    var infoBox = el('div', { class: 'hint2', style: { gridColumn: '1 / -1' } });
    function showInfo() {
      var m = models.filter(function (x) { return x.file === (modelSel.value || (models[0] && models[0].file)); })[0];
      LA.clear(infoBox); if (!m) return;
      infoBox.appendChild(document.createTextNode(m.name + ' · ' + m.arch + ' · ' + m.quant + ' · 训练上下文 ' + LA.fmtN(m.ctx_train) + ' '));
      if (m.moe) infoBox.appendChild(el('span', { class: 'badge moe', text: 'MoE ' + m.experts_used + '/' + m.experts + ' 专家' }));
      if (m.vision) infoBox.appendChild(el('span', { class: 'badge vis', text: '多模态' }));
    }
    pane.appendChild(F('模型文件', modelSel, '放在项目根目录 models/ 下的 .gguf'));
    pane.appendChild(infoBox); showInfo();
    pane.appendChild(F('视觉投影 (mmproj)', mmSel, '多模态模型需要与之配套的 mmproj 文件'));

    pane.appendChild(sec('上下文长度'));
    var ctxVal = LA.cfg('engine.ctx', 'auto');
    var modeSel = el('select', { class: 'inp' }, el('option', { value: 'auto', text: '自动（按需增长）' }), el('option', { value: 'manual', text: '手动固定' }));
    modeSel.value = ctxVal === 'auto' ? 'auto' : 'manual';
    var autoBox = el('div', { class: 'ctl' }), manBox = el('div', { class: 'ctl' });
    function ctxMaxHint() { var m = models.filter(function (x) { return x.file === (modelSel.value || (models[0] && models[0].file)); })[0]; return m ? m.ctx_train : 262144; }
    var rng = el('input', { type: 'range', min: 2048, max: Math.min(ctxMaxHint(), 524288), step: 1024, value: ctxVal === 'auto' ? 32768 : ctxVal });
    var rngTxt = el('span', { text: LA.fmtN(rng.value) });
    rng.addEventListener('input', function () { rngTxt.textContent = LA.fmtN(rng.value); });
    rng.addEventListener('change', function () { LA.cfgSet('engine.ctx', parseInt(rng.value, 10)); });
    manBox.append(rng, rngTxt);
    function drawCtxMode() {
      autoBox.hidden = modeSel.value !== 'auto'; manBox.hidden = modeSel.value !== 'manual';
    }
    modeSel.addEventListener('change', function () {
      drawCtxMode();
      LA.cfgSet('engine.ctx', modeSel.value === 'auto' ? 'auto' : parseInt(rng.value, 10), { immediate: true });
    });
    pane.appendChild(F('模式', modeSel, '自动：从起始值开始，不够用时先裁剪旧工具输出，再自动加倍上下文（重启引擎），最后才压缩摘要。'));
    autoBox.append(
      el('label', { class: 'lb', text: '起始' }), num('engine.ctx_start', { min: 2048, step: 1024 }),
      el('label', { class: 'lb', text: '上限' }), num('engine.ctx_max', { min: 4096, step: 1024 }),
      sw('engine.ctx_auto_grow', '允许自动增长'));
    pane.appendChild(F('自动参数', autoBox, '上限受显存/内存限制；KV 缓存随长度线性增长。', null));
    var manField = F('固定长度', manBox); pane.appendChild(manField);
    // 让手动字段随模式显示
    (function () { var o = drawCtxMode; drawCtxMode = function () { o(); manField.hidden = modeSel.value !== 'manual'; }; })();
    drawCtxMode();

    pane.appendChild(sec('资源预估'));
    pane.appendChild(el('div', { class: 'toolbar' }, btn('预估显存与启动参数', function () {
      LA.flushConfig().then(function () {
        return LA.post('/api/engine/plan', { model: modelSel.value || (models[0] && models[0].file) || '', mmproj: mmSel.value, ctx: modeSel.value === 'auto' ? 'auto' : parseInt(rng.value, 10) });
      }).then(function (r) {
        LA.clear(planBox);
        var p = r.plan || {};
        planBox.appendChild(el('div', { class: 'plan-box' }, el('b', { text: p.summary || '' }),
          p.notes && p.notes.length ? el('ul', null, p.notes.map(function (n) { return el('li', { text: n }); })) : null,
          el('pre', { text: r.command || '' })));
      }).catch(LA.fail);
    })));
    pane.appendChild(planBox);

    var adv = el('details', { style: { marginTop: '10px' } }, el('summary', { text: '高级选项', style: { cursor: 'pointer', fontWeight: 500, padding: '6px 0' } }));
    adv.appendChild(F('llama-server 路径', txt('engine.binary', { wide: true, ph: '留空则在 bin/ 与 PATH 中查找' })));
    adv.appendChild(F('GPU 层数', autoNum('engine.gpu_layers'), '留空 = 自动（--fit 按显存分配）；填数字则手动指定。'));
    adv.appendChild(F('MoE 专家放 CPU 的层数', autoNum('engine.n_cpu_moe'), '留空 = 自动。MoE 模型把专家权重留在内存、注意力放显存，是最划算的拆分。'));
    adv.appendChild(F('视觉投影放 GPU', sel('engine.mmproj_offload', [['auto', '自动'], ['true', '是'], ['false', '否']], function (v) { return v === 'auto' ? 'auto' : v === 'true'; })));
    adv.appendChild(F('Flash Attention', sel('engine.flash_attn', [['auto', '自动'], ['on', '开'], ['off', '关']])));
    adv.appendChild(F('KV 缓存精度', sel('engine.kv_cache_type', [['auto', '自动'], ['f16', 'f16'], ['q8_0', 'q8_0（省显存）'], ['q4_0', 'q4_0（更省，质量略降）']])));
    adv.appendChild(F('CPU 线程', num('engine.threads', { min: 0 }), '0 = 自动'));
    adv.appendChild(F('batch / ubatch', el('span', { class: 'ctl' }, num('engine.batch', { min: 32, step: 32 }), num('engine.ubatch', { min: 32, step: 32 }))));
    adv.appendChild(F('图片最大 token', num('engine.image_max_tokens', { min: 64, step: 64 }), '越大看得越清楚，也越占上下文。'));
    adv.appendChild(F('前缀缓存内存 (MB)', num('engine.cache_ram_mb', { min: 0, step: 256 })));
    adv.appendChild(F('显存预留 (MB)', num('engine.vram_reserve_mb', { min: 0, step: 128 })));
    var extra = el('input', { class: 'inp wide', value: (LA.cfg('engine.extra_args', []) || []).join(' '), placeholder: '例如 --no-mmap --cache-reuse 256', spellcheck: false });
    extra.addEventListener('change', function () { LA.cfgSet('engine.extra_args', extra.value.trim() ? extra.value.trim().split(/\s+/) : []); });
    adv.appendChild(F('额外参数', extra, '--host / --port / --api-key / -m 等安全相关参数会被拒绝。'));
    pane.appendChild(adv);

    pane.appendChild(sec('日志'));
    pane.appendChild(logBox);
    drawLog();
    later(function () { drawStatus(); drawLog(); }, 2000);
    engineRedraw = function () { drawStatus(); };
  }
  LA.bus.on('state', function () { if (modal && !modal.closed && tab === 'engine' && engineRedraw) { engineRedraw(); renderBanner(); } });

  /* ------------------------------------------------------------ 生成 */
  function tabGen() {
    pane.appendChild(sec('采样'));
    pane.appendChild(F('思考模式', sel('generation.thinking', [['auto', '自动（模型默认）'], ['on', '开启'], ['off', '关闭']]), '推理模型会先“想”再答：更准但更慢。关闭可明显加快简单任务。'));
    pane.appendChild(F('temperature', num('generation.temperature', { float: true, step: 0.05, min: 0, max: 2 })));
    pane.appendChild(F('top_p', num('generation.top_p', { float: true, step: 0.05, min: 0, max: 1 })));
    pane.appendChild(F('top_k', num('generation.top_k', { min: 0 })));
    pane.appendChild(F('min_p', num('generation.min_p', { float: true, step: 0.01, min: 0, max: 1 })));
    pane.appendChild(F('presence_penalty', num('generation.presence_penalty', { float: true, step: 0.1, min: -2, max: 2 }), '部分量化模型出现重复循环时可设为 0.5~1.5。'));
    pane.appendChild(F('单次最大输出 token', num('generation.max_tokens', { min: 256, step: 256 })));
    pane.appendChild(sec('Agent'));
    pane.appendChild(F('每轮最多工具步数', num('agent.max_steps', { min: 1, max: 200 }), '防止模型陷入无限循环。'));
    pane.appendChild(F('自动生成标题', sw('agent.auto_title', '用首条消息生成'), '使用规则截取，不额外调用模型，避免挤掉前缀缓存。'));
    pane.appendChild(F('自动召回记忆', sw('agent.memory_auto_recall', '每次提问前检索相关记忆')));
    pane.appendChild(F('召回条数', num('agent.memory_recall_k', { min: 0, max: 20 })));
    pane.appendChild(F('追加系统提示', txt('agent.system_prompt_extra', { area: true, rows: 5 }), '写入你的偏好或规则，例如“回答用中文”“改文件前先说明计划”。会放进系统提示的末尾。', 'stack'));
  }

  /* ------------------------------------------------------------ 安全 */
  function tabSecurity() {
    var sb = (LA.S.state && LA.S.state.sandbox) || {};
    pane.appendChild(sec('权限与沙箱'));
    pane.appendChild(F('默认权限模式', sel('agent.permission_mode', [['ask', '逐项确认（推荐）'], ['auto_edit', '自动编辑'], ['plan', '只读规划'], ['auto', '全自动（谨慎）']]),
      '也可以在输入框下方随时切换。无论哪种模式，敏感文件（.env、密钥、.git 等）与不可信上下文下的操作都会被拦截或强制确认。'));
    pane.appendChild(F('命令沙箱', sel('security.command_sandbox', [['auto', '自动（可用时启用）'], ['off', '关闭']])));
    pane.appendChild(el('div', { class: 'banner ' + (sb.available ? 'info' : 'warn') }, el('span', {
      text: sb.available ? '命令沙箱可用：' + sb.kind + '。' + (sb.note || '') : '当前系统没有可用的命令沙箱。命令将在受限环境变量下直接运行，仅靠“确认 + 黑名单”保护——请保持“逐项确认”模式，且不要放行来路不明的命令。' + (sb.note ? ' ' + sb.note : '')
    })));
    pane.appendChild(F('允许联网命令', sw('security.allow_network_commands', 'curl / wget / git clone 等'), '默认关闭；开启后仍需确认。联网读取的内容会被标记为不可信。'));
    pane.appendChild(F('命令超时（秒）', num('security.command_timeout_s', { min: 5, max: 3600 })));
    pane.appendChild(F('单文件大小上限 (MB)', num('security.max_file_mb', { min: 1, max: 500 })));
    pane.appendChild(F('回收站保留（天）', num('security.trash_retention_days', { min: 0, max: 365 }), '删除的文件先进入回收站，可在 工作区面板→变更 里恢复。'));
    pane.appendChild(sec('提示注入防护'));
    pane.appendChild(F('污染追踪', sw('security.taint_tracking', '读取网页/文档后强制确认写入与命令'), '网页、搜索结果、Office/PDF 与上传文件里可能藏有“给模型的指令”。开启后，读过它们的会话里任何修改都需你点头。'));
    pane.appendChild(F('屏蔽远程图片', sw('security.block_remote_images', '不加载回答里的外链图片'), '避免通过图片 URL 把数据带出去。'));
    pane.appendChild(sec('审计日志'));
    var out = el('div');
    pane.appendChild(el('div', { class: 'toolbar' },
      btn('查看日志', function () {
        LA.get('/api/audit?n=300').then(function (r) {
          LA.clear(out);
          var box = el('div', { class: 'logbox', style: { height: '260px' } });
          (r.entries || []).slice().reverse().forEach(function (x) {
            box.appendChild(el('div', { class: 'audit-row', text: new Date((x.ts || 0) * 1000).toLocaleString() + '  ' + (x.event || '') + '  ' + Object.keys(x).filter(function (k) { return ['ts', 'event', 'prev', 'hash', 'seq'].indexOf(k) < 0; }).map(function (k) { return k + '=' + (typeof x[k] === 'object' ? JSON.stringify(x[k]) : x[k]); }).join(' ') }));
          });
          if (!r.entries || !r.entries.length) box.textContent = '（暂无记录）';
          out.appendChild(box);
        }).catch(LA.fail);
      }),
      btn('校验完整性', function () {
        LA.get('/api/audit/verify').then(function (r) { r.ok ? LA.toast('哈希链完整：' + r.count + ' 条记录未被篡改', 'ok', 3500) : LA.toast('检测到日志被篡改或损坏！' + (r.detail || ''), 'err', 6000); }).catch(LA.fail);
      }, 'ghost')));
    pane.appendChild(out);
  }

  /* ------------------------------------------------------------ 搜索 */
  function tabSearch() {
    var all = Object.keys(SEARCH_ENGINES);
    function enabled() { return (LA.cfg('search.providers', []) || []).filter(function (p) { return SEARCH_ENGINES[p]; }); }
    var listBox = el('div', { class: 'plist' });
    function setProviders(l) { LA.cfgSet('search.providers', l, { immediate: true }); drawList(); }
    function drawList() {
      LA.clear(listBox);
      var on = enabled(), order = on.concat(all.filter(function (p) { return on.indexOf(p) < 0; }));
      order.forEach(function (p) {
        var info = SEARCH_ENGINES[p], isOn = on.indexOf(p) >= 0, i = on.indexOf(p);
        var keyOk = !info.key || LA.cfg('search.keys.' + info.key, false) && (p !== 'google_cse' || LA.cfg('search.keys.google_cx', false));
        var needs = p === 'searxng' ? !LA.cfg('search.searxng_url', '') : !keyOk;
        var cb = el('input', { type: 'checkbox' }); cb.checked = isOn;
        cb.addEventListener('change', function () { var l = enabled().filter(function (x) { return x !== p; }); if (cb.checked) l.push(p); setProviders(l); });
        listBox.appendChild(el('div', { class: 'prow' + (isOn ? '' : ' off') }, cb,
          el('div', { class: 'nm' }, info.name, needs ? el('span', { class: 'badge warn', text: '未配置' }) : null, el('small', { text: info.note })),
          isOn ? el('button', { class: 'icon-btn', title: '上移', html: icon('up', 14), disabled: i === 0, on: { click: function () { var l = enabled(); l.splice(i - 1, 0, l.splice(i, 1)[0]); setProviders(l); } } }) : null,
          isOn ? el('button', { class: 'icon-btn', title: '下移', html: icon('down', 14), disabled: i === on.length - 1, on: { click: function () { var l = enabled(); l.splice(i + 1, 0, l.splice(i, 1)[0]); setProviders(l); } } }) : null));
      });
    }
    pane.appendChild(sec('搜索引擎'));
    pane.appendChild(el('div', { class: 'hint2', style: { marginBottom: '8px' }, text: '勾选启用并上下调整优先级。无密钥的引擎通过解析网页结果页实现，页面改版时可能失效——此时请改用 SearXNG 或 API 类引擎。' }));
    pane.appendChild(listBox); drawList();
    pane.appendChild(F('多引擎策略', sel('search.mode', [['merge', '合并（RRF 融合多引擎结果）'], ['first', '依次尝试，取第一个成功的']])));
    pane.appendChild(F('每次结果数', num('search.max_results', { min: 1, max: 30 })));
    pane.appendChild(F('地区', txt('search.region', { ph: '例如 cn-zh、us-en、wt-wt' }), '语法随引擎不同，留空为默认。'));
    pane.appendChild(F('安全搜索', sel('search.safe', [['moderate', '适中'], ['strict', '严格'], ['off', '关闭']])));
    pane.appendChild(F('代理', txt('search.proxy', { wide: true, ph: 'http://127.0.0.1:7890（可选，访问 Google/DDG 等需要时填写）' }), '仅支持 HTTP 代理。对搜索与网页抓取生效。'));
    pane.appendChild(F('SearXNG 地址', txt('search.searxng_url', { wide: true, ph: 'http://127.0.0.1:8080' }), '自建实例需在 settings.yml 中启用 json 格式。'));
    pane.appendChild(sec('API 密钥'));
    pane.appendChild(el('div', { class: 'hint2', style: { marginBottom: '4px' }, text: '密钥单独保存在 data/secrets.json（权限 0600），界面不会回显。' }));
    [['brave', 'Brave Search Key'], ['google_cse', 'Google API Key'], ['google_cx', 'Google 搜索引擎 ID (cx)'], ['tavily', 'Tavily Key'], ['serper', 'Serper Key']].forEach(function (k) {
      var isSet = !!LA.cfg('search.keys.' + k[0], false);
      var i = el('input', { class: 'inp', type: k[0] === 'google_cx' ? 'text' : 'password', autocomplete: 'off', placeholder: isSet ? '已设置（留空保持不变）' : '未设置', spellcheck: false });
      i.addEventListener('change', function () { if (!i.value.trim()) return; LA.cfgSet('search.keys.' + k[0], i.value.trim(), { immediate: true }); i.value = ''; LA.flushConfig().then(function () { show('search'); }); });
      var ctl = [i];
      if (isSet) ctl.push(btn('清除', function () { LA.cfgSet('search.keys.' + k[0], '', { immediate: true }); LA.flushConfig().then(function () { show('search'); }); }, 'ghost'));
      pane.appendChild(F(k[1], ctl));
    });
    pane.appendChild(sec('测试'));
    var q = el('input', { class: 'inp', value: 'llama.cpp release', style: { flex: 1, minWidth: '160px' } });
    var ps = el('select', { class: 'inp' }, el('option', { value: '', text: '已启用的全部' }));
    all.forEach(function (p) { ps.appendChild(el('option', { value: p, text: SEARCH_ENGINES[p].name })); });
    var res = el('div');
    var go = btn('搜索', function () {
      go.disabled = true; LA.clear(res); res.appendChild(el('div', { class: 'hint2', text: '搜索中…' }));
      LA.flushConfig().then(function () { return LA.post('/api/search/test', { query: q.value, provider: ps.value || undefined }); }).then(function (r) {
        LA.clear(res);
        Object.keys(r.errors || {}).forEach(function (p) { res.appendChild(banner('warn', (SEARCH_ENGINES[p] ? SEARCH_ENGINES[p].name : p) + '：' + r.errors[p])); });
        (r.results || []).forEach(function (x) {
          res.appendChild(el('div', { class: 'sresult' }, el('a', { href: x.url, target: '_blank', rel: 'noopener noreferrer nofollow', text: x.title || x.url }), el('small', { text: (x.engine ? '[' + x.engine + '] ' : '') + x.url }), el('div', { text: x.snippet || '' })));
        });
        if (!(r.results || []).length && !Object.keys(r.errors || {}).length) res.appendChild(el('div', { class: 'hint2', text: '没有结果' }));
      }).catch(function (e) { LA.clear(res); LA.fail(e); }).then(function () { go.disabled = false; });
    }, 'primary');
    q.addEventListener('keydown', function (e) { if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) go.click(); });
    pane.appendChild(el('div', { class: 'toolbar' }, q, ps, go));
    pane.appendChild(res);
  }

  /* ------------------------------------------------------------ 外观 */
  function tabMcp() { LA.mcpTab(pane, { F: F, btn: btn, sec: sec, banner: banner }); }

  function tabUi() {
    pane.appendChild(sec('外观'));
    pane.appendChild(F('主题', sel('ui.theme', [['auto', '跟随系统'], ['light', '浅色'], ['dark', '深色']])));
    var fs = el('input', { type: 'range', min: 13, max: 19, step: 1, value: LA.cfg('ui.font_size', 15) });
    var fv = el('span', { text: fs.value + 'px' });
    fs.addEventListener('input', function () { fv.textContent = fs.value + 'px'; LA.cfgSet('ui.font_size', parseInt(fs.value, 10)); });
    pane.appendChild(F('字号', [fs, fv]));
    pane.appendChild(sec('快捷键'));
    var keys = [['Enter', '发送'], ['Shift + Enter', '换行'], ['/', '选择技能'], ['Ctrl/⌘ + Shift + O', '新对话'], ['Ctrl/⌘ + B', '收起/展开侧栏'], ['Esc', '关闭弹窗 / 拒绝当前审批']];
    keys.forEach(function (k) { pane.appendChild(F(k[0], el('span', { text: k[1], style: { color: 'var(--muted)' } }))); });
    pane.appendChild(sec('关于'));
    pane.appendChild(el('div', { class: 'hint2', text: 'LocalAgent ' + ((LA.S.state && LA.S.state.version) || '') + ' · 所有数据保存在本机 data/ 目录；界面不加载任何外部资源。' }));
  }
})();
