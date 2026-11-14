/* MARSOUD-INSIGHTS-UX-P78 - shared rich reply renderer (markdown in, styled report HTML out).
 * Attaches window.agFmt(text). Pure string -> string, every piece of model text is HTML-escaped first. */
(function (w) {
  'use strict';
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function inline(s) {
    var t = esc(s);
    t = t.replace(/`([^`\n]+)`/g, '<code>$1</code>');
    t = t.replace(/\*\*([^*\n]+?)\*\*/g, '<strong>$1</strong>');
    t = t.replace(/__([^_\n]+?)__/g, '<strong>$1</strong>');
    t = t.replace(/(^|[\s(،:])\*([^*\s][^*\n]*?)\*(?=$|[\s).،:!?؟])/g, '$1<em>$2</em>');
    t = t.replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    t = t.replace(/\*\*|__/g, '');
    t = t.replace(/(\d)\*(?=\d)/g, '$1\u0001').replace(/\*/g, '').replace(/\u0001/g, '*');
    t = t.replace(/(^|[\s(>:])([+＋])(\d[\d.,٫]*)\s?(%|٪)/g, '$1<span class="ag-d ag-up">▲ $3%</span>');
    t = t.replace(/(^|[\s(>:])([\-−–])(\d[\d.,٫]*)\s?(%|٪)/g, '$1<span class="ag-d ag-dn">▼ $3%</span>');
    return t;
  }
  var AR = '٠١٢٣٤٥٦٧٨٩';
  function toLatin(s) { return String(s).replace(/[٠-٩]/g, function (c) { return String(AR.indexOf(c)); }); }
  var NUM = /^[(\-+−–]?\s*[\d٠-٩][\d٠-٩,٬.٫\s]*\)?\s*(%|٪|ر\.?\s?س\.?|ريال|ج\.?\s?م\.?|جنيه|SAR|EGP|SR|AED|USD|\$)?\s*$/;
  function plain(c) { return String(c).replace(/\*\*|__/g, '').trim(); }
  function isNum(c) { return NUM.test(plain(c)); }
  function isNeg(c) { return /^\s*[(\-−–]/.test(plain(c)); }
  var TOT = /^(\*\*|__)?\s*(الإجمالي|الاجمالي|إجمالي|اجمالي|المجموع|مجموع|الصافي|صافي|المتوسط|total)/i;

  function isRow(l) { return /^\s*\|.*\S/.test(l); }
  function cells(l) {
    var t = l.trim();
    if (t.charAt(0) === '|') t = t.slice(1);
    if (t.charAt(t.length - 1) === '|') t = t.slice(0, -1);
    return t.split('|').map(function (c) { return c.trim(); });
  }
  function isSep(l) {
    if (!isRow(l)) return false;
    var c = cells(l);
    return c.length > 0 && c.every(function (x) { return /^:?-{2,}:?$/.test(x); });
  }
  var LI_UL = /^(\s*)([-*•●▪◦–—])\s+(.+)$/;
  var LI_OL = /^(\s*)(\d{1,3}|[٠-٩]{1,3})[.)]\s+(.+)$/;
  function listItem(l) {
    var m = l.match(LI_OL);
    if (m) return { t: 'ol', lv: m[1].replace(/\t/g, '  ').length >= 2 ? 1 : 0, n: parseInt(toLatin(m[2]), 10), x: m[3] };
    m = l.match(LI_UL);
    if (m) return { t: 'ul', lv: m[1].replace(/\t/g, '  ').length >= 2 ? 1 : 0, n: 0, x: m[3] };
    return null;
  }
  function renderTable(hdr, rows) {
    var nc = hdr.length;
    rows.forEach(function (r) { if (r.length > nc) nc = r.length; });
    var numCol = [];
    for (var j = 0; j < nc; j++) {
      var any = false, all = true;
      rows.forEach(function (r) {
        var c = r[j] == null ? '' : r[j];
        if (c === '' || c === '—' || c === '-') return;
        if (isNum(c)) any = true; else all = false;
      });
      numCol[j] = any && all;
    }
    var h = '<div class="ag-tw"><table class="ag-t"><thead><tr>';
    for (var a = 0; a < nc; a++) h += '<th' + (numCol[a] ? ' class="num"' : '') + '>' + inline(hdr[a] || '') + '</th>';
    h += '</tr></thead><tbody>';
    rows.forEach(function (r) {
      var tot = TOT.test(r[0] || '');
      h += '<tr' + (tot ? ' class="tot"' : '') + '>';
      for (var b = 0; b < nc; b++) {
        var c = r[b] == null ? '' : r[b];
        var cl = [];
        if (numCol[b]) cl.push('num');
        if (numCol[b] && c && isNum(c) && isNeg(c)) cl.push('neg');
        h += '<td' + (cl.length ? ' class="' + cl.join(' ') + '"' : '') + '>' + inline(c) + '</td>';
      }
      h += '</tr>';
    });
    return h + '</tbody></table></div>';
  }
  function renderList(items) {
    var out = '', cur = null;
    function close() {
      if (!cur) return;
      var tag = cur.t === 'ol' ? 'ol' : 'ul';
      out += '<' + tag + ' class="ag-' + tag + '"' + (cur.t === 'ol' && cur.n > 1 ? ' style="counter-reset:ag-n ' + (cur.n - 1) + '"' : '') + '>';
      cur.lis.forEach(function (li) {
        out += '<li><div class="ag-li">' + inline(li.x);
        if (li.subs.length) {
          out += '<ul class="ag-sub">' + li.subs.map(function (s) { return '<li><span>' + inline(s) + '</span></li>'; }).join('') + '</ul>';
        }
        out += '</div></li>';
      });
      out += '</' + tag + '>';
      cur = null;
    }
    items.forEach(function (it) {
      if (it.lv === 1 && cur && cur.lis.length) { cur.lis[cur.lis.length - 1].subs.push(it.x); return; }
      if (cur && cur.t !== it.t) close();
      if (!cur) cur = { t: it.t, n: it.n, lis: [] };
      cur.lis.push({ x: it.x, subs: [] });
    });
    close();
    return out;
  }
  var CALL = /^(✅|⚠️?|❌|ℹ️?|💡|📌|🔴|🟢|🟡)/;
  function callKind(t) {
    if (/^(✅|🟢)/.test(t)) return 'ok';
    if (/^(⚠|🟡)/.test(t)) return 'warn';
    if (/^(❌|🔴)/.test(t)) return 'err';
    return 'info';
  }
  var SUM = /^(?:\*\*|__)?(الخلاصة|الملخص)\s*[:：](?:\*\*|__)?\s*(.+)$/;
  var ACT = /^(?:\*\*|__)?(الإجراء المقترح|إجراء مقترح|الإجراء|التوصية|الخطوة المقترحة|الخطوة التالية)\s*[:：](?:\*\*|__)?\s*(.+)$/;
  var KV = /^(?:[•●▪◦\-*–—]\s+)?(?:\*\*|__)?([^:：|*_]{2,36}?)(?:\*\*|__)?\s*[:：]\s*(?:\*\*|__)?([+\-−(]?\s*[\d٠-٩][^\n]{0,28}?)(?:\*\*|__)?\s*$/;
  function kvOf(l) {
    var m = String(l).trim().match(KV);
    if (!m) return null;
    if (SUM.test(l.trim()) || ACT.test(l.trim())) return null;
    return { k: m[1].trim(), v: m[2].trim() };
  }
  function renderKpis(items) {
    return '<div class="ag-kpis">' + items.map(function (it) {
      var neg = /^[(\-−–]/.test(it.v) && !/%/.test(it.v) ? ' ag-kneg' : '';
      return '<div class="ag-kpi' + neg + '"><div class="ag-kl">' + inline(it.k) + '</div><div class="ag-kv">' + inline(it.v) + '</div></div>';
    }).join('') + '</div>';
  }

  function agFmt(text) {
    var lines = String(text == null ? '' : text).replace(/\r\n?/g, '\n').split('\n');
    var out = [], para = [], i = 0, n = lines.length;
    function flush() {
      if (para.length) { out.push('<p class="ag-p">' + para.map(inline).join('<br>') + '</p>'); para = []; }
    }
    while (i < n) {
      var ln = lines[i], tr = ln.trim();
      if (!tr) { flush(); i++; continue; }
      if (/^```/.test(tr)) {
        flush();
        var buf = []; i++;
        while (i < n && !/^```/.test(lines[i].trim())) { buf.push(lines[i]); i++; }
        i++;
        out.push('<pre class="ag-code">' + esc(buf.join('\n')) + '</pre>');
        continue;
      }
      if (isRow(ln) && i + 1 < n && isSep(lines[i + 1])) {
        flush();
        var hdr = cells(ln), rows = [];
        i += 2;
        while (i < n && isRow(lines[i])) { if (!isSep(lines[i])) rows.push(cells(lines[i])); i++; }
        out.push(renderTable(hdr, rows));
        continue;
      }
      var sm = tr.match(SUM), am = tr.match(ACT);
      if (sm) { flush(); out.push('<div class="ag-sum"><span class="ag-si">🧭</span><div><b>' + inline(sm[1]) + '</b><span>' + inline(sm[2]) + '</span></div></div>'); i++; continue; }
      if (am) { flush(); out.push('<div class="ag-act"><span class="ag-si">🎯</span><div><b>' + inline(am[1]) + '</b><span>' + inline(am[2]) + '</span></div></div>'); i++; continue; }
      var k0 = kvOf(ln);
      if (k0) {
        var j = i, kv = [];
        while (j < n) { var kk = kvOf(lines[j]); if (!kk) break; kv.push(kk); j++; }
        if (kv.length >= 3) { flush(); out.push(renderKpis(kv)); i = j; continue; }
      }
      var hm = tr.match(/^#{1,6}\s+(.+?)\s*#*$/);
      var bm = tr.match(/^(\*\*|__)(.{1,80}?)\1\s*[:：]?$/);
      var cm = !listItem(ln) && !CALL.test(tr) ? tr.match(/^([^|•\-*\d>`#][^:：|]{1,50})[:：]$/) : null;
      if (hm || bm || cm) {
        flush();
        out.push('<div class="ag-h">' + inline(hm ? hm[1] : bm ? bm[2] : cm[1]) + '</div>');
        i++; continue;
      }
      if (/^([-*_])(\s*\1){2,}$/.test(tr)) { flush(); out.push('<hr class="ag-hr">'); i++; continue; }
      if (/^>/.test(tr)) {
        flush();
        var q = [];
        while (i < n && /^\s*>/.test(lines[i])) { q.push(lines[i].replace(/^\s*>\s?/, '')); i++; }
        out.push('<div class="ag-note">' + q.map(inline).join('<br>') + '</div>');
        continue;
      }
      if (listItem(ln)) {
        flush();
        var items = [];
        while (i < n) {
          var it = listItem(lines[i]);
          if (it) { items.push(it); i++; continue; }
          if (!lines[i].trim()) {
            var k = i + 1;
            while (k < n && !lines[k].trim()) k++;
            if (k < n && listItem(lines[k])) { i = k; continue; }
            break;
          }
          if (/^\s{2,}\S/.test(lines[i]) && items.length && !isRow(lines[i])) {
            items[items.length - 1].x += ' ' + lines[i].trim(); i++; continue;
          }
          break;
        }
        out.push(renderList(items));
        continue;
      }
      if (CALL.test(tr)) {
        flush();
        out.push('<div class="ag-call ag-call-' + callKind(tr) + '">' + inline(tr) + '</div>');
        i++; continue;
      }
      para.push(tr);
      i++;
    }
    flush();
    return '<div class="ag-rich">' + out.join('') + '</div>';
  }
  w.agFmt = agFmt;
})(window);
