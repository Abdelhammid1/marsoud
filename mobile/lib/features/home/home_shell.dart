// MARSOUD-MOBILE-FLUTTER — persona-aware shell that matches the web.
//
// Web sidebar collapses to a hamburger drawer on narrow viewports; the
// mobile app just uses the drawer form by default. Top bar mirrors the
// web: brand mark + active-company label + a bell icon (notifications)
// + hamburger. No bottom nav — the web doesn't have one, so we don't
// either; every deep screen is reached from the drawer.
import 'dart:async';   // MARSOUD-MOBILE-LOGOUT-HANG-01 — unawaited + timeout

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../app/theme.dart';
import '../../data/auth_state.dart';
import '../../data/my_account_repository.dart';
import '../../data/push_service.dart';

class _DrawerLink {
  final String label;
  final String emoji;
  final String route;
  const _DrawerLink(this.label, this.emoji, this.route);
}

// Mirrors app/templates/base.html:566-575 — the employee sidebar.
// الحضور and سجل نشاطي used to live here but are both reachable from
// the tab strip inside حسابي (My Account). Keeping duplicate entry
// points was noise — matches the web sidebar convention (base.html:566-575).
const _employeeDrawer = <_DrawerLink>[
  _DrawerLink('حسابي', '👤', '/home'),
  // MARSOUD-MOBILE-TKT-01 (2026-08-18) — three modules added:
  // leads, meetings, schedule. Placed near the top since the
  // ticket lists them as employee-critical.
  _DrawerLink('عملائي المحتملين', '🎯', '/leads'),
  _DrawerLink('اجتماعاتي', '📅', '/meetings'),
  _DrawerLink('جدولي', '🗓', '/schedule'),
  // MARSOUD-MOBILE-TKT-03 (2026-08-18) — طلبات الموظف
  // (leave / permission / advance forms).
  _DrawerLink('طلباتي', '📮', '/requests'),
  // إدارة العمل — matches base.html:658-664
  _DrawerLink('المهام', '✅', '/tasks'),
  _DrawerLink('المشاريع', '📂', '/projects'),
  _DrawerLink('أرشيفي', '🗂', '/archive'),
  // تقارير + عهد + ملفات + دعم
  _DrawerLink('تقاريري اليومية', '📝', '/daily-reports'),
  _DrawerLink('عهدتي النقدية', '💵', '/custody'),
  _DrawerLink('عهدي العينية', '📦', '/items'),
  _DrawerLink('ملفاتي', '📁', '/files'),
  _DrawerLink('الدعم الفني', '🆘', '/support'),
  _DrawerLink('الإشعارات', '🔔', '/notifications'),
];

// MARSOUD-MOBILE-SHIP-READY-01 (L1) — TODO(persona): the README
// documents Manager + Sales lanes but neither is implemented. Once
// the endpoints ship, this becomes a switch on role. Today every
// non-employee role also gets the Employee drawer — cosmetic today
// (all our test users are employees) but ships a wrong menu the
// moment we add a manager.
List<_DrawerLink> _drawerFor(String role) => _employeeDrawer;

class HomeShell extends ConsumerStatefulWidget {
  final Widget child;
  const HomeShell({super.key, required this.child});

  @override
  ConsumerState<HomeShell> createState() => _HomeShellState();
}

class _HomeShellState extends ConsumerState<HomeShell> {
  // MARSOUD-MOBILE-SHIP-READY-01 (M7) — was a module-level `final`
  // GlobalKey shared across every HomeShell instance. If ShellRoute
  // ever kept two shells alive during a transition, both would
  // fight for the same key → "duplicate GlobalKey" crash. Instance
  // scope kills that class of bug at the root.
  final _scaffoldKey = GlobalKey<ScaffoldState>();

  @override
  Widget build(BuildContext context) {
    final session = ref.watch(authProvider).value;
    if (session == null) {
      return const Scaffold(
        body: Center(child: CircularProgressIndicator()),
      );
    }
    // MARSOUD-MOBILE-TKT-05 (2026-08-18) — consume pending
    // deep-link from a push tap. Notifier gets set by
    // PushService when the user opens the app from a push;
    // navigate then clear.
    ref.listen<String?>(pendingDeepLinkProvider,
        (previous, next) {
      if (next != null && next.isNotEmpty) {
        WidgetsBinding.instance.addPostFrameCallback((_) {
          if (context.mounted) {
            context.go(next);
            ref.read(pendingDeepLinkProvider.notifier).state = null;
          }
        });
      }
    });
    return Scaffold(
      key: _scaffoldKey,
      backgroundColor: Colors.transparent,
      drawer: _SideDrawer(
        session: session,
        links: _drawerFor(session.activeRole),
        currentPath: GoRouterState.of(context).matchedLocation,
      ),
      body: ScaffoldGradient(
        child: SafeArea(
          bottom: false,
          child: Column(
            children: [
              _TopBar(
                session: session,
                onMenu: () => _scaffoldKey.currentState?.openDrawer(),
              ),
              Expanded(child: widget.child),
            ],
          ),
        ),
      ),
    );
  }
}

class _TopBar extends ConsumerWidget {
  final AuthSession session;
  final VoidCallback onMenu;
  const _TopBar({required this.session, required this.onMenu});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 8),
      decoration: BoxDecoration(
        color: Colors.white,
        border: Border(
          bottom: BorderSide(color: BrandColors.slate200.withValues(alpha: 0.7)),
        ),
      ),
      child: Row(
        children: [
          // MARSOUD-MOBILE-SHIP-READY-01 (H1) — was menu-only. On
          // detail screens (context.canPop) show a back arrow so
          // iOS users have a visible affordance to return. Menu
          // stays as fallback for the root shell tabs.
          if (Navigator.of(context).canPop())
            IconButton(
              icon: const Icon(Icons.arrow_forward,
                  color: BrandColors.navy900),
              tooltip: 'رجوع',
              onPressed: () => Navigator.of(context).maybePop(),
            )
          else
            IconButton(
              icon: const Icon(Icons.menu, color: BrandColors.navy900),
              tooltip: 'القائمة',
              onPressed: onMenu,
            ),
          Container(
            width: 36,
            height: 36,
            decoration: BoxDecoration(
              color: BrandColors.emerald50,
              borderRadius: BorderRadius.circular(10),
              border: Border.all(color: BrandColors.emerald100),
            ),
            alignment: Alignment.center,
            child: const Text(
              'م',
              style: TextStyle(
                color: BrandColors.emerald700,
                fontWeight: FontWeight.w800,
                fontSize: 17,
              ),
            ),
          ),
          const SizedBox(width: 10),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  session.activeCompany?.name ?? 'مرصود',
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                    color: BrandColors.navy900,
                    fontSize: 14,
                    fontWeight: FontWeight.w700,
                  ),
                ),
                Text(
                  session.user.name,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                    color: BrandColors.slate500,
                    fontSize: 11,
                  ),
                ),
              ],
            ),
          ),
          _NotificationBell(),
        ],
      ),
    );
  }
}

class _NotificationBell extends ConsumerWidget {
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final async = ref.watch(unreadCountProvider);
    final count = async.value ?? 0;
    return Stack(
      clipBehavior: Clip.none,
      children: [
        IconButton(
          onPressed: () => context.go('/notifications'),
          icon: const Icon(Icons.notifications_none,
              color: BrandColors.navy900),
          tooltip: 'الإشعارات',
        ),
        if (count > 0)
          Positioned(
            top: 6,
            left: 6,
            child: Container(
              padding: const EdgeInsets.symmetric(horizontal: 5, vertical: 1),
              decoration: BoxDecoration(
                color: BrandColors.red500,
                borderRadius: BorderRadius.circular(999),
                border: Border.all(color: Colors.white, width: 1.5),
              ),
              constraints: const BoxConstraints(
                minWidth: 18, minHeight: 18),
              alignment: Alignment.center,
              child: Text(
                count > 9 ? '9+' : '$count',
                style: const TextStyle(
                  color: Colors.white,
                  fontSize: 10,
                  fontWeight: FontWeight.w800,
                ),
              ),
            ),
          ),
      ],
    );
  }
}

// MARSOUD-MOBILE-SHIP-READY-01 (M4) — bell polling is `autoDispose`
// but the async* loop kept running as long as ANY widget listened.
// Add an onDispose latch so a cancellation actually breaks the loop
// on the next iteration (Riverpod alone won't propagate a Future
// cancellation into a `Future.delayed`). Result: when the user
// leaves HomeShell (backgrounded / signed out), polling stops on
// the next tick instead of continuing indefinitely.
final unreadCountProvider = StreamProvider.autoDispose<int>((ref) async* {
  final repo = ref.watch(myAccountRepoProvider);
  var cancelled = false;
  ref.onDispose(() { cancelled = true; });
  while (!cancelled) {
    try {
      yield await repo.unreadCount();
    } catch (_) {
      yield 0;
    }
    // Chunked sleep so cancellation is picked up within 5s instead
    // of the full 30s poll interval.
    for (var i = 0; i < 6 && !cancelled; i++) {
      await Future.delayed(const Duration(seconds: 5));
    }
  }
});

class _SideDrawer extends ConsumerWidget {
  final AuthSession session;
  final List<_DrawerLink> links;
  final String currentPath;
  const _SideDrawer({
    required this.session,
    required this.links,
    required this.currentPath,
  });

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return Drawer(
      backgroundColor: Colors.white,
      child: SafeArea(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Container(
              padding: const EdgeInsets.all(20),
              decoration: const BoxDecoration(
                gradient: LinearGradient(
                  begin: Alignment.topRight,
                  end: Alignment.bottomLeft,
                  colors: [BrandColors.navy900, BrandColors.navy700],
                ),
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Container(
                        width: 44,
                        height: 44,
                        decoration: BoxDecoration(
                          color: Colors.white.withValues(alpha: 0.15),
                          borderRadius: BorderRadius.circular(12),
                        ),
                        alignment: Alignment.center,
                        child: const Text(
                          'م',
                          style: TextStyle(
                            color: Colors.white,
                            fontWeight: FontWeight.w800,
                            fontSize: 22,
                          ),
                        ),
                      ),
                      const SizedBox(width: 12),
                      const Text(
                        'مرصود',
                        style: TextStyle(
                          color: Colors.white,
                          fontSize: 20,
                          fontWeight: FontWeight.w800,
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 14),
                  Text(
                    session.user.name,
                    style: const TextStyle(
                      color: Colors.white,
                      fontSize: 13,
                      fontWeight: FontWeight.w700,
                    ),
                    overflow: TextOverflow.ellipsis,
                  ),
                  const SizedBox(height: 2),
                  Text(
                    session.activeCompany?.name ?? '',
                    style: TextStyle(
                      color: Colors.white.withValues(alpha: 0.75),
                      fontSize: 11,
                    ),
                  ),
                ],
              ),
            ),
            const SizedBox(height: 8),
            Expanded(
              child: ListView(
                padding: const EdgeInsets.symmetric(vertical: 4),
                children: [
                  for (final l in links)
                    _DrawerItem(
                      link: l,
                      active: currentPath == l.route,
                      onTap: () {
                        Navigator.of(context).pop();
                        context.go(l.route);
                      },
                    ),
                ],
              ),
            ),
            const Divider(height: 1),
            ListTile(
              leading: const Icon(Icons.logout, color: BrandColors.slate500),
              title: const Text('تسجيل الخروج',
                  style: TextStyle(
                    color: BrandColors.slate700,
                    fontWeight: FontWeight.w600,
                  )),
              onTap: () async {
                Navigator.of(context).pop();
                // MARSOUD-MOBILE-LOGOUT-HANG-01 (2026-09-02) — was
                // `await pushService.onLogout()` before `authProvider
                // .clear()`. That call chains through
                // FirebaseMessaging.instance.getToken(), which on
                // some devices (no Google Play Services, offline,
                // FCM registration hiccup) hangs for 30-60s or
                // longer — from the user's POV "signout doesn't
                // work". Fix: give FCM cleanup 3 seconds max, then
                // clear the local session regardless. The server-
                // side FCM token stays orphaned for at most a day
                // (its next scheduled cleanup) — a tolerable trade
                // for a signout that always feels instant.
                unawaited(
                  ref.read(pushServiceProvider).onLogout()
                      .timeout(const Duration(seconds: 3),
                               onTimeout: () {})
                      .catchError((_) {}),
                );
                await ref.read(authProvider.notifier).clear();
              },
            ),
          ],
        ),
      ),
    );
  }
}

class _DrawerItem extends StatelessWidget {
  final _DrawerLink link;
  final bool active;
  final VoidCallback onTap;
  const _DrawerItem({
    required this.link,
    required this.active,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    // Matches base.html `.nav-link.active` — mint tint + right emerald
    // border + emerald text.
    return Container(
      margin: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
      decoration: BoxDecoration(
        color: active ? BrandColors.emerald50 : Colors.transparent,
        borderRadius: BorderRadius.circular(10),
        border: active
            ? const Border(
                right: BorderSide(
                    color: BrandColors.emerald500, width: 3),
              )
            : null,
      ),
      child: Material(
        color: Colors.transparent,
        borderRadius: BorderRadius.circular(10),
        child: InkWell(
          borderRadius: BorderRadius.circular(10),
          onTap: onTap,
          child: Padding(
            padding: const EdgeInsets.symmetric(
                horizontal: 14, vertical: 12),
            child: Row(
              children: [
                Text(link.emoji, style: const TextStyle(fontSize: 18)),
                const SizedBox(width: 12),
                Expanded(
                  child: Text(
                    link.label,
                    style: TextStyle(
                      color: active
                          ? BrandColors.emerald700
                          : BrandColors.slate700,
                      fontWeight: active
                          ? FontWeight.w800
                          : FontWeight.w600,
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
