// MARSOUD-MOBILE-FLUTTER — persona-aware shell that matches the web.
//
// Web sidebar collapses to a hamburger drawer on narrow viewports; the
// mobile app just uses the drawer form by default. Top bar mirrors the
// web: brand mark + active-company label + a bell icon (notifications)
// + hamburger. No bottom nav — the web doesn't have one, so we don't
// either; every deep screen is reached from the drawer.
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

List<_DrawerLink> _drawerFor(String role) => _employeeDrawer;

/// The Scaffold's state key — makes openDrawer reliable from any
/// descendant that doesn't have a `Scaffold.of(ctx)` friendly context.
/// Previously the top bar used `Builder + Scaffold.of(ctx)`, which
/// works but silently no-ops when the resolved context isn't strictly
/// inside the Scaffold subtree — that's what made the drawer look
/// "broken" earlier. A GlobalKey is bulletproof.
final _scaffoldKey = GlobalKey<ScaffoldState>();

class HomeShell extends ConsumerWidget {
  final Widget child;
  const HomeShell({super.key, required this.child});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
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
              _TopBar(session: session),
              Expanded(child: child),
            ],
          ),
        ),
      ),
    );
  }
}

class _TopBar extends ConsumerWidget {
  final AuthSession session;
  const _TopBar({required this.session});

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
          IconButton(
            icon: const Icon(Icons.menu, color: BrandColors.navy900),
            tooltip: 'القائمة',
            onPressed: () => _scaffoldKey.currentState?.openDrawer(),
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

final unreadCountProvider = StreamProvider.autoDispose<int>((ref) async* {
  final repo = ref.watch(myAccountRepoProvider);
  while (true) {
    try {
      yield await repo.unreadCount();
    } catch (_) {
      yield 0;
    }
    await Future.delayed(const Duration(seconds: 30));
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
                // MARSOUD-MOBILE-TKT-05 (2026-08-18) — revoke
                // the FCM token BEFORE clearing the bearer, so
                // the DELETE call still has auth. Best-effort.
                try {
                  await ref.read(pushServiceProvider).onLogout();
                } catch (_) {}
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
