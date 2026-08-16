// MARSOUD-MOBILE-FLUTTER — routing with an auth guard.
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../data/auth_state.dart';
import '../features/activity/activity_screen.dart';
import '../features/archive/archive_screen.dart';
import '../features/attendance/attendance_screen.dart';
import '../features/auth/login_screen.dart';
import '../features/custody/custody_screen.dart';
import '../features/daily_reports/daily_report_detail_screen.dart';
import '../features/daily_reports/daily_reports_screen.dart';
import '../features/files/files_screen.dart';
import '../features/home/home_shell.dart';
import '../features/items/items_screen.dart';
import '../features/my_account/my_account_screen.dart';
import '../features/notifications/notifications_screen.dart';
import '../features/projects/project_detail_screen.dart';
import '../features/projects/projects_screen.dart';
import '../features/splash/splash_screen.dart';
import '../features/support/support_screen.dart';
import '../features/tasks/task_detail_screen.dart';
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
