// MARSOUD-MOBILE-FLUTTER — persona-aware bottom-nav shell.
//
// The bottom nav is picked per role (mirrors app/templates/base.html:566
// for the employee, 598-694 for the owner/manager). Only the Employee
// nav is wired in this landing; Manager and Sales lanes are Phase 4
// tickets that add more tabs.
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../data/auth_state.dart';

class _Tab {
  final String label;
  final IconData icon;
  final String route;
  const _Tab(this.label, this.icon, this.route);
}

const _employeeTabs = <_Tab>[
  _Tab('حسابي', Icons.person, '/home'),
  _Tab('الحضور', Icons.fingerprint, '/attendance'),
  _Tab('الإشعارات', Icons.notifications, '/notifications'),
];

const _managerTabs = <_Tab>[
  _Tab('حسابي', Icons.person, '/home'),
  _Tab('الحضور', Icons.fingerprint, '/attendance'),
  _Tab('الإشعارات', Icons.notifications, '/notifications'),
  // Manager-only tabs (leave-inbox / employees) come with Phase 3+4.
];

List<_Tab> _tabsFor(String role) {
  const managerRoles = {'owner', 'admin', 'hr_manager', 'ceo'};
  if (managerRoles.contains(role)) return _managerTabs;
  return _employeeTabs;
}

int _indexFor(List<_Tab> tabs, String location) {
  for (var i = 0; i < tabs.length; i++) {
    if (location.startsWith(tabs[i].route)) return i;
  }
  return 0;
}

class HomeShell extends ConsumerWidget {
  final Widget child;
  const HomeShell({super.key, required this.child});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final session = ref.watch(authProvider).value;
    if (session == null) {
      return const Scaffold(body: Center(child: CircularProgressIndicator()));
    }
    final tabs = _tabsFor(session.activeRole);
    final loc = GoRouterState.of(context).matchedLocation;
    final idx = _indexFor(tabs, loc);

    return Scaffold(
      appBar: AppBar(
        title: Text(
          session.activeCompany?.name ?? 'مرصود',
          overflow: TextOverflow.ellipsis,
        ),
        actions: [
          IconButton(
            icon: const Icon(Icons.logout),
            tooltip: 'تسجيل الخروج',
            onPressed: () async {
              // Best-effort revoke, then always clear.
              await ref
                  .read(authProvider.notifier)
                  .clear(); // will bounce to /login.
            },
          ),
        ],
      ),
      body: child,
      bottomNavigationBar: NavigationBar(
        selectedIndex: idx,
        onDestinationSelected: (i) => context.go(tabs[i].route),
        destinations: [
          for (final t in tabs)
            NavigationDestination(
              icon: Icon(t.icon),
              label: t.label,
            ),
        ],
      ),
    );
  }
}
