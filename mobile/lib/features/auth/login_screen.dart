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

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

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
      setState(() => _error = 'حدث خطأ غير متوقع: $e');
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
                              onPressed: () {},
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
        // Logo puck — the web uses a PNG asset; we render a mint circle
        // with an emerald initial as a placeholder so the app has an
        // identity mark without shipping the .png through Flutter yet.
        Container(
          width: 56,
          height: 56,
          decoration: BoxDecoration(
            color: BrandColors.emerald50,
            borderRadius: BorderRadius.circular(16),
            border: Border.all(color: BrandColors.emerald100, width: 1.5),
          ),
          alignment: Alignment.center,
          child: const Text(
            'م',
            style: TextStyle(
              color: BrandColors.emerald700,
              fontSize: 28,
              fontWeight: FontWeight.w800,
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
