// MARSOUD-MOBILE-FLUTTER — the only unauthenticated screen.
import 'dart:io' show Platform;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../data/api_client.dart';
import '../../data/auth_repository.dart';
import '../../data/auth_state.dart';

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
      // Router redirect will bounce us to /home.
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
      default:
        return e.message;
    }
  }

  String _deviceLabel() {
    try {
      return '${Platform.operatingSystem}-${Platform.operatingSystemVersion}'.substring(0, 40);
    } catch (_) {
      return 'unknown';
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: SafeArea(
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(24),
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 420),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  const SizedBox(height: 24),
                  Text('مرصود',
                      textAlign: TextAlign.center,
                      style: Theme.of(context)
                          .textTheme
                          .displaySmall
                          ?.copyWith(fontWeight: FontWeight.bold)),
                  const SizedBox(height: 8),
                  Text('تسجيل الدخول',
                      textAlign: TextAlign.center,
                      style: Theme.of(context).textTheme.titleMedium),
                  const SizedBox(height: 32),
                  TextField(
                    controller: _emailCtrl,
                    keyboardType: TextInputType.emailAddress,
                    autofillHints: const [AutofillHints.email],
                    textInputAction: TextInputAction.next,
                    decoration:
                        const InputDecoration(labelText: 'البريد الإلكتروني'),
                  ),
                  const SizedBox(height: 16),
                  TextField(
                    controller: _passCtrl,
                    obscureText: !_showPassword,
                    autofillHints: const [AutofillHints.password],
                    textInputAction: TextInputAction.done,
                    onSubmitted: (_) => _submit(),
                    decoration: InputDecoration(
                      labelText: 'كلمة السر',
                      suffixIcon: IconButton(
                        icon: Icon(_showPassword
                            ? Icons.visibility_off
                            : Icons.visibility),
                        onPressed: () => setState(
                            () => _showPassword = !_showPassword),
                      ),
                    ),
                  ),
                  if (_error != null) ...[
                    const SizedBox(height: 16),
                    Container(
                      padding: const EdgeInsets.all(12),
                      decoration: BoxDecoration(
                        color: Colors.red.shade50,
                        border: Border.all(color: Colors.red.shade300),
                        borderRadius: BorderRadius.circular(8),
                      ),
                      child: Text(_error!,
                          style: TextStyle(color: Colors.red.shade900)),
                    ),
                  ],
                  const SizedBox(height: 24),
                  ElevatedButton(
                    onPressed: _submitting ? null : _submit,
                    child: _submitting
                        ? const SizedBox(
                            height: 20,
                            width: 20,
                            child: CircularProgressIndicator(
                              strokeWidth: 2, color: Colors.white,
                            ),
                          )
                        : const Text('دخول'),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}
