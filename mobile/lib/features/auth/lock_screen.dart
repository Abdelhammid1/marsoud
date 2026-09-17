// MARSOUD-MOBILE-BIOMETRIC-01 (2026-09-17) — biometric gate shown
// on cold-start / when the user hasn't unlocked this session yet.
//
// Auto-prompts the OS auth sheet on mount so the user rarely sees
// this screen at all — it's really just the frame the Face ID /
// Fingerprint modal sits inside.  On success → context.go('/home'),
// on failure → the user gets a "حاول تاني" button + a "خروج
// وتسجيل دخول تاني" escape hatch.
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../app/theme.dart';
import '../../data/auth_state.dart';
import '../../data/biometric_service.dart';
import '../../widgets/gradient_button.dart';


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
    // Auto-launch the OS prompt on first frame so the user doesn't
    // have to tap through the marsoud UI to reach the actual auth
    // affordance.
    WidgetsBinding.instance.addPostFrameCallback((_) => _tryUnlock());
  }

  Future<void> _tryUnlock() async {
    if (_busy) return;
    setState(() { _busy = true; _error = null; });
    final ok = await ref.read(biometricServiceProvider)
        .authenticate(reason: 'افتح تطبيق مرصود');
    if (!mounted) return;
    if (ok) {
      // Clear the router's gate so the redirect lets /home paint.
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
    return Scaffold(
      backgroundColor: BrandColors.slate50,
      body: SafeArea(
        child: Center(
          child: Padding(
            padding: const EdgeInsets.all(28),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                Container(
                  width: 96,
                  height: 96,
                  decoration: BoxDecoration(
                    color: Colors.white,
                    borderRadius: BorderRadius.circular(24),
                    border: Border.all(color: BrandColors.emerald100),
                    boxShadow: [
                      BoxShadow(
                        color: BrandColors.emerald500
                            .withValues(alpha: 0.15),
                        blurRadius: 20,
                        offset: const Offset(0, 6),
                      ),
                    ],
                  ),
                  child: const Icon(Icons.fingerprint,
                      size: 56, color: BrandColors.emerald700),
                ),
                const SizedBox(height: 20),
                const Text(
                  'مرصود مقفول',
                  style: TextStyle(
                    color: BrandColors.navy900,
                    fontSize: 20,
                    fontWeight: FontWeight.w800,
                  ),
                ),
                const SizedBox(height: 6),
                const Text(
                  'استخدم بصمتك أو Face ID للدخول.',
                  style: TextStyle(
                    color: BrandColors.slate500,
                    fontSize: 13,
                  ),
                  textAlign: TextAlign.center,
                ),
                if (_error != null) ...[
                  const SizedBox(height: 16),
                  Container(
                    padding: const EdgeInsets.symmetric(
                        horizontal: 12, vertical: 8),
                    decoration: BoxDecoration(
                      color: const Color(0xFFFEE2E2),
                      borderRadius: BorderRadius.circular(8),
                    ),
                    child: Text(
                      _error!,
                      style: const TextStyle(
                        color: Color(0xFFB91C1C),
                        fontSize: 12,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  ),
                ],
                const SizedBox(height: 24),
                GradientButton(
                  onPressed: _busy ? null : _tryUnlock,
                  loading: _busy,
                  label: 'حاول مرة أخرى',
                ),
                const SizedBox(height: 8),
                TextButton(
                  onPressed: _busy ? null : _logout,
                  child: const Text('تسجيل خروج والدخول بكلمة السر'),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
