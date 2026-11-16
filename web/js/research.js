/* Research Engine & 网络账本检查器面板（M5）。 */
(function () {
  'use strict';
  var LA = window.LA, el = LA.el, icon = LA.icon;
  var R = LA.researchPanel = {};
  var activeSse = null;
  var selectedId = null;

  function closeSse() {
    if (typeof activeSse === 'function') activeSse();
    activeSse = null;
  }

  function sid() { return LA.S.cur ? LA.S.cur.id : ''; }

  function fmtTime(ts) {
    if (!ts) return '-';
    return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }

  function connectSse(rid, onUpdate) {
    closeSse();
    if (!rid) return;
    activeSse = LA.sse('/api/research/' + encodeURIComponent(rid) + '/events', {
      research_plan: function () { onUpdate(); },
      research_query: function () { onUpdate(); },
      research_fetch: function () { onUpdate(); },
      research_blocked: function () { onUpdate(); },
      research_done: function () {
        closeSse();
        onUpdate();
      }
    });
  }

  function renderReportWithCitations(reportMd, sources, box) {
    var html = LA.renderMarkdown ? LA.renderMarkdown(reportMd || '') : LA.esc(reportMd || '');
    var wrap = el('div', { class: 'res-report md', html: html });
    // 将 [n] 引用转换为可点击按钮，高亮对应来源卡片
    var map = {};
    (sources || []).forEach(function (s) { map[String(s.ref_num)] = s; });
    wrap.innerHTML = wrap.innerHTML.replace(/\[(\d+)\]/g, function (m, num) {
      var s = map[num];
      if (!s) return m;
      return '<button type="button" class="res-cite-link" data-ref="' + num + '" title="' + LA.esc(s.title || s.url) + '">[' + num + ']</button>';
    });
    wrap.addEventListener('click', function (e) {
      var btn = e.target.closest('.res-cite-link');
      if (!btn) return;
      var ref = btn.getAttribute('data-ref');
      var card = box.querySelector('[data-src-ref="' + ref + '"]');
      if (card) {
        card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        card.classList.add('flash');
        setTimeout(function () { card.classList.remove('flash'); }, 1200);
      }
    });
    return wrap;
  }

  R.renderResearch = function (host) {
    closeSse();
    var mode = LA.cfg('research.mode', 'off');
    var offline = !!(LA.S.state && LA.S.state.offline);

    var head = el('div', { class: 'res-head' },
      el('div', { class: 'res-mode-bar' },
        el('span', { class: 'badge ' + (offline || mode === 'off' ? 'warn' : 'moe'), text: offline ? '离线模式 (--offline)' : ('策略: ' + mode) }),
        el('button', { class: 'btn sm ghost', type: 'button', text: LA.t('research.policy', '策略设置'), on: { click: function () { LA.settings && LA.settings.open && LA.settings.open('search'); } } })
      )
    );

    var qInp = el('input', { class: 'inp', placeholder: LA.t('research.placeholder', '输入需要多源调研与交叉验证的问题…'), spellcheck: false, style: { flex: '1', minWidth: '140px' } });
    var depthSel = el('select', { class: 'inp' },
      el('option', { value: 'quick', text: '快速 (quick)' }),
      el('option', { value: 'standard', selected: true, text: '标准 (standard)' }),
      el('option', { value: 'deep', text: '深度 (deep)' })
    );
    var goBtn = el('button', { class: 'btn sm primary', type: 'button', text: LA.t('research.start', '开始调研'), disabled: offline || mode === 'off' });
    var form = el('div', { class: 'toolbar' }, qInp, depthSel, goBtn);

    var body = el('div', { class: 'res-body' }, el('div', { class: 'hint2', text: '加载中…' }));
    host.append(head, form, body);

    if (offline || mode === 'off') {
      body.insertBefore(el('div', { class: 'banner warn', text: offline ? '当前处于 --offline 模式，禁止一切联网调研。' : 'research.mode 默认为 off（关闭）。请在“搜索”设置中改为 open / allowlist / ask 后开启。' }), body.firstChild);
    }

    function drawDetail(snap) {
      if (!body.isConnected) return;
      LA.clear(body);
      var t = snap.task;
      var srcs = snap.sources || [];
      var clms = snap.claims || [];
      var pending = snap.pending_domains || [];
      var blocked = t.blocked_domains || {};

      var top = el('div', { class: 'res-task-head' },
        el('div', { class: 'res-task-title', text: t.question }),
        el('div', { class: 'ms-sub' },
          el('span', { class: 'ms-pill st-' + (t.status === 'completed' ? 'completed' : (t.status === 'running' ? 'running' : 'blocked')), text: t.status }),
          el('span', { class: 'hint2', text: '来源 ' + srcs.length + ' · 论断 ' + clms.length }),
          t.status === 'running' ? el('button', { class: 'btn sm danger', type: 'button', text: '停止', on: { click: function () { LA.post('/api/research/' + encodeURIComponent(t.id) + '/stop').then(refreshDetail).catch(LA.fail); } } }) : null
        )
      );
      body.appendChild(top);

      if (pending.length) {
        pending.forEach(function (dom) {
          body.appendChild(el('div', { class: 'ms-apr-card' },
            el('div', { class: 'ms-apr-info' }, el('b', { text: '等待域批准: ' + dom })),
            el('button', { class: 'btn sm primary', type: 'button', text: '批准域名', on: { click: function () {
              LA.post('/api/research/' + encodeURIComponent(t.id) + '/approve-domain', { domain: dom }).then(drawDetail).catch(LA.fail);
            } } })
          ));
        });
      }

      if ((t.sub_questions || []).length) {
        var sqWrap = el('div', { class: 'res-sec' }, el('div', { class: 'dr-sec', text: '子问题树' }));
        t.sub_questions.forEach(function (sq, idx) {
          sqWrap.appendChild(el('div', { class: 'res-sq', text: (idx + 1) + '. ' + sq }));
        });
        body.appendChild(sqWrap);
      }

      if (Object.keys(blocked).length) {
        var blkWrap = el('div', { class: 'res-sec' }, el('div', { class: 'dr-sec', text: '受限 / 跳过站点（不绕过）' }));
        Object.keys(blocked).forEach(function (d) {
          blkWrap.appendChild(el('div', { class: 'res-blk-row' }, el('code', { text: d }), el('span', { class: 'muted', text: blocked[d] })));
        });
        body.appendChild(blkWrap);
      }

      if (t.cite_check && typeof t.cite_check.ok === 'boolean') {
        body.appendChild(el('div', { class: 'art-sec-bar' },
          el('span', { class: 'art-sec-pill ' + (t.cite_check.ok ? '' : 'warn'), text: t.cite_check.ok ? ('cite_check 通过 (' + (t.cite_check.checked || 0) + ' 条引用)') : 'cite_check 发现异常' }),
          el('span', { class: 'hint2', text: '覆盖率 ' + Math.round((t.cite_check.score || 0) * 100) + '%' })
        ));
      }

      if (t.report_md) {
        body.appendChild(el('div', { class: 'dr-sec', text: '调研报告（点击 [n] 定位证据）' }));
        body.appendChild(renderReportWithCitations(t.report_md, srcs, body));
      }

      if (srcs.length) {
        body.appendChild(el('div', { class: 'dr-sec', text: '来源与证据摘录' }));
        srcs.forEach(function (s) {
          var card = el('div', { class: 'res-src-card', 'data-src-ref': String(s.ref_num) },
            el('div', { class: 'res-src-top' },
              el('span', { class: 'badge', text: '[' + s.ref_num + '] ' + (s.domain || '-') }),
              el('span', { class: 'badge ' + (s.robots_ok ? 'moe' : 'warn'), text: s.robots_ok ? 'robots:OK' : 'robots:deny' }),
              el('span', { class: 'hint2', text: '可信度 ' + Math.round((s.cred_score || 0) * 100) + '% · ' + fmtTime(s.fetched_at) })
            ),
            el('a', { class: 'res-src-link', href: s.url, target: '_blank', rel: 'noopener noreferrer nofollow', text: s.title || s.url }),
            s.excerpt ? el('div', { class: 'res-src-excerpt', text: s.excerpt }) : el('div', { class: 'hint2', text: '（受 noarchive/noai 指令约束，未保留正文摘录）' })
          );
          body.appendChild(card);
        });
      }
    }

    function refreshDetail() {
      if (!selectedId) return;
      LA.get('/api/research/' + encodeURIComponent(selectedId)).then(function (snap) {
        drawDetail(snap);
        if (snap.task && snap.task.status === 'running') connectSse(selectedId, refreshDetail);
      }).catch(function () {});
    }

    function loadList() {
      var q = sid() ? '?session_id=' + encodeURIComponent(sid()) : '';
      LA.get('/api/research' + q).then(function (r) {
        var tasks = r.tasks || [];
        if (!tasks.length) {
          LA.clear(body);
          body.appendChild(el('div', { class: 'dr-empty', text: LA.t('research.empty', '暂无调研任务。输入问题并点击「开始调研」，或使用 /research 命令。') }));
          return;
        }
        if (!selectedId || !tasks.some(function (x) { return x.id === selectedId; })) {
          selectedId = tasks[0].id;
        }
        refreshDetail();
      }).catch(function (e) {
        LA.clear(body);
        body.appendChild(el('div', { class: 'dr-empty', text: e.message }));
      });
    }

    goBtn.addEventListener('click', function () {
      var q = qInp.value.trim();
      if (!q) return;
      goBtn.disabled = true;
      LA.post('/api/research', { question: q, depth: depthSel.value, session_id: sid() }).then(function (snap) {
        qInp.value = '';
        selectedId = snap.task.id;
        drawDetail(snap);
        connectSse(selectedId, refreshDetail);
      }).catch(LA.fail).then(function () { goBtn.disabled = offline || mode === 'off'; });
    });
    qInp.addEventListener('keydown', function (e) { if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) goBtn.click(); });
    loadList();
  };

  R.renderNetLedger = function (host) {
    closeSse();
    var domInp = el('input', { class: 'inp', placeholder: '按域名过滤…', style: { flex: '1', minWidth: '100px' }, spellcheck: false });
    var decSel = el('select', { class: 'inp' },
      el('option', { value: '', text: '全部状态' }),
      el('option', { value: 'allow', text: '仅放行' }),
      el('option', { value: 'deny', text: '仅策略拒绝' }),
      el('option', { value: 'blocked', text: '仅访问受限' })
    );
    var refBtn = el('button', { class: 'btn sm ghost', type: 'button', text: '刷新' });
    var expBtn = el('button', { class: 'btn sm ghost', type: 'button', text: '导出 CSV' });
    var sumBox = el('div', { class: 'net-summary' });
    var listBox = el('div', { class: 'net-list' }, el('div', { class: 'hint2', text: '加载中…' }));
    host.append(el('div', { class: 'toolbar' }, domInp, decSel, refBtn, expBtn), sumBox, listBox);

    function load() {
      var qs = [];
      if (domInp.value.trim()) qs.push('domain=' + encodeURIComponent(domInp.value.trim()));
      if (decSel.value) qs.push('decision=' + encodeURIComponent(decSel.value));
      qs.push('limit=100');
      LA.get('/api/net/ledger?' + qs.join('&')).then(function (r) {
        if (!listBox.isConnected) return;
        var sm = r.summary || {};
        LA.clear(sumBox);
        sumBox.append(
          el('span', { class: 'badge', text: '请求 ' + (sm.total || 0) }),
          el('span', { class: 'badge moe', text: '放行 ' + (sm.allowed || 0) }),
          el('span', { class: 'badge warn', text: '拦截 ' + (sm.blocked || 0) }),
          el('span', { class: 'badge', text: '缓存 ' + (sm.cached || 0) }),
          el('span', { class: 'hint2', text: LA.fmtSize ? LA.fmtSize(sm.total_bytes || 0) : ((sm.total_bytes || 0) + ' B') })
        );
        LA.clear(listBox);
        var entries = r.entries || [];
        if (!entries.length) {
          listBox.appendChild(el('div', { class: 'dr-empty', text: LA.t('net.empty', '暂无出站联网记录。') }));
          return;
        }
        entries.forEach(function (x) {
          var cls = (x.decision === 'deny' || x.decision === 'blocked') ? 'warn' : (x.cached ? '' : 'moe');
          var lbl = x.cached ? 'CACHED' : String(x.decision || '').toUpperCase();
          listBox.appendChild(el('div', { class: 'net-row' },
            el('div', { class: 'net-row-top' },
              el('span', { class: 'badge ' + cls, text: lbl }),
              el('code', { text: x.method + ' ' + (x.status || '-') }),
              el('span', { class: 'net-dom', text: x.domain || x.url }),
              el('span', { class: 'hint2', text: fmtTime(x.ts) })
            ),
            el('div', { class: 'net-row-sub', text: (x.purpose ? '[' + x.purpose + '] ' : '') + (x.reason || x.url) })
          ));
        });
      }).catch(function (e) {
        LA.clear(listBox);
        listBox.appendChild(el('div', { class: 'dr-empty', text: e.message }));
      });
    }

    refBtn.addEventListener('click', load);
    decSel.addEventListener('change', load);
    domInp.addEventListener('keydown', function (e) { if (e.key === 'Enter') load(); });
    expBtn.addEventListener('click', function () {
      window.open('/api/net/ledger?format=csv', '_blank', 'noopener');
    });
    load();
  };

  R.close = closeSse;
})();
