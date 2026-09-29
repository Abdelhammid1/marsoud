// Shown while AuthNotifier restores the token from secure storage.
//
// MARSOUD-MOBILE-BIOMETRIC-01 (2026-09-17) — the splash also
// consults SharedPreferences on boot for the biometric_enabled
// flag; if true AND a valid session was restored, we flip
// biometricLockedProvider to true so the router's redirect sends
// the user to /lock BEFORE any authenticated screen paints.
//
// MARSOUD-MOBILE-DESIGN-PREVIEW-01 (2026-09-29) — visual layout
// swapped to the Stitch-generated design (navy deep bg + emerald
// shield mark + Cairo headings + emerald spinner).  Auth-restore +
// biometric-lock plumbing is unchanged: this is a UI-only rewrite.
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../data/auth_state.dart';
import '../../data/biometric_service.dart';


class SplashScreen extends ConsumerStatefulWidget {
  const SplashScreen({super.key});
  @override
  ConsumerState<SplashScreen> createState() => _SplashScreenState();
}

class _SplashScreenState extends ConsumerState<SplashScreen>
    with SingleTickerProviderStateMixin {
  bool _checked = false;
  late AnimationController _controller;
  late Animation<double> _fadeAnimation;

  @override
  void initState() {
    super.initState();
    _controller = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1000),
    );
    _fadeAnimation = CurvedAnimation(parent: _controller, curve: Curves.easeIn);
    _controller.forward();
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
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Directionality(
      textDirection: TextDirection.rtl,
      child: Scaffold(
        // Deep corporate navy — matches the Stitch splash
        backgroundColor: const Color(0xFF0A2540),
        body: Center(
          child: FadeTransition(
            opacity: _fadeAnimation,
            child: Column(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                // MARSOUD-MOBILE-DESIGN-PREVIEW-01 (2026-09-29) —
                // real Marsoud logo asset (assets/images/logo.png)
                // on an emerald plate, not the Stitch shield icon.
                // errorBuilder falls back to the "م" glyph if the
                // asset is ever missing so the mark is still
                // identifiable.
                Container(
                  width: 96,
                  height: 96,
                  decoration: BoxDecoration(
                    color: const Color(0xFF059669),
                    borderRadius: BorderRadius.circular(24),
                    boxShadow: [
                      BoxShadow(
                        color: const Color(0xFF059669).withOpacity(0.35),
                        blurRadius: 24,
                        offset: const Offset(0, 8),
                      ),
                    ],
                  ),
                  alignment: Alignment.center,
                  child: ClipRRect(
                    borderRadius: BorderRadius.circular(18),
                    child: Image.asset(
                      'assets/images/logo.png',
                      width: 68,
                      height: 68,
                      fit: BoxFit.contain,
                      errorBuilder: (_, __, ___) => const Text(
                        'م',
                        style: TextStyle(
                          color: Colors.white,
                          fontSize: 48,
                          fontWeight: FontWeight.w900,
                        ),
                      ),
                    ),
                  ),
                ),
                const SizedBox(height: 24),
                const Text(
                  'مرصود',
                  style: TextStyle(                    fontSize: 32,
                    fontWeight: FontWeight.w900,
                    color: Colors.white,
                    letterSpacing: 0.5,
                  ),
                ),
                const SizedBox(height: 6),
                const Text(
                  'نظام إدارة الأعمال',
                  style: TextStyle(                    fontSize: 14,
                    color: Color(0xFF94A3B8),
                  ),
                ),
                const SizedBox(height: 60),
                const SizedBox(
                  width: 24,
                  height: 24,
                  child: CircularProgressIndicator(
                    strokeWidth: 2.5,
                    valueColor:
                        AlwaysStoppedAnimation<Color>(Color(0xFF059669)),
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
