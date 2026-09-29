import 'package:flutter/material.dart';

/// ============================================================================
/// MARSOUD ERP - EMPLOYEE & SELF-SERVICE SCREENS (Flutter Dart Widgets - Part 1)
/// ============================================================================
/// Screens included:
/// 1. My Account (حسابي)
/// 2. Attendance & Geofencing (الحضور والانصراف الذكي)
/// 3. Daily Reports List (قائمة التقارير اليومية)
/// 4. Daily Report Detail (تفاصيل التقرير اليومي)
/// 5. My Requests & Leave (طلباتي والإجازات)
/// 6. Cash Custody (عهدتي النقدية)
/// 7. Item / Asset Custody (عهدي العينية والأصول)
/// 8. My Files & Documents (ملفاتي والمستندات)
/// ============================================================================

// -----------------------------------------------------------------------------
// 1. MY ACCOUNT SCREEN (حسابي)
// -----------------------------------------------------------------------------
class MarsoudMyAccountScreen extends StatelessWidget {
  const MarsoudMyAccountScreen({super.key});

  @override
  Widget build(BuildContext context) {
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
          title: const Text('حسابي', style: TextStyle(fontWeight: FontWeight.w800, fontSize: 17, color: Color(0xFF0A2540))),
          actions: [
            IconButton(icon: const Icon(Icons.qr_code_rounded, color: Color(0xFF0A2540)), onPressed: () {}),
          ],
        ),
        body: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            // User Profile Header Card
            Container(
              padding: const EdgeInsets.all(16),
              decoration: BoxDecoration(
                color: Colors.white,
                borderRadius: BorderRadius.circular(16),
                border: Border.all(color: const Color(0xFFE2E8F0)),
              ),
              child: Row(
                children: [
                  Container(
                    width: 58,
                    height: 58,
                    alignment: Alignment.center,
                    decoration: BoxDecoration(
                      color: const Color(0xFFECFDF5),
                      borderRadius: BorderRadius.circular(16),
                      border: Border.all(color: const Color(0xFF059669).withOpacity(0.25)),
                    ),
                    child: const Text('س.م', style: TextStyle(fontSize: 20, fontWeight: FontWeight.w900, color: Color(0xFF059669))),
                  ),
                  const SizedBox(width: 14),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: const [
                        Text('سلمان المطيري', style: TextStyle(fontSize: 16, fontWeight: FontWeight.w800, color: Color(0xFF0A2540))),
                        SizedBox(height: 2),
                        Text('مدير العمليات التنفيذي • الرقم الوظيفي: #8821', style: TextStyle(fontSize: 12, color: Color(0xFF64748B))),
                        SizedBox(height: 4),
                        Text('شركة الأفق للاستشارات والتقنية', style: TextStyle(fontSize: 11.5, fontWeight: FontWeight.w600, color: Color(0xFF059669))),
                      ],
                    ),
                  ),
                ],
              ),
            ),
            const SizedBox(height: 16),

            // Employee Metric Highlights
            Row(
              children: [
                _metricBox('رصيد الإجازات', '18 يوم', const Color(0xFF059669), Icons.beach_access_rounded),
                const SizedBox(width: 12),
                _metricBox('العهدة النقدية', '3,450 ر.س', const Color(0xFF2563EB), Icons.account_balance_wallet_rounded),
                const SizedBox(width: 12),
                _metricBox('تقييم الأداء', '4.9 / 5', const Color(0xFF7C3AED), Icons.star_rounded),
              ],
            ),
            const SizedBox(height: 20),

            // Account Settings Group 1
            const Text('البيانات الشخصية والوظيفية', style: TextStyle(fontSize: 13, fontWeight: FontWeight.w800, color: Color(0xFF64748B))),
            const SizedBox(height: 8),
            _menuGroup([
              _menuTile(Icons.badge_outlined, 'بيانات الهوية والعقد الوظيفي', 'ساري حتى 2026', () {}),
              _menuTile(Icons.mail_outline_rounded, 'البريد الإلكتروني', 'salman@alofooq.sa', () {}),
              _menuTile(Icons.phone_iphone_rounded, 'رقم الجوال المؤسسي', '+966 50 123 4567', () {}),
              _menuTile(Icons.lock_outline_rounded, 'الأمان والقفل الحيوي', 'مفعل (Face ID)', () {}),
            ]),
            const SizedBox(height: 20),

            // Account Settings Group 2
            const Text('تفضيلات التطبيق والنظام', style: TextStyle(fontSize: 13, fontWeight: FontWeight.w800, color: Color(0xFF64748B))),
            const SizedBox(height: 8),
            _menuGroup([
              _menuTile(Icons.notifications_none_rounded, 'إعدادات الإشعارات والتنبيهات', 'الكل مفعّل', () {}),
              _menuTile(Icons.language_rounded, 'لغة التطبيق', 'العربية (المملكة)', () {}),
              _menuTile(Icons.policy_outlined, 'الشروط والأحكام وسياسة الخصوصية', '', () {}),
              _menuTile(Icons.info_outline_rounded, 'إصدار التطبيق', 'Marsoud v2.4.1 (Build 890)', () {}),
            ]),
          ],
        ),
      ),
    );
  }

  static Widget _metricBox(String label, String value, Color color, IconData icon) {
    return Expanded(
      child: Container(
        padding: const EdgeInsets.symmetric(vertical: 12, horizontal: 10),
        decoration: BoxDecoration(
          color: Colors.white,
          borderRadius: BorderRadius.circular(14),
          border: Border.all(color: const Color(0xFFE2E8F0)),
        ),
        child: Column(
          children: [
            Icon(icon, size: 20, color: color),
            const SizedBox(height: 6),
            Text(value, style: TextStyle(fontSize: 14, fontWeight: FontWeight.w900, color: color)),
            const SizedBox(height: 2),
            Text(label, style: const TextStyle(fontSize: 10.5, color: Color(0xFF64748B)), textAlign: TextAlign.center),
          ],
        ),
      ),
    );
  }

  static Widget _menuGroup(List<Widget> children) {
    return Container(
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: const Color(0xFFE2E8F0)),
      ),
      child: Column(children: children),
    );
  }

  static Widget _menuTile(IconData icon, String title, String subtitle, VoidCallback onTap) {
    return ListTile(
      leading: Icon(icon, size: 20, color: const Color(0xFF0A2540)),
      title: Text(title, style: const TextStyle(fontSize: 13, fontWeight: FontWeight.w700, color: Color(0xFF0A2540))),
      trailing: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          if (subtitle.isNotEmpty)
            Text(subtitle, style: const TextStyle(fontSize: 11.5, color: Color(0xFF94A3B8))),
          const SizedBox(width: 6),
          const Icon(Icons.arrow_back_ios_new_rounded, size: 14, color: Color(0xFFCBD5E1)),
        ],
      ),
      onTap: onTap,
    );
  }
}

// -----------------------------------------------------------------------------
// 2. ATTENDANCE SCREEN (الحضور والانصراف الذكي)
// -----------------------------------------------------------------------------
class MarsoudAttendanceScreen extends StatelessWidget {
  const MarsoudAttendanceScreen({super.key});

  @override
  Widget build(BuildContext context) {
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
          title: const Text('الحضور والانصراف', style: TextStyle(fontWeight: FontWeight.w800, fontSize: 17, color: Color(0xFF0A2540))),
          actions: [
            IconButton(icon: const Icon(Icons.history_rounded, color: Color(0xFF0A2540)), onPressed: () {}),
          ],
        ),
        body: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            // Geofence Map / GPS Card
            Container(
              padding: const EdgeInsets.all(16),
              decoration: BoxDecoration(
                color: Colors.white,
                borderRadius: BorderRadius.circular(16),
                border: Border.all(color: const Color(0xFFE2E8F0)),
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Container(
                        padding: const EdgeInsets.all(8),
                        decoration: BoxDecoration(color: const Color(0xFFECFDF5), borderRadius: BorderRadius.circular(10)),
                        child: const Icon(Icons.location_on_rounded, color: Color(0xFF059669), size: 22),
                      ),
                      const SizedBox(width: 12),
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: const [
                            Text('برج الرياض المالي - المقر الرئيسي', style: TextStyle(fontWeight: FontWeight.w800, fontSize: 14, color: Color(0xFF0A2540))),
                            Text('النطاق الجغرافي: ضمن السياج المعتمد (15 متر)', style: TextStyle(fontSize: 11.5, color: Color(0xFF059669), fontWeight: FontWeight.w600)),
                          ],
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 16),

                  // Punch Action Button Area
                  Container(
                    width: double.infinity,
                    padding: const EdgeInsets.symmetric(vertical: 24),
                    decoration: BoxDecoration(
                      color: const Color(0xFFF8FAFC),
                      borderRadius: BorderRadius.circular(16),
                      border: Border.all(color: const Color(0xFFE2E8F0)),
                    ),
                    child: Column(
                      children: [
                        const Text('08:32:15 ص', style: TextStyle(fontSize: 30, fontWeight: FontWeight.w900, color: Color(0xFF0A2540))),
                        const Text('الأربعاء، 24 أكتوبر 2024', style: TextStyle(fontSize: 12, color: Color(0xFF64748B))),
                        const SizedBox(height: 20),
                        ElevatedButton.icon(
                          onPressed: () {},
                          icon: const Icon(Icons.fingerprint_rounded, size: 28),
                          label: const Text('تسجيل الانصراف اليومي', style: TextStyle(fontSize: 15, fontWeight: FontWeight.w800)),
                          style: ElevatedButton.styleFrom(
                            backgroundColor: const Color(0xFFEF4444),
                            foregroundColor: Colors.white,
                            padding: const EdgeInsets.symmetric(horizontal: 28, vertical: 14),
                            shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(30)),
                          ),
                        ),
                        const SizedBox(height: 8),
                        const Text('تم تسجيل حضورك اليوم بنجاح الساعة 08:30 ص', style: TextStyle(fontSize: 11, color: Color(0xFF059669), fontWeight: FontWeight.w700)),
                      ],
                    ),
                  ),
                ],
              ),
            ),
            const SizedBox(height: 20),

            // Today's Work Summary
            Row(
              children: [
                _summaryItem('وقت الحضور', '08:30 ص', const Color(0xFF059669)),
                const SizedBox(width: 10),
                _summaryItem('ساعات العمل', '5 س 12 د', const Color(0xFF2563EB)),
                const SizedBox(width: 10),
                _summaryItem('الوقت المتبقي', '2 س 48 د', const Color(0xFF64748B)),
              ],
            ),
            const SizedBox(height: 20),

            // Week Attendance Log
            const Text('سجل الحضور لهذا الأسبوع', style: TextStyle(fontSize: 14, fontWeight: FontWeight.w800, color: Color(0xFF0A2540))),
            const SizedBox(height: 10),
            _attendanceRow('الأربعاء 24 أكتوبر', '08:30 ص', 'جاري العمل', 'في الموعد', const Color(0xFF059669), const Color(0xFFECFDF5)),
            _attendanceRow('الثلاثاء 23 أكتوبر', '08:28 ص', '05:04 م', '8 س 36 د', const Color(0xFF059669), const Color(0xFFECFDF5)),
            _attendanceRow('الإثنين 22 أكتوبر', '08:42 ص', '05:15 م', 'تأخير 12 د', const Color(0xFFD97706), const Color(0xFFFFFBEB)),
            _attendanceRow('الأحد 21 أكتوبر', '08:25 ص', '05:00 م', '8 س 35 د', const Color(0xFF059669), const Color(0xFFECFDF5)),
          ],
        ),
      ),
    );
  }

  static Widget _summaryItem(String title, String val, Color color) {
    return Expanded(
      child: Container(
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(
          color: Colors.white,
          borderRadius: BorderRadius.circular(12),
          border: Border.all(color: const Color(0xFFE2E8F0)),
        ),
        child: Column(
          children: [
            Text(val, style: TextStyle(fontSize: 15, fontWeight: FontWeight.w800, color: color)),
            const SizedBox(height: 2),
            Text(title, style: const TextStyle(fontSize: 11, color: Color(0xFF64748B))),
          ],
        ),
      ),
    );
  }

  static Widget _attendanceRow(String date, String inTime, String outTime, String badge, Color badgeColor, Color badgeBg) {
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: const Color(0xFFE2E8F0)),
      ),
      child: Row(
        mainAxisAlignment: MainAxisAlignment.spaceBetween,
        children: [
          Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(date, style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 13, color: Color(0xFF0A2540))),
              Text('الدخول: $inTime • الخروج: $outTime', style: const TextStyle(fontSize: 11.5, color: Color(0xFF64748B))),
            ],
          ),
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
            decoration: BoxDecoration(color: badgeBg, borderRadius: BorderRadius.circular(6)),
            child: Text(badge, style: TextStyle(fontSize: 11, fontWeight: FontWeight.bold, color: badgeColor)),
          ),
        ],
      ),
    );
  }
}

// -----------------------------------------------------------------------------
// 3. DAILY REPORTS LIST SCREEN (التقارير اليومية)
// -----------------------------------------------------------------------------
class MarsoudDailyReportsScreen extends StatelessWidget {
  const MarsoudDailyReportsScreen({super.key});

  @override
  Widget build(BuildContext context) {
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
          title: const Text('التقارير اليومية', style: TextStyle(fontWeight: FontWeight.w800, fontSize: 17, color: Color(0xFF0A2540))),
          actions: [
            IconButton(icon: const Icon(Icons.filter_list_rounded, color: Color(0xFF0A2540)), onPressed: () {}),
          ],
        ),
        body: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            // Status bar
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
              decoration: BoxDecoration(
                color: const Color(0xFFECFDF5),
                borderRadius: BorderRadius.circular(12),
                border: Border.all(color: const Color(0xFF059669).withOpacity(0.2)),
              ),
              child: Row(
                children: const [
                  Icon(Icons.check_circle_rounded, color: Color(0xFF059669), size: 18),
                  SizedBox(width: 8),
                  Text('نسبة تسليم التقارير لهذا الشهر: 100% (22/22 يوم)', style: TextStyle(fontSize: 12, fontWeight: FontWeight.w700, color: Color(0xFF047857))),
                ],
              ),
            ),
            const SizedBox(height: 16),

            _reportCard(
              title: 'التقرير اليومي - 24 أكتوبر 2024',
              project: 'مشروع بوابة التوريد الرقمية ZATCA',
              status: 'مسودة جارية',
              statusColor: const Color(0xFFD97706),
              statusBg: const Color(0xFFFFFBEB),
              completedTasks: '4 مهام منجزة',
              hours: '6.5 ساعات',
            ),
            _reportCard(
              title: 'التقرير اليومي - 23 أكتوبر 2024',
              project: 'استشارات التحول المالي والإداري',
              status: 'معتمد',
              statusColor: const Color(0xFF059669),
              statusBg: const Color(0xFFECFDF5),
              completedTasks: '6 مهام منجزة',
              hours: '8 ساعات',
            ),
            _reportCard(
              title: 'التقرير اليومي - 22 أكتوبر 2024',
              project: 'تهيئة الخوادم السحابية والربط',
              status: 'معتمد',
              statusColor: const Color(0xFF059669),
              statusBg: const Color(0xFFECFDF5),
              completedTasks: '5 مهام منجزة',
              hours: '8 ساعات',
            ),
          ],
        ),
        floatingActionButton: FloatingActionButton.extended(
          onPressed: () {},
          backgroundColor: const Color(0xFF059669),
          icon: const Icon(Icons.note_add_rounded, color: Colors.white),
          label: const Text('كتابة تقرير اليوم', style: TextStyle(fontWeight: FontWeight.w700, color: Colors.white)),
        ),
      ),
    );
  }

  static Widget _reportCard({
    required String title,
    required String project,
    required String status,
    required Color statusColor,
    required Color statusBg,
    required String completedTasks,
    required String hours,
  }) {
    return Container(
      margin: const EdgeInsets.only(bottom: 12),
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: const Color(0xFFE2E8F0)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              Text(title, style: const TextStyle(fontWeight: FontWeight.w800, fontSize: 14, color: Color(0xFF0A2540))),
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                decoration: BoxDecoration(color: statusBg, borderRadius: BorderRadius.circular(6)),
                child: Text(status, style: TextStyle(fontSize: 11, fontWeight: FontWeight.bold, color: statusColor)),
              ),
            ],
          ),
          const SizedBox(height: 6),
          Text(project, style: const TextStyle(fontSize: 12, color: Color(0xFF64748B))),
          const Divider(height: 20, color: Color(0xFFF1F5F9)),
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              Row(
                children: [
                  const Icon(Icons.task_alt_rounded, size: 15, color: Color(0xFF059669)),
                  const SizedBox(width: 4),
                  Text(completedTasks, style: const TextStyle(fontSize: 11.5, color: Color(0xFF475569))),
                  const SizedBox(width: 14),
                  const Icon(Icons.access_time_rounded, size: 15, color: Color(0xFF64748B)),
                  const SizedBox(width: 4),
                  Text(hours, style: const TextStyle(fontSize: 11.5, color: Color(0xFF475569))),
                ],
              ),
              const Icon(Icons.arrow_back_ios_new_rounded, size: 13, color: Color(0xFF94A3B8)),
            ],
          ),
        ],
      ),
    );
  }
}

// -----------------------------------------------------------------------------
// 4. DAILY REPORT DETAIL SCREEN (تفاصيل التقرير)
// -----------------------------------------------------------------------------
class MarsoudDailyReportDetailScreen extends StatelessWidget {
  const MarsoudDailyReportDetailScreen({super.key});

  @override
  Widget build(BuildContext context) {
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
          title: const Text('تفاصيل التقرير', style: TextStyle(fontWeight: FontWeight.w800, fontSize: 17, color: Color(0xFF0A2540))),
          actions: [
            IconButton(icon: const Icon(Icons.print_outlined, color: Color(0xFF0A2540)), onPressed: () {}),
          ],
        ),
        body: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            // Header Card
            Container(
              padding: const EdgeInsets.all(16),
              decoration: BoxDecoration(
                color: Colors.white,
                borderRadius: BorderRadius.circular(16),
                border: Border.all(color: const Color(0xFFE2E8F0)),
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    mainAxisAlignment: MainAxisAlignment.spaceBetween,
                    children: [
                      const Text('تقرير الأربعاء 24 أكتوبر 2024', style: TextStyle(fontWeight: FontWeight.w800, fontSize: 15, color: Color(0xFF0A2540))),
                      Container(
                        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                        decoration: BoxDecoration(color: const Color(0xFFECFDF5), borderRadius: BorderRadius.circular(6)),
                        child: const Text('معتمد', style: TextStyle(fontSize: 11, fontWeight: FontWeight.bold, color: Color(0xFF059669))),
                      ),
                    ],
                  ),
                  const SizedBox(height: 6),
                  const Text('مقدّم بواسطة: سلمان المطيري (مدير العمليات)', style: TextStyle(fontSize: 12, color: Color(0xFF64748B))),
                  const SizedBox(height: 4),
                  const Text('اعتماد: فهد السبيعي (رئيس إدارة المشاريع)', style: TextStyle(fontSize: 11.5, color: Color(0xFF059669), fontWeight: FontWeight.w600)),
                ],
              ),
            ),
            const SizedBox(height: 16),

            // Tasks Accomplished
            Container(
              padding: const EdgeInsets.all(16),
              decoration: BoxDecoration(
                color: Colors.white,
                borderRadius: BorderRadius.circular(16),
                border: Border.all(color: const Color(0xFFE2E8F0)),
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const Text('المهام المنجزة خلال ساعات العمل', style: TextStyle(fontWeight: FontWeight.w800, fontSize: 14, color: Color(0xFF0A2540))),
                  const SizedBox(height: 12),
                  _taskBullet('إغلاق اختبارات ربط الفوترة الإلكترونية مع هيئة الزكاة والضريبة (ZATCA Stage 2).'),
                  _taskBullet('عقد اجتماع تنسيقي مع الإدارة المالية لمجموعة الأفق لمناقشة عروض الأسعار المحدثة.'),
                  _taskBullet('مراجعة وتحديث سياسة الصرف للعهدة الميدانية للفصل الرابع.'),
                ],
              ),
            ),
            const SizedBox(height: 16),

            // Blockers & Notes
            Container(
              padding: const EdgeInsets.all(16),
              decoration: BoxDecoration(
                color: Colors.white,
                borderRadius: BorderRadius.circular(16),
                border: Border.all(color: const Color(0xFFE2E8F0)),
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: const [
                  Text('المعوقات والملاحظات الإدارية', style: TextStyle(fontWeight: FontWeight.w800, fontSize: 14, color: Color(0xFF0A2540))),
                  SizedBox(height: 8),
                  Text('لا توجد معوقات جوهرية اليوم، تم حل تأخير استجابة السيرفر عبر تحديث شهادة الأمان SSL بنجاح.',
                      style: TextStyle(fontSize: 12.5, color: Color(0xFF475569), height: 1.5)),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }

  static Widget _taskBullet(String text) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 8.0),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Icon(Icons.check_circle_rounded, color: Color(0xFF059669), size: 16),
          const SizedBox(width: 8),
          Expanded(child: Text(text, style: const TextStyle(fontSize: 12.5, color: Color(0xFF334155), height: 1.4))),
        ],
      ),
    );
  }
}

// -----------------------------------------------------------------------------
// 5. MY REQUESTS SCREEN (طلباتي والإجازات)
// -----------------------------------------------------------------------------
class MarsoudMyRequestsScreen extends StatelessWidget {
  const MarsoudMyRequestsScreen({super.key});

  @override
  Widget build(BuildContext context) {
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
          title: const Text('طلباتي', style: TextStyle(fontWeight: FontWeight.w800, fontSize: 17, color: Color(0xFF0A2540))),
        ),
        body: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            // Leave Balance Header
            Container(
              padding: const EdgeInsets.all(16),
              decoration: BoxDecoration(
                color: const Color(0xFF0A2540),
                borderRadius: BorderRadius.circular(16),
              ),
              child: Row(
                mainAxisAlignment: MainAxisAlignment.spaceBetween,
                children: [
                  Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: const [
                      Text('رصيد الإجازات السنوية المتاح', style: TextStyle(fontSize: 12, color: Color(0xFF94A3B8))),
                      SizedBox(height: 4),
                      Text('18 يوماً متبقية', style: TextStyle(fontSize: 20, fontWeight: FontWeight.w900, color: Colors.white)),
                    ],
                  ),
                  ElevatedButton(
                    onPressed: () {},
                    style: ElevatedButton.styleFrom(
                      backgroundColor: const Color(0xFF059669),
                      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
                      padding: const EdgeInsets.symmetric(horizontal: 16),
                    ),
                    child: const Text('طلب جديد', style: TextStyle(fontSize: 13, fontWeight: FontWeight.w700, color: Colors.white)),
                  ),
                ],
              ),
            ),
            const SizedBox(height: 16),

            // Requests List
            _requestItem(
              type: 'طلب إجازة اعتيادية (5 أيام)',
              date: 'من 18 نوفمبر إلى 22 نوفمبر 2024',
              status: 'قيد الاعتماد',
              statusColor: const Color(0xFFD97706),
              statusBg: const Color(0xFFFFFBEB),
              icon: Icons.beach_access_rounded,
            ),
            _requestItem(
              type: 'طلب تغذية عهدة نقدية (3,000 ر.س)',
              date: 'تم التقديم: 20 أكتوبر 2024',
              status: 'معتمد ومحوّل',
              statusColor: const Color(0xFF059669),
              statusBg: const Color(0xFFECFDF5),
              icon: Icons.account_balance_wallet_rounded,
            ),
            _requestItem(
              type: 'شهادة تعريف بالراتب موجهة للبنك',
              date: 'تم التقديم: 12 أكتوبر 2024',
              status: 'مكتمل وجاهز للتحميل',
              statusColor: const Color(0xFF2563EB),
              statusBg: const Color(0xFFEFF6FF),
              icon: Icons.description_rounded,
            ),
          ],
        ),
      ),
    );
  }

  static Widget _requestItem({
    required String type,
    required String date,
    required String status,
    required Color statusColor,
    required Color statusBg,
    required IconData icon,
  }) {
    return Container(
      margin: const EdgeInsets.only(bottom: 12),
      padding: const EdgeInsets.all(16),
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
            child: Icon(icon, color: const Color(0xFF0A2540), size: 20),
          ),
          const SizedBox(width: 14),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(type, style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 13.5, color: Color(0xFF0A2540))),
                const SizedBox(height: 3),
                Text(date, style: const TextStyle(fontSize: 11.5, color: Color(0xFF64748B))),
              ],
            ),
          ),
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
            decoration: BoxDecoration(color: statusBg, borderRadius: BorderRadius.circular(6)),
            child: Text(status, style: TextStyle(fontSize: 11, fontWeight: FontWeight.bold, color: statusColor)),
          ),
        ],
      ),
    );
  }
}

// -----------------------------------------------------------------------------
// 6. CASH CUSTODY SCREEN (عهدتي النقدية)
// -----------------------------------------------------------------------------
class MarsoudCashCustodyScreen extends StatelessWidget {
  const MarsoudCashCustodyScreen({super.key});

  @override
  Widget build(BuildContext context) {
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
          title: const Text('عهدتي النقدية', style: TextStyle(fontWeight: FontWeight.w800, fontSize: 17, color: Color(0xFF0A2540))),
        ),
        body: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            // Balance Card
            Container(
              padding: const EdgeInsets.all(20),
              decoration: BoxDecoration(
                color: Colors.white,
                borderRadius: BorderRadius.circular(16),
                border: Border.all(color: const Color(0xFFE2E8F0)),
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const Text('الرصيد المتبقي في العهدة الحالية', style: TextStyle(fontSize: 12, color: Color(0xFF64748B))),
                  const SizedBox(height: 4),
                  const Text('3,450.00 ر.س', style: TextStyle(fontSize: 28, fontWeight: FontWeight.w900, color: Color(0xFF059669))),
                  const Divider(height: 24, color: Color(0xFFF1F5F9)),
                  Row(
                    mainAxisAlignment: MainAxisAlignment.spaceBetween,
                    children: const [
                      Text('إجمالي المنصرف: 1,550 ر.س', style: TextStyle(fontSize: 12, color: Color(0xFF64748B))),
                      Text('سقف العهدة: 5,000 ر.س', style: TextStyle(fontSize: 12, fontWeight: FontWeight.w700, color: Color(0xFF0A2540))),
                    ],
                  ),
                ],
              ),
            ),
            const SizedBox(height: 16),

            // Actions
            Row(
              children: [
                Expanded(
                  child: ElevatedButton.icon(
                    onPressed: () {},
                    icon: const Icon(Icons.receipt_long_rounded, size: 18),
                    label: const Text('إضافة فاتورة صرف', style: TextStyle(fontSize: 13, fontWeight: FontWeight.w700)),
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
                    icon: const Icon(Icons.add_card_rounded, size: 18, color: Color(0xFF0A2540)),
                    label: const Text('طلب تغذية نقدية', style: TextStyle(fontSize: 13, fontWeight: FontWeight.w700, color: Color(0xFF0A2540))),
                    style: OutlinedButton.styleFrom(
                      minimumSize: const Size.fromHeight(44),
                      side: const BorderSide(color: Color(0xFFE2E8F0)),
                      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
                    ),
                  ),
                ),
              ],
            ),
            const SizedBox(height: 20),

            // Expenses List
            const Text('سجل المصروفات والفواتير', style: TextStyle(fontSize: 14, fontWeight: FontWeight.w800, color: Color(0xFF0A2540))),
            const SizedBox(height: 10),
            _expenseRow('سداد وقود ونقل ميداني لفريق العمل', '24 أكتوبر • ضريبة ZATCA', '240.00 ر.س', 'مطابق'),
            _expenseRow('شراء مستلزمات مكتبية وضيافة عملاء', '22 أكتوبر • مؤسسة الركائز', '380.00 ر.س', 'مطابق'),
            _expenseRow('تجديد اشتراك خدمة سحابية عاجلة', '18 أكتوبر • فاتورة إلكترونية', '930.00 ر.س', 'مطابق'),
          ],
        ),
      ),
    );
  }

  static Widget _expenseRow(String title, String sub, String amount, String tag) {
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: const Color(0xFFE2E8F0)),
      ),
      child: Row(
        mainAxisAlignment: MainAxisAlignment.spaceBetween,
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(title, style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 13, color: Color(0xFF0A2540))),
                const SizedBox(height: 2),
                Text(sub, style: const TextStyle(fontSize: 11, color: Color(0xFF64748B))),
              ],
            ),
          ),
          Column(
            crossAxisAlignment: CrossAxisAlignment.end,
            children: [
              Text(amount, style: const TextStyle(fontWeight: FontWeight.w800, fontSize: 14, color: Color(0xFFEF4444))),
              Text(tag, style: const TextStyle(fontSize: 10.5, fontWeight: FontWeight.bold, color: Color(0xFF059669))),
            ],
          ),
        ],
      ),
    );
  }
}

// -----------------------------------------------------------------------------
// 7. ITEM CUSTODY SCREEN (عهدي العينية والأصول)
// -----------------------------------------------------------------------------
class MarsoudItemCustodyScreen extends StatelessWidget {
  const MarsoudItemCustodyScreen({super.key});

  @override
  Widget build(BuildContext context) {
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
          title: const Text('عهدي العينية', style: TextStyle(fontWeight: FontWeight.w800, fontSize: 17, color: Color(0xFF0A2540))),
        ),
        body: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            _assetCard(
              title: 'جهاز MacBook Pro 16 M2 Max',
              serial: 'SN: C02G8940MD6R',
              tag: 'أصل ثابت مسلّم',
              date: 'تاريخ التسليم: 15 يناير 2024',
              icon: Icons.laptop_mac_rounded,
            ),
            _assetCard(
              title: 'شاشة Dell UltraSharp 27 4K',
              serial: 'SN: CN-0T992F-72872',
              tag: 'أصل مكتبي',
              date: 'تاريخ التسليم: 15 يناير 2024',
              icon: Icons.desktop_windows_rounded,
            ),
            _assetCard(
              title: 'بطاقة دخول رقمية مشفرة وسوار أمان',
              serial: 'RFID: #8821-SEC-A',
              tag: 'تصريح أمني',
              date: 'تاريخ التسليم: 01 يناير 2024',
              icon: Icons.badge_outlined,
            ),
          ],
        ),
      ),
    );
  }

  static Widget _assetCard({
    required String title,
    required String serial,
    required String tag,
    required String date,
    required IconData icon,
  }) {
    return Container(
      margin: const EdgeInsets.only(bottom: 12),
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: const Color(0xFFE2E8F0)),
      ),
      child: Row(
        children: [
          Container(
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(color: const Color(0xFFECFDF5), borderRadius: BorderRadius.circular(12)),
            child: Icon(icon, color: const Color(0xFF059669), size: 24),
          ),
          const SizedBox(width: 14),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(title, style: const TextStyle(fontWeight: FontWeight.w800, fontSize: 13.5, color: Color(0xFF0A2540))),
                const SizedBox(height: 2),
                Text(serial, style: const TextStyle(fontSize: 11.5, color: Color(0xFF64748B))),
                const SizedBox(height: 4),
                Text(date, style: const TextStyle(fontSize: 10.5, color: Color(0xFF94A3B8))),
              ],
            ),
          ),
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
            decoration: BoxDecoration(color: const Color(0xFFF1F5F9), borderRadius: BorderRadius.circular(6)),
            child: Text(tag, style: const TextStyle(fontSize: 11, fontWeight: FontWeight.bold, color: Color(0xFF475569))),
          ),
        ],
      ),
    );
  }
}

// -----------------------------------------------------------------------------
// 8. MY FILES SCREEN (ملفاتي والمستندات)
// -----------------------------------------------------------------------------
class MarsoudMyFilesScreen extends StatelessWidget {
  const MarsoudMyFilesScreen({super.key});

  @override
  Widget build(BuildContext context) {
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
          title: const Text('ملفاتي', style: TextStyle(fontWeight: FontWeight.w800, fontSize: 17, color: Color(0xFF0A2540))),
          actions: [
            IconButton(icon: const Icon(Icons.search_rounded, color: Color(0xFF0A2540)), onPressed: () {}),
          ],
        ),
        body: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            _fileTile('مسودة عقد التوريد الموحد v1.2.pdf', '3.4 MB • 24 أكتوبر 2024', Icons.picture_as_pdf_rounded, const Color(0xFFEF4444)),
            _fileTile('شهادة الاعتماد الضريبي ZATCA 2024.pdf', '1.1 MB • 15 أكتوبر 2024', Icons.picture_as_pdf_rounded, const Color(0xFFEF4444)),
            _fileTile('جدول الميزانية التقديرية للربع الرابع.xlsx', '850 KB • 10 أكتوبر 2024', Icons.table_chart_rounded, const Color(0xFF059669)),
            _fileTile('العرض التقديمي لمنظومة مرصود ERP.pptx', '12.8 MB • 05 أكتوبر 2024', Icons.slideshow_rounded, const Color(0xFFD97706)),
          ],
        ),
        floatingActionButton: FloatingActionButton(
          onPressed: () {},
          backgroundColor: const Color(0xFF059669),
          child: const Icon(Icons.upload_file_rounded, color: Colors.white),
        ),
      ),
    );
  }

  static Widget _fileTile(String name, String meta, IconData icon, Color iconColor) {
    return Container(
      margin: const EdgeInsets.only(bottom: 10),
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
            decoration: BoxDecoration(color: iconColor.withOpacity(0.1), borderRadius: BorderRadius.circular(10)),
            child: Icon(icon, color: iconColor, size: 22),
          ),
          const SizedBox(width: 14),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(name, style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 13, color: Color(0xFF0A2540))),
                const SizedBox(height: 2),
                Text(meta, style: const TextStyle(fontSize: 11, color: Color(0xFF64748B))),
              ],
            ),
          ),
          IconButton(icon: const Icon(Icons.more_vert_rounded, color: Color(0xFF94A3B8)), onPressed: () {}),
        ],
      ),
    );
  }
}
