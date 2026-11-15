/* 对话视图：渲染会话 JSON、处理运行事件流（流式 / 工具 / 审批 / 提问 / 任务清单）。 */
(function () {
  'use strict';
  var LA = window.LA, el = LA.el, icon = LA.icon;

  var col, scrollEl, toBottom;
  var stick = true;
  var raf = {};            // 待渲染块（rAF 合并）
  LA.live = null;          // {runId, sid, close, view, model}

  var TOOL_ICON = {
    read_file: 'fileText', write_file: 'edit', edit_file: 'edit', edit_office: 'edit',
    write_document: 'fileText', write_spreadsheet: 'table', write_presentation: 'fileText',
    list_dir: 'folder', glob: 'search', grep: 'search', run_command: 'terminal',
    delete_path: 'trash', move_path: 'folder', copy_path: 'copy', make_dir: 'folder',
    web_search: 'globe', web_fetch: 'globe',
    memory_save: 'brain', memory_search: 'brain', memory_forget: 'brain',
    use_skill: 'puzzle', read_skill_file: 'puzzle', create_skill: 'puzzle',
    todo_write: 'list', ask_user: 'help'
  };

  function fmtDur(ms) {
    if (!ms && ms !== 0) return '';
    return ms >= 1000 ? (ms / 1000).toFixed(1) + 's' : ms + 'ms';
  }

  /* ---------------------------------------------------------------- 滚动 */
  function nearBottom() { return scrollEl.scrollHeight - scrollEl.scrollTop - scrollEl.clientHeight < 90; }
  LA.chat = {
    scrollToBottom: function (force) {
      if (force) stick = true;
      if (stick) { scrollEl.scrollTop = scrollEl.scrollHeight; toBottom.hidden = true; }
      else if (LA.live) toBottom.hidden = false;
    },
    init: function () {
      col = LA.$('#col'); scrollEl = LA.$('#scroll'); toBottom = LA.$('#toBottom');
      toBottom.innerHTML = icon('down') + '<span>回到底部</span>';
      scrollEl.addEventListener('scroll', function () { stick = nearBottom(); if (stick) toBottom.hidden = true; });
      toBottom.addEventListener('click', function () { LA.chat.scrollToBottom(true); });
      LA.bus.on('state', function () { LA.chat.updateEmptyCards(); });
    },
    isEmpty: function () { return !!LA.$('.empty', col); }
  };

  function schedule(key, fn) {
    if (raf[key]) { raf[key].fn = fn; return; }
    raf[key] = { fn: fn };
    requestAnimationFrame(function () { var f = raf[key].fn; delete raf[key]; f(); LA.chat.scrollToBottom(); });
  }

  /* ---------------------------------------------------------------- 空状态 */
  function greeting() {
    var h = new Date().getHours();
    return h < 5 ? '夜深了' : h < 11 ? '早上好' : h < 13 ? '中午好' : h < 18 ? '下午好' : '晚上好';
  }

  var QUICK_CARDS = [
    {
      ic: 'code',
      title: '代码审查与解释',
      desc: '审查工作区代码结构与主要逻辑，指出潜在缺陷并给出重构方案',
      prompt: '请帮我审查当前工作区的代码结构与主要逻辑，指出潜在问题并给出优化建议。'
    },
    {
      ic: 'folder',
      title: '整理工作区结构',
      desc: '梳理目录结构、文件组织与依赖关系，生成详尽的 README 说明',
      prompt: '梳理当前工作区文件夹里有哪些内容，并生成一份结构清晰的 README.md 说明文档。'
    },
    {
      ic: 'globe',
      title: '深入调研与简报',
      desc: '结合网络搜索与本地文档，快速撰写客观严谨的对比调研简报',
      prompt: '结合网络搜索和本地资料，针对当前主题做一次深度调研，列出核心要点与对比总结。'
    },
    {
      ic: 'chart',
      title: '数据分析与洞察',
      desc: '读取并清洗工作区表格数据，计算核心指标并提炼关键洞察',
      prompt: '读取工作区中的数据与表格文件，分析关键指标并以表格和要点形式给出总结。'
    }
  ];

  LA.chat.renderEmpty = function () {
    LA.clear(col);

    var welcomeWrap = el('div', { class: 'empty' });

    // 品牌与硬件状态徽章
    var st = LA.S.state || {}, e = (st && st.engine) || {};
    var modelName = e.model ? String(e.model).replace(/\.gguf$/i, '') : '';
    var statusText = e.status === 'ready' ? ('已就绪 · ' + (modelName || '本地模型')) : (e.status === 'starting' ? '正在启动模型…' : '模型待加载');

    var emblem = el('div', { class: 'welcome-emblem' },
      el('span', { class: 'welcome-spark', html: LA.SPARK }),
      el('span', { class: 'welcome-status-pill' + (e.status === 'ready' ? ' ready' : '') },
        el('span', { class: 'dot ' + (e.status || 'stopped') }),
        el('span', { text: statusText })
      )
    );

    var h1 = el('h1', null, el('span', { class: 'g', html: LA.SPARK.replace('<svg', '<svg style="width:.75em;height:.75em;fill:currentColor;vertical-align:-.02em"') }), greeting());
    var pSub = el('p', { class: 'welcome-sub', text: '本地运行，数据不出设备。你的私有 AI 协作工作台。' });

    // 引导卡片容器（ui_smoke 依赖 #emptyCards）
    var emptyCards = el('div', { class: 'cards', id: 'emptyCards' });

    // 快捷建议卡片网格
    var suggestGrid = el('div', { class: 'welcome-grid' });
    QUICK_CARDS.forEach(function (c) {
      var item = el('button', {
        class: 'quick-card',
        type: 'button',
        on: {
          click: function () {
            LA.composer.setText(c.prompt);
            LA.composer.focus();
          }
        }
      },
      el('div', { class: 'qc-icon', html: icon(c.ic, 18) }),
      el('div', { class: 'qc-text' },
        el('b', { class: 'qc-title', text: c.title }),
        el('span', { class: 'qc-desc', text: c.desc })
      ));
      suggestGrid.appendChild(item);
    });

    // 快速命令提示栏
    var cmdHints = el('div', { class: 'cmd-hints' },
      el('span', { class: 'cmd-hint-title', text: '常用快捷指令' }),
      el('button', { class: 'cmd-pill', type: 'button', text: '/init 初始化', on: { click: function () { LA.composer.setText('/init '); LA.composer.focus(); } } }),
      el('button', { class: 'cmd-pill', type: 'button', text: '/review 代码评审', on: { click: function () { LA.composer.setText('/review '); LA.composer.focus(); } } }),
      el('button', { class: 'cmd-pill', type: 'button', text: '@引用文件', on: { click: function () { LA.panels.pickWorkspace(); } } }),
      el('span', { class: 'cmd-tip', text: '输入 / 选择技能 · Enter 发送' })
    );

    welcomeWrap.append(emblem, h1, pSub, emptyCards, suggestGrid, cmdHints);
    col.appendChild(welcomeWrap);

    LA.chat.updateEmptyCards();
  };

  LA.chat.updateEmptyCards = function () {
    var box = LA.$('#emptyCards'); if (!box || !LA.S.state) return;
    LA.clear(box);
    var st = LA.S.state, e = st.engine, cur = LA.S.cur;
    var hasWs = !!((cur && cur.workspace) || st.workspace);
    function card(ic, title, body, btn) {
      box.appendChild(el('div', { class: 'card' },
        el('span', { class: 'ci', html: icon(ic, 20) }),
        el('div', { class: 'card-body' }, el('b', { text: title }), body, btn || null)
      ));
    }
    if (!st.models.length) {
      card('cpu', '还没有模型',
        el('p', { html: '把 <code>.gguf</code> 模型文件放进项目根目录的 <code>models/</code> 文件夹（多模态模型请把配套的 <code>mmproj-*.gguf</code> 放在同一目录），然后刷新。' }),
        el('button', { class: 'btn sm', text: '重新扫描', on: { click: function () { LA.refreshState(); } } })
      );
    }
    if (!e.binary) {
      card('zap', '没有找到 llama-server',
        el('p', { html: '请把 llama.cpp 发行包解压到 <code>bin/</code> 目录（含 <code>llama-server</code>），或在 设置→模型与引擎 里指定路径。' }),
        el('button', { class: 'btn sm', text: '打开设置', on: { click: function () { LA.settings.open('engine'); } } })
      );
    }
    if (!hasWs) {
      card('folder', '选择一个工作区',
        el('p', { text: 'Agent 只能在你选定的文件夹里读写文件、运行命令，保证环境安全可控。' }),
        el('button', { class: 'btn sm primary', text: '选择工作区', on: { click: function () { LA.panels.pickWorkspace(); } } })
      );
    }
    if (st.models.length && e.binary && (e.status === 'stopped' || e.status === 'error')) {
      card(e.status === 'error' ? 'alert' : 'play', e.status === 'error' ? '引擎启动失败' : '模型尚未加载',
        e.status === 'error' ? el('p', { text: e.error || '未知错误' }) : el('p', { text: '加载后即可开始对话。首次加载大模型可能需要几十秒。' }),
        el('button', { class: 'btn sm primary', text: '加载模型', on: { click: function () { LA.startEngine(); } } })
      );
    }
  };

  /* ---------------------------------------------------------------- 会话渲染 */
  var CHAT_WINDOW = 32;
  LA.chat.renderSession = function (view, opts) {
    opts = opts || {};
    LA.clear(col);
    var msgs = view.messages || [];
    if (view.active_run && msgs.length && msgs[msgs.length - 1].role === 'assistant' && msgs[msgs.length - 1].status === 'running') msgs = msgs.slice(0, -1);
    if (!msgs.length && !view.active_run) { LA.chat.renderEmpty(); return; }
    var wrap = el('div', { class: 'msgs', id: 'msgs' });
    col.appendChild(wrap);
    var start = Math.max(0, msgs.length - CHAT_WINDOW);
    var moreBar = null;
    function renderSlice(fromIdx, toIdx, beforeNode) {
      var frag = document.createDocumentFragment();
      for (var i = fromIdx; i < toIdx; i++) {
        var m = msgs[i];
        frag.appendChild(m.role === 'user' ? renderUser(m, i) : new AstView(m, i).el);
      }
      if (beforeNode) wrap.insertBefore(frag, beforeNode);
      else wrap.appendChild(frag);
    }
    if (start > 0) {
      moreBar = el('div', { class: 'msg-more-bar' });
      function updateMoreBar() {
        LA.clear(moreBar);
        if (start <= 0) { moreBar.remove(); return; }
        var step = Math.min(start, CHAT_WINDOW);
        moreBar.appendChild(el('button', {
          class: 'btn sm ghost',
          type: 'button',
          text: '显示更早 ' + step + ' 条消息（共隐藏 ' + start + ' 条）',
          on: {
            click: function () {
              var prevH = scrollEl.scrollHeight;
              var nextStart = Math.max(0, start - CHAT_WINDOW);
              renderSlice(nextStart, start, moreBar.nextSibling);
              start = nextStart;
              updateMoreBar();
              scrollEl.scrollTop += Math.max(0, scrollEl.scrollHeight - prevH);
            }
          }
        }));
      }
      wrap.appendChild(moreBar);
      updateMoreBar();
    }
    renderSlice(start, msgs.length, null);
    stick = true;
    requestAnimationFrame(function () { scrollEl.scrollTop = scrollEl.scrollHeight; });
  };
  function msgsWrap() {
    var w = LA.$('#msgs', col);
    if (!w) { LA.clear(col); w = el('div', { class: 'msgs', id: 'msgs' }); col.appendChild(w); }
    return w;
  }

  function renderUser(m, idx) {
    var d = el('div', { class: 'msg user', dataset: { idx: idx } });
    if (m.images && m.images.length) d.appendChild(el('div', { class: 'u-imgs' }, m.images.map(function (u) { return el('img', { src: u, alt: '图片' }); })));
    var chips = [];
    (m.files || []).forEach(function (f) { chips.push(el('span', { class: 'attach-chip' }, el('span', { html: icon(LA.fileIcon ? LA.fileIcon(f) : 'file', 14) }), el('span', { text: f }))); });
    (m.skills || []).forEach(function (s) { chips.push(el('span', { class: 'attach-chip skill' }, el('span', { html: icon('puzzle', 14) }), el('span', { text: '/' + s }))); });
    if (chips.length) d.appendChild(el('div', { class: 'u-chips' }, chips));
    if (m.text) d.appendChild(el('div', { class: 'bubble', text: m.text }));
    d.appendChild(el('div', { class: 'msg-acts' },
      el('button', { class: 'act', html: icon('edit', 14) + ' 编辑', on: { click: function () { editFrom(idx, m); } } }),
      el('button', { class: 'act', html: icon('copy', 14) + ' 复制', on: { click: function () { LA.copyText(m.text || ''); } } })));
    return d;
  }

  function guardIdle() { if (LA.live) { LA.toast('请先等待当前回答完成或点击停止'); return false; } return true; }
  function editFrom(idx, m) {
    if (!guardIdle()) return;
    LA.post('/api/sessions/' + LA.S.cur.id + '/truncate', { from_index: idx }).then(function (v) {
      LA.setSession(v);
      if (LA.composer.setDraft) LA.composer.setDraft({ text: m.text || '', images: m.images || [], files: m.files || [], skills: m.skills || [] });
      else LA.composer.setText(m.text || '');
      LA.composer.focus();
    }).catch(LA.fail);
  }
  function regenerate(idx) {
    if (!guardIdle()) return;
    var msgs = LA.S.cur.messages, u = idx - 1;
    if (u < 0 || msgs[u].role !== 'user') return;
    var mu = msgs[u], text = mu.text || '';
    LA.post('/api/sessions/' + LA.S.cur.id + '/truncate', { from_index: u }).then(function (v) {
      LA.setSession(v);
      LA.composer.sendText(text, { keepAttachments: true, images: mu.images || [], files: mu.files || [], skills: mu.skills || [] });
    }).catch(LA.fail);
  }

  /* ---------------------------------------------------------------- 助手消息视图 */
  function AstView(model, idx) {
    this.model = model; this.idx = idx; this.views = []; this.lastType = null; this.toolsWrap = null; this.todoEl = null;
    this.body = el('div', { class: 'ast-body' });
    this.usageEl = el('div', { class: 'usage' });
    this.acts = el('div', { class: 'msg-acts' });
    this.el = el('div', { class: 'msg assistant', dataset: { idx: idx } },
      el('div', { class: 'ast-head' }, el('span', { class: 'avatar', html: LA.SPARK })), this.body, this.usageEl, this.acts);
    var self = this;
    (model.blocks || []).forEach(function (b) { self.addBlock(b); });
    if (model.error) this.showError(model.error);
    if (model.status === 'cancelled') this.addNote('已停止', true);
    this.renderFooter();
  }
  AstView.prototype.addBlock = function (b) {
    var v;
    if (b.type === 'tool') {
      if (!this.toolsWrap || this.lastType !== 'tool') { this.toolsWrap = el('div', { class: 'tools' }); this.body.appendChild(this.toolsWrap); }
      v = mkTool(b); this.toolsWrap.appendChild(v.el);
    } else {
      this.toolsWrap = null;
      v = b.type === 'thinking' ? mkThinking(b) : mkText(b);
      this.body.appendChild(v.el);
    }
    this.lastType = b.type; this.views.push(v);
    return v;
  };
  AstView.prototype.addNote = function (text, warn) {
    this.toolsWrap = null; this.lastType = 'note';
    this.body.appendChild(el('div', { class: 'pill-row' }, el('span', { class: 'pill-note' + (warn ? ' warn' : ''), text: text })));
  };
  AstView.prototype.showError = function (msg) { this.body.appendChild(el('div', { class: 'err-box', text: msg })); };
  AstView.prototype.setTodos = function (items) {
    if (!items || !items.length) return;
    if (!this.todoEl) { this.todoEl = el('details', { class: 'todo-card', open: true }); this.body.insertBefore(this.todoEl, this.body.firstChild); }
    var done = items.filter(function (i) { return i.status === 'done'; }).length;
    LA.clear(this.todoEl);
    this.todoEl.appendChild(el('summary', null, el('span', { html: icon('list', 15) }), '任务清单 ' + done + '/' + items.length));
    this.todoEl.appendChild(todoList(items));
  };
  AstView.prototype.renderFooter = function () {
    var m = this.model, u = m.usage, self = this;
    LA.clear(this.usageEl);
    if (u && (u.prompt_tokens || u.completion_tokens)) {
      var inPill = el('span', { class: 'u-pill', text: '输入 ' + LA.fmtN(u.prompt_tokens) });
      var outPill = el('span', { class: 'u-pill', text: '输出 ' + LA.fmtN(u.completion_tokens) });
      this.usageEl.append(inPill, outPill);
      if (u.tps) {
        this.usageEl.appendChild(el('span', { class: 'u-pill tps', text: u.tps + ' tok/s' }));
      }
      if (u.ttft_ms) {
        var ttft = u.ttft_ms >= 1000 ? (u.ttft_ms / 1000).toFixed(1) + 's' : u.ttft_ms + 'ms';
        this.usageEl.appendChild(el('span', { class: 'u-pill', text: '首字 ' + ttft }));
      }
      if (u.cache_hit_rate != null && u.cache_hit_rate > 0) {
        this.usageEl.appendChild(el('span', { class: 'u-pill kv', text: 'KV 缓存 ' + Math.round(u.cache_hit_rate * 100) + '%' }));
      }
    }
    LA.clear(this.acts);
    if (m.status === 'running') return;
    this.acts.appendChild(el('button', { class: 'act', html: icon('copy', 14) + ' 复制回答', on: { click: function () {
      LA.copyText((m.blocks || []).filter(function (b) { return b.type === 'text'; }).map(function (b) { return b.text; }).join('\n\n')); } } }));
    this.acts.appendChild(el('button', { class: 'act', html: icon('refresh', 14) + ' 重新生成', on: { click: function () { regenerate(self.idx); } } }));
  };

  function todoList(items) {
    var ul = el('ul', { class: 'todo-list' });
    items.forEach(function (i) {
      ul.appendChild(el('li', { class: i.status },
        el('span', { class: 'st', html: i.status === 'done' ? icon('check', 11) : '' }),
        el('span', { class: 'tx', text: i.text })
      ));
    });
    return ul;
  }
  LA.chat.todoList = todoList;

  /* ---------------------------------------------------------------- 块：思考 / 文本 / 工具 */
  function mkThinking(b) {
    var chevEl = el('span', { class: 'chev', html: icon('chev', 14) });
    var pulseEl = el('span', { class: 'think-pulse' });
    var lblEl = el('span', { class: 'lbl' });
    var metaEl = el('span', { class: 'think-meta' });
    var sum = el('summary', null, pulseEl, chevEl, lblEl, metaEl);
    var body = el('div', { class: 'tbody' });
    var d = el('details', { class: 'think' }, sum, body);
    var v = { el: d, type: 'thinking', b: b };

    v.update = function () {
      body.textContent = b.text || '';
      var charCount = b.text ? b.text.length : 0;
      var timeSec = b.ms >= 500 ? (b.ms / 1000).toFixed(1) + 's' : '';
      if (b.live) {
        d.classList.add('live');
        lblEl.textContent = '深度思考中…';
        metaEl.textContent = timeSec ? (timeSec + (charCount ? ' · ' + charCount + ' 字' : '')) : '';
      } else {
        d.classList.remove('live');
        lblEl.textContent = '已思考';
        metaEl.textContent = (timeSec ? timeSec : '') + (charCount ? (timeSec ? ' · ' : '') + charCount + ' 字' : '');
      }
      if (b.live) body.scrollTop = body.scrollHeight;
    };
    if (b.live) d.open = true;
    v.update();
    return v;
  }

  function splitMarkdownBlocks(src) {
    var lines = String(src || '').split('\n'), fence = null, cur = [], out = [];
    for (var i = 0; i < lines.length; i++) {
      var ln = lines[i], trimmed = ln.trim();
      var fm = /^(```+|~~~+)/.exec(trimmed);
      if (fm) {
        if (!fence) fence = fm[1][0];
        else if (trimmed[0] === fence && /^[`~]+\s*$/.test(trimmed)) fence = null;
        cur.push(ln);
      } else if (!fence && trimmed === '') {
        if (cur.length) { out.push(cur.join('\n')); cur = []; }
      } else {
        cur.push(ln);
      }
    }
    if (cur.length) out.push(cur.join('\n'));
    return out;
  }

  function linkifyWorkspacePaths(rootEl) {
    var ws = (LA.S.cur && LA.S.cur.workspace) || (LA.S.state && LA.S.state.workspace);
    if (!ws || !LA.panels || !LA.panels.openFile) return;
    var codes = rootEl.querySelectorAll('code');
    var rx = /^([A-Za-z0-9_.\-/\\]+\.[A-Za-z0-9]{1,8})(?::(\d+))?$/;
    for (var i = 0; i < codes.length; i++) {
      var c = codes[i];
      if (c.closest('pre') || c.classList.contains('file-ref')) continue;
      var txt = (c.textContent || '').trim();
      var m = rx.exec(txt);
      if (!m || m[1].indexOf('://') >= 0) continue;
      c.classList.add('file-ref');
      c.title = '在工作区面板打开 ' + m[1];
      (function (fpath, lno) {
        c.addEventListener('click', function (ev) {
          ev.stopPropagation();
          LA.panels.openFile(fpath.replace(/\\/g, '/'), lno ? parseInt(lno, 10) : 0);
        });
      })(m[1], m[2]);
    }
  }

  function mkText(b) {
    var d = el('div', { class: 'md' });
    var stableWrap = null, tailEl = null, blockNodes = [], lastTail = '', lastRendered = null, linkifiedFinal = false;
    var v = { el: d, type: 'text', b: b };
    v.update = function () {
      var txt = b.text || '';
      if (b.live || stableWrap) {
        if (!stableWrap) {
          LA.clear(d);
          stableWrap = el('div', { class: 'md-stable' });
          tailEl = el('div', { class: 'md-tail' });
          d.append(stableWrap, tailEl);
        }
        var chunks = splitMarkdownBlocks(txt);
        var committed = b.live ? (chunks.length > 1 ? chunks.slice(0, -1) : []) : chunks;
        var tail = b.live ? (chunks[chunks.length - 1] || '') : '';
        var common = 0;
        while (common < blockNodes.length && common < committed.length && blockNodes[common]._raw === committed[common]) {
          common++;
        }
        while (blockNodes.length > common) {
          var popNode = blockNodes.pop();
          popNode.remove();
        }
        for (var j = common; j < committed.length; j++) {
          var blk = el('div', { class: 'md-blk', html: LA.renderMarkdown(committed[j]) });
          blk._raw = committed[j];
          if (!b.live) linkifyWorkspacePaths(blk);
          stableWrap.appendChild(blk);
          blockNodes.push(blk);
        }
        if (tail !== lastTail) {
          lastTail = tail;
          tailEl.innerHTML = tail ? LA.renderMarkdown(tail) : '';
        }
        if (!b.live) {
          if (tailEl) { tailEl.remove(); tailEl = null; }
          if (!linkifiedFinal) {
            linkifiedFinal = true;
            for (var k = 0; k < common; k++) linkifyWorkspacePaths(blockNodes[k]);
          }
        }
      } else if (lastRendered !== txt) {
        lastRendered = txt;
        d.innerHTML = LA.renderMarkdown(txt);
        linkifyWorkspacePaths(d);
      }
      d.classList.toggle('cursor', !!b.live);
    };
    v.update();
    return v;
  }

  function mkTool(b) {
    var open = false;
    var head = el('button', { class: 't-head', 'aria-expanded': 'false', type: 'button' });
    var ti = el('span', { class: 'ti' }), tt = el('span', { class: 'tt' }), ts = el('span', { class: 'ts' }), ch = el('span', { class: 'chev', html: icon('chev', 14) });
    head.append(ti, tt, ts, ch);
    var body = el('div', { class: 't-body' });
    var apBox = el('div');
    var root = el('div', { class: 'tool' }, head, body, apBox);
    var v = { el: root, type: 'tool', b: b };
    var apId = null;

    head.addEventListener('click', function () {
      open = !open;
      root.classList.toggle('open', open);
      head.setAttribute('aria-expanded', open ? 'true' : 'false');
    });

    v.update = function () {
      root.className = 'tool ' + b.status + (open ? ' open' : '');
      ti.innerHTML = icon(TOOL_ICON[b.name] || 'zap', 16);
      tt.textContent = b.title || b.name;
      var s = b.status;
      LA.clear(ts);

      if (s === 'running') {
        ts.innerHTML = '<span class="tool-badge running"><span class="spin"></span><span>运行中</span></span>';
      } else if (s === 'ok') {
        ts.innerHTML = '<span class="tool-badge ok">' + icon('check', 14) + '<span>完成</span></span>';
      } else if (s === 'error') {
        ts.innerHTML = '<span class="tool-badge error">' + icon('alert', 14) + '<span>失败</span></span>';
      } else if (s === 'denied') {
        ts.innerHTML = '<span class="tool-badge denied">已拒绝</span>';
      } else if (s === 'pending_approval') {
        ts.innerHTML = '<span class="tool-badge pending">' + icon('lock', 14) + '<span>等待确认</span></span>';
      }

      if (b.duration_ms && s !== 'running') {
        ts.appendChild(el('span', { class: 'dur-pill', text: fmtDur(b.duration_ms) }));
      }

      if (b.status === 'pending_approval' && !open) {
        open = true;
        root.classList.add('open');
        head.setAttribute('aria-expanded', 'true');
      }

      // 详情
      LA.clear(body);
      var args = b.args && Object.keys(b.args).length ? JSON.stringify(b.args, null, 2) : '';
      if (args) {
        var argHdr = el('div', { class: 't-sec-head' }, el('h6', { text: '调用参数' }),
          el('button', { class: 'btn-mini-act', text: '复制', type: 'button', on: { click: function (e) { e.stopPropagation(); LA.copyText(args); } } }));
        body.appendChild(argHdr);
        body.appendChild(el('pre', { text: args.length > 3000 ? args.slice(0, 3000) + '\n…' : args }));
      }
      if (b.content) {
        var resHdr = el('div', { class: 't-sec-head' }, el('h6', { text: '执行结果' }),
          el('button', { class: 'btn-mini-act', text: '复制', type: 'button', on: { click: function (e) { e.stopPropagation(); LA.copyText(b.content); } } }));
        body.appendChild(resHdr);
        body.appendChild(el('pre', { text: b.content }));
      } else if (s === 'running' && b.live_output) {
        if (!open) { open = true; root.classList.add('open'); head.setAttribute('aria-expanded', 'true'); }
        var liveHdr = el('div', { class: 't-sec-head' }, el('h6', { text: '实时输出' }));
        var livePre = el('pre', { class: 't-live', text: b.live_output });
        body.append(liveHdr, livePre);
        livePre.scrollTop = livePre.scrollHeight;
      }
      if (b.substeps && b.substeps.length) {
        if (s === 'running' && !open) { open = true; root.classList.add('open'); head.setAttribute('aria-expanded', 'true'); }
        var subHdr = el('div', { class: 't-sec-head' }, el('h6', { text: '子代理执行轨迹（' + b.substeps.length + '）' }));
        var subList = el('div', { class: 't-substeps' });
        b.substeps.forEach(function (st) {
          var ic = st.status === 'running' ? '<span class="spin"></span>'
            : st.status === 'ok' ? icon('check', 13) : icon('alert', 13);
          subList.appendChild(el('div', { class: 't-substep ' + (st.status || 'ok') },
            el('span', { class: 't-sub-ic', html: ic }),
            el('span', { class: 't-sub-title', text: st.title || st.name || '' }),
            st.duration_ms ? el('span', { class: 'dur-pill', text: fmtDur(st.duration_ms) }) : null));
        });
        body.append(subHdr, subList);
      }
      var meta = b.meta || {};
      var chips = el('div', { class: 't-meta' });
      if (meta.path) chips.appendChild(el('span', { class: 'meta-chip', text: meta.path }));
      if (meta.sandbox) chips.appendChild(el('span', { class: 'meta-chip sandbox', text: '沙箱: ' + meta.sandbox }));
      if (meta.undo_id) chips.appendChild(el('button', { class: 'btn sm', html: icon('undo', 13) + ' 撤销操作', on: { click: function (e) { e.stopPropagation(); undo(meta.undo_id, chips); } } }));
      if (chips.childNodes.length) body.appendChild(chips);

      // 审批卡片与拒绝后引导
      if (b.approval) {
        if (apId !== b.approval.approval_id) {
          LA.clear(apBox);
          apBox.appendChild(approvalCard(b));
          apId = b.approval.approval_id;
        }
      } else if (s === 'denied') {
        apId = null;
        LA.clear(apBox);
        var denyActs = [el('span', { class: 'hint2', text: '操作已被拒绝 · ' }),
          el('button', { class: 'btn sm', type: 'button', text: '重述替代方案', on: { click: function () {
            if (LA.composer && LA.composer.setText) {
              LA.composer.setText('请不要执行刚才被拒绝的操作（' + (b.title || b.name) + '），改用只读排查或更安全的方案：');
              LA.composer.focus();
            }
          } } })];
        var effMode = (LA.S.cur && LA.S.cur.permission_mode) || LA.cfg('agent.permission_mode', 'ask');
        if (effMode === 'ask' && LA.setSessionMode) {
          denyActs.push(el('button', { class: 'btn sm ghost', type: 'button', text: '本会话改为自动编辑', on: { click: function () {
            LA.setSessionMode('auto_edit').then(function () { LA.toast('当前会话已切换为“自动编辑”模式', 'ok'); });
          } } }));
        }
        apBox.appendChild(el('div', { class: 'deny-actions' }, denyActs));
      } else if (apId || apBox.childNodes.length) {
        LA.clear(apBox);
        apId = null;
      }
    };
    v.update();
    return v;
  }

  function undo(id, chips, force) {
    LA.post('/api/journal/' + id + '/undo', force ? { force: true } : {}).then(function () {
      LA.toast('已成功撤销', 'ok');
      chips.innerHTML = '';
      chips.appendChild(el('span', { class: 'meta-chip', text: '已撤销' }));
      LA.bus.emit('fschanged');
    }).catch(function (e) {
      if (e.status === 409 && !force) {
        LA.confirm((e.message || '').replace(/^conflict:\s*/, '') + '\n\n仍要强制撤销吗？这会覆盖之后的修改。', { danger: true, okText: '强制撤销' }).then(function (ok) {
          if (ok) undo(id, chips, true);
        });
      } else {
        LA.fail(e);
      }
    });
  }

  function diffView(text) {
    var d = el('div', { class: 'diff' });
    String(text || '').split('\n').forEach(function (ln) {
      var c = /^(\+\+\+|---)/.test(ln) ? 'hunk' : ln[0] === '+' ? 'add' : ln[0] === '-' ? 'del' : /^@@/.test(ln) ? 'hunk' : '';
      d.appendChild(el('span', { class: 'l ' + c, text: ln || ' ' }));
    });
    return d;
  }

  function approvalCard(b) {
    var a = b.approval, done = false;
    var card = el('div', { class: 'approval', tabIndex: 0, role: 'group', 'aria-label': '操作确认' });
    var riskLabels = { high: '高风险', medium: '中风险', low: '低风险' };
    var reasonHdr = el('div', { class: 'reason' },
      el('div', { class: 'reason-title' },
        el('span', { class: 'reason-ic', html: icon('shield', 16) }),
        el('b', { text: '需要你的操作确认' }),
        el('span', { class: 'risk ' + a.risk, text: riskLabels[a.risk] || a.risk })
      ),
      a.reason ? el('div', { class: 'reason-desc', text: a.reason }) : null
    );
    card.appendChild(reasonHdr);

    if (a.tainted) {
      card.appendChild(el('div', { class: 'taint-banner' },
        el('span', { class: 'tb-icon', html: icon('alert', 16) }),
        el('span', { text: '当前上下文读入过网页或文档等外部不可信内容。请仔细核验这一步指令，确认其符合你的预期。' })
      ));
    }

    var pv = a.preview || {};
    if (pv.kind === 'diff') card.appendChild(diffView(pv.text));
    else if (pv.text) card.appendChild(el('div', { class: 'cmdbox', text: pv.text }));

    function decide(dec) {
      if (done) return; done = true;
      LA.$$('button', card).forEach(function (x) { x.disabled = true; });
      LA.post('/api/approvals/' + a.approval_id, { decision: dec }).catch(function (e) {
        done = false;
        LA.$$('button', card).forEach(function (x) { x.disabled = false; });
        LA.fail(e);
      });
    }

    var ok = el('button', { class: 'btn primary', type: 'button', text: '允许一次', on: { click: function () { decide('allow'); } } });
    var sess = el('button', { class: 'btn', type: 'button', text: '本会话允许', title: '本会话内对相同类型的操作不再询问（敏感文件/不可信上下文除外）', on: { click: function () { decide('allow_session'); } } });
    var no = el('button', { class: 'btn danger', type: 'button', text: '拒绝', on: { click: function () { decide('deny'); } } });
    card.appendChild(el('div', { class: 'approve-btns' }, ok, sess, no));

    card.addEventListener('keydown', function (e) {
      if (e.target !== card) return;
      if (e.key === 'Enter') { e.preventDefault(); decide('allow'); }
      else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); decide('deny'); }
    });

    setTimeout(function () {
      var ae = document.activeElement;
      if (!ae || ae === document.body || (ae.id === 'input' && !ae.value)) {
        try { card.focus({ preventScroll: true }); } catch (e) {}
      }
    }, 50);

    return card;
  }

  function questionCard(d, view) {
    var done = false;
    var box = el('div', { class: 'question' },
      el('div', { class: 'q-head' }, el('span', { html: icon('help', 16) }), el('span', { class: 'q', text: d.question }))
    );
    function answer(t) {
      if (done || !t) return; done = true;
      LA.post('/api/questions/' + d.question_id, { answer: t }).then(function () {
        box.remove();
        view.addNote('你的回答: ' + t);
      }).catch(function (e) { done = false; LA.fail(e); });
    }
    if (d.options && d.options.length) {
      box.appendChild(el('div', { class: 'opts' }, d.options.map(function (o) {
        return el('button', { class: 'btn sm', type: 'button', text: o, on: { click: function () { answer(o); } } });
      })));
    }
    var inp = el('input', { class: 'inp', placeholder: '或者输入你的自定义回答…' });
    inp.addEventListener('keydown', function (e) { if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) answer(inp.value.trim()); });
    box.appendChild(el('div', { class: 'free' }, inp, el('button', { class: 'btn sm primary', type: 'button', text: '回答', on: { click: function () { answer(inp.value.trim()); } } })));
    return box;
  }

  /* ---------------------------------------------------------------- 运行事件流 */
  LA.chat.startRun = function (runId, sid, opts) {
    opts = opts || {};
    var model = { id: 'live', role: 'assistant', status: 'running', blocks: [], usage: null, error: null };
    var wrap = msgsWrap();
    var idx = LA.S.cur.messages.length;
    if (opts.pushModel !== false) LA.S.cur.messages.push(model);
    var view = new AstView(model, idx);
    wrap.appendChild(view.el);
    view.el.classList.add('live');
    LA.chat.scrollToBottom(true);
    var cur = { thinking: null, text: null };
    var live = LA.live = { runId: runId, sid: sid, view: view, model: model, close: null };
    LA.bus.emit('runstate', true);

    function endBlocks() {
      if (cur.thinking) { cur.thinking.b.live = false; cur.thinking.update(); cur.thinking.el.open = false; cur.thinking = null; }
      if (cur.text) { cur.text.b.live = false; cur.text.update(); cur.text = null; }
    }
    function sched(v) { schedule(v.b.__k || (v.b.__k = 'b' + Math.random()), function () { v.update(); }); }
    function findTool(id) { for (var i = view.views.length - 1; i >= 0; i--) { var v = view.views[i]; if (v.type === 'tool' && v.b.id === id) return v; } return null; }
    var apQueueBar = null;
    function syncApQueue() {
      var pending = view.views.filter(function (v) { return v.type === 'tool' && v.b.status === 'pending_approval'; });
      if (pending.length >= 2) {
        if (!apQueueBar) {
          apQueueBar = el('div', { class: 'ap-queue-bar' });
          view.el.insertBefore(apQueueBar, view.usageEl);
        }
        LA.clear(apQueueBar);
        apQueueBar.appendChild(el('span', { text: '审批队列：当前有 ' + pending.length + ' 个操作等待确认' }));
        apQueueBar.appendChild(el('button', { class: 'btn sm primary', type: 'button', text: '跳至下一项', on: { click: function () {
          pending[0].el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        } } }));
      } else if (apQueueBar) {
        apQueueBar.remove();
        apQueueBar = null;
      }
    }

    var H = {
      thinking_delta: function (d) {
        if (cur.text) { cur.text.b.live = false; cur.text.update(); cur.text = null; }
        if (!cur.thinking) { var b = { type: 'thinking', text: '', ms: 0, live: true, t0: Date.now() }; model.blocks.push(b); cur.thinking = view.addBlock(b); }
        cur.thinking.b.text += d.text; cur.thinking.b.ms = Date.now() - cur.thinking.b.t0; sched(cur.thinking);
      },
      text_delta: function (d) {
        if (cur.thinking) { cur.thinking.b.live = false; cur.thinking.update(); cur.thinking.el.open = false; cur.thinking = null; }
        if (!cur.text) { var b = { type: 'text', text: '', live: true }; model.blocks.push(b); cur.text = view.addBlock(b); }
        cur.text.b.text += d.text; sched(cur.text);
      },
      tool_start: function (d) {
        endBlocks();
        var b = { type: 'tool', id: d.id, name: d.name, title: d.title, args: d.args, status: 'running', content: '', live_output: '', meta: {}, duration_ms: 0, approval: null };
        model.blocks.push(b); view.addBlock(b); LA.chat.scrollToBottom();
      },
      tool_output_delta: function (d) {
        var v = findTool(d.id); if (!v) return;
        v.b.live_output = ((v.b.live_output || '') + (d.chunk || '')).slice(-16000);
        sched(v);
      },
      subagent_step: function (d) {
        var v = findTool(d.tool_id); if (!v) return;
        v.b.substeps = v.b.substeps || [];
        if (d.phase === 'start') {
          v.b.substeps.push({ call_id: d.call_id || '', name: d.name || '', title: d.title || d.name || '', status: 'running', duration_ms: 0 });
        } else {
          var hit = null;
          for (var i = v.b.substeps.length - 1; i >= 0; i--) {
            if ((d.call_id && v.b.substeps[i].call_id === d.call_id) || v.b.substeps[i].status === 'running') {
              hit = v.b.substeps[i]; break;
            }
          }
          if (hit) {
            hit.status = d.status || 'ok';
            hit.duration_ms = d.duration_ms || 0;
          }
        }
        sched(v);
      },
      approval_request: function (d) {
        var v = findTool(d.tool_id); if (!v) return;
        v.b.status = 'pending_approval'; v.b.approval = { approval_id: d.approval_id, reason: d.reason, risk: d.risk, preview: d.preview, tainted: d.tainted };
        v.update(); syncApQueue(); LA.chat.scrollToBottom(true);
        if (document.hidden && 'Notification' in window && Notification.permission === 'granted') { try { new Notification('LocalAgent 需要你确认', { body: d.title }); } catch (e) {} }
        document.title = '⏸ 需要确认 · LocalAgent';
      },
      approval_resolved: function (d) {
        var v = findTool(d.tool_id); if (!v) return; v.b.approval = null; v.b.status = d.decision === 'deny' ? 'denied' : 'running'; v.update(); syncApQueue();
        document.title = 'LocalAgent';
      },
      tool_result: function (d) {
        var v = findTool(d.id); if (!v) return;
        v.b.status = d.status; v.b.content = d.content; v.b.live_output = ''; v.b.meta = d.meta || {}; v.b.duration_ms = d.duration_ms; v.b.approval = null; v.update(); syncApQueue();
        if ((d.meta && (d.meta.path || d.meta.undo_id)) || d.name === 'run_command') LA.bus.emit('fschanged');
      },
      question: function (d) { endBlocks(); view.toolsWrap = null; view.lastType = 'note'; view.body.appendChild(questionCard(d, view)); LA.chat.scrollToBottom(true); },
      todo_update: function (d) { LA.S.cur.todos = d.items; view.setTodos(d.items); LA.bus.emit('todos', d.items); },
      usage: function (d) {
        model.usage = { prompt_tokens: d.prompt_tokens, completion_tokens: d.completion_tokens, tps: d.tps, cache_hit_rate: d.cache_hit_rate, ttft_ms: d.ttft_ms };
        view.renderFooter(); LA.S.cur.context = { ctx: d.ctx, used: d.used, pct: d.pct }; LA.bus.emit('context', LA.S.cur.context);
      },
      context_event: function (d) { endBlocks(); view.addNote(d.detail, d.kind === 'trimmed'); if (d.kind === 'grow_done') LA.refreshState(); },
      notice: function (d) { view.addNote(d.text, d.level === 'warn'); },
      taint: function () { LA.S.cur.tainted = true; LA.bus.emit('taint', true); },
      assistant_done: function () { endBlocks(); },
      run_end: function (d) {
        endBlocks(); close();
        model.status = d.status === 'cancelled' ? 'cancelled' : d.status === 'error' ? 'error' : 'done';
        if (d.error) { model.error = d.error; view.showError(d.error); }
        if (d.status === 'cancelled') view.addNote('已停止', true);
        view.el.classList.remove('live'); view.renderFooter();
        LA.live = null; document.title = 'LocalAgent'; LA.bus.emit('runstate', false);
        LA.get('/api/sessions/' + sid).then(function (v) {
          if (LA.S.cur && LA.S.cur.id === sid) {
            LA.S.cur.title = v.title;
            LA.S.cur.context = v.context;
            LA.S.cur.tainted = v.tainted;
            LA.S.cur.todos = v.todos;
            LA.bus.emit('sessionmeta', v);
          }
        }).catch(function () {});
        LA.refreshSessions(); LA.bus.emit('fschanged');
        if (d.status === 'done' && document.hidden && 'Notification' in window && Notification.permission === 'granted') { try { new Notification('LocalAgent 已完成'); } catch (e) {} }
        if (d.status !== 'cancelled' && LA.composer && LA.composer.drainQueue) setTimeout(LA.composer.drainQueue, 20);
      }
    };

    var lastSeq = 0;
    var wrapped = {};
    Object.keys(H).forEach(function (k) {
      wrapped[k] = function (d, ev) {
        if (ev && ev.lastEventId) {
          var n = parseInt(ev.lastEventId, 10);
          if (!isNaN(n)) { if (n <= lastSeq) return; lastSeq = n; }
        }
        H[k](d, ev);
      };
    });

    var closeFn = function () {};
    function connectSse(afterSeq) {
      closeFn();
      closeFn = LA.sse('/api/runs/' + runId + '/events?after=' + (afterSeq || 0), wrapped, {
        onerror: function (final) {
          if (final && LA.live && LA.live.runId === runId) {
            var retryBtn = el('button', { class: 'btn sm', text: '重连', on: { click: function () {
              wrapErr.remove();
              LA.live = live; model.status = 'running'; view.el.classList.add('live'); LA.bus.emit('runstate', true);
              connectSse(lastSeq);
            } } });
            var wrapErr = el('div', { class: 'sse-retry' }, el('span', { text: '与服务的事件流连接中断' }), retryBtn);
            LA.live = null; model.status = 'error'; view.el.classList.remove('live');
            view.body.appendChild(wrapErr); view.renderFooter(); LA.bus.emit('runstate', false);
          }
        }
      });
    }
    connectSse(0);

    function close() { closeFn(); }
    live.close = close;
    return live;
  };

  /** 切换会话时断开事件流 */
  LA.chat.detach = function () { if (LA.live) { LA.live.close(); LA.live = null; LA.bus.emit('runstate', false); } };
  LA.chat.cancel = function () { if (LA.live) LA.post('/api/runs/' + LA.live.runId + '/cancel').catch(LA.fail); };
  LA.chat.appendUser = function (m) {
    var wrap = msgsWrap();
    var idx = LA.S.cur.messages.length;
    LA.S.cur.messages.push(m);
    wrap.appendChild(renderUser(m, idx));
    LA.chat.scrollToBottom(true);
  };
})();
