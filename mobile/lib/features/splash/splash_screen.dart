// Shown while AuthNotifier restores the token from secure storage.
//
// MARSOUD-MOBILE-BIOMETRIC-01 (2026-09-17) — the splash also
// consults SharedPreferences on boot for the biometric_enabled
// flag; if true AND a valid session was restored, we flip
// biometricLockedProvider to true so the router's redirect sends
// the user to /lock BEFORE any authenticated screen paints.
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/theme.dart';
import '../../data/auth_state.dart';
import '../../data/biometric_service.dart';


class SplashScreen extends ConsumerStatefulWidget {
  const SplashScreen({super.key});
  @override
  ConsumerState<SplashScreen> createState() => _SplashScreenState();
}

class _SplashScreenState extends ConsumerState<SplashScreen> {
  bool _checked = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) async {
      if (_checked) return;
      _checked = true;
      // Only lock the app when a session actually exists — a
      // logged-out user hits /login, not /lock.  The auth notifier
      // fires _restore() from its own constructor; wait a tick so
      // the state reflects it.
      await Future.delayed(const Duration(milliseconds: 50));
      final session = ref.read(authProvider).value;
      if (session == null) return;
      final bio = ref.read(biometricServiceProvider);
      final enabled = await bio.isEnabled();
      if (enabled && !bio.isUnlocked && mounted) {
        ref.read(biometricLockedProvider.notifier).state = true;
      }
    });
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: ScaffoldGradient(
        child: Center(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Container(
                width: 84,
                height: 84,
                decoration: BoxDecoration(
                  color: BrandColors.emerald50,
                  borderRadius: BorderRadius.circular(24),
                  border: Border.all(
                      color: BrandColors.emerald100, width: 2),
                ),
                alignment: Alignment.center,
                child: const Text(
                  'م',
                  style: TextStyle(
                    color: BrandColors.emerald700,
                    fontSize: 40,
                    fontWeight: FontWeight.w800,
                  ),
                ),
              ),
              const SizedBox(height: 20),
              const CircularProgressIndicator(
                strokeWidth: 3,
                color: BrandColors.emerald600,
              ),
            ],
          ),
        ),
      ),
    );
  }
}
