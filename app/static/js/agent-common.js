/* MARSOUD-INSIGHTS-CONVERSATIONS-01 (2026-08-08) — shared helpers
 * for the accountant + insights chat pages. First file under
 * app/static/js/; both templates load this via <script src=...>
 * and drop their inline duplicates of escapeHtml. Whenever a bug
 * appears in table rendering or escape logic, fixing it here fixes
 * both agents at once — that's the point.
 *
 * Every exported symbol is attached to window so the templates can
 * call them from their existing inline scripts without ES modules.
 */
(function (w) {
  'use strict';

  // Same one-liner both templates used to define inline. XSS-safe
  // for the six HTML-active chars.
  function escapeHtml(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/[&<>"']/g, function (c) {
      return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;',
                '"': '&quot;', "'": '&#39;' }[c]);
    });
  }

  // Detects markdown tables and returns HTML with real <table>s in
  // place of the pipe/dash pseudo-tables. Non-table text is passed
  // through escapeHtml + whitespace-pre-wrap-friendly (\n preserved
  // as literal newlines in the escaped output; the containing bubble
  // already carries whitespace-pre-wrap so line breaks render).
  //
  // A markdown "table" here means: two or more consecutive lines
  // that start+end with `|`, where the SECOND line is a separator
  // (cells match /^\s*:?-{3,}:?\s*$/). This is the shape the model
  // returns for financial output (columns like الحساب / رصيد / إلخ).
  //
  // Security: cell contents pass through escapeHtml before landing
  // in the DOM, so no XSS from the model. The table-detection regex
  // never emits raw model text unescaped.
  function renderMarkdownTables(text) {
    if (!text) return '';
    var src = String(text);
    var lines = src.split('\n');
    var out = [];
    var i = 0;

    // A single-line utility test for "looks like a table row".
    function isRow(line) {
      return /^\s*\|.*\|\s*$/.test(line);
    }
    // The separator row: cells are just dashes (optional colons for
    // alignment; markdown-standard but we don't act on them beyond
    // recognition).
    function isSeparator(line) {
      if (!isRow(line)) return false;
      var cells = splitRow(line);
      if (cells.length === 0) return false;
      return cells.every(function (c) {
        return /^\s*:?-{3,}:?\s*$/.test(c);
      });
    }
    // Split "| a | b | c |" → ["a", "b", "c"] (trims outer pipes +
    // per-cell whitespace).
    function splitRow(line) {
      var trimmed = line.trim();
      if (trimmed.startsWith('|')) trimmed = trimmed.slice(1);
      if (trimmed.endsWith('|')) trimmed = trimmed.slice(0, -1);
      return trimmed.split('|').map(function (c) { return c.trim(); });
    }

    while (i < lines.length) {
      // Table? Need header row + separator on next line.
      if (i + 1 < lines.length && isRow(lines[i]) && isSeparator(lines[i + 1])) {
        var header = splitRow(lines[i]);
        var body = [];
        i += 2; // skip header + separator
        while (i < lines.length && isRow(lines[i])) {
          body.push(splitRow(lines[i]));
          i++;
        }
        out.push(renderTable(header, body));
        continue;
      }
      // Plain line — escape and emit as-is. Newline is preserved
      // via the container's whitespace-pre-wrap.
      out.push(escapeHtml(lines[i]));
      i++;
    }
    return out.join('\n');
  }

  // Reuses the exact classes tool_trace tables already use in
  // chat.html — same visual language, no new design.
  function renderTable(header, body) {
    var thead = '<thead><tr>' + header.map(function (h) {
      return '<th class="px-2 py-1 text-right border-b border-slate-200">' +
             escapeHtml(h) + '</th>';
    }).join('') + '</tr></thead>';
    var tbody = '<tbody>' + body.map(function (row) {
      return '<tr>' + row.map(function (cell) {
        return '<td class="px-2 py-1 border-b border-slate-100 text-right">' +
               escapeHtml(cell) + '</td>';
      }).join('') + '</tr>';
    }).join('') + '</tbody>';
    return '<div class="overflow-x-auto my-2">' +
           '<table class="w-full text-xs">' + thead + tbody + '</table>' +
           '</div>';
  }

  w.escapeHtml = escapeHtml;
  w.renderMarkdownTables = renderMarkdownTables;
})(window);
