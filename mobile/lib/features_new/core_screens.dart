import 'package:flutter/material.dart';

/// ============================================================================
/// MARSOUD ERP - CORE SCREENS (Flutter Dart Widgets)
/// ============================================================================
/// Flow: Splash -> Login -> Biometric Lock -> Dashboard -> Notifications
/// ============================================================================

// -----------------------------------------------------------------------------
// 1. SPLASH SCREEN (شاشة البداية)
// -----------------------------------------------------------------------------
class MarsoudSplashScreen extends StatefulWidget {
  const MarsoudSplashScreen({super.key});

  @override
  State<MarsoudSplashScreen> createState() => _MarsoudSplashScreenState();
}

class _MarsoudSplashScreenState extends State<MarsoudSplashScreen> with SingleTickerProviderStateMixin {
  late AnimationController _controller;
  late Animation<double> _fadeAnimation;

  @override
  void initState() {
    super.initState();
    _controller = AnimationController(vsync: this, duration: const Duration(milliseconds: 1200));
    _fadeAnimation = CurvedAnimation(parent: _controller, curve: Curves.easeIn);
    _controller.forward();
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
        backgroundColor: const Color(0xFF0A2540), // Deep corporate navy
        body: Center(
          child: FadeTransition(
            opacity: _fadeAnimation,
            child: Column(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                Container(
                  width: 96,
                  height: 96,
                  decoration: BoxDecoration(
                    color: const Color(0xFF059669), // Emerald Brand
                    borderRadius: BorderRadius.circular(24),
                    boxShadow: [
                      BoxShadow(
                        color: const Color(0xFF059669).withOpacity(0.35),
                        blurRadius: 24,
                        offset: const Offset(0, 8),
                      ),
                    ],
                  ),
                  child: const Center(
                    child: Icon(Icons.shield_rounded, size: 54, color: Colors.white),
                  ),
                ),
                const SizedBox(height: 24),
                const Text(
                  'مرصود ERP',
                  style: TextStyle(                    fontSize: 32,
                    fontWeight: FontWeight.w900,
                    color: Colors.white,
                    letterSpacing: 0.5,
                  ),
                ),
                const SizedBox(height: 8),
                const Text(
                  'المنظومة الإدارية المتكاملة والذكية',
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
                    valueColor: AlwaysStoppedAnimation<Color>(Color(0xFF059669)),
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

// -----------------------------------------------------------------------------
// 2. LOGIN SCREEN (تسجيل الدخول)
// -----------------------------------------------------------------------------
class MarsoudLoginScreen extends StatefulWidget {
  const MarsoudLoginScreen({super.key});

  @override
  State<MarsoudLoginScreen> createState() => _MarsoudLoginScreenState();
}

class _MarsoudLoginScreenState extends State<MarsoudLoginScreen> {
  final _emailController = TextEditingController(text: 'salman@alofooq.sa');
  final _passwordController = TextEditingController(text: '••••••••••••');
  bool _obscurePassword = true;
  bool _rememberMe = true;

  @override
  Widget build(BuildContext context) {
    return Directionality(
      textDirection: TextDirection.rtl,
      child: Scaffold(
        backgroundColor: Colors.white,
        body: SafeArea(
          child: SingleChildScrollView(
            padding: const EdgeInsets.symmetric(horizontal: 24.0, vertical: 20),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                const SizedBox(height: 20),
                // Logo & Header
                Align(
                  alignment: Alignment.centerRight,
                  child: Container(
                    width: 52,
                    height: 52,
                    decoration: BoxDecoration(
                      color: const Color(0xFFECFDF5),
                      borderRadius: BorderRadius.circular(14),
                    ),
                    child: const Icon(Icons.shield_rounded, color: Color(0xFF059669), size: 30),
                  ),
                ),
                const SizedBox(height: 24),
                const Text(
                  'تسجيل الدخول',
                  style: TextStyle(                    fontSize: 26,
                    fontWeight: FontWeight.w800,
                    color: Color(0xFF0A2540),
                  ),
                ),
                const SizedBox(height: 6),
                const Text(
                  'مرحباً بك مجدداً، أدخل بياناتك المؤسسية للمتابعة',
                  style: TextStyle(                    fontSize: 14,
                    color: Color(0xFF64748B),
                  ),
                ),
                const SizedBox(height: 32),

                // Form Fields
                const Text('البريد الإلكتروني المؤسسي',
                    style: TextStyle(fontSize: 13, fontWeight: FontWeight.w700, color: Color(0xFF0A2540))),
                const SizedBox(height: 8),
                TextFormField(
                  controller: _emailController,
                  keyboardType: TextInputType.emailAddress,
                  style: const TextStyle(fontSize: 14),
                  decoration: InputDecoration(
                    prefixIcon: const Icon(Icons.alternate_email_rounded, size: 20, color: Color(0xFF64748B)),
                    filled: true,
                    fillColor: const Color(0xFFF8FAFC),
                    border: OutlineInputBorder(borderRadius: BorderRadius.circular(12), borderSide: const BorderSide(color: Color(0xFFE2E8F0))),
                    enabledBorder: OutlineInputBorder(borderRadius: BorderRadius.circular(12), borderSide: const BorderSide(color: Color(0xFFE2E8F0))),
                  ),
                ),
                const SizedBox(height: 20),

                const Text('كلمة المرور',
                    style: TextStyle(fontSize: 13, fontWeight: FontWeight.w700, color: Color(0xFF0A2540))),
                const SizedBox(height: 8),
                TextFormField(
                  controller: _passwordController,
                  obscureText: _obscurePassword,
                  style: const TextStyle(fontSize: 14),
                  decoration: InputDecoration(
                    prefixIcon: const Icon(Icons.lock_outline_rounded, size: 20, color: Color(0xFF64748B)),
                    suffixIcon: IconButton(
                      icon: Icon(_obscurePassword ? Icons.visibility_off_outlined : Icons.visibility_outlined, size: 20, color: const Color(0xFF64748B)),
                      onPressed: () => setState(() => _obscurePassword = !_obscurePassword),
                    ),
                    filled: true,
                    fillColor: const Color(0xFFF8FAFC),
                    border: OutlineInputBorder(borderRadius: BorderRadius.circular(12), borderSide: const BorderSide(color: Color(0xFFE2E8F0))),
                    enabledBorder: OutlineInputBorder(borderRadius: BorderRadius.circular(12), borderSide: const BorderSide(color: Color(0xFFE2E8F0))),
                  ),
                ),
                const SizedBox(height: 12),

                // Remember Me + Forgot Password
                Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: [
                    Row(
                      children: [
                        Checkbox(
                          value: _rememberMe,
                          activeColor: const Color(0xFF059669),
                          onChanged: (v) => setState(() => _rememberMe = v ?? false),
                        ),
                        const Text('تذكر جهازي', style: TextStyle(fontSize: 13, color: Color(0xFF334155))),
                      ],
                    ),
                    TextButton(
                      onPressed: () {},
                      child: const Text('نسيت كلمة المرور؟', style: TextStyle(fontSize: 13, fontWeight: FontWeight.w600, color: Color(0xFF059669))),
                    ),
                  ],
                ),
                const SizedBox(height: 24),

                // Submit Button
                ElevatedButton(
                  onPressed: () {},
                  style: ElevatedButton.styleFrom(
                    backgroundColor: const Color(0xFF059669),
                    minimumSize: const Size.fromHeight(50),
                    shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
                  ),
                  child: const Text('تسجيل الدخول للمنظومة', style: TextStyle(fontSize: 15, fontWeight: FontWeight.w700, color: Colors.white)),
                ),
                const SizedBox(height: 20),

                // SSO / Nafath Option
                OutlinedButton.icon(
                  onPressed: () {},
                  icon: const Icon(Icons.fingerprint_rounded, color: Color(0xFF0A2540)),
                  label: const Text('الدخول عبر النفاذ الوطني الموحد', style: TextStyle(fontSize: 14, fontWeight: FontWeight.w700, color: Color(0xFF0A2540))),
                  style: OutlinedButton.styleFrom(
                    minimumSize: const Size.fromHeight(50),
                    side: const BorderSide(color: Color(0xFFE2E8F0)),
                    shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
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

// -----------------------------------------------------------------------------
// 3. BIOMETRIC LOCK SCREEN (القفل الحيوي)
// -----------------------------------------------------------------------------
class MarsoudBiometricScreen extends StatelessWidget {
  const MarsoudBiometricScreen({super.key});

  @override
  Widget build(BuildContext context) {
    return Directionality(
      textDirection: TextDirection.rtl,
      child: Scaffold(
        backgroundColor: Colors.white,
        body: SafeArea(
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 24.0, vertical: 30),
            child: Column(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                const Spacer(),
                Container(
                  width: 90,
                  height: 90,
                  decoration: BoxDecoration(
                    color: const Color(0xFFECFDF5),
                    shape: BoxShape.circle,
                    border: Border.all(color: const Color(0xFF059669).withOpacity(0.2), width: 3),
                  ),
                  child: const Icon(Icons.fingerprint_rounded, size: 54, color: Color(0xFF059669)),
                ),
                const SizedBox(height: 28),
                const Text(
                  'تأكيد الهوية الحيوية',
                  style: TextStyle(fontSize: 22, fontWeight: FontWeight.w800, color: Color(0xFF0A2540)),
                ),
                const SizedBox(height: 8),
                const Text(
                  'استخدم بصمة الإصبع أو Face ID للوصول إلى بياناتك المؤسسية بأمان',
                  textAlign: TextAlign.center,
                  style: TextStyle(fontSize: 14, color: Color(0xFF64748B), height: 1.5),
                ),
                const Spacer(),
                ElevatedButton(
                  onPressed: () {},
                  style: ElevatedButton.styleFrom(
                    backgroundColor: const Color(0xFF059669),
                    minimumSize: const Size.fromHeight(50),
                    shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
                  ),
                  child: const Text('تفعيل المستشعر الحيوي', style: TextStyle(fontSize: 15, fontWeight: FontWeight.w700, color: Colors.white)),
                ),
                const SizedBox(height: 12),
                TextButton(
                  onPressed: () {},
                  child: const Text('استخدام رمز المرور المؤسسي (PIN)', style: TextStyle(fontSize: 14, color: Color(0xFF64748B))),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

// -----------------------------------------------------------------------------
// 4. NOTIFICATIONS SCREEN (مركز الإشعارات)
// -----------------------------------------------------------------------------
class MarsoudNotificationsScreen extends StatelessWidget {
  const MarsoudNotificationsScreen({super.key});

  @override
  Widget build(BuildContext context) {
    final notifications = [
      {
        'title': 'اعتماد طلب إجازة',
        'desc': 'وافق مدير العمليات على طلب إجازتك السنوية المجدولة.',
        'time': 'منذ 15 دقيقة',
        'isNew': true,
        'icon': Icons.check_circle_outline_rounded,
        'color': const Color(0xFF059669),
      },
      {
        'title': 'مهمة مستعجلة جديدة',
        'desc': 'تم إسناد مهمة "إعداد الإقرار الضريبي ZATCA" إليك كأولوية قصوى.',
        'time': 'منذ ساعتين',
        'isNew': true,
        'icon': Icons.priority_high_rounded,
        'color': const Color(0xFFEF4444),
      },
      {
        'title': 'تحديث عهدة نقدية',
        'desc': 'تم تحويل مبلغ 3,000 ر.س تغذية لحساب عهدتك الميدانية.',
        'time': 'أمس، 02:30 م',
        'isNew': false,
        'icon': Icons.account_balance_wallet_outlined,
        'color': const Color(0xFF3B82F6),
      },
      {
        'title': 'تذكير باجتماع المبيعات',
        'desc': 'يبدأ اجتماع مناقشة عقد شركة الأفق بعد 30 دقيقة في القاعة الرئيسية.',
        'time': 'أمس، 10:00 ص',
        'isNew': false,
        'icon': Icons.calendar_today_rounded,
        'color': const Color(0xFFF59E0B),
      },
    ];

    return Directionality(
      textDirection: TextDirection.rtl,
      child: Scaffold(
        backgroundColor: const Color(0xFFF8FAFC),
        appBar: AppBar(
          backgroundColor: Colors.white,
          elevation: 0,
          leading: IconButton(
            icon: const Icon(Icons.arrow_forward_ios_rounded, size: 20, color: Color(0xFF0A2540)),
            onPressed: () => Navigator.of(context).maybePop(),
          ),
          title: const Text('الإشعارات والتنبيهات', style: TextStyle(fontWeight: FontWeight.w700, fontSize: 17, color: Color(0xFF0A2540))),
          actions: [
            TextButton(
              onPressed: () {},
              child: const Text('تحديد الكل كمقروء', style: TextStyle(fontSize: 13, color: Color(0xFF059669), fontWeight: FontWeight.w600)),
            ),
          ],
        ),
        body: ListView.separated(
          padding: const EdgeInsets.all(16),
          itemCount: notifications.length,
          separatorBuilder: (_, __) => const SizedBox(height: 12),
          itemBuilder: (context, index) {
            final item = notifications[index];
            final isNew = item['isNew'] as bool;
            return Container(
              padding: const EdgeInsets.all(16),
              decoration: BoxDecoration(
                color: isNew ? Colors.white : const Color(0xFFF8FAFC),
                borderRadius: BorderRadius.circular(14),
                border: Border.all(color: isNew ? const Color(0xFF059669).withOpacity(0.3) : const Color(0xFFE2E8F0)),
              ),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Container(
                    padding: const EdgeInsets.all(10),
                    decoration: BoxDecoration(
                      color: (item['color'] as Color).withOpacity(0.12),
                      shape: BoxShape.circle,
                    ),
                    child: Icon(item['icon'] as IconData, color: item['color'] as Color, size: 22),
                  ),
                  const SizedBox(width: 14),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Row(
                          mainAxisAlignment: MainAxisAlignment.spaceBetween,
                          children: [
                            Text(
                              item['title'] as String,
                              style: TextStyle(                                fontWeight: isNew ? FontWeight.w800 : FontWeight.w700,
                                fontSize: 14,
                                color: const Color(0xFF0A2540),
                              ),
                            ),
                            Text(
                              item['time'] as String,
                              style: const TextStyle(fontSize: 11, color: Color(0xFF94A3B8)),
                            ),
                          ],
                        ),
                        const SizedBox(height: 6),
                        Text(
                          item['desc'] as String,
                          style: const TextStyle(fontSize: 12.5, color: Color(0xFF475569), height: 1.45),
                        ),
                      ],
                    ),
                  ),
                ],
              ),
            );
          },
        ),
      ),
    );
  }
}

// -----------------------------------------------------------------------------
// 5. DASHBOARD SCREEN (لوحة التحكم الرئيسية)
// -----------------------------------------------------------------------------
class MarsoudDashboardScreen extends StatelessWidget {
  const MarsoudDashboardScreen({super.key});

  @override
  Widget build(BuildContext context) {
    return SingleChildScrollView(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          // Greeting & Date
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: const [
                  Text(
                    'أهلاً، سلمان 👋',
                    style: TextStyle(fontSize: 22, fontWeight: FontWeight.w800, color: Color(0xFF0A2540)),
                  ),
                  SizedBox(height: 2),
                  Text('الأربعاء، 24 أكتوبر 2024', style: TextStyle(fontSize: 12.5, color: Color(0xFF64748B))),
                ],
              ),
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
                decoration: BoxDecoration(
                  color: const Color(0xFFECFDF5),
                  borderRadius: BorderRadius.circular(20),
                  border: Border.all(color: const Color(0xFF059669).withOpacity(0.2)),
                ),
                child: Row(
                  children: const [
                    CircleAvatar(radius: 4, backgroundColor: Color(0xFF059669)),
                    SizedBox(width: 6),
                    Text('نشط الآن', style: TextStyle(fontSize: 11.5, color: Color(0xFF059669), fontWeight: FontWeight.w700)),
                  ],
                ),
              ),
            ],
          ),
          const SizedBox(height: 20),

          // 4 Clean KPI Tiles (Clean - NO back arrows)
          GridView.count(
            crossAxisCount: 2,
            shrinkWrap: true,
            physics: const NeverScrollableScrollPhysics(),
            crossAxisSpacing: 12,
            mainAxisSpacing: 12,
            childAspectRatio: 1.35,
            children: const [
              _KpiTile(
                title: 'مهامي المفتوحة',
                value: '14',
                badgeText: '+2 اليوم',
                badgeColor: Color(0xFFECFDF5),
                textColor: Color(0xFF059669),
                icon: Icons.task_alt_rounded,
              ),
              _KpiTile(
                title: 'عملاؤي المحتملين',
                value: '28',
                badgeText: '5 مؤهلين',
                badgeColor: Color(0xFFEFF6FF),
                textColor: Color(0xFF2563EB),
                icon: Icons.people_outline_rounded,
              ),
              _KpiTile(
                title: 'اجتماعات الأسبوع',
                value: '6',
                badgeText: 'قريباً',
                badgeColor: Color(0xFFF1F5F9),
                textColor: Color(0xFF475569),
                icon: Icons.calendar_today_rounded,
              ),
              _KpiTile(
                title: 'تنبيهات عاجلة',
                value: '3',
                badgeText: 'تتطلب إجراء',
                badgeColor: Color(0xFFFEF2F2),
                textColor: Color(0xFFEF4444),
                icon: Icons.notifications_active_outlined,
              ),
            ],
          ),
          const SizedBox(height: 20),

          // Quick Actions
          const Text('إجراءات سريعة', style: TextStyle(fontSize: 15, fontWeight: FontWeight.w800, color: Color(0xFF0A2540))),
          const SizedBox(height: 10),
          Row(
            children: [
              Expanded(
                child: ElevatedButton.icon(
                  onPressed: () {},
                  icon: const Icon(Icons.add_task_rounded, size: 18),
                  label: const Text('إضافة مهمة جديدة', style: TextStyle(fontSize: 13, fontWeight: FontWeight.w700)),
                  style: ElevatedButton.styleFrom(
                    backgroundColor: const Color(0xFF059669),
                    minimumSize: const Size.fromHeight(44),
                    shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
                  ),
                ),
              ),
              const SizedBox(width: 10),
              Expanded(
                child: OutlinedButton.icon(
                  onPressed: () {},
                  icon: const Icon(Icons.fingerprint_rounded, size: 18, color: Color(0xFF059669)),
                  label: const Text('تسجيل حضور وانصراف', style: TextStyle(fontSize: 13, fontWeight: FontWeight.w700, color: Color(0xFF0A2540))),
                  style: OutlinedButton.styleFrom(
                    minimumSize: const Size.fromHeight(44),
                    side: const BorderSide(color: Color(0xFFE2E8F0)),
                    shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
                  ),
                ),
              ),
            ],
          ),
          const SizedBox(height: 24),

          // Upcoming Meetings List
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: const [
              Text('اجتماعات قادمة', style: TextStyle(fontSize: 15, fontWeight: FontWeight.w800, color: Color(0xFF0A2540))),
              Text('عرض الكل', style: TextStyle(fontSize: 13, color: Color(0xFF059669), fontWeight: FontWeight.w700)),
            ],
          ),
          const SizedBox(height: 12),
          _meetingItem('مراجعة عقد شركة الأفق المالية', 'اليوم • 10:30 ص', 'مؤكد', const Color(0xFFEFF6FF), const Color(0xFF2563EB)),
          const SizedBox(height: 8),
          _meetingItem('جلسة نقاش مع العميل المحتمل', 'اليوم • 02:00 م', 'عن بُعد', const Color(0xFFECFDF5), const Color(0xFF059669)),
        ],
      ),
    );
  }

  static Widget _meetingItem(String title, String time, String tag, Color tagBg, Color tagColor) {
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: const Color(0xFFE2E8F0)),
      ),
      child: Row(
        children: [
          Container(
            padding: const EdgeInsets.all(10),
            decoration: BoxDecoration(color: const Color(0xFFF1F5F9), borderRadius: BorderRadius.circular(10)),
            child: const Icon(Icons.event_note_rounded, color: Color(0xFF0A2540), size: 22),
          ),
          const SizedBox(width: 14),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(title, style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 13.5, color: Color(0xFF0A2540))),
                const SizedBox(height: 4),
                Text(time, style: const TextStyle(fontSize: 12, color: Color(0xFF64748B))),
              ],
            ),
          ),
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
            decoration: BoxDecoration(color: tagBg, borderRadius: BorderRadius.circular(6)),
            child: Text(tag, style: TextStyle(fontSize: 11, fontWeight: FontWeight.bold, color: tagColor)),
          ),
        ],
      ),
    );
  }
}

class _KpiTile extends StatelessWidget {
  final String title;
  final String value;
  final String badgeText;
  final Color badgeColor;
  final Color textColor;
  final IconData icon;

  const _KpiTile({
    required this.title,
    required this.value,
    required this.badgeText,
    required this.badgeColor,
    required this.textColor,
    required this.icon,
  });

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: const Color(0xFFE2E8F0)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisAlignment: MainAxisAlignment.spaceBetween,
        children: [
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              Icon(icon, size: 22, color: textColor),
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                decoration: BoxDecoration(color: badgeColor, borderRadius: BorderRadius.circular(6)),
                child: Text(badgeText, style: TextStyle(fontSize: 10.5, fontWeight: FontWeight.bold, color: textColor)),
              ),
            ],
          ),
          Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(value, style: const TextStyle(fontSize: 24, fontWeight: FontWeight.w800, color: Color(0xFF0A2540), height: 1.1)),
              const SizedBox(height: 2),
              Text(title, style: const TextStyle(fontSize: 11.5, color: Color(0xFF64748B))),
            ],
          ),
        ],
      ),
    );
  }
}
