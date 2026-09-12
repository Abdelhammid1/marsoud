// MARSOUD-MOBILE-REACCEPT-TERMS-01 (2026-09-12) — the "قبول الشروط
// المحدّثة" screen.
//
// When /api/v1/auth/login returns 403 terms_acceptance_required,
// login_screen pushes THIS screen instead of showing a "open the
// browser" error. The screen:
//
//   1. Fetches the current terms + privacy HTML via
//      GET /api/v1/auth/legal (server-authored, super-admin-published)
//   2. Renders both inside scrollable cards
//   3. Requires a "أوافق على الشروط والخصوصية" checkbox tick
//   4. On Continue → calls POST /api/v1/auth/accept-terms with the
//      same credentials the user already typed on login, which
//      updates their terms_version + mints the bearer
//   5. Pops back to login_screen with `true` so it knows the session
//      is set + can navigate home
//
// Apple App Store guideline 5.1.1 requires in-app account
// management; this replaces the browser hand-off that would have
// tripped the review.
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/theme.dart';
import '../../data/api_client.dart';
import '../../data/auth_repository.dart';
import '../../data/auth_state.dart';
import '../../widgets/gradient_button.dart';

class ReacceptTermsScreen extends ConsumerStatefulWidget {
  const ReacceptTermsScreen({
    super.key,
    required this.email,
    required this.password,
    required this.deviceLabel,
  });

  final String email;
  final String password;
  final String deviceLabel;

  @override
  ConsumerState<ReacceptTermsScreen> createState() =>
      _ReacceptTermsScreenState();
}

class _ReacceptTermsScreenState
    extends ConsumerState<ReacceptTermsScreen> {
  bool _loading = true;
  bool _agreed = false;
  bool _submitting = false;
  String? _error;
  Map<String, String> _content = const {
    'terms_html': '',
    'privacy_html': '',
    'terms_version': '',
  };

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final content =
          await ref.read(authRepositoryProvider).fetchLegalContent();
      if (!mounted) return;
      setState(() {
        _content = content;
        _loading = false;
      });
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() {
        _error = e.message;
        _loading = false;
      });
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _error = 'تعذّر جلب نص الشروط — تأكد من الإنترنت.';
        _loading = false;
      });
    }
  }

  Future<void> _submit() async {
    if (!_agreed) return;
    setState(() {
      _submitting = true;
      _error = null;
    });
    try {
      final session =
          await ref.read(authRepositoryProvider).acceptTermsAndLogin(
                email: widget.email,
                password: widget.password,
                deviceName: widget.deviceLabel,
              );
      await ref.read(authProvider.notifier).setSession(session);
      if (!mounted) return;
      Navigator.of(context).pop(true);
    } on ApiException catch (e) {
      setState(() {
        _error = _humanize(e);
        _submitting = false;
      });
    } catch (_) {
      setState(() {
        _error = 'تعذّر الاتصال — تأكد من الإنترنت وحاول مرة أخرى.';
        _submitting = false;
      });
    }
  }

  String _humanize(ApiException e) {
    switch (e.message) {
      case 'agreement_required':
        return 'لازم توافق على الشروط قبل المتابعة.';
      case 'invalid_credentials':
        return 'كلمة السر تغيرت — ارجع لصفحة الدخول وحاول تاني.';
      case 'account_locked':
        return 'حسابك مقفل مؤقتاً — حاول لاحقاً.';
      case 'plan_selection_required':
        return 'باقة الاشتراك مش مختارة — كمّل من المتصفح.';
      default:
        return e.message;
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: BrandColors.slate50,
      appBar: AppBar(
        title: const Text('قبول الشروط المحدّثة'),
        automaticallyImplyLeading: true,
      ),
      body: SafeArea(
        child: _loading
            ? const Center(child: CircularProgressIndicator())
            : SingleChildScrollView(
                padding: const EdgeInsets.all(20),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    _HeaderCard(version: _content['terms_version'] ?? ''),
                    const SizedBox(height: 16),
                    _ContentCard(
                      icon: '📄',
                      title: 'شروط وأحكام الاستخدام',
                      html: _content['terms_html'] ?? '',
                    ),
                    const SizedBox(height: 12),
                    _ContentCard(
                      icon: '🔒',
                      title: 'سياسة الخصوصية',
                      html: _content['privacy_html'] ?? '',
                    ),
                    const SizedBox(height: 20),
                    Container(
                      padding: const EdgeInsets.symmetric(
                          horizontal: 12, vertical: 8),
                      decoration: BoxDecoration(
                        color: Colors.white,
                        border: Border.all(color: BrandColors.slate200),
                        borderRadius: BorderRadius.circular(10),
                      ),
                      child: CheckboxListTile(
                        value: _agreed,
                        onChanged: _submitting
                            ? null
                            : (v) => setState(() => _agreed = v ?? false),
                        title: const Text(
                          'أوافق على الشروط وسياسة الخصوصية.',
                          style: TextStyle(
                              fontSize: 13.5,
                              fontWeight: FontWeight.w700,
                              color: BrandColors.navy900),
                        ),
                        controlAffinity: ListTileControlAffinity.leading,
                      ),
                    ),
                    if (_error != null) ...[
                      const SizedBox(height: 12),
                      Container(
                        padding: const EdgeInsets.all(12),
                        decoration: BoxDecoration(
                          color: const Color(0xFFFEE2E2),
                          border: Border.all(color: const Color(0xFFFCA5A5)),
                          borderRadius: BorderRadius.circular(8),
                        ),
                        child: Text(
                          _error!,
                          style: const TextStyle(
                              color: Color(0xFFB91C1C),
                              fontWeight: FontWeight.w600),
                        ),
                      ),
                    ],
                    const SizedBox(height: 20),
                    GradientButton(
                      onPressed:
                          (_agreed && !_submitting) ? _submit : null,
                      loading: _submitting,
                      label: 'متابعة',
                    ),
                    const SizedBox(height: 12),
                    TextButton(
                      onPressed: _submitting
                          ? null
                          : () => Navigator.of(context).pop(false),
                      child: const Text('رجوع'),
                    ),
                  ],
                ),
              ),
      ),
    );
  }
}

class _HeaderCard extends StatelessWidget {
  const _HeaderCard({required this.version});
  final String version;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: const Color(0xFFEFF6FF),
        border: Border.all(color: const Color(0xFFBFDBFE)),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              const Text('📄',
                  style: TextStyle(fontSize: 18)),
              const SizedBox(width: 8),
              const Expanded(
                child: Text(
                  'تحديث في الشروط والخصوصية',
                  style: TextStyle(
                    fontSize: 14,
                    fontWeight: FontWeight.w800,
                    color: Color(0xFF1E3A8A),
                  ),
                ),
              ),
              if (version.isNotEmpty)
                Text(
                  'إصدار $version',
                  style: const TextStyle(
                    fontSize: 11,
                    fontFamily: 'monospace',
                    color: Color(0xFF3B82F6),
                  ),
                ),
            ],
          ),
          const SizedBox(height: 6),
          const Text(
            'قبل ما تكمل، اقرأ النسخة الجديدة ووافق عليها للمتابعة. '
            'موافقتك بتترصد في سجل الامتثال ولا تقدر تتراجع عنها من هنا — '
            'حذف الحساب من "حسابي" لو حبيت.',
            style: TextStyle(
              fontSize: 12.5,
              color: Color(0xFF3B4A6B),
              height: 1.6,
            ),
          ),
        ],
      ),
    );
  }
}

class _ContentCard extends StatelessWidget {
  const _ContentCard({
    required this.icon,
    required this.title,
    required this.html,
  });

  final String icon;
  final String title;
  final String html;

  @override
  Widget build(BuildContext context) {
    // Strip tags for a lightweight text render. We deliberately DON'T
    // pull in a full HTML renderer here — the super-admin publishes
    // paragraphs, not tables/images, so a `Text` is enough. Any user
    // who wants the styled version can open /terms on the web.
    final plain = _htmlToPlain(html);
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: Colors.white,
        border: Border.all(color: BrandColors.slate200),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Text(icon, style: const TextStyle(fontSize: 18)),
              const SizedBox(width: 8),
              Text(
                title,
                style: const TextStyle(
                  fontSize: 14,
                  fontWeight: FontWeight.w800,
                  color: BrandColors.navy900,
                ),
              ),
            ],
          ),
          const SizedBox(height: 10),
          ConstrainedBox(
            constraints: const BoxConstraints(maxHeight: 260),
            child: SingleChildScrollView(
              child: Text(
                plain.isEmpty
                    ? '(لم ينشر المدير محتوى بعد. للنسخة الكاملة، افتح الرابط من المتصفح.)'
                    : plain,
                style: const TextStyle(
                  fontSize: 12.5,
                  color: BrandColors.slate700,
                  height: 1.7,
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }

  static String _htmlToPlain(String html) {
    if (html.isEmpty) return '';
    // Convert <p>, <br>, <li> to newlines, then strip other tags.
    final withBreaks = html
        .replaceAll(RegExp(r'</p>', caseSensitive: false), '\n\n')
        .replaceAll(RegExp(r'<br\s*/?>', caseSensitive: false), '\n')
        .replaceAll(RegExp(r'</li>', caseSensitive: false), '\n');
    final stripped =
        withBreaks.replaceAll(RegExp(r'<[^>]+>'), '');
    // Collapse whitespace runs.
    return stripped
        .replaceAll(RegExp(r'\n{3,}'), '\n\n')
        .replaceAll('&nbsp;', ' ')
        .replaceAll('&amp;', '&')
        .replaceAll('&lt;', '<')
        .replaceAll('&gt;', '>')
        .trim();
  }
}
