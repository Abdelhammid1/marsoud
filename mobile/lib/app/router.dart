// MARSOUD-MOBILE-FLUTTER — routing with an auth guard.
//
// Unauthenticated → /login. Authenticated → /home. All screens live
// under /home/... so a single ShellRoute renders the persona-aware
// bottom nav (implemented in features/home/home_shell.dart).
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../data/auth_state.dart';
import '../features/attendance/attendance_screen.dart';
import '../features/auth/login_screen.dart';
import '../features/home/home_shell.dart';
import '../features/my_account/my_account_screen.dart';
import '../features/notifications/notifications_screen.dart';
import '../features/splash/splash_screen.dart';

final routerProvider = Provider<GoRouter>((ref) {
  final auth = ref.watch(authProvider);
  return GoRouter(
    initialLocation: '/splash',
    debugLogDiagnostics: false,
    refreshListenable: _AuthChangeNotifier(ref),
    redirect: (context, state) {
      final loc = state.matchedLocation;
      final session = auth.value;
      final loading = auth.isLoading;
      if (loading) return loc == '/splash' ? null : '/splash';
      final loggedIn = session != null;
      final onAuthPage = loc == '/login';
      if (!loggedIn) return onAuthPage ? null : '/login';
      if (loggedIn && (loc == '/login' || loc == '/splash')) {
        return '/home';
      }
      return null;
    },
    routes: [
      GoRoute(path: '/splash', builder: (_, __) => const SplashScreen()),
      GoRoute(path: '/login', builder: (_, __) => const LoginScreen()),
      ShellRoute(
        builder: (context, state, child) => HomeShell(child: child),
        routes: [
          GoRoute(
            path: '/home',
            builder: (_, __) => const MyAccountScreen(),
          ),
          GoRoute(
            path: '/attendance',
            builder: (_, __) => const AttendanceScreen(),
          ),
          GoRoute(
            path: '/notifications',
            builder: (_, __) => const NotificationsScreen(),
          ),
        ],
      ),
    ],
  );
});

/// Bridge Riverpod → go_router — the router refreshes whenever the
/// auth state changes so the redirect above re-runs.
class _AuthChangeNotifier extends ChangeNotifier {
  _AuthChangeNotifier(Ref ref) {
    ref.listen(authProvider, (_, __) => notifyListeners());
  }
}
