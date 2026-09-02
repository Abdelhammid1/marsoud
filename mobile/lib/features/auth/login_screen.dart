// MARSOUD-MOBILE-FLUTTER — login screen, matched to app/templates/auth/login.html
//
// Layout (mirrors the web):
//   [logo circle] + [مرصود gradient title] + subtitle
//   heading "مرحباً بعودتك" + "سجّل دخولك للاستمرار"
//   white card, rounded-2xl, shadow-xl, border, p-8
//     · email + password inputs (12px radius, blue focus ring)
//     · navy-gradient submit button
//   inline error surface
import 'dart:async';
import 'dart:io' show Platform;

import 'package:flutter/foundation.dart';   // kDebugMode + debugPrint
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/env.dart';
import '../../app/theme.dart';
import '../../data/api_client.dart';
import '../../data/auth_repository.dart';
import '../../data/auth_state.dart';
import '../../data/push_service.dart';
import '../../widgets/gradient_button.dart';
import '../../widgets/gradient_heading.dart';

class LoginScreen extends ConsumerStatefulWidget {
  const LoginScreen({super.key});

  @override
  ConsumerState<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends ConsumerState<LoginScreen> {
  final _emailCtrl = TextEditingController();
  final _passCtrl = TextEditingController();
  bool _submitting = false;
  String? _error;
  bool _showPassword = false;

  @override
  void dispose() {
    _emailCtrl.dispose();
    _passCtrl.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    if (_submitting) return;
    // MARSOUD-MOBILE-SHIP-READY-01 (M8) — trivial client-side
    // validation so we don't pay a network round-trip for an empty
    // form (also gives bad-actor rate-limit surface for /login POSTs
    // less air to breathe).
    final email = _emailCtrl.text.trim();
    final pw = _passCtrl.text;
    if (email.isEmpty || pw.isEmpty) {
      setState(() => _error = 'أدخل البريد وكلمة السر.');
      return;
    }
    if (!email.contains('@') || !email.contains('.')) {
      setState(() => _error = 'صيغة البريد الإلكتروني غير صحيحة.');
      return;
    }
    setState(() {
      _submitting = true;
      _error = null;
    });
    try {
      final repo = ref.read(authRepositoryProvider);
      final session = await repo.login(
        email: _emailCtrl.text,
        password: _passCtrl.text,
        deviceName: _deviceLabel(),
      );
      await ref.read(authProvider.notifier).setSession(session);
      // MARSOUD-MOBILE-TKT-05 (2026-08-18) — register the FCM
      // token with the backend right after login. Best-effort —
      // silent on any Firebase/permission failure. Fire-and-
      // forget: we don't want a Firebase hiccup to hold up the
      // login navigation.
      unawaited(ref.read(pushServiceProvider).onLogin());
    } on ApiException catch (e) {
      setState(() => _error = _humanize(e));
    } catch (e) {
      // MARSOUD-MOBILE-SHIP-READY-01 (H7) — used to show raw `$e`
      // which may include a stack-tracey / mixed-language string
      // (SocketException, TimeoutException, TypeError on bad JSON).
      // Keep a short, user-facing sentence; the details go to debug.
      if (kDebugMode) debugPrint('[login] $e');
      setState(() => _error = 'تعذّر الاتصال — تأكد من الإنترنت وحاول مرة أخرى.');
    } finally {
      if (mounted) setState(() => _submitting = false);
    }
  }

  String _humanize(ApiException e) {
    switch (e.message) {
      case 'missing_credentials':
        return 'أدخل البريد وكلمة السر.';
      case 'invalid_credentials':
        return 'البريد أو كلمة السر غير صحيحة.';
      case 'rate_limited':
        final sec = (e.body is Map)
            ? (e.body['retry_after_seconds'] ?? '?')
            : '?';
        return 'محاولات كثيرة — انتظر $sec ثانية.';
      case 'account_locked':
        final min = (e.body is Map)
            ? (e.body['retry_after_minutes'] ?? '?')
            : '?';
        return 'حسابك مقفل مؤقتاً — حاول بعد $min دقيقة.';
      case 'account_inactive':
        return 'حسابك غير مفعّل — تواصل مع مالك الشركة.';
      case 'no_companies':
      case 'all_companies_suspended':
        return 'لا توجد شركة نشطة مرتبطة بحسابك.';
      case 'email_verification_required':
        return 'يجب تفعيل بريدك الإلكتروني أولاً — افتح تطبيق مرصود من المتصفح لإكمال التفعيل.';
      case 'terms_acceptance_required':
        return 'يجب قبول شروط الاستخدام المحدّثة — افتح تطبيق مرصود من المتصفح لقبولها.';
      case 'plan_selection_required':
        return 'يجب اختيار باقة اشتراك — افتح تطبيق مرصود من المتصفح لاختيارها.';
      default:
        return e.message;
    }
  }

  Future<void> _openForgotPasswordSheet() async {
    // MARSOUD-MOBILE-FORGOT-PW-01 + SHIP-READY-01 (H9) — surface the
    // WEB host (Env.webBaseUrl), NOT the API host. In split-domain
    // prod (api.marsoud.com vs app.marsoud.com), the reset page
    // lives on the web host — pasting an API-host URL into a browser
    // 404s. `webBaseUrl` falls back to `apiBaseUrl` when unset, so
    // dev + single-host prod still work.
    final base = Env.webBaseUrl;
    final url = base.isEmpty
        ? '/forgot-password'
        : '${base.replaceAll(RegExp(r"/+\$"), "")}/forgot-password';
    if (!mounted) return;
    await showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      backgroundColor: Colors.white,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(20)),
      ),
      builder: (ctx) => Padding(
        padding: EdgeInsets.only(
          left: 20, right: 20, top: 20,
          bottom: 20 + MediaQuery.of(ctx).viewInsets.bottom,
        ),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            const Text(
              'استعادة كلمة السر',
              textAlign: TextAlign.center,
              style: TextStyle(
                color: BrandColors.navy900,
                fontWeight: FontWeight.w800,
                fontSize: 18,
              ),
            ),
            const SizedBox(height: 12),
            const Text(
              'استعادة كلمة السر متاحة حالياً من متصفح الويب فقط. '
              'افتح الرابط التالي من المتصفح، أدخل بريدك، وستصلك '
              'رسالة بتفعيل كلمة سر جديدة:',
              textAlign: TextAlign.center,
              style: TextStyle(
                color: BrandColors.slate500,
                fontSize: 13,
                height: 1.7,
              ),
            ),
            const SizedBox(height: 14),
            Container(
              padding: const EdgeInsets.all(12),
              decoration: BoxDecoration(
                color: BrandColors.slate100,
                borderRadius: BorderRadius.circular(10),
              ),
              child: SelectableText(
                url,
                textDirection: TextDirection.ltr,
                textAlign: TextAlign.center,
                style: const TextStyle(
                  color: BrandColors.navy900,
                  fontFamily: 'monospace',
                  fontSize: 13,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ),
            const SizedBox(height: 8),
            const Text(
              'يمكنك الضغط طويلاً على الرابط لنسخه.',
              textAlign: TextAlign.center,
              style: TextStyle(
                color: BrandColors.slate400,
                fontSize: 11,
              ),
            ),
            const SizedBox(height: 16),
            GradientButton.navy(
              label: 'تم',
              onPressed: () => Navigator.of(ctx).pop(),
            ),
          ],
        ),
      ),
    );
  }

  String _deviceLabel() {
    try {
      final s =
          '${Platform.operatingSystem}-${Platform.operatingSystemVersion}';
      final end = s.length < 40 ? s.length : 40;
      return s.substring(0, end);
    } catch (_) {
      return 'unknown';
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: ScaffoldGradient(
        child: SafeArea(
          child: Center(
            child: SingleChildScrollView(
              padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 32),
              child: ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 440),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    const SizedBox(height: 16),
                    _LogoRow(),
                    const SizedBox(height: 24),
                    GradientHeading.navy(
                      'مرحباً بعودتك',
                      fontSize: 28,
                    ),
                    const SizedBox(height: 8),
                    const Text(
                      'سجّل دخولك للاستمرار',
                      textAlign: TextAlign.center,
                      style: TextStyle(
                        color: BrandColors.slate500,
                        fontSize: 15,
                      ),
                    ),
                    const SizedBox(height: 28),
                    // Card — matches the web `.bg-white rounded-2xl shadow-xl border p-8`.
                    Container(
                      decoration: BoxDecoration(
                        color: Colors.white,
                        borderRadius: BorderRadius.circular(20),
                        border: Border.all(color: BrandColors.slate100),
                        boxShadow: [
                          BoxShadow(
                            color: BrandColors.navy900.withValues(alpha: 0.08),
                            blurRadius: 32,
                            offset: const Offset(0, 12),
                          ),
                        ],
                      ),
                      padding: const EdgeInsets.all(28),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        children: [
                          if (_error != null) ...[
                            _ErrorBanner(_error!),
                            const SizedBox(height: 16),
                          ],
                          const _FieldLabel('البريد الإلكتروني'),
                          const SizedBox(height: 6),
                          Directionality(
                            textDirection: TextDirection.ltr,
                            child: TextField(
                              controller: _emailCtrl,
                              keyboardType: TextInputType.emailAddress,
                              autofillHints: const [AutofillHints.email],
                              textInputAction: TextInputAction.next,
                              textAlign: TextAlign.left,
                              decoration: const InputDecoration(
                                hintText: 'you@company.com',
                                prefixIcon: Icon(Icons.email_outlined,
                                    color: BrandColors.slate400),
                              ),
                            ),
                          ),
                          const SizedBox(height: 16),
                          const _FieldLabel('كلمة المرور'),
                          const SizedBox(height: 6),
                          TextField(
                            controller: _passCtrl,
                            obscureText: !_showPassword,
                            autofillHints: const [AutofillHints.password],
                            textInputAction: TextInputAction.done,
                            onSubmitted: (_) => _submit(),
                            decoration: InputDecoration(
                              prefixIcon: const Icon(Icons.lock_outline,
                                  color: BrandColors.slate400),
                              suffixIcon: IconButton(
                                color: BrandColors.slate400,
                                icon: Icon(_showPassword
                                    ? Icons.visibility_off
                                    : Icons.visibility),
                                onPressed: () => setState(
                                    () => _showPassword = !_showPassword),
                              ),
                            ),
                          ),
                          const SizedBox(height: 24),
                          GradientButton.navy(
                            label: 'تسجيل الدخول',
                            onPressed: _submitting ? null : _submit,
                            loading: _submitting,
                          ),
                          const SizedBox(height: 16),
                          Align(
                            alignment: Alignment.center,
                            child: TextButton(
                              // MARSOUD-MOBILE-FORGOT-PW-01 (2026-09-02)
                              // — used to be `onPressed: () {}` (a
                              // dead no-op). Until the JSON /api/v1/
                              // auth/forgot-password endpoint ships,
                              // point users at the web flow which is
                              // already live at /forgot-password —
                              // same shape as the guidance we already
                              // give for email_verification_required.
                              onPressed: _openForgotPasswordSheet,
                              style: TextButton.styleFrom(
                                foregroundColor: BrandColors.slate500,
                              ),
                              child: const Text(
                                'نسيت كلمة السر؟',
                                style: TextStyle(fontWeight: FontWeight.w600),
                              ),
                            ),
                          ),
                        ],
                      ),
                    ),
                    const SizedBox(height: 20),
                    Text(
                      'نظام إدارة أعمال متكامل للشركات',
                      textAlign: TextAlign.center,
                      style: TextStyle(
                        color: BrandColors.slate400,
                        fontSize: 12,
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

class _LogoRow extends StatelessWidget {
  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisAlignment: MainAxisAlignment.center,
      children: [
        // MARSOUD-MOBILE-BRAND-LOGO-01 (2026-09-03) — real logo
        // asset (copied from app/static/img/logo.png). The old
        // emerald "م" puck stays as an errorBuilder fallback so a
        // missing/corrupt asset still ships an identifiable mark.
        Container(
          width: 56,
          height: 56,
          decoration: BoxDecoration(
            color: BrandColors.emerald50,
            borderRadius: BorderRadius.circular(16),
            border: Border.all(color: BrandColors.emerald100, width: 1.5),
          ),
          alignment: Alignment.center,
          child: ClipRRect(
            borderRadius: BorderRadius.circular(12),
            child: Image.asset(
              'assets/images/logo.png',
              width: 44,
              height: 44,
              fit: BoxFit.contain,
              errorBuilder: (_, __, ___) => const Text(
                'م',
                style: TextStyle(
                  color: BrandColors.emerald700,
                  fontSize: 28,
                  fontWeight: FontWeight.w800,
                ),
              ),
            ),
          ),
        ),
        const SizedBox(width: 12),
        Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisSize: MainAxisSize.min,
          children: [
            GradientHeading(
              'مرصود',
              fontSize: 26,
              fontWeight: FontWeight.w800,
              align: TextAlign.start,
            ),
            const SizedBox(height: 2),
            Text(
              'نظام إدارة أعمال متكامل',
              style: TextStyle(
                color: BrandColors.slate500,
                fontSize: 11,
              ),
            ),
          ],
        ),
      ],
    );
  }
}

class _FieldLabel extends StatelessWidget {
  final String text;
  const _FieldLabel(this.text);
  @override
  Widget build(BuildContext context) {
    return Text(
      text,
      style: const TextStyle(
        color: BrandColors.slate700,
        fontSize: 14,
        fontWeight: FontWeight.w600,
      ),
    );
  }
}

class _ErrorBanner extends StatelessWidget {
  final String msg;
  const _ErrorBanner(this.msg);
  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: BrandColors.red50,
        border: Border.all(color: const Color(0xFFFECACA)),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Row(
        children: [
          const Icon(Icons.error_outline, color: BrandColors.red700, size: 20),
          const SizedBox(width: 10),
          Expanded(
            child: Text(
              msg,
              style: const TextStyle(
                color: BrandColors.red700,
                fontWeight: FontWeight.w600,
                fontSize: 13,
                height: 1.5,
              ),
            ),
          ),
        ],
      ),
    );
  }
}
