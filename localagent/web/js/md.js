/* 安全 Markdown 渲染器：先转义一切，只输出自己生成的标签；链接仅 http/https/mailto；外部图片一律拦截（防数据外泄/追踪像素）。 */
(function () {
  'use strict';
  var LA = window.LA = window.LA || {};
  var esc = function (s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;'); };

  /* ---------- 语法高亮（极简、正则式） ---------- */
  var KW = {
    py: 'and as assert async await break class continue def del elif else except False finally for from global if import in is lambda None nonlocal not or pass raise return True try while with yield self print',
    js: 'async await break case catch class const continue debugger default delete do else export extends false finally for function if import in instanceof let new null of return super switch this throw true try typeof undefined var void while with yield interface type enum implements public private protected readonly string number boolean any',
    sh: 'if then else elif fi for while do done case esac function in echo cd ls cat grep sed awk export local return exit sudo git npm pip python node curl rm mv cp mkdir',
    c: 'auto break case char const continue default do double else enum extern float for goto if inline int long register return short signed sizeof static struct switch typedef union unsigned void volatile while class namespace template typename public private protected new delete using bool true false nullptr virtual override std string vector',
    go: 'break case chan const continue default defer else fallthrough for func go goto if import interface map package range return select struct switch type var true false nil string int bool error',
    rs: 'as break const continue crate else enum extern false fn for if impl in let loop match mod move mut pub ref return self Self static struct super trait true type unsafe use where while async await dyn String Vec Option Result Some None Ok Err',
    sql: 'select from where and or not insert into values update set delete create table drop alter add index join left right inner outer on group by order having limit as distinct null primary key foreign references union all case when then else end like in is count sum avg min max',
    json: 'true false null', css: 'important', html: ''
  };
  var ALIAS = { python: 'py', py: 'py', javascript: 'js', js: 'js', jsx: 'js', ts: 'js', typescript: 'js', tsx: 'js', json: 'json', bash: 'sh', sh: 'sh', shell: 'sh', zsh: 'sh', powershell: 'sh', ps1: 'sh',
    c: 'c', cpp: 'c', 'c++': 'c', h: 'c', java: 'c', cs: 'c', csharp: 'c', go: 'go', golang: 'go', rust: 'rs', rs: 'rs', sql: 'sql', css: 'css', html: 'html', xml: 'html', yaml: 'sh', yml: 'sh', toml: 'sh' };
  function highlight(code, lang) {
    var g = ALIAS[(lang || '').toLowerCase()];
    if (!g) return esc(code);
    var kws = {}; (KW[g] || '').split(' ').forEach(function (w) { if (w) kws[g === 'sql' ? w.toLowerCase() : w] = 1; });
    var com = g === 'py' || g === 'sh' ? '#[^\\n]*' : g === 'sql' ? '--[^\\n]*|\\/\\*[\\s\\S]*?\\*\\/' : g === 'html' ? '<!--[\\s\\S]*?-->' : '\\/\\/[^\\n]*|\\/\\*[\\s\\S]*?\\*\\/';
    var str = g === 'py' ? '"""[\\s\\S]*?"""|\'\'\'[\\s\\S]*?\'\'\'|"(?:\\\\.|[^"\\\\\\n])*"|\'(?:\\\\.|[^\'\\\\\\n])*\'' : '"(?:\\\\.|[^"\\\\\\n])*"|\'(?:\\\\.|[^\'\\\\\\n])*\'|`(?:\\\\.|[^`\\\\])*`';
    var re = new RegExp('(' + com + ')|(' + str + ')|(\\b\\d[\\d_.]*(?:e[+-]?\\d+)?\\b)|([A-Za-z_$][\\w$]*)', 'g');
    var out = '', last = 0, m;
    while ((m = re.exec(code))) {
      out += esc(code.slice(last, m.index));
      if (m[1]) out += '<span class="tk-c">' + esc(m[1]) + '</span>';
      else if (m[2]) out += '<span class="tk-s">' + esc(m[2]) + '</span>';
      else if (m[3]) out += '<span class="tk-n">' + esc(m[3]) + '</span>';
      else {
        var w = m[4], k = g === 'sql' ? w.toLowerCase() : w;
        if (kws[k] && g !== 'json') out += '<span class="tk-k">' + esc(w) + '</span>';
        else if (g === 'json' && kws[k]) out += '<span class="tk-n">' + esc(w) + '</span>';
        else if (code.charAt(re.lastIndex) === '(' && g !== 'json') out += '<span class="tk-f">' + esc(w) + '</span>';
        else out += esc(w);
      }
      last = re.lastIndex;
      if (m[0].length === 0) re.lastIndex++;
    }
    return out + esc(code.slice(last));
  }
  LA.highlight = highlight;

  /* ---------- 行内 ---------- */
  function safeUrl(u) {
    u = String(u).trim();
    if (/^(https?:\/\/|mailto:)/i.test(u)) return u;
    return null;
  }
  function hostOf(u) { var m = /^[a-z]+:\/\/([^\/?#]+)/i.exec(u); return m ? m[1] : u; }

  function inlineText(s) {
    // s 已是未转义的纯文本片段（不含代码 span）
    var tokens = [];
    var hold = function (html) { tokens.push(html); return '\u0000' + (tokens.length - 1) + '\u0001'; };
    // 图片
    s = s.replace(/!\[([^\]\n]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g, function (_, alt, src) {
      if (/^data:image\/(png|jpe?g|gif|webp);base64,[A-Za-z0-9+\/=]+$/i.test(src)) return hold('<img alt="' + esc(alt) + '" src="' + esc(src) + '">');
      return hold('<span class="blocked-img">已拦截外部图片: ' + esc(hostOf(src).slice(0, 60)) + '</span>');
    });
    // 链接 [text](url)
    s = s.replace(/\[([^\]\n]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g, function (_, text, url) {
      var u = safeUrl(url);
      if (!u) return hold(esc(text));
      return hold('<a href="' + esc(u) + '" target="_blank" rel="noopener noreferrer">' + inlineFmt(esc(text)) + '</a>');
    });
    // 裸链接
    s = s.replace(/(^|[\s(（])((?:https?:\/\/)[^\s<>"'）)\]]+[^\s<>"'）)\].,;:!?，。；：！？])/g, function (_, pre, url) {
      return pre + hold('<a href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">' + esc(url) + '</a>');
    });
    s = inlineFmt(esc(s));
    return s.replace(/\u0000(\d+)\u0001/g, function (_, i) { return tokens[+i]; });
  }
  function inlineFmt(h) {
    h = h.replace(/\*\*([^*\n]+?)\*\*/g, '<strong>$1</strong>').replace(/__([^_\n]+?)__/g, '<strong>$1</strong>');
    h = h.replace(/(^|[^*\w])\*([^*\s][^*\n]*?)\*(?![*\w])/g, '$1<em>$2</em>').replace(/(^|[^_\w])_([^_\s][^_\n]*?)_(?![_\w])/g, '$1<em>$2</em>');
    h = h.replace(/~~([^~\n]+?)~~/g, '<del>$1</del>');
    return h;
  }
  function inline(s) {
    var out = '', last = 0, re = /(`+)([\s\S]*?[^`])\1(?!`)/g, m;
    while ((m = re.exec(s))) {
      out += inlineText(s.slice(last, m.index)) + '<code>' + esc(m[2]) + '</code>';
      last = re.lastIndex;
    }
    return out + inlineText(s.slice(last));
  }

  /* ---------- 块级 ---------- */
  function codeBlock(lang, code) {
    var l = esc((lang || '').replace(/[^\w+#.-]/g, '').slice(0, 20));
    return '<div class="codeblock"><div class="cb-head"><span>' + (l || 'text') + '</span><button type="button" data-copy="1">复制</button></div><pre><code>' + highlight(code, lang) + '</code></pre></div>';
  }
  function splitRow(line) {
    var s = line.trim().replace(/^\|/, '').replace(/\|$/, '');
    var cells = [], cur = '', esc2 = false;
    for (var i = 0; i < s.length; i++) {
      var ch = s[i];
      if (esc2) { cur += ch; esc2 = false; } else if (ch === '\\') { esc2 = true; } else if (ch === '|') { cells.push(cur.trim()); cur = ''; } else cur += ch;
    }
    cells.push(cur.trim());
    return cells;
  }
  var reHr = /^ {0,3}([-*_])(?:\s*\1){2,}\s*$/, reH = /^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$/, reUl = /^(\s*)[-*+]\s+(.*)$/, reOl = /^(\s*)(\d{1,9})[.)]\s+(.*)$/,
    reFence = /^ {0,3}(`{3,}|~{3,})\s*([^\s`]*)[^`]*$/, reSep = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

  function renderList(lines, i, baseIndent) {
    var m0 = reUl.exec(lines[i]) || reOl.exec(lines[i]);
    var ordered = !!reOl.exec(lines[i]) && !reUl.exec(lines[i]);
    var tag = ordered ? 'ol' : 'ul';
    var start = ordered ? parseInt(reOl.exec(lines[i])[2], 10) : 1;
    var html = '<' + tag + (ordered && start !== 1 ? ' start="' + start + '"' : '') + '>';
    while (i < lines.length) {
      var u = reUl.exec(lines[i]), o = reOl.exec(lines[i]);
      var m = u || o;
      if (!m) break;
      var indent = m[1].replace(/\t/g, '    ').length;
      if (indent < baseIndent) break;
      if (indent > baseIndent) {
        var sub = renderList(lines, i, indent);
        html = html.replace(/<\/li>$/, sub.html + '</li>');
        i = sub.i; continue;
      }
      if (!!o && !u !== ordered) break;
      var text = u ? u[2] : o[3];
      var task = /^\[([ xX])\]\s+/.exec(text), cls = '', pre = '';
      if (task) { text = text.slice(task[0].length); cls = ' class="task"'; pre = '<input type="checkbox" disabled' + (task[1] !== ' ' ? ' checked' : '') + '>'; }
      var item = text; i++;
      while (i < lines.length && lines[i].trim() !== '' && !reUl.exec(lines[i]) && !reOl.exec(lines[i]) && /^\s{2,}\S/.test(lines[i]) && !reFence.test(lines[i].trim())) { item += ' ' + lines[i].trim(); i++; }
      html += '<li' + cls + '>' + pre + inline(item) + '</li>';
    }
    return { html: html + '</' + tag + '>', i: i };
  }

  LA.renderMarkdown = function (src) {
    src = String(src == null ? '' : src).replace(/\r\n?/g, '\n');
    var lines = src.split('\n'), out = [], i = 0;
    while (i < lines.length) {
      var ln = lines[i], m;
      if (ln.trim() === '') { i++; continue; }
      if ((m = reFence.exec(ln))) {
        var fence = m[1], lang = m[2], body = [];
        i++;
        while (i < lines.length && !(lines[i].trim().indexOf(fence) === 0 && /^[`~]+\s*$/.test(lines[i].trim()))) { body.push(lines[i]); i++; }
        i++; // 吃掉收尾围栏；流式时可能没有 → 当作代码到文末
        out.push(codeBlock(lang, body.join('\n')));
        continue;
      }
      if ((m = reH.exec(ln))) { var lv = Math.min(m[1].length, 4); out.push('<h' + lv + '>' + inline(m[2]) + '</h' + lv + '>'); i++; continue; }
      if (reHr.test(ln)) { out.push('<hr>'); i++; continue; }
      if (/^ {0,3}>/.test(ln)) {
        var q = [];
        while (i < lines.length && /^ {0,3}>/.test(lines[i])) { q.push(lines[i].replace(/^ {0,3}> ?/, '')); i++; }
        out.push('<blockquote>' + LA.renderMarkdown(q.join('\n')) + '</blockquote>');
        continue;
      }
      if (ln.indexOf('|') >= 0 && i + 1 < lines.length && reSep.test(lines[i + 1]) && lines[i + 1].indexOf('-') >= 0) {
        var head = splitRow(ln), aligns = splitRow(lines[i + 1]).map(function (c) { return /^:-+:$/.test(c) ? 'center' : /-:$/.test(c) ? 'right' : ''; });
        i += 2;
        var rows = [];
        while (i < lines.length && lines[i].trim() !== '' && lines[i].indexOf('|') >= 0) { rows.push(splitRow(lines[i])); i++; }
        var t = '<div class="tbl"><table><thead><tr>' + head.map(function (c, k) { return '<th' + (aligns[k] ? ' style="text-align:' + aligns[k] + '"' : '') + '>' + inline(c) + '</th>'; }).join('') + '</tr></thead><tbody>';
        rows.forEach(function (r) { t += '<tr>' + head.map(function (_, k) { return '<td' + (aligns[k] ? ' style="text-align:' + aligns[k] + '"' : '') + '>' + inline(r[k] || '') + '</td>'; }).join('') + '</tr>'; });
        out.push(t + '</tbody></table></div>');
        continue;
      }
      if (reUl.test(ln) || reOl.test(ln)) {
        var base = (reUl.exec(ln) || reOl.exec(ln))[1].replace(/\t/g, '    ').length;
        var r = renderList(lines, i, base); out.push(r.html); i = r.i; continue;
      }
      // 段落
      var para = [ln]; i++;
      while (i < lines.length && lines[i].trim() !== '' && !reFence.test(lines[i]) && !reH.test(lines[i]) && !reHr.test(lines[i]) && !/^ {0,3}>/.test(lines[i]) &&
        !reUl.test(lines[i]) && !reOl.test(lines[i]) && !(lines[i].indexOf('|') >= 0 && i + 1 < lines.length && reSep.test(lines[i + 1]))) { para.push(lines[i]); i++; }
      out.push('<p>' + para.map(function (p, k) { return inline(p.replace(/\s+$/, k < para.length - 1 && / {2,}$/.test(p) ? '' : '')); }).join('<br>').replace(/<br>/g, ' ') .replace(/ {2}$/, '') + '</p>');
    }
    return out.join('\n');
  };

  /* 复制按钮：事件委托（无内联事件处理器） */
  document.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('button[data-copy]');
    if (!b) return;
    var pre = b.closest('.codeblock').querySelector('pre');
    LA.copyText(pre.textContent);
  });
})();
