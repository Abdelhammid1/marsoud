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
//
// MARSOUD-MOBILE-SHELL-POLISH-01 (2026-09-17) — sidebar reorganized
// into three top-level buckets to reduce scroll + let the user find
// things by category, not by scanning a 14-item flat list:
//
//   حسابي bucket:      طلباتي, تقاريري اليومية, عهدتي, عهدي, ملفاتي
//   المهام bucket:     أرشيفي (subtasks under Tasks)
//   Everything else:  top-level (Leads, Projects, Meetings, …)
//
// Rendered as ExpansionTile in the drawer so buckets can collapse.
// The `children` field is empty for a plain link.
class _DrawerSection {
  final String label;
  final String emoji;
  final String? route;             // top-level link (children empty)
  final List<_DrawerLink> children; // nested expander (route null)
  const _DrawerSection({
    required this.label,
    required this.emoji,
    this.route,
    this.children = const [],
  });
  bool get isExpander => route == null;
}

const _employeeDrawer = <_DrawerSection>[
  // MARSOUD-MOBILE-DASHBOARD-01 (2026-09-17) — dashboard sits at
  // the top of the drawer too so users who prefer the drawer can
  // still get there in one tap.  Duplicates the bottom-nav
  // destination on purpose — the two navs coexist.
  _DrawerSection(label: 'الرئيسية', emoji: '🏠', route: '/dashboard'),
  _DrawerSection(
    label: 'حسابي', emoji: '👤',
    children: [
      _DrawerLink('نظرة عامة', '👤', '/home'),
      _DrawerLink('طلباتي', '📮', '/requests'),
      _DrawerLink('تقاريري اليومية', '📝', '/daily-reports'),
      _DrawerLink('عهدتي النقدية', '💵', '/custody'),
      _DrawerLink('عهدي العينية', '📦', '/items'),
      _DrawerLink('ملفاتي', '📁', '/files'),
    ],
  ),
  _DrawerSection(label: 'عملائي المحتملين', emoji: '🎯', route: '/leads'),
  _DrawerSection(label: 'اجتماعاتي', emoji: '📅', route: '/meetings'),
  _DrawerSection(label: 'جدولي', emoji: '🗓', route: '/schedule'),
  _DrawerSection(
    label: 'المهام', emoji: '✅',
    children: [
      _DrawerLink('كل المهام', '✅', '/tasks'),
      _DrawerLink('أرشيفي', '🗂', '/archive'),
    ],
  ),
  _DrawerSection(label: 'المشاريع', emoji: '📂', route: '/projects'),
  _DrawerSection(label: 'الإشعارات', emoji: '🔔', route: '/notifications'),
  _DrawerSection(label: 'الدعم الفني', emoji: '🆘', route: '/support'),
];

// MARSOUD-MOBILE-SHIP-READY-01 (L1) — TODO(persona): the README
// documents Manager + Sales lanes but neither is implemented. Once
// the endpoints ship, this becomes a switch on role. Today every
// non-employee role also gets the Employee drawer — cosmetic today
// (all our test users are employees) but ships a wrong menu the
// moment we add a manager.
List<_DrawerSection> _drawerFor(String role) => _employeeDrawer;

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
    final currentPath = GoRouterState.of(context).matchedLocation;
    return Scaffold(
      key: _scaffoldKey,
      backgroundColor: Colors.transparent,
      drawer: _SideDrawer(
        session: session,
        links: _drawerFor(session.activeRole),
        currentPath: currentPath,
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
      // MARSOUD-MOBILE-DASHBOARD-01 (2026-09-17) — bottom nav gives
      // users a one-tap path to the four screens they open every
      // day: Dashboard, Tasks, Leads, and the full menu.  Drawer
      // is still the source of truth for the long-tail (projects,
      // meetings, files, requests, notifications, support) — the
      // bottom nav is deliberately narrow, not a duplicate of the
      // drawer.
      bottomNavigationBar: _BottomNav(
        currentPath: currentPath,
        onOpenMenu: () => _scaffoldKey.currentState?.openDrawer(),
      ),
    );
  }
}

/// MARSOUD-MOBILE-DASHBOARD-01 (2026-09-17) — persistent bottom nav
/// pinned to four destinations that users hit the most.  The fourth
/// tab is a "menu" opener rather than a page — it slides the drawer
/// out so the long-tail nav stays a single tap away without
/// duplicating a dozen icons in the bar.
class _BottomNav extends StatelessWidget {
  final String currentPath;
  final VoidCallback onOpenMenu;
  const _BottomNav({required this.currentPath, required this.onOpenMenu});

  int _indexFor(String path) {
    if (path == '/dashboard') return 0;
    // A path like '/tasks/new' or '/tasks/123' still counts as the
    // tasks tab — the user is inside the tasks stack.
    if (path == '/tasks' || path.startsWith('/tasks/')) return 1;
    if (path == '/leads' || path.startsWith('/leads/')) return 2;
    return -1;   // no tab highlighted (e.g. /notifications, /files)
  }

  @override
  Widget build(BuildContext context) {
    final idx = _indexFor(currentPath);
    return NavigationBar(
      selectedIndex: idx < 0 ? 0 : idx,
      // idx == -1 → nothing lit; keep index 0 for the Widget but
      // dim the tint so the user isn't misled.  A tap always
      // navigates regardless.
      backgroundColor: Colors.white,
      indicatorColor:
          idx < 0 ? Colors.transparent : BrandColors.emerald50,
      surfaceTintColor: Colors.white,
      height: 60,
      labelBehavior: NavigationDestinationLabelBehavior.alwaysShow,
      destinations: const [
        NavigationDestination(
          icon: Icon(Icons.dashboard_outlined),
          selectedIcon:
              Icon(Icons.dashboard, color: BrandColors.emerald700),
          label: 'الرئيسية',
        ),
        NavigationDestination(
          icon: Icon(Icons.check_circle_outline),
          selectedIcon:
              Icon(Icons.check_circle, color: BrandColors.emerald700),
          label: 'مهامي',
        ),
        NavigationDestination(
          icon: Icon(Icons.track_changes_outlined),
          selectedIcon:
              Icon(Icons.track_changes, color: BrandColors.emerald700),
          label: 'عملائي',
        ),
        NavigationDestination(
          icon: Icon(Icons.menu),
          label: 'القائمة',
        ),
      ],
      onDestinationSelected: (i) {
        switch (i) {
          case 0:
            context.go('/dashboard');
            break;
          case 1:
            context.go('/tasks');
            break;
          case 2:
            context.go('/leads');
            break;
          case 3:
            onOpenMenu();
            break;
        }
      },
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
          // iOS users have a visible affordance to return.
          //
          // MARSOUD-MOBILE-SHELL-POLISH-01 (2026-09-17) — the old
          // check used `Navigator.of(context).canPop()`, which
          // returns FALSE inside a ShellRoute even when go_router
          // has a real stack (leads → lead detail, tasks → task
          // detail, projects → project detail).  That's why users
          // saw the hamburger on every screen and no way back.
          //
          // Switched to `GoRouter.of(context).canPop()` which
          // consults the actual go_router stack, so every
          // `context.push('/leads/123')` now gets a working back
          // arrow.  Icon is Icons.arrow_back (Flutter mirrors
          // it to arrow_forward automatically in RTL layouts, so
          // the RTL user sees an arrow that points right → the
          // direction their eye reads).
          Builder(builder: (ctx) {
            final canPop = GoRouter.of(ctx).canPop();
            return IconButton(
              icon: Icon(
                canPop ? Icons.arrow_back : Icons.menu,
                color: BrandColors.navy900,
              ),
              tooltip: canPop ? 'رجوع' : 'القائمة',
              onPressed: canPop
                  ? () => GoRouter.of(ctx).pop()
                  : onMenu,
            );
          }),
          // MARSOUD-MOBILE-COMPANY-LOGO-01 (2026-09-17) — top-bar
          // prefers the tenant's own logo when set (from
          // company.logo_url or company.logo_path via SITE_URL).
          // Falls back to the Marsoud brand mark if the tenant
          // hasn't uploaded one, and to the "م" glyph if that
          // asset itself ever fails to load.
          Container(
            width: 36,
            height: 36,
            decoration: BoxDecoration(
              color: BrandColors.emerald50,
              borderRadius: BorderRadius.circular(10),
              border: Border.all(color: BrandColors.emerald100),
            ),
            alignment: Alignment.center,
            child: ClipRRect(
              borderRadius: BorderRadius.circular(7),
              child: _CompanyLogo(logoUrl: session.activeCompany?.logoUrl),
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

/// MARSOUD-MOBILE-COMPANY-LOGO-01 (2026-09-17) — 28x28 tenant logo
/// with a two-level fallback. If [logoUrl] is set, tries to load it
/// over the network; on failure OR when null, shows the Marsoud
/// asset; if THAT fails, shows the "م" glyph.  Kept as its own
/// widget so the fallback chain reads top-to-bottom.
class _CompanyLogo extends StatelessWidget {
  final String? logoUrl;
  const _CompanyLogo({required this.logoUrl});

  Widget _marsoudFallback() => Image.asset(
        'assets/images/logo.png',
        width: 28,
        height: 28,
        fit: BoxFit.contain,
        errorBuilder: (_, __, ___) => const Text(
          'م',
          style: TextStyle(
            color: BrandColors.emerald700,
            fontWeight: FontWeight.w800,
            fontSize: 17,
          ),
        ),
      );

  @override
  Widget build(BuildContext context) {
    final url = logoUrl;
    if (url == null || url.isEmpty) return _marsoudFallback();
    return Image.network(
      url,
      width: 28,
      height: 28,
      fit: BoxFit.contain,
      errorBuilder: (_, __, ___) => _marsoudFallback(),
      loadingBuilder: (context, child, progress) {
        if (progress == null) return child;
        return _marsoudFallback();
      },
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
// MARSOUD-MOBILE-COMPANY-SWITCHER-01 (2026-09-03) — bottom sheet
// listing every company on the session; tap → switchCompany +
// close the drawer + navigate to /home so the user lands somewhere
// safe after the tenant flips.
Future<void> _pickCompany(
    BuildContext context, WidgetRef ref, AuthSession session) async {
  await showModalBottomSheet<void>(
    context: context,
    backgroundColor: Colors.white,
    shape: const RoundedRectangleBorder(
      borderRadius: BorderRadius.vertical(top: Radius.circular(20)),
    ),
    builder: (ctx) => SafeArea(
      child: Padding(
        padding: const EdgeInsets.symmetric(vertical: 8),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Padding(
              padding: EdgeInsets.fromLTRB(20, 12, 20, 4),
              child: Row(
                children: [
                  Icon(Icons.business, color: BrandColors.navy900),
                  SizedBox(width: 10),
                  Text('اختر الشركة',
                      style: TextStyle(
                        color: BrandColors.navy900,
                        fontWeight: FontWeight.w800,
                        fontSize: 16,
                      )),
                ],
              ),
            ),
            const Divider(height: 1),
            for (final c in session.companies)
              ListTile(
                leading: Icon(
                  c.id == session.activeCompanyId
                      ? Icons.check_circle
                      : Icons.circle_outlined,
                  color: c.id == session.activeCompanyId
                      ? BrandColors.emerald600
                      : BrandColors.slate400,
                ),
                title: Text(c.name,
                    style: const TextStyle(
                      color: BrandColors.navy900,
                      fontWeight: FontWeight.w700,
                    )),
                subtitle: Text(c.role,
                    style: const TextStyle(
                      color: BrandColors.slate500, fontSize: 11,
                    )),
                onTap: () async {
                  Navigator.of(ctx).pop();
                  if (c.id == session.activeCompanyId) return;
                  await ref.read(authProvider.notifier)
                      .switchCompany(c.id);
                  // Close the parent drawer + reset to home so any
                  // company-scoped list refetches cleanly.
                  if (context.mounted) {
                    Navigator.of(context).maybePop();
                    context.go('/home');
                  }
                },
              ),
          ],
        ),
      ),
    ),
  );
}


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
  final List<_DrawerSection> links;
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
            // MARSOUD-MOBILE-SHELL-POLISH-01 (2026-09-17) — was a
            // dark navy gradient (navy900 → navy700). Feedback:
            // "خلي الباك جراوند اللي تحتها فاتحة". Switched to a
            // soft emerald-tinted white so the Marsoud logo reads
            // and the section separator with the list below feels
            // like one canvas, not a hard cut. Text colours
            // adjusted to navy900 for contrast on the light bg.
            Container(
              padding: const EdgeInsets.all(20),
              decoration: BoxDecoration(
                gradient: LinearGradient(
                  begin: Alignment.topRight,
                  end: Alignment.bottomLeft,
                  colors: [
                    BrandColors.emerald50,
                    Colors.white,
                  ],
                ),
                border: Border(
                  bottom: BorderSide(
                    color: BrandColors.slate200.withValues(alpha: 0.6),
                  ),
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
                          color: Colors.white,
                          borderRadius: BorderRadius.circular(12),
                          border: Border.all(
                              color: BrandColors.emerald100),
                          boxShadow: [
                            BoxShadow(
                              color: BrandColors.emerald500
                                  .withValues(alpha: 0.15),
                              blurRadius: 8,
                              offset: const Offset(0, 2),
                            ),
                          ],
                        ),
                        alignment: Alignment.center,
                        child: ClipRRect(
                          borderRadius: BorderRadius.circular(8),
                          child: Image.asset(
                            'assets/images/logo.png',
                            width: 34,
                            height: 34,
                            fit: BoxFit.contain,
                            errorBuilder: (_, __, ___) => const Text(
                              'م',
                              style: TextStyle(
                                color: BrandColors.emerald700,
                                fontWeight: FontWeight.w800,
                                fontSize: 22,
                              ),
                            ),
                          ),
                        ),
                      ),
                      const SizedBox(width: 12),
                      const Text(
                        'مرصود',
                        style: TextStyle(
                          color: BrandColors.navy900,
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
                      color: BrandColors.navy900,
                      fontSize: 13,
                      fontWeight: FontWeight.w700,
                    ),
                    overflow: TextOverflow.ellipsis,
                  ),
                  const SizedBox(height: 2),
                  // MARSOUD-MOBILE-COMPANY-SWITCHER-01 (2026-09-03)
                  // — was a static Text. If the user is on more
                  // than one company, tap opens a picker sheet.
                  Builder(builder: (ctx) {
                    final label = session.activeCompany?.name ?? '';
                    if (session.companies.length <= 1) {
                      return Text(label,
                          style: const TextStyle(
                            color: BrandColors.slate500,
                            fontSize: 11,
                          ));
                    }
                    return InkWell(
                      onTap: () => _pickCompany(ctx, ref, session),
                      borderRadius: BorderRadius.circular(6),
                      child: Padding(
                        padding: const EdgeInsets.symmetric(
                            horizontal: 6, vertical: 4),
                        child: Row(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            Flexible(
                              child: Text(
                                label,
                                style: const TextStyle(
                                  color: BrandColors.slate700,
                                  fontSize: 11,
                                  fontWeight: FontWeight.w600,
                                ),
                                overflow: TextOverflow.ellipsis,
                              ),
                            ),
                            const SizedBox(width: 4),
                            const Icon(Icons.swap_horiz,
                                size: 14,
                                color: BrandColors.slate500),
                          ],
                        ),
                      ),
                    );
                  }),
                ],
              ),
            ),
            const SizedBox(height: 8),
            Expanded(
              child: ListView(
                padding: const EdgeInsets.symmetric(vertical: 4),
                children: [
                  for (final s in links)
                    if (s.isExpander)
                      _DrawerExpander(
                        section: s,
                        currentPath: currentPath,
                        onLinkTap: (route) {
                          Navigator.of(context).pop();
                          context.go(route);
                        },
                      )
                    else
                      _DrawerItem(
                        link: _DrawerLink(s.label, s.emoji, s.route!),
                        active: currentPath == s.route,
                        onTap: () {
                          Navigator.of(context).pop();
                          context.go(s.route!);
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
  // MARSOUD-MOBILE-SHELL-POLISH-01 (2026-09-17) — the same widget
  // renders both top-level shell links and the nested children
  // inside an expander section (طلباتي under حسابي, أرشيفي under
  // المهام).  `dense: true` shrinks the vertical padding so nested
  // children look like siblings, not a duplicate top-level list.
  final bool dense;
  const _DrawerItem({
    required this.link,
    required this.active,
    required this.onTap,
    this.dense = false,
  });

  @override
  Widget build(BuildContext context) {
    // Matches base.html `.nav-link.active` — mint tint + right emerald
    // border + emerald text.
    return Container(
      margin: EdgeInsets.symmetric(
          horizontal: dense ? 4 : 8, vertical: 2),
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
            padding: EdgeInsets.symmetric(
                horizontal: dense ? 12 : 14,
                vertical: dense ? 8 : 12),
            child: Row(
              children: [
                Text(link.emoji,
                    style: TextStyle(fontSize: dense ? 15 : 18)),
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
                      fontSize: dense ? 13 : 14,
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


// MARSOUD-MOBILE-SHELL-POLISH-01 (2026-09-17) — a collapsible
// section in the drawer. Used for حسابي (with طلباتي / تقاريري /
// عهدتي / …) and المهام (with أرشيفي). Auto-expands when the
// current path matches any of the section's children so the user
// doesn't lose their spot after navigating.
class _DrawerExpander extends StatelessWidget {
  final _DrawerSection section;
  final String currentPath;
  final ValueChanged<String> onLinkTap;
  const _DrawerExpander({
    required this.section,
    required this.currentPath,
    required this.onLinkTap,
  });

  @override
  Widget build(BuildContext context) {
    final childRoutes = section.children.map((l) => l.route).toSet();
    final isChildActive = childRoutes.contains(currentPath);
    return Theme(
      // ExpansionTile paints a divider under the header when
      // expanded; kill it so the whole drawer reads as one canvas.
      data: Theme.of(context).copyWith(
        dividerColor: Colors.transparent,
      ),
      child: ExpansionTile(
        initiallyExpanded: isChildActive,
        tilePadding: const EdgeInsets.symmetric(
            horizontal: 14, vertical: 2),
        childrenPadding: const EdgeInsets.only(
            right: 12, left: 8, bottom: 4),
        leading: Text(section.emoji,
            style: const TextStyle(fontSize: 18)),
        title: Text(
          section.label,
          style: TextStyle(
            color: isChildActive
                ? BrandColors.emerald700
                : BrandColors.slate700,
            fontWeight: isChildActive
                ? FontWeight.w800
                : FontWeight.w700,
          ),
        ),
        iconColor: BrandColors.slate500,
        collapsedIconColor: BrandColors.slate500,
        children: [
          for (final child in section.children)
            _DrawerItem(
              link: child,
              active: currentPath == child.route,
              onTap: () => onLinkTap(child.route),
              dense: true,
            ),
        ],
      ),
    );
  }
}
