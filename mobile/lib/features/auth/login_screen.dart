// MARSOUD-MOBILE-FLUTTER — login screen.
//
// MARSOUD-MOBILE-DESIGN-PREVIEW-01 (2026-09-29) — visual layout
// swapped to the Stitch-generated design (white bg, emerald shield
// mark, Cairo headings, fill-tinted inputs, emerald primary CTA).
// All the real behaviour is preserved:
//   · authRepositoryProvider.login() → authProvider.setSession()
//   · FCM token registration after login (fire-and-forget)
//   · terms_acceptance_required → ReacceptTermsScreen push
//   · humanised error messages via _humanize()
//   · forgot-password bottom sheet pointing at the web reset flow
//   · device label attached to the login for the session record
import 'dart:async';
import 'dart:io' show Platform;

import 'package:flutter/foundation.dart';   // kDebugMode + debugPrint
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/env.dart';
import '../../data/api_client.dart';
import '../../data/auth_repository.dart';
import '../../data/auth_state.dart';
import '../../data/push_service.dart';
import 'reaccept_terms_screen.dart';

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
      unawaited(ref.read(pushServiceProvider).onLogin());
    } on ApiException catch (e) {
      if (e.message == 'terms_acceptance_required' && mounted) {
        final ok = await Navigator.of(context).push<bool>(
          MaterialPageRoute(
            builder: (_) => ReacceptTermsScreen(
              email: _emailCtrl.text.trim(),
              password: _passCtrl.text,
              deviceLabel: _deviceLabel(),
            ),
          ),
        );
        if (ok == true) return;
      }
      setState(() => _error = _humanize(e));
    } catch (e) {
      if (kDebugMode) debugPrint('[login] $e');
      setState(() =>
          _error = 'تعذّر الاتصال — تأكد من الإنترنت وحاول مرة أخرى.');
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
        return 'لم يتم قبول الشروط. لتسجيل الدخول لازم تقبل الشروط المحدّثة.';
      case 'plan_selection_required':
        return 'يجب اختيار باقة اشتراك — افتح تطبيق مرصود من المتصفح لاختيارها.';
      default:
        return e.message;
    }
  }

  Future<void> _openForgotPasswordSheet() async {
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
      builder: (ctx) => Directionality(
        textDirection: TextDirection.rtl,
        child: Padding(
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
                style: TextStyle(                  color: Color(0xFF0A2540),
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
                style: TextStyle(                  color: Color(0xFF64748B),
                  fontSize: 13,
                  height: 1.7,
                ),
              ),
              const SizedBox(height: 14),
              Container(
                padding: const EdgeInsets.all(12),
                decoration: BoxDecoration(
                  color: const Color(0xFFF1F5F9),
                  borderRadius: BorderRadius.circular(10),
                ),
                child: SelectableText(
                  url,
                  textDirection: TextDirection.ltr,
                  textAlign: TextAlign.center,
                  style: const TextStyle(
                    color: Color(0xFF0A2540),
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
                style: TextStyle(                  color: Color(0xFF94A3B8),
                  fontSize: 11,
                ),
              ),
              const SizedBox(height: 16),
              ElevatedButton(
                onPressed: () => Navigator.of(ctx).pop(),
                style: ElevatedButton.styleFrom(
                  backgroundColor: const Color(0xFF059669),
                  minimumSize: const Size.fromHeight(48),
                  shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(12)),
                ),
                child: const Text(
                  'تم',
                  style: TextStyle(                    fontSize: 15,
                    fontWeight: FontWeight.w700,
                    color: Colors.white,
                  ),
                ),
              ),
            ],
          ),
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
    return Directionality(
      textDirection: TextDirection.rtl,
      child: Scaffold(
        backgroundColor: Colors.white,
        body: SafeArea(
          child: SingleChildScrollView(
            padding:
                const EdgeInsets.symmetric(horizontal: 24.0, vertical: 20),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                const SizedBox(height: 20),
                // Real Marsoud logo asset (not the Stitch shield).
                Align(
                  alignment: Alignment.centerRight,
                  child: Container(
                    width: 56,
                    height: 56,
                    decoration: BoxDecoration(
                      color: const Color(0xFFECFDF5),
                      borderRadius: BorderRadius.circular(14),
                      border: Border.all(color: const Color(0xFFD1FAE5)),
                    ),
                    alignment: Alignment.center,
                    child: ClipRRect(
                      borderRadius: BorderRadius.circular(10),
                      child: Image.asset(
                        'assets/images/logo.png',
                        width: 42,
                        height: 42,
                        fit: BoxFit.contain,
                        errorBuilder: (_, __, ___) => const Text(
                          'م',
                          style: TextStyle(
                            color: Color(0xFF047857),
                            fontSize: 28,
                            fontWeight: FontWeight.w900,
                          ),
                        ),
                      ),
                    ),
                  ),
                ),
                const SizedBox(height: 24),
                const Text(
                  'تسجيل الدخول',
                  style: TextStyle(                    fontSize: 26,
                    fontWeight: FontWeight.w800,
                    color: Color(0xFF0A2540),
                  ),
                ),
                const SizedBox(height: 6),
                const Text(
                  'مرحباً بك مجدداً، أدخل بياناتك للمتابعة',
                  style: TextStyle(                    fontSize: 14,
                    color: Color(0xFF64748B),
                  ),
                ),
                const SizedBox(height: 32),

                // Inline error surface
                if (_error != null) ...[
                  Container(
                    padding: const EdgeInsets.all(14),
                    decoration: BoxDecoration(
                      color: const Color(0xFFFEE2E2),
                      borderRadius: BorderRadius.circular(12),
                      border: Border.all(color: const Color(0xFFFECACA)),
                    ),
                    child: Row(
                      children: [
                        const Icon(Icons.error_outline,
                            color: Color(0xFFDC2626), size: 20),
                        const SizedBox(width: 10),
                        Expanded(
                          child: Text(
                            _error!,
                            style: const TextStyle(                              color: Color(0xFFDC2626),
                              fontWeight: FontWeight.w600,
                              fontSize: 13,
                              height: 1.5,
                            ),
                          ),
                        ),
                      ],
                    ),
                  ),
                  const SizedBox(height: 20),
                ],

                // Email
                const Text(
                  'البريد الإلكتروني',
                  style: TextStyle(                    fontSize: 13,
                    fontWeight: FontWeight.w700,
                    color: Color(0xFF0A2540),
                  ),
                ),
                const SizedBox(height: 8),
                Directionality(
                  textDirection: TextDirection.ltr,
                  child: TextFormField(
                    controller: _emailCtrl,
                    keyboardType: TextInputType.emailAddress,
                    autofillHints: const [AutofillHints.email],
                    textInputAction: TextInputAction.next,
                    textAlign: TextAlign.left,
                    style: const TextStyle(
                        fontSize: 14),
                    decoration: InputDecoration(
                      hintText: 'you@company.com',
                      hintStyle: const TextStyle(                          fontSize: 13,
                          color: Color(0xFF94A3B8)),
                      prefixIcon: const Icon(
                          Icons.alternate_email_rounded,
                          size: 20,
                          color: Color(0xFF64748B)),
                      filled: true,
                      fillColor: const Color(0xFFF8FAFC),
                      border: OutlineInputBorder(
                          borderRadius: BorderRadius.circular(12),
                          borderSide: const BorderSide(
                              color: Color(0xFFE2E8F0))),
                      enabledBorder: OutlineInputBorder(
                          borderRadius: BorderRadius.circular(12),
                          borderSide: const BorderSide(
                              color: Color(0xFFE2E8F0))),
                      focusedBorder: OutlineInputBorder(
                          borderRadius: BorderRadius.circular(12),
                          borderSide: const BorderSide(
                              color: Color(0xFF059669), width: 1.5)),
                    ),
                  ),
                ),
                const SizedBox(height: 20),

                // Password
                const Text(
                  'كلمة المرور',
                  style: TextStyle(                    fontSize: 13,
                    fontWeight: FontWeight.w700,
                    color: Color(0xFF0A2540),
                  ),
                ),
                const SizedBox(height: 8),
                TextFormField(
                  controller: _passCtrl,
                  obscureText: !_showPassword,
                  autofillHints: const [AutofillHints.password],
                  textInputAction: TextInputAction.done,
                  onFieldSubmitted: (_) => _submit(),
                  style: const TextStyle(
                      fontSize: 14),
                  decoration: InputDecoration(
                    prefixIcon: const Icon(Icons.lock_outline_rounded,
                        size: 20, color: Color(0xFF64748B)),
                    suffixIcon: IconButton(
                      icon: Icon(
                          _showPassword
                              ? Icons.visibility_off_outlined
                              : Icons.visibility_outlined,
                          size: 20,
                          color: const Color(0xFF64748B)),
                      onPressed: () => setState(
                          () => _showPassword = !_showPassword),
                    ),
                    filled: true,
                    fillColor: const Color(0xFFF8FAFC),
                    border: OutlineInputBorder(
                        borderRadius: BorderRadius.circular(12),
                        borderSide:
                            const BorderSide(color: Color(0xFFE2E8F0))),
                    enabledBorder: OutlineInputBorder(
                        borderRadius: BorderRadius.circular(12),
                        borderSide:
                            const BorderSide(color: Color(0xFFE2E8F0))),
                    focusedBorder: OutlineInputBorder(
                        borderRadius: BorderRadius.circular(12),
                        borderSide: const BorderSide(
                            color: Color(0xFF059669), width: 1.5)),
                  ),
                ),
                const SizedBox(height: 12),

                Align(
                  alignment: Alignment.centerLeft,
                  child: TextButton(
                    onPressed:
                        _submitting ? null : _openForgotPasswordSheet,
                    child: const Text(
                      'نسيت كلمة السر؟',
                      style: TextStyle(                        fontSize: 13,
                        fontWeight: FontWeight.w600,
                        color: Color(0xFF059669),
                      ),
                    ),
                  ),
                ),
                const SizedBox(height: 12),

                // Submit
                ElevatedButton(
                  onPressed: _submitting ? null : _submit,
                  style: ElevatedButton.styleFrom(
                    backgroundColor: const Color(0xFF059669),
                    disabledBackgroundColor: const Color(0xFF94A3B8),
                    minimumSize: const Size.fromHeight(50),
                    shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(12)),
                  ),
                  child: _submitting
                      ? const SizedBox(
                          height: 22,
                          width: 22,
                          child: CircularProgressIndicator(
                            strokeWidth: 2.5,
                            valueColor:
                                AlwaysStoppedAnimation<Color>(Colors.white),
                          ),
                        )
                      : const Text(
                          'تسجيل الدخول',
                          style: TextStyle(                            fontSize: 15,
                            fontWeight: FontWeight.w700,
                            color: Colors.white,
                          ),
                        ),
                ),

                const SizedBox(height: 24),
                const Center(
                  child: Text(
                    'نظام إدارة أعمال متكامل للشركات',
                    style: TextStyle(                      color: Color(0xFF94A3B8),
                      fontSize: 12,
                    ),
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
