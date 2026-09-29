// MARSOUD-MOBILE-BIOMETRIC-01 (2026-09-17) — biometric gate shown
// on cold-start / when the user hasn't unlocked this session yet.
//
// Auto-prompts the OS auth sheet on mount so the user rarely sees
// this screen at all — it's really just the frame the Face ID /
// Fingerprint modal sits inside.  On success → context.go('/home'),
// on failure → the user gets a "حاول تاني" button + a "خروج
// وتسجيل دخول تاني" escape hatch.
//
// MARSOUD-MOBILE-DESIGN-PREVIEW-01 (2026-09-29) — layout swapped
// to the Stitch design (white bg, emerald-tinted fingerprint puck,
// Cairo headings, emerald CTA).  Real biometric + logout plumbing
// preserved.
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../data/auth_state.dart';
import '../../data/biometric_service.dart';


class LockScreen extends ConsumerStatefulWidget {
  const LockScreen({super.key});
  @override
  ConsumerState<LockScreen> createState() => _LockScreenState();
}

class _LockScreenState extends ConsumerState<LockScreen> {
  bool _busy = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) => _tryUnlock());
  }

  Future<void> _tryUnlock() async {
    if (_busy) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    final ok = await ref
        .read(biometricServiceProvider)
        .authenticate(reason: 'افتح تطبيق مرصود');
    if (!mounted) return;
    if (ok) {
      ref.read(biometricLockedProvider.notifier).state = false;
      context.go('/home');
    } else {
      setState(() {
        _busy = false;
        _error = 'تعذّر التحقق. حاول مرة أخرى.';
      });
    }
  }

  Future<void> _logout() async {
    final auth = ref.read(authProvider.notifier);
    final bio = ref.read(biometricServiceProvider);
    bio.clearUnlockLatch();
    ref.read(biometricLockedProvider.notifier).state = false;
    await auth.clear();
    if (mounted) context.go('/login');
  }

  @override
  Widget build(BuildContext context) {
    return Directionality(
      textDirection: TextDirection.rtl,
      child: Scaffold(
        backgroundColor: Colors.white,
        body: SafeArea(
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 24.0, vertical: 30),
            child: Column(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                const Spacer(),
                // Real Marsoud logo on an emerald-soft plate + a
                // fingerprint badge under it — replaces the plain
                // fingerprint circle from the Stitch design so the
                // user sees which app is asking for their biometric.
                Container(
                  width: 100,
                  height: 100,
                  decoration: BoxDecoration(
                    color: const Color(0xFFECFDF5),
                    shape: BoxShape.circle,
                    border: Border.all(
                        color: const Color(0xFF059669).withValues(alpha: 0.2),
                        width: 3),
                  ),
                  alignment: Alignment.center,
                  child: ClipRRect(
                    borderRadius: BorderRadius.circular(999),
                    child: Image.asset(
                      'assets/images/logo.png',
                      width: 56,
                      height: 56,
                      fit: BoxFit.contain,
                      errorBuilder: (_, __, ___) => const Icon(
                        Icons.fingerprint_rounded,
                        size: 54,
                        color: Color(0xFF059669),
                      ),
                    ),
                  ),
                ),
                const SizedBox(height: 28),
                const Text(
                  'مرصود مقفول',
                  style: TextStyle(                    fontSize: 22,
                    fontWeight: FontWeight.w800,
                    color: Color(0xFF0A2540),
                  ),
                ),
                const SizedBox(height: 8),
                const Text(
                  'استخدم بصمة الإصبع أو Face ID للوصول إلى بياناتك بأمان',
                  textAlign: TextAlign.center,
                  style: TextStyle(                    fontSize: 14,
                    color: Color(0xFF64748B),
                    height: 1.5,
                  ),
                ),
                if (_error != null) ...[
                  const SizedBox(height: 16),
                  Container(
                    padding: const EdgeInsets.symmetric(
                        horizontal: 14, vertical: 10),
                    decoration: BoxDecoration(
                      color: const Color(0xFFFEE2E2),
                      borderRadius: BorderRadius.circular(10),
                      border: Border.all(color: const Color(0xFFFECACA)),
                    ),
                    child: Text(
                      _error!,
                      style: const TextStyle(                        color: Color(0xFFDC2626),
                        fontSize: 12.5,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  ),
                ],
                const Spacer(),
                ElevatedButton.icon(
                  onPressed: _busy ? null : _tryUnlock,
                  icon: _busy
                      ? const SizedBox(
                          width: 18,
                          height: 18,
                          child: CircularProgressIndicator(
                            strokeWidth: 2.4,
                            valueColor:
                                AlwaysStoppedAnimation<Color>(Colors.white),
                          ),
                        )
                      : const Icon(Icons.fingerprint_rounded,
                          size: 22, color: Colors.white),
                  label: const Text(
                    'تفعيل المستشعر الحيوي',
                    style: TextStyle(                      fontSize: 15,
                      fontWeight: FontWeight.w700,
                      color: Colors.white,
                    ),
                  ),
                  style: ElevatedButton.styleFrom(
                    backgroundColor: const Color(0xFF059669),
                    disabledBackgroundColor: const Color(0xFF94A3B8),
                    minimumSize: const Size.fromHeight(50),
                    shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(12)),
                  ),
                ),
                const SizedBox(height: 12),
                TextButton(
                  onPressed: _busy ? null : _logout,
                  child: const Text(
                    'تسجيل خروج والدخول بكلمة السر',
                    style: TextStyle(                      fontSize: 14,
                      color: Color(0xFF64748B),
                      fontWeight: FontWeight.w600,
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
