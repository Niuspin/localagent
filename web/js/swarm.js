/* Swarm 面板（工作区检查器第 4 标签）：DAG 拓扑可视化、每代理卡片、合并冲突与预算追踪。 */
(function () {
  'use strict';
  var LA = window.LA, el = LA.el, icon = LA.icon;
  var activeSse = null, activeSwarmId = '';

  function closeSse() {
    if (activeSse) { try { activeSse(); } catch (e) {} activeSse = null; }
  }

  function statusColor(st) {
    if (st === 'done') return 'var(--ok)';
    if (st === 'running') return 'var(--accent)';
    if (st === 'failed') return 'var(--danger)';
    if (st === 'blocked' || st === 'cancelled') return 'var(--warn)';
    return 'var(--border-strong)';
  }

  function renderDagSvg(graph) {
    var nodes = (graph && graph.nodes) || [];
    var edges = (graph && graph.edges) || [];
    if (!nodes.length) return null;

    var byId = {};
    nodes.forEach(function (n) { byId[n.id] = n; });
    var level = {};
    function depth(id, seen) {
      if (level[id] != null) return level[id];
      seen = seen || {};
      if (seen[id]) return 0;
      seen[id] = true;
      var n = byId[id];
      var d = 0;
      ((n && n.deps) || []).forEach(function (p) { d = Math.max(d, depth(p, seen) + 1); });
      level[id] = d;
      return d;
    }
    nodes.forEach(function (n) { depth(n.id); });

    var cols = {};
    var maxCol = 0;
    nodes.forEach(function (n) {
      var c = level[n.id] || 0;
      maxCol = Math.max(maxCol, c);
      (cols[c] = cols[c] || []).push(n);
    });

    var pos = {};
    var colW = 112, rowH = 44, maxRows = 1;
    Object.keys(cols).forEach(function (c) {
      maxRows = Math.max(maxRows, cols[c].length);
      cols[c].forEach(function (n, rIdx) {
        pos[n.id] = { x: 14 + (+c) * colW, y: 12 + rIdx * rowH, w: 94, h: 30 };
      });
    });

    var svgW = Math.max(280, (maxCol + 1) * colW + 20);
    var svgH = Math.max(60, maxRows * rowH + 18);
    var ns = 'http://www.w3.org/2000/svg';
    var svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', '0 0 ' + svgW + ' ' + svgH);

    edges.forEach(function (e) {
      var a = pos[e.from], b = pos[e.to];
      if (!a || !b) return;
      var path = document.createElementNS(ns, 'path');
      var x1 = a.x + a.w, y1 = a.y + a.h / 2;
      var x2 = b.x, y2 = b.y + b.h / 2;
      var mx = (x1 + x2) / 2;
      path.setAttribute('d', 'M' + x1 + ' ' + y1 + ' C' + mx + ' ' + y1 + ' ' + mx + ' ' + y2 + ' ' + x2 + ' ' + y2);
      var dst = byId[e.to];
      path.setAttribute('class', 'sw-edge' + (dst && dst.status === 'running' ? ' active' : ''));
      svg.appendChild(path);
    });

    nodes.forEach(function (n) {
      var p = pos[n.id];
      var g = document.createElementNS(ns, 'g');
      var rect = document.createElementNS(ns, 'rect');
      rect.setAttribute('x', p.x); rect.setAttribute('y', p.y);
      rect.setAttribute('width', p.w); rect.setAttribute('height', p.h);
      rect.setAttribute('rx', '7');
      rect.setAttribute('fill', 'var(--bg-elev)');
      rect.setAttribute('stroke', statusColor(n.status));
      rect.setAttribute('stroke-width', n.status === 'running' ? '2.2' : '1.4');
      var txt = document.createElementNS(ns, 'text');
      txt.setAttribute('x', p.x + p.w / 2); txt.setAttribute('y', p.y + 19);
      txt.setAttribute('text-anchor', 'middle');
      txt.setAttribute('fill', 'var(--text)');
      txt.setAttribute('font-size', '10.5');
      txt.textContent = (n.role || 'agent') + ' · ' + n.id.slice(-4);
      g.append(rect, txt);
      svg.appendChild(g);
    });

    return el('div', { class: 'sw-dag' }, svg);
  }

  function openNewSwarmModal(onCreated) {
    var goalInp = el('input', { class: 'inp wide', placeholder: '例如：并行重构 auth 模块并核验测试', spellcheck: false });
    var nodesInp = el('textarea', {
      class: 'inp wide',
      rows: 6,
      spellcheck: false,
      value: JSON.stringify([
        { id: 'plan_1', role: 'planner', goal: '分析代码依赖并输出修改边界' },
        { id: 'rev_1', role: 'reviewer', goal: '检查安全边界与回归测试清单', deps: ['plan_1'] }
      ], null, 2)
    });
    var createBtn = el('button', { class: 'btn primary', type: 'button', text: '启动 Swarm' });
    var m = LA.modal({
      title: LA.t('swarm.new', '新建 Swarm DAG'),
      width: 560,
      body: [
        el('div', { class: 'field stack' }, el('label', { text: 'Swarm 目标' }), goalInp),
        el('div', { class: 'field stack' }, el('label', { text: 'DAG 节点定义 (JSON 数组)' }), nodesInp)
      ],
      footer: [el('button', { class: 'btn sm', type: 'button', text: '取消', on: { click: function () { m.close(); } } }), createBtn]
    });
    createBtn.addEventListener('click', function () {
      var nodes;
      try { nodes = JSON.parse(nodesInp.value); } catch (e) { return LA.toast('JSON 格式错误：' + e.message, 'err'); }
      createBtn.disabled = true;
      LA.post('/api/swarms', {
        session_id: LA.S.cur ? LA.S.cur.id : undefined,
        workspace: (LA.S.cur && LA.S.cur.workspace) || (LA.S.state && LA.S.state.workspace) || '',
        goal: goalInp.value.trim() || 'Swarm 并行任务',
        nodes: nodes
      }).then(function (snap) {
        m.close();
        if (snap && snap.id) activeSwarmId = snap.id;
        LA.toast('Swarm 已启动', 'ok');
        if (onCreated) onCreated();
      }).catch(function (e) { createBtn.disabled = false; LA.fail(e); });
    });
  }

  function renderDetail(container, snap, reload) {
    LA.clear(container);
    var swid = snap.id;
    var isLive = snap.status === 'running' || snap.status === 'paused';

    var ctrls = el('div', { class: 'sw-ctrls' });
    if (snap.status === 'running') {
      ctrls.appendChild(el('button', { class: 'btn sm', type: 'button', html: icon('pause', 13) + ' ' + LA.t('swarm.pause', '暂停'), on: { click: function () {
        LA.post('/api/swarms/' + swid + '/pause').then(reload).catch(LA.fail);
      } } }));
    } else if (snap.status === 'paused') {
      ctrls.appendChild(el('button', { class: 'btn sm primary', type: 'button', html: icon('play', 13) + ' ' + LA.t('swarm.resume', '恢复'), on: { click: function () {
        LA.post('/api/swarms/' + swid + '/resume').then(reload).catch(LA.fail);
      } } }));
    }
    if (isLive) {
      ctrls.appendChild(el('button', { class: 'btn sm danger', type: 'button', text: LA.t('swarm.cancel', '终止'), on: { click: function () {
        LA.post('/api/swarms/' + swid + '/cancel').then(reload).catch(LA.fail);
      } } }));
    }
    ctrls.appendChild(el('button', { class: 'btn sm ghost', type: 'button', text: '+ DAG', on: { click: function () { openNewSwarmModal(reload); } } }));

    container.appendChild(el('div', { class: 'sw-head' },
      el('div', { class: 'sw-title', text: snap.goal || swid, title: snap.id }),
      ctrls));

    // Budget Meter
    var bud = snap.budget || {}, tot = bud.totals || {}, lim = tot.limits || {};
    var ratioPct = Math.min(100, Math.round((+bud.ratio || 0) * 100));
    container.appendChild(el('div', { class: 'sw-budget' },
      el('div', { style: { display: 'flex', justifyContent: 'space-between' } },
        el('span', { text: LA.t('swarm.budget', '集群预算') + ' · ' + snap.status }),
        el('span', { class: 'sw-num', text: (tot.steps || 0) + '/' + (lim.steps || 120) + ' 步 · ' + ratioPct + '%' })),
      el('div', { class: 'sw-bar-track' },
        el('div', { class: 'sw-bar-fill' + (ratioPct >= 80 ? ' hot' : ''), style: { width: ratioPct + '%' } }))));

    var dag = renderDagSvg(snap.graph);
    if (dag) container.appendChild(dag);

    // Agent Cards
    var agWrap = el('div', { class: 'sw-agents' });
    var nodes = (snap.graph && snap.graph.nodes) || [];
    var agBudget = (bud.agents) || {};
    nodes.forEach(function (n) {
      var ab = agBudget[n.id] || {};
      var card = el('div', { class: 'sw-card ' + n.status },
        el('div', { class: 'sw-card-top' },
          el('span', { class: 'sw-role', text: n.role + ' (' + n.id + ')' }),
          n.tainted ? el('span', { class: 'badge warn', text: 'taint' }) : null,
          el('span', { class: 'sw-st ' + n.status, text: n.status })),
        el('div', { class: 'sw-goal', text: n.goal }),
        el('div', { class: 'sw-meta' },
          el('span', { text: '步数 ' + (ab.steps || 0) + '/' + (ab.max_steps || 30) + ' · Token ' + (ab.tokens || 0) }),
          (n.status === 'running' || n.status === 'ready' || n.status === 'pending') ? el('button', {
            class: 'btn-mini-act',
            type: 'button',
            text: '取消节点',
            on: { click: function () { LA.post('/api/swarms/' + swid + '/agents/' + encodeURIComponent(n.id) + '/cancel').then(reload).catch(LA.fail); } }
          }) : null),
        n.error ? el('div', { class: 'hint2', style: { color: 'var(--danger)', marginTop: '4px' }, text: n.error }) : null);
      agWrap.appendChild(card);
    });
    container.appendChild(agWrap);

    // Conflicts
    var pendingConflicts = (snap.conflicts || []).filter(function (c) { return c.status === 'pending'; });
    if (pendingConflicts.length) {
      container.appendChild(el('div', { class: 'dr-sec', text: LA.t('swarm.conflicts', '合并冲突卡') }));
      pendingConflicts.forEach(function (c) {
        container.appendChild(el('div', { class: 'sw-conflict' },
          el('b', { text: '冲突文件：' + (c.files || []).join(', ') }),
          el('div', { class: 'toolbar', style: { marginTop: '6px', marginBottom: 0 } },
            el('button', { class: 'btn sm', type: 'button', text: '保留主工作区', on: { click: function () {
              LA.post('/api/swarms/' + swid + '/conflicts/' + c.id + '/resolve', { decision: 'resolved_ours' }).then(reload).catch(LA.fail);
            } } }),
            el('button', { class: 'btn sm ghost', type: 'button', text: '忽略', on: { click: function () {
              LA.post('/api/swarms/' + swid + '/conflicts/' + c.id + '/resolve', { decision: 'dismissed' }).then(reload).catch(LA.fail);
            } } }))));
      });
    }

    // Mailbox tail
    var mails = (snap.mailbox || []).slice(-6);
    if (mails.length) {
      container.appendChild(el('div', { class: 'dr-sec', text: LA.t('swarm.mailbox', '邮箱通信') }));
      var mbox = el('div', { class: 'sw-mail' });
      mails.forEach(function (m) {
        mbox.appendChild(el('div', { class: 'sw-mail-row', text: '[' + m.kind + '] ' + m.from_agent + ' → ' + m.to_agent + ': ' + (m.body || '').slice(0, 120) }));
      });
      container.appendChild(mbox);
    }
  }

  LA.swarmPanel = {
    close: closeSse,
    render: function (container) {
      closeSse();
      var sid = LA.S.cur ? LA.S.cur.id : '';
      var url = '/api/swarms' + (sid ? '?session_id=' + encodeURIComponent(sid) : '');
      function load() {
        LA.get(url).then(function (r) {
          var list = (r && r.swarms) || [];
          if (!list.length) {
            LA.clear(container);
            container.appendChild(el('div', { class: 'dr-empty' },
              el('p', { text: LA.t('swarm.empty', '当前会话暂无 Swarm 任务') }),
              el('button', { class: 'btn sm primary', type: 'button', text: LA.t('swarm.new', '新建 Swarm DAG'), on: { click: function () { openNewSwarmModal(load); } } })));
            return;
          }
          var picked = list.find(function (x) { return x.id === activeSwarmId; }) || list[0];
          activeSwarmId = picked.id;
          LA.get('/api/swarms/' + encodeURIComponent(picked.id)).then(function (snap) {
            renderDetail(container, snap, load);
            if (snap.status === 'running' || snap.status === 'paused') {
              closeSse();
              var refresh = LA.debounce(load, 300);
              activeSse = LA.sse('/api/swarms/' + encodeURIComponent(picked.id) + '/events', {
                swarm_started: refresh,
                agent_spawned: refresh,
                agent_step: refresh,
                agent_done: refresh,
                agent_blocked: refresh,
                agent_message: refresh,
                merge_started: refresh,
                merge_conflict: refresh,
                budget_warn: refresh,
                swarm_done: load
              }, { onerror: function () {} });
            }
          }).catch(LA.fail);
        }).catch(LA.fail);
      }
      load();
    }
  };
})();
