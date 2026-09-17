// MARSOUD-MOBILE-FLUTTER — routing with an auth guard.
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../data/auth_state.dart';
import '../data/biometric_service.dart';
import '../features/activity/activity_screen.dart';
import '../features/archive/archive_screen.dart';
import '../features/attendance/attendance_screen.dart';
import '../features/auth/lock_screen.dart';
import '../features/auth/login_screen.dart';
import '../features/custody/custody_screen.dart';
import '../features/daily_reports/daily_report_detail_screen.dart';
import '../features/daily_reports/daily_reports_screen.dart';
import '../features/dashboard/dashboard_screen.dart';
import '../features/files/files_screen.dart';
import '../features/home/home_shell.dart';
import '../features/items/items_screen.dart';
// MARSOUD-MOBILE-TKT-01 (2026-08-18) — three new modules.
import '../features/leads/lead_detail_screen.dart';
import '../features/leads/leads_screen.dart';
import '../features/meetings/meetings_screen.dart';
import '../features/my_account/my_account_screen.dart';
import '../features/notifications/notifications_screen.dart';
import '../features/projects/project_detail_screen.dart';
import '../features/projects/projects_screen.dart';
import '../features/requests/requests_screen.dart';
import '../features/schedule/schedule_screen.dart';
import '../features/splash/splash_screen.dart';
import '../features/support/support_screen.dart';
import '../features/tasks/task_detail_screen.dart';
import '../features/tasks/task_new_screen.dart';
import '../features/tasks/tasks_screen.dart';

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
        // MARSOUD-MOBILE-DASHBOARD-01 (2026-09-17) — the landing
        // screen after login is now /dashboard (KPI tiles +
        // quick actions), not the old /home (which was My Account
        // and is still reachable from the drawer + dashboard).
        return '/dashboard';
      }
      // MARSOUD-MOBILE-BIOMETRIC-01 (2026-09-17) — if biometric is
      // on for this session AND the user hasn't unlocked it yet
      // this app run → send them to the lock screen. The
      // biometricLockedProvider is set by SplashScreen on boot
      // (from shared_preferences) and cleared by LockScreen on a
      // successful OS-auth prompt.  /lock never redirects to itself,
      // and /login stays reachable so "logout and log in with
      // password" is always an option.
      final locked = ref.read(biometricLockedProvider);
      if (locked && loc != '/lock' && loc != '/login') {
        return '/lock';
      }
      if (!locked && loc == '/lock') {
        return '/dashboard';
      }
      return null;
    },
    routes: [
      GoRoute(path: '/splash', builder: (_, __) => const SplashScreen()),
      GoRoute(path: '/login', builder: (_, __) => const LoginScreen()),
      // MARSOUD-MOBILE-BIOMETRIC-01 (2026-09-17) — biometric gate.
      // Sits OUTSIDE the ShellRoute so it doesn't get the drawer /
      // top-bar chrome — the user is locked out until they auth.
      GoRoute(path: '/lock', builder: (_, __) => const LockScreen()),
      ShellRoute(
        builder: (context, state, child) => HomeShell(child: child),
        routes: [
          // MARSOUD-MOBILE-DASHBOARD-01 (2026-09-17) — new landing.
          // KPI tiles + quick actions + upcoming meetings.  /home
          // stays as My Account so old push deep-links + drawer
          // entries continue to work.
          GoRoute(path: '/dashboard',
              builder: (_, __) => const DashboardScreen()),
          GoRoute(path: '/home',
              builder: (_, __) => const MyAccountScreen()),
          GoRoute(path: '/attendance',
              builder: (_, __) => const AttendanceScreen()),
          GoRoute(path: '/notifications',
              builder: (_, __) => const NotificationsScreen()),
          GoRoute(path: '/daily-reports',
              builder: (_, __) => const DailyReportsScreen()),
          GoRoute(
            path: '/daily-reports/:id',
            builder: (context, state) => DailyReportDetailScreen(
              reportId: int.parse(state.pathParameters['id']!),
            ),
          ),
          GoRoute(path: '/archive',
              builder: (_, __) => const ArchiveScreen()),
          GoRoute(path: '/custody',
              builder: (_, __) => const CustodyScreen()),
          GoRoute(path: '/items',
              builder: (_, __) => const ItemsScreen()),
          GoRoute(path: '/files',
              builder: (_, __) => const FilesScreen()),
          GoRoute(path: '/support',
              builder: (_, __) => const SupportScreen()),
          GoRoute(path: '/activity',
              builder: (_, __) => const ActivityScreen()),
          GoRoute(path: '/tasks',
              builder: (_, __) => const TasksScreen()),
          // MARSOUD-MOBILE-TASK-CREATE-01 (2026-09-17) — /tasks/new
          // route MUST come BEFORE '/tasks/:id' so the "new" literal
          // wins the go_router match; otherwise the ":id" segment
          // would grab "new" and int.parse would throw.
          GoRoute(path: '/tasks/new',
              builder: (_, __) => const TaskNewScreen()),
          GoRoute(
            path: '/tasks/:id',
            builder: (context, state) => TaskDetailScreen(
              taskId: int.parse(state.pathParameters['id']!),
            ),
          ),
          GoRoute(path: '/projects',
              builder: (_, __) => const ProjectsScreen()),
          GoRoute(
            path: '/projects/:id',
            builder: (context, state) => ProjectDetailScreen(
              projectId: int.parse(state.pathParameters['id']!),
            ),
          ),
          // MARSOUD-MOBILE-TKT-01 (2026-08-18)
          GoRoute(path: '/leads',
              builder: (_, __) => const LeadsScreen()),
          GoRoute(
            path: '/leads/:id',
            builder: (context, state) => LeadDetailScreen(
              leadId: int.parse(state.pathParameters['id']!),
            ),
          ),
          GoRoute(path: '/meetings',
              builder: (_, __) => const MeetingsScreen()),
          GoRoute(path: '/schedule',
              builder: (_, __) => const ScheduleScreen()),
          // MARSOUD-MOBILE-TKT-03 (2026-08-18) — طلباتي
          GoRoute(path: '/requests',
              builder: (_, __) => const RequestsScreen()),
        ],
      ),
    ],
  );
});

class _AuthChangeNotifier extends ChangeNotifier {
  _AuthChangeNotifier(Ref ref) {
    ref.listen(authProvider, (_, __) => notifyListeners());
  }
}
