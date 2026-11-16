/* M4 产物检查器与对话产物卡片：图表/SVG、PDF 分页、表格分页、PPTX、Markdown/代码、HTML 沙箱预览、Bundle 打包与会话导出。 */
(function () {
  'use strict';
  var LA = window.LA, el = LA.el, icon = LA.icon;
  var activeAid = null, curPreview = null, selectedIds = {}, containerEl = null;

  var KIND_META = {
    chart: { ic: 'chart', label: '图表' },
    table: { ic: 'table', label: '表格' },
    html: { ic: 'code', label: '网页' },
    pdf: { ic: 'fileText', label: 'PDF' },
    document: { ic: 'fileText', label: '文档' },
    presentation: { ic: 'layers', label: '演示' },
    image: { ic: 'image', label: '图片' },
    bundle: { ic: 'layers', label: '打包' },
    audio: { ic: 'activity', label: '音频' },
    markdown: { ic: 'fileText', label: 'Markdown' },
    export: { ic: 'download', label: '导出' },
    file: { ic: 'file', label: '产物' }
  };

  function sid() { return LA.S.cur ? LA.S.cur.id : ''; }
  function kindInfo(kind) { return KIND_META[kind] || KIND_META.file; }
  function btn(text, fn, cls, title) {
    return el('button', { class: 'btn sm ' + (cls || ''), type: 'button', text: text, title: title || '', on: { click: fn } });
  }

  function exportSessionFile(fmt, incThink, incTools) {
    var s = sid();
    if (!s) { LA.toast('请先打开或新建一个会话', 'err'); return; }
    var url = '/api/sessions/' + encodeURIComponent(s) + '/export?format=' + encodeURIComponent(fmt)
      + '&thinking=' + (incThink ? '1' : '0') + '&tools=' + (incTools ? '1' : '0');
    fetch(url, { method: 'POST', credentials: 'same-origin' }).then(function (r) {
      if (!r.ok) {
        return r.json().catch(function () { return { detail: 'HTTP ' + r.status }; }).then(function (d) {
          throw new Error((d && d.detail) || ('HTTP ' + r.status));
        });
      }
      var ct = r.headers.get('content-type') || 'application/octet-stream';
      return r.blob().then(function (blob) { return { blob: blob, ct: ct }; });
    }).then(function (res) {
      LA.download('session-' + s + '.' + fmt, res.blob, res.ct);
      LA.toast('已导出为 ' + fmt.toUpperCase(), 'ok');
    }).catch(LA.fail);
  }

  function createBundle(ids) {
    var s = sid();
    if (!s) return;
    var body = { session_id: s };
    if (ids && ids.length) body.artifact_ids = ids;
    LA.post('/api/artifacts/bundle', body).then(function (art) {
      selectedIds = {};
      activeAid = art.id;
      LA.toast('产物打包完成（含 MANIFEST.json）', 'ok');
      if (containerEl) renderPanel(containerEl);
    }).catch(LA.fail);
  }

  /* ---------------------------------------------------------------- 对话内联产物卡片 */
  function card(art) {
    if (!art || !art.id) return null;
    var km = kindInfo(art.kind);
    return el('div', { class: 'art-card', dataset: { artifactId: art.id } },
      el('div', { class: 'art-card-ic', html: icon(km.ic, 16) }),
      el('div', { class: 'art-card-main' },
        el('div', { class: 'art-card-top' },
          el('span', { class: 'art-kind-badge', text: km.label }),
          el('b', { class: 'art-card-title', text: art.title || art.id, title: art.title || '' })
        ),
        el('div', { class: 'art-card-sub', text: LA.fmtSize(art.size || 0) + (art.mime ? ' · ' + art.mime : '') })
      ),
      el('div', { class: 'art-card-acts' },
        btn(LA.t('artifacts.preview', '预览'), function (e) {
          e.stopPropagation();
          if (LA.panels && LA.panels.openArtifact) LA.panels.openArtifact(art.id);
        }, 'primary'),
        el('a', {
          class: 'btn sm ghost',
          href: art.download_url || ('/api/artifacts/' + encodeURIComponent(art.id)),
          download: art.title || (art.id + '.bin'),
          text: LA.t('artifacts.download', '下载'),
          on: { click: function (e) { e.stopPropagation(); } }
        })
      )
    );
  }

  /* ---------------------------------------------------------------- 预览渲染 */
  function renderPreviewView(box, aid, opts) {
    opts = opts || {};
    var page = opts.page || 1, sheet = opts.sheet || 0, offset = opts.offset || 0, limit = opts.limit || 80;
    LA.clear(box);
    box.appendChild(el('div', { class: 'dr-empty', text: '正在加载产物预览…' }));
    var qs = '?page=' + page + '&sheet=' + sheet + '&offset=' + offset + '&limit=' + limit;
    LA.get('/api/artifacts/' + encodeURIComponent(aid) + '/preview' + qs).then(function (r) {
      LA.clear(box);
      var art = r.artifact || {};
      var km = kindInfo(art.kind);
      var hdr = el('div', { class: 'preview' });
      var back = el('button', {
        class: 'icon-btn',
        type: 'button',
        title: '返回产物列表',
        'aria-label': '返回',
        html: icon('chev', 15).replace('class="ic"', 'class="ic" style="transform:rotate(180deg)"'),
        on: { click: function () { activeAid = null; curPreview = null; renderPanel(box); } }
      });
      hdr.appendChild(el('div', { class: 'ph' },
        back,
        el('span', { class: 'art-kind-badge', text: km.label }),
        el('b', { text: art.title || art.id, style: { flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } })
      ));

      var actBar = el('div', { class: 'toolbar' },
        el('span', { class: 'hint2', text: LA.fmtSize(art.size || 0) + ' · ' + (art.mime || '') }),
        el('a', {
          class: 'btn sm primary',
          href: r.download_url || ('/api/artifacts/' + encodeURIComponent(aid)),
          download: art.title || (aid + '.bin'),
          text: '下载'
        }),
        btn('系统打开', function () {
          LA.post('/api/artifacts/' + encodeURIComponent(aid) + '/open', {}).then(function () {
            LA.toast('已在系统默认程序中打开', 'ok');
          }).catch(LA.fail);
        }, 'ghost'),
        btn('删除', function () {
          LA.confirm('删除产物「' + (art.title || aid) + '」？', { danger: true, okText: '删除' }).then(function (ok) {
            if (!ok) return;
            LA.del('/api/artifacts/' + encodeURIComponent(aid)).then(function () {
              activeAid = null; curPreview = null; renderPanel(box);
            }).catch(LA.fail);
          });
        }, 'ghost')
      );
      hdr.appendChild(actBar);
      box.appendChild(hdr);

      var stage = el('div', { class: 'art-stage' });
      hdr.appendChild(stage);
      var pk = r.preview_kind;

      if (pk === 'svg') {
        if (r.source_preview) {
          actBar.appendChild(btn('复制 SVG', function () { LA.copyText(r.svg || r.source_preview); }, 'ghost'));
        }
        if (art.kind === 'chart' && r.svg) {
          stage.appendChild(el('div', { class: 'art-svg-stage', html: r.svg }));
        } else if (r.data_url) {
          stage.appendChild(el('img', { class: 'art-img', src: r.data_url, alt: art.title || 'SVG' }));
        }
      } else if (pk === 'image') {
        if (r.data_url) stage.appendChild(el('img', { class: 'art-img', src: r.data_url, alt: art.title || 'Image' }));
        else stage.appendChild(el('div', { class: 'dr-empty', text: '图片较大，请直接点击下载查看。' }));
      } else if (pk === 'html') {
        var secBar = el('div', { class: 'art-sec-bar' },
          el('span', { class: 'art-sec-pill' + (r.scripts_authorized ? ' warn' : ''), text: r.scripts_authorized ? '已授权脚本（仍禁网/隔离同源）' : '沙箱隔离（已禁用脚本与联网）' }),
          btn(r.scripts_authorized ? '撤回脚本权限' : '授权运行脚本', function () {
            var next = !r.scripts_authorized;
            var go = function () {
              LA.post('/api/artifacts/' + encodeURIComponent(aid) + '/authorize-scripts', { enabled: next }).then(function () {
                LA.toast(next ? '已授权此 HTML 产物运行脚本（禁网）' : '已恢复禁止脚本', 'ok');
                renderPreviewView(box, aid, opts);
              }).catch(LA.fail);
            };
            if (next) LA.confirm('仅为当前 HTML 产物启用沙箱脚本（仍禁止一切网络请求及主页面访问）？', { okText: '允许脚本' }).then(function (ok) { if (ok) go(); });
            else go();
          }, nextBtnCls(r.scripts_authorized))
        );
        stage.appendChild(secBar);
        var iframe = document.createElement('iframe');
        iframe.className = 'art-iframe';
        iframe.title = art.title || 'HTML Sandbox Preview';
        iframe.setAttribute('sandbox', r.scripts_authorized ? 'allow-scripts' : '');
        iframe.src = r.iframe_url;
        stage.appendChild(iframe);
      } else if (pk === 'pdf') {
        var totalP = r.page_count || 1, curP = r.page || 1, showText = false;
        var textBox = el('pre', { class: 'art-pre', hidden: true, text: r.text || '(本页无提取文本)' });
        var imgEl = r.data_url ? el('img', { class: 'art-img', src: r.data_url, alt: 'PDF Page ' + curP }) : el('div', { class: 'dr-empty', text: '无 pypdfium2 渲染器，请切换文本视图或下载查看。' });
        var navBar = el('div', { class: 'art-pdf-bar' },
          btn('上一页', function () { if (curP > 1) renderPreviewView(box, aid, { page: curP - 1 }); }, 'ghost'),
          el('span', { class: 'hint2', text: '第 ' + curP + ' / ' + totalP + ' 页' }),
          btn('下一页', function () { if (curP < totalP) renderPreviewView(box, aid, { page: curP + 1 }); }, 'ghost'),
          btn('切换文本', function () { showText = !showText; textBox.hidden = !showText; if (imgEl) imgEl.hidden = showText; }, 'ghost')
        );
        stage.append(navBar, imgEl, textBox);
      } else if (pk === 'table') {
        var sheets = r.sheets || [];
        if (sheets.length > 1) {
          var shBar = el('div', { class: 'art-sheets' });
          sheets.forEach(function (nm, idx) {
            shBar.appendChild(el('button', {
              class: 'btn sm ' + (idx === r.active_sheet ? 'primary' : 'ghost'),
              type: 'button',
              text: nm,
              on: { click: function () { renderPreviewView(box, aid, { sheet: idx, offset: 0, limit: limit }); } }
            }));
          });
          stage.appendChild(shBar);
        }
        var pager = el('div', { class: 'art-pdf-bar' },
          btn('上一批', function () { if (offset > 0) renderPreviewView(box, aid, { sheet: sheet, offset: Math.max(0, offset - limit), limit: limit }); }, 'ghost'),
          el('span', { class: 'hint2', text: '行 ' + (offset + 1) + '–' + (offset + (r.rows || []).length) + ' / 共 ' + (r.total_rows || 0) + ' 行' }),
          btn('下一批', function () { if ((offset + (r.rows || []).length) < (r.total_rows || 0)) renderPreviewView(box, aid, { sheet: sheet, offset: offset + limit, limit: limit }); }, 'ghost')
        );
        stage.appendChild(pager);
        var tbl = el('table', { class: 'art-table' });
        if (r.headers && r.headers.length) {
          var thead = el('thead'), trH = el('tr');
          r.headers.forEach(function (h) { trH.appendChild(el('th', { text: h })); });
          thead.appendChild(trH); tbl.appendChild(thead);
        }
        var tbody = el('tbody');
        (r.rows || []).forEach(function (row) {
          var tr = el('tr');
          row.forEach(function (c) { tr.appendChild(el('td', { text: c })); });
          tbody.appendChild(tr);
        });
        tbl.appendChild(tbody);
        stage.appendChild(el('div', { class: 'art-table-wrap' }, tbl));
      } else if (pk === 'slides') {
        (r.slides || []).forEach(function (sl) {
          var cardEl = el('div', { class: 'art-slide' },
            el('div', { class: 'art-slide-head' }, el('span', { class: 'badge', text: '#' + sl.index }), el('b', { text: sl.title }))
          );
          if (sl.bullets && sl.bullets.length) {
            var ul = el('ul', { class: 'art-slide-ul' });
            sl.bullets.forEach(function (b) { ul.appendChild(el('li', { text: b })); });
            cardEl.appendChild(ul);
          }
          if (sl.notes) cardEl.appendChild(el('div', { class: 'art-slide-notes', text: '备注: ' + sl.notes }));
          stage.appendChild(cardEl);
        });
      } else if (pk === 'archive') {
        if (r.manifest) {
          stage.appendChild(el('div', { class: 'art-sec-bar' }, el('span', { class: 'hint2', text: '清单: ' + (r.manifest.title || '') + ' · 共 ' + (r.manifest.count || 0) + ' 项' })));
        }
        var list = el('div', { class: 'art-list' });
        (r.entries || []).forEach(function (ent) {
          list.appendChild(el('div', { class: 'jr-item' },
            el('span', { html: icon('file', 14) }),
            el('span', { class: 'tx', text: ent.name }),
            el('span', { class: 'tm', text: LA.fmtSize(ent.size || 0) })
          ));
        });
        stage.appendChild(list);
      } else if (pk === 'audio') {
        var aud = document.createElement('audio');
        aud.controls = true;
        aud.src = r.audio_url;
        aud.style.width = '100%';
        stage.appendChild(aud);
      } else if (pk === 'markdown') {
        actBar.appendChild(btn('复制内容', function () { LA.copyText(r.content || ''); }, 'ghost'));
        stage.appendChild(el('div', { class: 'md art-md-box', html: LA.renderMarkdown(r.content || '') }));
      } else if (pk === 'text') {
        actBar.appendChild(btn('复制内容', function () { LA.copyText(r.content || ''); }, 'ghost'));
        var code = el('code', { class: 'pv-code', html: LA.highlight ? LA.highlight(r.content || '', r.lang || '') : LA.esc(r.content || '') });
        stage.appendChild(el('pre', { class: 'art-pre' }, code));
      } else {
        stage.appendChild(el('div', { class: 'dr-empty', text: r.note || '当前文件不支持内联预览，请点击下载或系统打开。' }));
      }
      curPreview = aid;
    }).catch(function (e) {
      LA.clear(box);
      box.appendChild(el('div', { class: 'dr-empty', text: '预览失败: ' + e.message }));
    });
  }

  function nextBtnCls(authorized) { return authorized ? 'danger' : ''; }

  /* ---------------------------------------------------------------- 面板渲染 */
  function renderPanel(body) {
    containerEl = body;
    var s = sid();
    if (!s) {
      LA.clear(body);
      body.appendChild(el('div', { class: 'dr-empty', text: LA.t('artifacts.nosession', '请先创建或打开一个会话后再查看产物与导出记录。') }));
      return;
    }
    if (activeAid) {
      renderPreviewView(body, activeAid);
      return;
    }
    LA.clear(body);

    // 1. 会话导出控制条（MD / HTML / PDF + 思考块/工具轨迹开关）
    var chkThink = el('input', { type: 'checkbox', checked: true });
    var chkTools = el('input', { type: 'checkbox', checked: true });
    var exportBar = el('div', { class: 'art-export-box' },
      el('div', { class: 'art-exp-head' }, el('span', { text: LA.t('artifacts.export.title', '会话导出') })),
      el('div', { class: 'art-exp-opts' },
        el('label', { class: 'chk-row' }, chkThink, el('span', { text: LA.t('artifacts.export.think', '含思考块') })),
        el('label', { class: 'chk-row' }, chkTools, el('span', { text: LA.t('artifacts.export.tools', '含工具轨迹') }))
      ),
      el('div', { class: 'art-exp-btns' },
        btn('Markdown', function () { exportSessionFile('md', chkThink.checked, chkTools.checked); }, 'ghost'),
        btn('HTML', function () { exportSessionFile('html', chkThink.checked, chkTools.checked); }, 'ghost'),
        btn('PDF', function () { exportSessionFile('pdf', chkThink.checked, chkTools.checked); }, 'ghost')
      )
    );
    body.appendChild(exportBar);

    // 2. 产物清单工具栏与打包控件
    var listHead = el('div', { class: 'dr-sec' }, el('span', { text: LA.t('artifacts.list', '本会话产物') }));
    var bundleBtn = btn(LA.t('artifacts.bundle', '打包 Zip'), function () {
      var picked = Object.keys(selectedIds).filter(function (k) { return selectedIds[k]; });
      createBundle(picked);
    }, '');
    listHead.appendChild(bundleBtn);
    body.appendChild(listHead);

    var listBox = el('div', { class: 'art-list', text: '加载中…' });
    body.appendChild(listBox);

    LA.get('/api/artifacts?session_id=' + encodeURIComponent(s) + '&limit=200').then(function (r) {
      if (containerEl !== body || activeAid) return;
      LA.clear(listBox);
      var arts = (r && r.artifacts) || [];
      if (!arts.length) {
        listBox.appendChild(el('div', { class: 'dr-empty', text: LA.t('artifacts.empty', '当前会话还没有生成产物（图表 / 表格 / 文档 / 网页 / Zip）。') }));
        return;
      }
      arts.forEach(function (a) {
        var km = kindInfo(a.kind);
        var cb = el('input', { type: 'checkbox', checked: !!selectedIds[a.id] });
        cb.addEventListener('click', function (e) { e.stopPropagation(); selectedIds[a.id] = cb.checked; });
        var row = el('div', { class: 'art-row', tabIndex: 0 },
          cb,
          el('span', { class: 'art-row-ic', html: icon(km.ic, 15) }),
          el('div', { class: 'art-row-info' },
            el('div', { class: 'art-row-top' },
              el('span', { class: 'art-kind-badge', text: km.label }),
              el('span', { class: 'art-row-title', text: a.title || a.id, title: a.title || '' })
            ),
            el('div', { class: 'art-row-meta', text: LA.fmtSize(a.size || 0) + ' · ' + (a.created_by || 'main') })
          ),
          el('a', {
            class: 'icon-btn',
            href: a.download_url || ('/api/artifacts/' + encodeURIComponent(a.id)),
            download: a.title || (a.id + '.bin'),
            title: '下载',
            html: icon('download', 14),
            on: { click: function (e) { e.stopPropagation(); } }
          })
        );
        var openFn = function () { activeAid = a.id; renderPanel(body); };
        row.addEventListener('click', openFn);
        row.addEventListener('keydown', function (ev) { if (ev.key === 'Enter') openFn(); });
        listBox.appendChild(row);
      });
    }).catch(function (e) { LA.clear(listBox); listBox.textContent = e.message; });
  }

  LA.bus.on('session', function () { activeAid = null; curPreview = null; selectedIds = {}; });
  LA.bus.on('artifact', function () {
    if (containerEl && LA.S.drawerOpen && !activeAid) renderPanel(containerEl);
  });

  LA.artifacts = {
    card: card,
    renderPanel: renderPanel,
    open: function (aid, box) { activeAid = aid || null; if (box) renderPanel(box); },
    close: function () { containerEl = null; },
    resetPreview: function () { activeAid = null; curPreview = null; }
  };
})();
