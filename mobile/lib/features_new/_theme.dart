import 'package:flutter/material.dart';

/// ============================================================================
/// MARSOUD ERP (مرصود) - DESIGN SYSTEM & THEME
/// ============================================================================
/// Theme specifications:
/// Primary Emerald: #059669
/// Primary Container: #047857
/// Navy Heading: #0A2540
/// Surface Light: #F8FAFC
/// Arabic RTL First with Cairo font
/// ============================================================================

class MarsoudColors {
  // Brand Emeralds
  static const Color primary = Color(0xFF059669);
  static const Color primaryContainer = Color(0xFF047857);
  static const Color primaryLight = Color(0xFFECFDF5);
  static const Color primaryDark = Color(0xFF065F46);

  // Corporate Navy / Dark tones
  static const Color navyHeading = Color(0xFF0A2540);
  static const Color textBody = Color(0xFF334155);
  static const Color textMuted = Color(0xFF64748B);
  static const Color textPlaceholder = Color(0xFF94A3B8);

  // Background & Surfaces
  static const Color background = Color(0xFFF8FAFC);
  static const Color surface = Colors.white;
  static const Color surfaceDim = Color(0xFFF1F5F9);
  static const Color border = Color(0xFFE2E8F0);
  static const Color divider = Color(0xFFEDF2F7);

  // Functional Semantic Accents
  static const Color success = Color(0xFF10B981);
  static const Color successLight = Color(0xFFD1FAE5);
  static const Color warning = Color(0xFFF59E0B);
  static const Color warningLight = Color(0xFFFEF3C7);
  static const Color error = Color(0xFFEF4444);
  static const Color errorLight = Color(0xFFFEE2E2);
  static const Color info = Color(0xFF3B82F6);
  static const Color infoLight = Color(0xFFDBEAFE);
}

class MarsoudTheme {
  static ThemeData get lightTheme {
    return ThemeData(
      useMaterial3: true,      brightness: Brightness.light,
      primaryColor: MarsoudColors.primary,
      scaffoldBackgroundColor: MarsoudColors.background,
      colorScheme: const ColorScheme.light(
        primary: MarsoudColors.primary,
        primaryContainer: MarsoudColors.primaryContainer,
        secondary: MarsoudColors.navyHeading,
        surface: MarsoudColors.surface,
        error: MarsoudColors.error,
        onPrimary: Colors.white,
        onSecondary: Colors.white,
        onSurface: MarsoudColors.navyHeading,
      ),
      appBarTheme: const AppBarTheme(
        backgroundColor: MarsoudColors.surface,
        foregroundColor: MarsoudColors.navyHeading,
        elevation: 0,
        centerTitle: true,
        surfaceTintColor: Colors.transparent,
        titleTextStyle: TextStyle(          fontSize: 18,
          fontWeight: FontWeight.w700,
          color: MarsoudColors.navyHeading,
        ),
      ),
      cardTheme: CardThemeData(
        color: MarsoudColors.surface,
        elevation: 0,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(16),
          side: const BorderSide(color: MarsoudColors.border, width: 1),
        ),
        margin: EdgeInsets.zero,
      ),
      elevatedButtonTheme: ElevatedButtonThemeData(
        style: ElevatedButton.styleFrom(
          backgroundColor: MarsoudColors.primary,
          foregroundColor: Colors.white,
          minimumSize: const Size.fromHeight(48),
          elevation: 0,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(12),
          ),
          textStyle: const TextStyle(            fontSize: 15,
            fontWeight: FontWeight.w700,
          ),
        ),
      ),
    );
  }
}

/// ============================================================================
/// 1) REUSABLE APP SHELL COMPONENTS (Widgets)
/// ============================================================================

/// Marsoud Master Scaffold with optional TopBar, BottomNav, and SideDrawer
class MarsoudShellScaffold extends StatelessWidget {
  final Widget body;
  final String? title;
  final bool showHeader;
  final bool showBottomNav;
  final int currentNavIndex;
  final ValueChanged<int>? onNavTap;
  final bool isRootDashboard;
  final Widget? floatingActionButton;
  final List<Widget>? topBarActions;

  const MarsoudShellScaffold({
    super.key,
    required this.body,
    this.title,
    this.showHeader = true,
    this.showBottomNav = true,
    this.currentNavIndex = 0,
    this.onNavTap,
    this.isRootDashboard = false,
    this.floatingActionButton,
    this.topBarActions,
  });

  @override
  Widget build(BuildContext context) {
    return Directionality(
      textDirection: TextDirection.rtl,
      child: Scaffold(
        drawer: const MarsoudSideDrawer(),
        appBar: showHeader
            ? MarsoudTopAppBar(
                title: title,
                isRootDashboard: isRootDashboard,
                actions: topBarActions,
              )
            : null,
        body: SafeArea(child: body),
        bottomNavigationBar: showBottomNav
            ? MarsoudBottomNavigationBar(
                currentIndex: currentNavIndex,
                onTap: onNavTap ?? (_) {},
              )
            : null,
        floatingActionButton: floatingActionButton,
      ),
    );
  }
}

/// Standardized Marsoud Top App Bar
class MarsoudTopAppBar extends StatelessWidget implements PreferredSizeWidget {
  final String? title;
  final bool isRootDashboard;
  final List<Widget>? actions;

  const MarsoudTopAppBar({
    super.key,
    this.title,
    this.isRootDashboard = false,
    this.actions,
  });

  @override
  Size get preferredSize => const Size.fromHeight(60);

  @override
  Widget build(BuildContext context) {
    final canPop = ModalRoute.of(context)?.canPop ?? false;

    return AppBar(
      leading: isRootDashboard
          ? Builder(
              builder: (ctx) => IconButton(
                icon: const Icon(Icons.menu_rounded, color: MarsoudColors.navyHeading, size: 26),
                onPressed: () => Scaffold.of(ctx).openDrawer(),
              ),
            )
          : canPop
              ? IconButton(
                  icon: const Icon(Icons.arrow_forward_ios_rounded, size: 20),
                  onPressed: () => Navigator.of(context).maybePop(),
                )
              : null,
      title: isRootDashboard
          ? Container(
              padding: const EdgeInsets.all(8),
              decoration: BoxDecoration(
                color: MarsoudColors.primaryLight,
                shape: BoxShape.circle,
              ),
              child: const Icon(Icons.shield_outlined, color: MarsoudColors.primary, size: 24),
            )
          : Text(title ?? ''),
      actions: actions ??
          [
            // Notifications button with badge
            Stack(
              alignment: Alignment.center,
              children: [
                IconButton(
                  icon: const Icon(Icons.notifications_none_rounded, color: MarsoudColors.navyHeading, size: 24),
                  onPressed: () {},
                ),
                Positioned(
                  top: 10,
                  right: 10,
                  child: Container(
                    padding: const EdgeInsets.all(3),
                    decoration: const BoxDecoration(
                      color: MarsoudColors.error,
                      shape: BoxShape.circle,
                    ),
                    constraints: const BoxConstraints(minWidth: 16, minHeight: 16),
                    child: const Text(
                      '3',
                      style: TextStyle(color: Colors.white, fontSize: 10, fontWeight: FontWeight.bold),
                      textAlign: TextAlign.center,
                    ),
                  ),
                ),
              ],
            ),
            const SizedBox(width: 4),
            // User initials avatar (NO realistic photo)
            const Padding(
              padding: EdgeInsets.only(left: 12.0),
              child: MarsoudInitialsAvatar(
                initials: 'س.م',
                backgroundColor: MarsoudColors.primaryLight,
                textColor: MarsoudColors.primaryDark,
                size: 34,
              ),
            ),
          ],
    );
  }
}

/// Standardized Marsoud Bottom Navigation Bar
class MarsoudBottomNavigationBar extends StatelessWidget {
  final int currentIndex;
  final ValueChanged<int> onTap;

  const MarsoudBottomNavigationBar({
    super.key,
    required this.currentIndex,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: const BoxDecoration(
        color: MarsoudColors.surface,
        border: Border(top: BorderSide(color: MarsoudColors.border, width: 1)),
      ),
      child: BottomNavigationBar(
        currentIndex: currentIndex,
        onTap: onTap,
        type: BottomNavigationBarType.fixed,
        backgroundColor: MarsoudColors.surface,
        selectedItemColor: MarsoudColors.primary,
        unselectedItemColor: MarsoudColors.textMuted,
        selectedLabelStyle: const TextStyle(fontWeight: FontWeight.w700, fontSize: 11),
        unselectedLabelStyle: const TextStyle(fontWeight: FontWeight.w500, fontSize: 11),
        elevation: 0,
        items: const [
          BottomNavigationBarItem(icon: Icon(Icons.grid_view_rounded), label: 'الرئيسية'),
          BottomNavigationBarItem(icon: Icon(Icons.check_circle_outline_rounded), label: 'مهامي'),
          BottomNavigationBarItem(icon: Icon(Icons.people_alt_outlined), label: 'عملائي'),
          BottomNavigationBarItem(icon: Icon(Icons.folder_outlined), label: 'ملفاتي'),
          BottomNavigationBarItem(icon: Icon(Icons.menu_rounded), label: 'القائمة'),
        ],
      ),
    );
  }
}

/// Marsoud Clean Corporate Side Drawer
class MarsoudSideDrawer extends StatelessWidget {
  const MarsoudSideDrawer({super.key});

  @override
  Widget build(BuildContext context) {
    return Drawer(
      backgroundColor: MarsoudColors.surface,
      child: SafeArea(
        child: Column(
          children: [
            // Drawer Header with Company info & User profile
            Container(
              padding: const EdgeInsets.all(20),
              decoration: const BoxDecoration(
                border: Border(bottom: BorderSide(color: MarsoudColors.border)),
              ),
              child: Row(
                children: [
                  const MarsoudInitialsAvatar(
                    initials: 'س.م',
                    size: 48,
                    backgroundColor: MarsoudColors.primary,
                    textColor: Colors.white,
                  ),
                  const SizedBox(width: 14),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: const [
                        Text(
                          'سلمان المطيري',
                          style: TextStyle(                            fontWeight: FontWeight.w800,
                            fontSize: 16,
                            color: MarsoudColors.navyHeading,
                          ),
                        ),
                        Text(
                          'مدير العمليات • شركة الأفق',
                          style: TextStyle(                            fontSize: 12,
                            color: MarsoudColors.textMuted,
                          ),
                        ),
                      ],
                    ),
                  ),
                ],
              ),
            ),
            // Menu Items List
            Expanded(
              child: ListView(
                padding: const EdgeInsets.symmetric(vertical: 8),
                children: [
                  _drawerItem(Icons.dashboard_outlined, 'لوحة التحكم', true, () {}),
                  _drawerItem(Icons.folder_shared_outlined, 'المشاريع', false, () {}),
                  _drawerItem(Icons.task_alt_rounded, 'قائمة المهام', false, () {}),
                  _drawerItem(Icons.account_balance_wallet_outlined, 'العهد النقدية والعينية', false, () {}),
                  _drawerItem(Icons.access_time_rounded, 'الحضور والانصراف', false, () {}),
                  _drawerItem(Icons.assignment_outlined, 'التقارير اليومية', false, () {}),
                  _drawerItem(Icons.headset_mic_outlined, 'تذاكر الدعم الفني', false, () {}),
                  _drawerItem(Icons.history_rounded, 'سجل النشاط المالي والإداري', false, () {}),
                  const Divider(color: MarsoudColors.border, height: 24),
                  _drawerItem(Icons.swap_horiz_rounded, 'تبديل المنشأة', false, () {}),
                  _drawerItem(Icons.security_rounded, 'الأمان والقفل الحيوي', false, () {}),
                  _drawerItem(Icons.settings_outlined, 'الإعدادات وحسابي', false, () {}),
                ],
              ),
            ),
            // Logout Footer
            Padding(
              padding: const EdgeInsets.all(16.0),
              child: OutlinedButton.icon(
                onPressed: () {},
                icon: const Icon(Icons.logout_rounded, color: MarsoudColors.error, size: 20),
                label: const Text(
                  'تسجيل الخروج',
                  style: TextStyle(color: MarsoudColors.error, fontWeight: FontWeight.bold),
                ),
                style: OutlinedButton.styleFrom(
                  side: const BorderSide(color: MarsoudColors.errorLight),
                  minimumSize: const Size.fromHeight(44),
                  shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _drawerItem(IconData icon, String title, bool isSelected, VoidCallback onTap) {
    return ListTile(
      leading: Icon(
        icon,
        color: isSelected ? MarsoudColors.primary : MarsoudColors.navyHeading,
        size: 22,
      ),
      title: Text(
        title,
        style: TextStyle(          fontSize: 14,
          fontWeight: isSelected ? FontWeight.w700 : FontWeight.w500,
          color: isSelected ? MarsoudColors.primary : MarsoudColors.navyHeading,
        ),
      ),
      selected: isSelected,
      selectedTileColor: MarsoudColors.primaryLight,
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
      contentPadding: const EdgeInsets.symmetric(horizontal: 20, vertical: 2),
      onTap: onTap,
    );
  }
}

/// Standardized Initials Avatar (Replacing Realistic Photos)
class MarsoudInitialsAvatar extends StatelessWidget {
  final String initials;
  final double size;
  final Color backgroundColor;
  final Color textColor;

  const MarsoudInitialsAvatar({
    super.key,
    required this.initials,
    this.size = 32,
    this.backgroundColor = MarsoudColors.primaryLight,
    this.textColor = MarsoudColors.primaryContainer,
  });

  @override
  Widget build(BuildContext context) {
    return Container(
      width: size,
      height: size,
      alignment: Alignment.center,
      decoration: BoxDecoration(
        color: backgroundColor,
        shape: BoxShape.circle,
        border: Border.all(color: MarsoudColors.border, width: 0.5),
      ),
      child: Text(
        initials,
        style: TextStyle(          fontSize: size * 0.4,
          fontWeight: FontWeight.w700,
          color: textColor,
        ),
      ),
    );
  }
}
