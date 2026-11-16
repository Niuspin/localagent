/* M2 Mission 运行时前端视图：侧栏 Missions 列表（状态点/进度/预算环）+ 详情时间线/MISSION.md 编辑器/门控结果/回滚/审批。 */
(function () {
  var LA = window.LA, el = LA.el, icon = LA.icon;
  var activeSse = null, activeMissionId = '';

  function statusLabel(st) {
    return ({
      queued: LA.t('mission.st.queued', '排队中'),
      running: LA.t('mission.st.running', '运行中'),
      paused: LA.t('mission.st.paused', '已暂停'),
      blocked: LA.t('mission.st.blocked', '待审批'),
      completed: LA.t('mission.st.completed', '已完成'),
      failed: LA.t('mission.st.failed', '失败'),
      cancelled: LA.t('mission.st.cancelled', '已终止')
    })[st] || st;
  }

  function budgetRing(ratio) {
    var r = Math.max(0, Math.min(1, Number(ratio) || 0));
    var c = 50.27;
    var off = (c * (1 - r)).toFixed(2);
    var cls = r >= 0.8 ? 'ms-ring-fg hot' : 'ms-ring-fg';
    return '<svg class="ms-ring" viewBox="0 0 22 22" width="18" height="18" aria-hidden="true">'
      + '<circle class="ms-ring-bg" cx="11" cy="11" r="8"/>'
      + '<circle class="' + cls + '" cx="11" cy="11" r="8" stroke-dasharray="' + c + '" stroke-dashoffset="' + off + '"/>'
      + '</svg>';
  }

  function closeSse() {
    if (activeSse && activeSse.close) activeSse.close();
    activeSse = null;
  }

  function fmtDur(sec) {
    sec = Math.max(0, Math.round(Number(sec) || 0));
    var h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    if (h) return h + 'h ' + m + 'm';
    if (m) return m + 'm ' + s + 's';
    return s + 's';
  }

  function openNewMissionModal(onCreated) {
    var titleInp = el('input', { class: 'inp wide', placeholder: '例：认证模块长期重构' });
    var goalInp = el('textarea', { class: 'inp wide', rows: 3, placeholder: '描述需要自主分段完成并自动验收的长期编程目标…' });
    var modeSel = el('select', { class: 'inp' },
      el('option', { value: 'attended', text: LA.t('mission.mode.attended', '值守模式 (attended)') }),
      el('option', { value: 'unattended', text: LA.t('mission.mode.unattended', '无人值守 (unattended)') })
    );
    var pathsInp = el('input', { class: 'inp wide', placeholder: '可选允许路径 glob，如: src/**, tests/**' });
    var cmdsInp = el('input', { class: 'inp wide', placeholder: '可选允许命令前缀，如: pytest*, ruff*' });
    var swarmChk = el('input', { type: 'checkbox' });
    var btn = el('button', { class: 'btn primary', type: 'button', text: LA.t('mission.create.submit', '创建并启动 Mission') });

    var modal = LA.modal({
      title: LA.t('mission.new', '新建长期 Mission'),
      width: 560,
      body: el('div', { class: 'col-form' },
        el('div', { class: 'field stack' }, el('label', { text: LA.t('mission.field.title', '标题（可选）') }), titleInp),
        el('div', { class: 'field stack' }, el('label', { text: LA.t('mission.field.goal', '任务目标') }), goalInp),
        el('div', { class: 'field stack' }, el('label', { text: LA.t('mission.field.mode', '运行策略') }), modeSel),
        el('div', { class: 'field stack' }, el('label', { text: 'allow_paths' }), pathsInp),
        el('div', { class: 'field stack' }, el('label', { text: 'allow_commands' }), cmdsInp),
        el('label', { class: 'chk-row' }, swarmChk, el('span', { text: LA.t('mission.field.swarm', '启用 Swarm 多代理协同执行里程碑') })),
        el('div', { class: 'btn-row' }, btn)
      )
    });

    btn.addEventListener('click', function () {
      var goal = goalInp.value.trim();
      if (!goal) return LA.toast('请填写 Mission 目标', 'err');
      btn.disabled = true;
      var splitCsv = function (s) { return (s || '').split(',').map(function (x) { return x.trim(); }).filter(Boolean); };
      var ws = (LA.S.cur && LA.S.cur.workspace) || (LA.S.state && LA.S.state.workspace) || '';
      LA.post('/api/missions', {
        session_id: LA.S.cur ? LA.S.cur.id : undefined,
        workspace: ws,
        title: titleInp.value.trim(),
        goal: goal,
        mode: modeSel.value,
        use_swarm: !!swarmChk.checked,
        policy: {
          mode: modeSel.value,
          allow_paths: splitCsv(pathsInp.value),
          allow_commands: splitCsv(cmdsInp.value)
        },
        auto_start: true
      }).then(function (snap) {
        modal.close();
        if (snap && snap.id) activeMissionId = snap.id;
        LA.toast('Mission 已启动', 'ok');
        LA.refreshMissions();
        if (onCreated) onCreated(snap);
      }).catch(function (e) { btn.disabled = false; LA.fail(e); });
    });
  }

  function renderBudgetBars(b) {
    b = b || {};
    var dims = [
      { key: 'Wall', used: fmtDur(b.used_wall_s), lim: fmtDur(b.wall_s), r: (b.used_wall_s || 0) / Math.max(1, b.wall_s || 1) },
      { key: 'Tokens', used: LA.fmtN(b.used_tokens || 0), lim: LA.fmtN(b.tokens || 0), r: (b.used_tokens || 0) / Math.max(1, b.tokens || 1) },
      { key: 'Steps', used: String(b.used_steps || 0), lim: String(b.steps || 0), r: (b.used_steps || 0) / Math.max(1, b.steps || 1) },
      { key: 'Disk', used: (Number(b.used_disk_mb || 0)).toFixed(1) + 'M', lim: (Number(b.disk_mb || 0)).toFixed(0) + 'M', r: (b.used_disk_mb || 0) / Math.max(1, b.disk_mb || 1) }
    ];
    var wrap = el('div', { class: 'ms-budget-grid' });
    dims.forEach(function (d) {
      var pct = Math.min(100, Math.round(d.r * 100));
      wrap.appendChild(el('div', { class: 'ms-bcard' },
        el('div', { class: 'ms-bhead' }, el('span', { text: d.key }), el('span', { class: 'mono', text: d.used + ' / ' + d.lim })),
        el('div', { class: 'sw-bar-track' }, el('div', { class: 'sw-bar-fill' + (pct >= 80 ? ' hot' : ''), style: { width: pct + '%' } }))
      ));
    });
    return wrap;
  }

  function renderDetail(container, snap, reload) {
    LA.clear(container);
    var mid = snap.id;
    var ctrls = el('div', { class: 'sw-ctrls' });
    if (snap.status === 'running') {
      ctrls.appendChild(el('button', {
        class: 'btn sm', type: 'button', html: icon('pause', 13) + ' ' + LA.t('mission.pause', '暂停'),
        on: { click: function () { LA.post('/api/missions/' + encodeURIComponent(mid) + '/pause').then(reload).catch(LA.fail); } }
      }));
    }
    if (snap.status === 'paused' || snap.status === 'queued') {
      ctrls.appendChild(el('button', {
        class: 'btn sm primary', type: 'button', html: icon('play', 13) + ' ' + LA.t('mission.resume', '恢复'),
        on: { click: function () { LA.post('/api/missions/' + encodeURIComponent(mid) + '/resume').then(reload).catch(LA.fail); } }
      }));
    }
    if (snap.status === 'running' || snap.status === 'paused' || snap.status === 'blocked' || snap.status === 'queued') {
      ctrls.appendChild(el('button', {
        class: 'btn sm danger', type: 'button', text: LA.t('mission.cancel', '终止'),
        on: { click: function () { LA.post('/api/missions/' + encodeURIComponent(mid) + '/cancel').then(reload).catch(LA.fail); } }
      }));
    }
    ctrls.appendChild(el('button', {
      class: 'btn sm ghost', type: 'button', html: icon('refresh', 13), title: '刷新',
      on: { click: reload }
    }));

    var prog = snap.progress || { done: 0, total: 0 };
    container.appendChild(el('div', { class: 'sw-head' },
      el('div', { class: 'ms-head-info' },
        el('div', { class: 'sw-title', text: snap.title || mid }),
        el('div', { class: 'ms-sub' },
          el('span', { class: 'ms-pill st-' + snap.status, text: statusLabel(snap.status) }),
          el('span', { class: 'badge', text: snap.mode || 'attended' }),
          el('span', { class: 'badge', text: prog.done + '/' + prog.total }),
          snap.branch ? el('span', { class: 'badge mono', html: icon('gitBranch', 11) + ' ' + LA.esc(snap.branch) }) : null
        )
      ),
      ctrls
    ));

    if (snap.pause_reason || snap.last_error) {
      container.appendChild(el('div', {
        class: 'banner ' + (snap.last_error ? 'danger' : 'warn'),
        text: (snap.last_error ? '错误: ' + snap.last_error : '暂停原因: ' + snap.pause_reason)
      }));
    }

    // 待审批队列（blocked）
    var pendingApr = (snap.approvals || []).filter(function (a) { return a.status === 'pending'; });
    if (pendingApr.length) {
      container.appendChild(el('div', { class: 'dr-sec', text: LA.t('mission.approvals', '阻塞待批清单') + ' (' + pendingApr.length + ')' }));
      pendingApr.forEach(function (a) {
        container.appendChild(el('div', { class: 'ms-apr-card' },
          el('div', { class: 'ms-apr-info' },
            el('b', { text: '[' + a.tool + '] ' + (a.target || a.milestone_id || '') }),
            el('div', { class: 'sw-goal', text: a.reason || '' })
          ),
          el('div', { class: 'btn-row' },
            el('button', {
              class: 'btn sm primary', type: 'button', text: LA.t('mission.approve', '批准并续跑'),
              on: { click: function () { LA.post('/api/missions/' + encodeURIComponent(mid) + '/approve/' + encodeURIComponent(a.id), { decision: 'allow' }).then(reload).catch(LA.fail); } }
            }),
            el('button', {
              class: 'btn sm danger', type: 'button', text: LA.t('mission.deny', '拒绝'),
              on: { click: function () { LA.post('/api/missions/' + encodeURIComponent(mid) + '/approve/' + encodeURIComponent(a.id), { decision: 'deny' }).then(reload).catch(LA.fail); } }
            })
          )
        ));
      });
    }

    // 预算卡片
    container.appendChild(el('div', { class: 'dr-sec', text: LA.t('mission.budget', '预算监控') }));
    container.appendChild(renderBudgetBars(snap.budget));

    // 里程碑与一键回滚 + Gates
    container.appendChild(el('div', { class: 'dr-sec', text: LA.t('mission.milestones', '里程碑与验收门控') }));
    var msWrap = el('div', { class: 'sw-agents' });
    (snap.milestones || []).forEach(function (ms) {
      var card = el('div', { class: 'sw-card ' + ms.status });
      var metaRow = el('div', { class: 'sw-card-top' },
        el('span', { class: 'sw-role', text: ms.id + ' · ' + ms.title }),
        el('span', { class: 'sw-st ' + ms.status, text: ms.status })
      );
      card.appendChild(metaRow);
      if (ms.description && ms.description !== ms.title) {
        card.appendChild(el('div', { class: 'sw-goal', text: ms.description }));
      }
      if (ms.gate_results && ms.gate_results.length) {
        var tbl = el('div', { class: 'ms-gates' });
        ms.gate_results.forEach(function (g) {
          tbl.appendChild(el('div', { class: 'ms-gate-row ' + (g.ok ? 'ok' : 'fail') },
            el('span', { class: 'ms-gmark', text: g.ok ? 'PASS' : 'FAIL' }),
            el('span', { class: 'mono', text: '[' + g.kind + '] ' + (g.target || '') }),
            el('span', { class: 'ms-gsum', text: g.summary || '' })
          ));
        });
        card.appendChild(tbl);
      }
      var foot = el('div', { class: 'sw-meta' },
        el('span', { text: 'attempts ' + (ms.attempts || 0) + '/' + (ms.max_attempts || 4) + (ms.commit_sha ? ' · ' + ms.commit_sha.slice(0, 8) : '') })
      );
      if (ms.commit_sha) {
        foot.appendChild(el('button', {
          class: 'btn sm ghost', type: 'button', html: icon('undo', 12) + ' ' + LA.t('mission.rollback', '回滚到此点'),
          on: { click: function () {
            LA.confirm('确定将代码库回滚到里程碑 ' + ms.id + ' 的提交点 (' + ms.commit_sha.slice(0, 8) + ') 吗？后续里程碑进度将被重置。', { danger: true, okText: '确认回滚' }).then(function (ok) {
              if (!ok) return;
              LA.post('/api/missions/' + encodeURIComponent(mid) + '/rollback', { target: ms.id }).then(function () {
                LA.toast('已回滚至 ' + ms.id, 'ok');
                reload();
              }).catch(LA.fail);
            });
          } }
        }));
      }
      card.appendChild(foot);
      msWrap.appendChild(card);
    });
    container.appendChild(msWrap);

    // 时间线（Episode / Milestone / Stall / Approval）
    container.appendChild(el('div', { class: 'dr-sec', text: LA.t('mission.timeline', '执行时间线') }));
    var tl = el('div', { class: 'sw-mail' });
    var items = (snap.episodes || []).map(function (ep) {
      var h = ep.handoff;
      return {
        ts: ep.started_at || 0,
        title: 'Episode #' + ep.id + ' [' + (ep.milestone_id || '-') + '] · ' + ep.status,
        detail: (h && h.summary) ? h.summary.slice(0, 140) : ('steps=' + ep.steps + ' tokens=' + ep.tokens)
      };
    }).concat((snap.timeline || []).map(function (t) {
      return {
        ts: t.ts || 0,
        title: String(t.kind || 'event'),
        detail: t.detail || t.target || t.milestone_id || t.dimension || ''
      };
    }));
    items.sort(function (a, b) { return (b.ts || 0) - (a.ts || 0); });
    if (!items.length) tl.appendChild(el('div', { class: 'dr-empty', text: '暂无时间线记录' }));
    else items.slice(0, 20).forEach(function (it) {
      tl.appendChild(el('div', { class: 'sw-mail-row' },
        el('b', { text: it.title }),
        it.detail ? el('div', { class: 'sw-goal', text: it.detail }) : null
      ));
    });
    container.appendChild(tl);

    // MISSION.md 预览与在线编辑
    container.appendChild(el('div', { class: 'dr-sec', text: LA.t('mission.md.title', 'MISSION.md（人工编辑后将在下一 Episode 自动生效）') }));
    var ta = el('textarea', { class: 'inp wide ms-md-editor', rows: 10 });
    ta.value = snap.mission_md || '';
    var saveBtn = el('button', {
      class: 'btn sm primary', type: 'button', text: LA.t('mission.md.save', '保存 MISSION.md'),
      on: { click: function () {
        saveBtn.disabled = true;
        LA.put('/api/missions/' + encodeURIComponent(mid) + '/mission-md', { content: ta.value }).then(function () {
          LA.toast('MISSION.md 已保存', 'ok');
          reload();
        }).catch(function (e) { saveBtn.disabled = false; LA.fail(e); });
      } }
    });
    container.appendChild(el('div', { class: 'ms-md-box' },
      snap.mission_md_path ? el('div', { class: 'ms-md-path mono', text: snap.mission_md_path }) : null,
      ta,
      el('div', { class: 'btn-row' }, saveBtn)
    ));
  }

  function renderSidebar(container) {
    if (!container) return;
    LA.clear(container);
    var ms = LA.S.missions || [];
    var hdr = el('div', { class: 'sb-group sb-ms-head' },
      el('span', { text: LA.t('mission.sidebar', 'Missions') }),
      el('button', {
        class: 'icon-btn ms-add-btn', type: 'button', title: LA.t('mission.new', '新建长期 Mission'),
        html: icon('plus', 14),
        on: { click: function () { openNewMissionModal(function (snap) { if (snap && snap.id) LA.missionPanel.openDetail(snap.id); }); } }
      })
    );
    container.appendChild(hdr);
    if (!ms.length) {
      container.appendChild(el('div', { class: 'ms-sb-empty', text: LA.t('mission.empty', '输入 /mission start 或点击 + 启动') }));
      return;
    }
    ms.slice(0, 8).forEach(function (m) {
      var p = m.progress || { done: 0, total: 0 };
      var row = el('button', {
        class: 'ms-sb-row' + (m.id === activeMissionId ? ' active' : ''),
        type: 'button',
        on: { click: function () { LA.missionPanel.openDetail(m.id); } }
      },
        el('span', { class: 'ms-dot ' + (m.status || 'queued'), title: statusLabel(m.status) }),
        el('span', { class: 'ms-sb-title', text: m.title || m.id }),
        (m.pending_approvals > 0) ? el('span', { class: 'badge warn', text: '!' + m.pending_approvals }) : null,
        el('span', { class: 'ms-sb-prog mono', text: p.done + '/' + p.total }),
        el('span', { class: 'ms-sb-ring', html: budgetRing(m.budget_ratio), title: '预算 ' + Math.round((m.budget_ratio || 0) * 100) + '%' })
      );
      container.appendChild(row);
    });
  }

  function watchMission(mid, onUpdate) {
    closeSse();
    var reload = LA.debounce(onUpdate, 250);
    activeSse = LA.sse('/api/missions/' + encodeURIComponent(mid) + '/events', {
      mission: reload,
      mission_started: reload,
      episode_started: reload,
      handoff_saved: reload,
      milestone_done: reload,
      gate_result: reload,
      stall_detected: reload,
      mission_blocked: reload,
      budget_warn: reload,
      mission_budget: reload,
      mission_paused: reload,
      mission_resumed: reload,
      mission_rollback: reload,
      mission_done: onUpdate
    });
  }

  LA.missionPanel = {
    close: closeSse,
    openNew: function () { openNewMissionModal(function (snap) { if (snap && snap.id) LA.missionPanel.openDetail(snap.id); }); },
    renderSidebar: renderSidebar,
    renderDrawer: function (container) {
      var list = LA.S.missions || [];
      if (!list.length) {
        closeSse();
        LA.clear(container);
        container.appendChild(el('div', { class: 'dr-empty' },
          el('p', { text: LA.t('mission.empty.drawer', '暂无 Mission 任务。可创建长期自主编程任务（支持断点续跑、预算监控与 Milestone 回滚）。') }),
          el('button', { class: 'btn sm primary', type: 'button', text: LA.t('mission.new', '新建长期 Mission'), on: { click: function () { openNewMissionModal(function (snap) { if (snap && snap.id) LA.missionPanel.renderDrawer(container); }); } } })
        ));
        return;
      }
      var picked = list.find(function (x) { return x.id === activeMissionId; }) || list[0];
      activeMissionId = picked.id;
      function loadOne() {
        LA.get('/api/missions/' + encodeURIComponent(activeMissionId)).then(function (snap) {
          renderDetail(container, snap, loadOne);
        }).catch(LA.fail);
      }
      loadOne();
      if (picked.status === 'running' || picked.status === 'blocked' || picked.status === 'paused') {
        watchMission(picked.id, function () { loadOne(); LA.refreshMissions(); });
      } else {
        closeSse();
      }
    },
    openDetail: function (mid) {
      activeMissionId = mid;
      var body = el('div', { class: 'ms-modal-body' });
      var modal = LA.modal({
        title: LA.t('mission.detail.title', 'Mission 运行监控'),
        width: 780,
        body: body,
        onClose: closeSse
      });
      function loadOne() {
        LA.get('/api/missions/' + encodeURIComponent(mid)).then(function (snap) {
          renderDetail(body, snap, loadOne);
          LA.refreshMissions();
        }).catch(function (e) { modal.close(); LA.fail(e); });
      }
      loadOne();
      watchMission(mid, loadOne);
    }
  };
})();
