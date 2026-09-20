// MARSOUD-MOBILE-STRIP-HTML-01 (2026-09-20) — the web's rich-text
// editor (MARSOUD-RICH-TEXT-EDITOR-01) stores task/lead descriptions
// as HTML (`<p>...`, `<strong>...`, `<code>...`, `<span class="ql-ui">`,
// etc.).  The Flutter `Text` widget renders that as literal tags,
// which the user saw as a wall of "<p><strong>...`" text in the
// task detail screen.
//
// This helper strips every HTML tag and decodes the handful of
// entities Quill emits, leaving readable plain text.  Deliberately
// lightweight — no `flutter_html` dependency (would drag several
// megabytes into the app) — because the mobile task/lead screens
// don't need full formatting, just legible text.  If we later want
// live formatting, swap this helper for a WebView/flutter_html
// render at the call sites.
//
// Idempotent + safe on empty / null-like input.

const _tagPattern = r'<[^>]*>';
final _tagRegex = RegExp(_tagPattern, multiLine: true, dotAll: true);
final _whitespaceRegex = RegExp(r'\s+');

const _entityMap = <String, String>{
  '&nbsp;': ' ',
  '&amp;': '&',
  '&lt;': '<',
  '&gt;': '>',
  '&quot;': '"',
  '&#39;': "'",
  '&apos;': "'",
  '&mdash;': '—',
  '&ndash;': '–',
  '&hellip;': '…',
  '&laquo;': '«',
  '&raquo;': '»',
  '&copy;': '©',
  '&reg;': '®',
  '&trade;': '™',
};

/// Return `raw` with every HTML tag removed and common entities
/// decoded.  Preserves line breaks by converting `<br>` and closing
/// block tags to newlines BEFORE the tag strip.  Empty/null-ish
/// input returns "".
String stripHtml(String? raw) {
  if (raw == null || raw.isEmpty) return '';
  var s = raw;
  // Preserve legible line breaks — a paragraph or list item ending
  // becomes a newline, so the resulting text isn't one long run-on.
  s = s.replaceAll(RegExp(r'<br\s*/?>', caseSensitive: false), '\n');
  s = s.replaceAll(
      RegExp(r'</(p|li|div|h[1-6])\s*>', caseSensitive: false), '\n');
  s = s.replaceAll(_tagRegex, '');
  // Numeric entities: `&#123;` and `&#x7B;`.
  s = s.replaceAllMapped(
    RegExp(r'&#(\d+);'),
    (m) {
      final code = int.tryParse(m.group(1)!);
      return code == null ? m.group(0)! : String.fromCharCode(code);
    },
  );
  s = s.replaceAllMapped(
    RegExp(r'&#x([0-9a-fA-F]+);'),
    (m) {
      final code = int.tryParse(m.group(1)!, radix: 16);
      return code == null ? m.group(0)! : String.fromCharCode(code);
    },
  );
  _entityMap.forEach((k, v) {
    s = s.replaceAll(k, v);
  });
  // Collapse runs of whitespace inside a single line (Quill leaves
  // artifact double-spaces) without touching real newlines.
  s = s.split('\n').map((ln) => ln.replaceAll(_whitespaceRegex, ' ').trim()).join('\n');
  // Trim excess blank lines at head/tail; internal blank stays.
  s = s.replaceAll(RegExp(r'\n{3,}'), '\n\n').trim();
  return s;
}

/// True when the string looks like it carries HTML markup — a
/// callable heuristic if a screen wants to conditionally strip.
/// Currently: it contains a `<`-then-tag-name-start sequence.
bool looksLikeHtml(String? raw) {
  if (raw == null || raw.isEmpty) return false;
  return RegExp(r'<[a-zA-Z/]').hasMatch(raw);
}
