import 'package:flutter/material.dart';

/// ============================================================================
/// MARSOUD ERP - TASKS, PROJECTS & SUPPORT SCREENS (Flutter Dart Widgets - Part 2)
/// ============================================================================
/// Screens included:
/// 1. Activity Log (سجل نشاطي)
/// 2. Support Tickets (تذاكر الدعم الفني)
/// 3. Tasks List (قائمة المهام)
/// 4. Task Detail (تفاصيل المهمة) - No realistic photos, 32x32 initials avatars
/// 5. New Task Form (مهمة جديدة)
/// 6. Task Archive (أرشيف المهام)
/// 7. Projects List (قائمة المشاريع)
/// 8. Project Detail (تفاصيل المشروع)
/// 9. Empty State System (حالات النظام الفارغة)
/// ============================================================================

// -----------------------------------------------------------------------------
// 1. ACTIVITY LOG SCREEN (سجل نشاطي)
// -----------------------------------------------------------------------------
class MarsoudActivityLogScreen extends StatelessWidget {
  const MarsoudActivityLogScreen({super.key});

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
          title: const Text('سجل نشاطي', style: TextStyle(fontFamily: 'Cairo', fontWeight: FontWeight.w800, fontSize: 17, color: Color(0xFF0A2540))),
        ),
        body: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            _logItem(
              title: 'إنشاء مهمة جديدة: إعداد التقارير الضريبية ZATCA',
              time: 'اليوم • 11:42 ص',
              tag: 'المهام',
              tagColor: const Color(0xFF059669),
              icon: Icons.task_alt_rounded,
            ),
            _logItem(
              title: 'تحديث مرحلة عميل: شركة الأفق للاستشارات المالية (85,000 ر.س)',
              time: 'اليوم • 10:15 ص',
              tag: 'العملاء',
              tagColor: const Color(0xFF2563EB),
              icon: Icons.people_outline_rounded,
            ),
            _logItem(
              title: 'تسجيل الحضور الذكي عبر السياج الجغرافي (برج الرياض)',
              time: 'اليوم • 08:32 ص',
              tag: 'الدوام',
              tagColor: const Color(0xFF059669),
              icon: Icons.fingerprint_rounded,
            ),
            _logItem(
              title: 'سداد فاتورة وقود ونقل ميداني (240.00 ر.س)',
              time: 'أمس • 02:15 م',
              tag: 'المالية',
              tagColor: const Color(0xFFD97706),
              icon: Icons.receipt_long_rounded,
            ),
          ],
        ),
      ),
    );
  }

  static Widget _logItem({
    required String title,
    required String time,
    required String tag,
    required Color tagColor,
    required IconData icon,
  }) {
    return Container(
      margin: const EdgeInsets.only(bottom: 10),
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: const Color(0xFFE2E8F0)),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Container(
            padding: const EdgeInsets.all(8),
            decoration: BoxDecoration(color: tagColor.withOpacity(0.12), shape: BoxShape.circle),
            child: Icon(icon, color: tagColor, size: 18),
          ),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(title, style: const TextStyle(fontFamily: 'Cairo', fontWeight: FontWeight.w700, fontSize: 13, color: Color(0xFF0A2540))),
                const SizedBox(height: 3),
                Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: [
                    Text(time, style: const TextStyle(fontFamily: 'Cairo', fontSize: 11, color: Color(0xFF94A3B8))),
                    Container(
                      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                      decoration: BoxDecoration(color: tagColor.withOpacity(0.1), borderRadius: BorderRadius.circular(4)),
                      child: Text(tag, style: TextStyle(fontFamily: 'Cairo', fontSize: 10, fontWeight: FontWeight.bold, color: tagColor)),
                    ),
                  ],
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

// -----------------------------------------------------------------------------
// 2. SUPPORT TICKETS SCREEN (تذاكر الدعم الفني)
// -----------------------------------------------------------------------------
class MarsoudSupportTicketsScreen extends StatelessWidget {
  const MarsoudSupportTicketsScreen({super.key});

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
          title: const Text('تذاكر الدعم الفني', style: TextStyle(fontFamily: 'Cairo', fontWeight: FontWeight.w800, fontSize: 17, color: Color(0xFF0A2540))),
        ),
        body: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            // Active Ticket Thread Preview
            Container(
              padding: const EdgeInsets.all(16),
              decoration: BoxDecoration(
                color: Colors.white,
                borderRadius: BorderRadius.circular(16),
                border: Border.all(color: const Color(0xFF059669).withOpacity(0.3)),
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    mainAxisAlignment: MainAxisAlignment.spaceBetween,
                    children: [
                      const Text('مشكلة مزامنة الفواتير الضريبية ZATCA', style: TextStyle(fontFamily: 'Cairo', fontWeight: FontWeight.w800, fontSize: 14, color: Color(0xFF0A2540))),
                      Container(
                        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                        decoration: BoxDecoration(color: const Color(0xFFEFF6FF), borderRadius: BorderRadius.circular(6)),
                        child: const Text('بانتظار ردك', style: TextStyle(fontFamily: 'Cairo', fontSize: 11, fontWeight: FontWeight.bold, color: Color(0xFF2563EB))),
                      ),
                    ],
                  ),
                  const SizedBox(height: 4),
                  const Text('تذكرة: #TCK-9412 • منذ 15 دقيقة', style: TextStyle(fontFamily: 'Cairo', fontSize: 11, color: Color(0xFF94A3B8))),
                  const Divider(height: 20, color: Color(0xFFF1F5F9)),
                  // Message Bubble
                  Container(
                    padding: const EdgeInsets.all(12),
                    decoration: BoxDecoration(color: const Color(0xFFF8FAFC), borderRadius: BorderRadius.circular(10)),
                    child: const Text(
                      'م. أحمد الناصر: تم فحص المفتاح الرقمي وتحديث الشهادة الضريبية على الخادم بنجاح. يرجى الضغط على زر إعادة المزامنة والتأكيد.',
                      style: TextStyle(fontFamily: 'Cairo', fontSize: 12.5, color: Color(0xFF334155), height: 1.45),
                    ),
                  ),
                ],
              ),
            ),
            const SizedBox(height: 16),

            // Past Tickets
            const Text('التذاكر السابقة', style: TextStyle(fontFamily: 'Cairo', fontSize: 14, fontWeight: FontWeight.w800, color: Color(0xFF0A2540))),
            const SizedBox(height: 10),
            _ticketItem('#TCK-8854', 'طلب ترقية صلاحيات الوصول إلى تقارير المبيعات', 'قيد المعالجة', const Color(0xFF2563EB), const Color(0xFFEFF6FF)),
            _ticketItem('#TCK-8120', 'عدم ظهور كشف عهدة الأصول في التطبيق الجوال', 'تم الحل والاعتماد', const Color(0xFF059669), const Color(0xFFECFDF5)),
          ],
        ),
        floatingActionButton: FloatingActionButton.extended(
          onPressed: () {},
          backgroundColor: const Color(0xFF059669),
          icon: const Icon(Icons.add_rounded, color: Colors.white),
          label: const Text('تذكرة جديدة', style: TextStyle(fontFamily: 'Cairo', fontWeight: FontWeight.w700, color: Colors.white)),
        ),
      ),
    );
  }

  static Widget _ticketItem(String code, String title, String status, Color statusColor, Color statusBg) {
    return Container(
      margin: const EdgeInsets.only(bottom: 10),
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
                Text(code, style: const TextStyle(fontFamily: 'Cairo', fontSize: 11, fontWeight: FontWeight.w700, color: Color(0xFF94A3B8))),
                Text(title, style: const TextStyle(fontFamily: 'Cairo', fontWeight: FontWeight.w700, fontSize: 13, color: Color(0xFF0A2540))),
              ],
            ),
          ),
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
            decoration: BoxDecoration(color: statusBg, borderRadius: BorderRadius.circular(6)),
            child: Text(status, style: TextStyle(fontFamily: 'Cairo', fontSize: 11, fontWeight: FontWeight.bold, color: statusColor)),
          ),
        ],
      ),
    );
  }
}

// -----------------------------------------------------------------------------
// 3. TASKS LIST SCREEN (قائمة المهام)
// -----------------------------------------------------------------------------
class MarsoudTasksListScreen extends StatefulWidget {
  const MarsoudTasksListScreen({super.key});

  @override
  State<MarsoudTasksListScreen> createState() => _MarsoudTasksListScreenState();
}

class _MarsoudTasksListScreenState extends State<MarsoudTasksListScreen> {
  int _selectedFilter = 0;
  final _filters = ['الكل (14)', 'قيد التنفيذ (6)', 'مراجعة (3)', 'مكتملة (5)'];

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
          title: const Text('قائمة المهام', style: TextStyle(fontFamily: 'Cairo', fontWeight: FontWeight.w800, fontSize: 17, color: Color(0xFF0A2540))),
          actions: [
            IconButton(icon: const Icon(Icons.tune_rounded, color: Color(0xFF0A2540)), onPressed: () {}),
          ],
        ),
        body: Column(
          children: [
            // Filter Pills Ribbon
            Container(
              color: Colors.white,
              height: 48,
              padding: const EdgeInsets.symmetric(horizontal: 16),
              child: ListView.separated(
                scrollDirection: Axis.horizontal,
                itemCount: _filters.length,
                separatorBuilder: (_, __) => const SizedBox(width: 8),
                itemBuilder: (context, i) {
                  final isSelected = _selectedFilter == i;
                  return ChoiceChip(
                    label: Text(_filters[i]),
                    selected: isSelected,
                    onSelected: (_) => setState(() => _selectedFilter = i),
                    selectedColor: const Color(0xFF059669),
                    backgroundColor: const Color(0xFFF1F5F9),
                    labelStyle: TextStyle(
                      fontFamily: 'Cairo',
                      fontSize: 12,
                      fontWeight: isSelected ? FontWeight.w700 : FontWeight.w500,
                      color: isSelected ? Colors.white : const Color(0xFF475569),
                    ),
                    shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(20), side: BorderSide.none),
                  );
                },
              ),
            ),
            const Divider(height: 1, color: Color(0xFFE2E8F0)),

            // Tasks List
            Expanded(
              child: ListView(
                padding: const EdgeInsets.all(16),
                children: [
                  _taskCard(
                    title: 'ربط بوابة الدفع ومدفوعات سداد',
                    project: 'مشروع بوابة التوريد الرقمية',
                    due: 'اليوم • أولوية قصوى',
                    progress: 0.9,
                    progressLabel: '90%',
                    priorityColor: const Color(0xFFEF4444),
                  ),
                  _taskCard(
                    title: 'اختبارات الأمان واختراق البيانات السحابية',
                    project: 'الربط الضريبي ZATCA 2',
                    due: '28 أكتوبر • أولوية متوسطة',
                    progress: 0.35,
                    progressLabel: '35%',
                    priorityColor: const Color(0xFFD97706),
                  ),
                  _taskCard(
                    title: 'مراجعة بنود عقد التوريد مع الفريق القانوني',
                    project: 'استشارات شركة الأفق',
                    due: '30 أكتوبر • أولوية عادية',
                    progress: 0.6,
                    progressLabel: '60%',
                    priorityColor: const Color(0xFF3B82F6),
                  ),
                ],
              ),
            ),
          ],
        ),
        floatingActionButton: FloatingActionButton.extended(
          onPressed: () {},
          backgroundColor: const Color(0xFF059669),
          icon: const Icon(Icons.add_task_rounded, color: Colors.white),
          label: const Text('مهمة جديدة', style: TextStyle(fontFamily: 'Cairo', fontWeight: FontWeight.w700, color: Colors.white)),
        ),
      ),
    );
  }

  static Widget _taskCard({
    required String title,
    required String project,
    required String due,
    required double progress,
    required String progressLabel,
    required Color priorityColor,
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
              Text(project, style: const TextStyle(fontFamily: 'Cairo', fontSize: 11.5, color: Color(0xFF64748B))),
              Container(
                width: 8,
                height: 8,
                decoration: BoxDecoration(color: priorityColor, shape: BoxShape.circle),
              ),
            ],
          ),
          const SizedBox(height: 6),
          Text(title, style: const TextStyle(fontFamily: 'Cairo', fontWeight: FontWeight.w800, fontSize: 14, color: Color(0xFF0A2540))),
          const SizedBox(height: 12),
          // Progress bar
          Row(
            children: [
              Expanded(
                child: ClipRRect(
                  borderRadius: BorderRadius.circular(4),
                  child: LinearProgressIndicator(
                    value: progress,
                    minHeight: 6,
                    backgroundColor: const Color(0xFFF1F5F9),
                    valueColor: const AlwaysStoppedAnimation<Color>(Color(0xFF059669)),
                  ),
                ),
              ),
              const SizedBox(width: 10),
              Text(progressLabel, style: const TextStyle(fontFamily: 'Cairo', fontSize: 11, fontWeight: FontWeight.bold, color: Color(0xFF059669))),
            ],
          ),
          const Divider(height: 20, color: Color(0xFFF1F5F9)),
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              Text(due, style: TextStyle(fontFamily: 'Cairo', fontSize: 11.5, fontWeight: FontWeight.w600, color: priorityColor)),
              // Initials avatar for assignee (NO realistic photo)
              Container(
                width: 26,
                height: 26,
                alignment: Alignment.center,
                decoration: const BoxDecoration(color: Color(0xFFECFDF5), shape: BoxShape.circle),
                child: const Text('س.م', style: TextStyle(fontFamily: 'Cairo', fontSize: 10, fontWeight: FontWeight.bold, color: Color(0xFF059669))),
              ),
            ],
          ),
        ],
      ),
    );
  }
}

// -----------------------------------------------------------------------------
// 4. TASK DETAIL SCREEN (تفاصيل المهمة) - No realistic photos, 32x32 initials
// -----------------------------------------------------------------------------
class MarsoudTaskDetailScreen extends StatelessWidget {
  const MarsoudTaskDetailScreen({super.key});

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
          title: const Text('تفاصيل المهمة', style: TextStyle(fontFamily: 'Cairo', fontWeight: FontWeight.w800, fontSize: 17, color: Color(0xFF0A2540))),
          actions: [
            IconButton(icon: const Icon(Icons.more_vert_rounded, color: Color(0xFF0A2540)), onPressed: () {}),
          ],
        ),
        body: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            // Task Title & Status
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
                      Container(
                        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                        decoration: BoxDecoration(color: const Color(0xFFFEF2F2), borderRadius: BorderRadius.circular(6)),
                        child: const Text('أولوية قصوى', style: TextStyle(fontFamily: 'Cairo', fontSize: 11, fontWeight: FontWeight.bold, color: Color(0xFFEF4444))),
                      ),
                      Container(
                        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                        decoration: BoxDecoration(color: const Color(0xFFECFDF5), borderRadius: BorderRadius.circular(6)),
                        child: const Text('قيد التنفيذ (90%)', style: TextStyle(fontFamily: 'Cairo', fontSize: 11, fontWeight: FontWeight.bold, color: Color(0xFF059669))),
                      ),
                    ],
                  ),
                  const SizedBox(height: 12),
                  const Text('ربط بوابة الدفع ومدفوعات سداد', style: TextStyle(fontFamily: 'Cairo', fontWeight: FontWeight.w800, fontSize: 16, color: Color(0xFF0A2540))),
                  const SizedBox(height: 4),
                  const Text('المشروع: بوابة التوريد الرقمية • الموعد النهائي: اليوم، 05:00 م', style: TextStyle(fontFamily: 'Cairo', fontSize: 12, color: Color(0xFF64748B))),
                ],
              ),
            ),
            const SizedBox(height: 16),

            // Assignees - Strictly 32x32 initials avatars (NO photos)
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
                  const Text('فريق العمل المكلّف', style: TextStyle(fontFamily: 'Cairo', fontWeight: FontWeight.w800, fontSize: 14, color: Color(0xFF0A2540))),
                  const SizedBox(height: 12),
                  Row(
                    children: [
                      _initialsAvatar('س.م', 'سلمان المطيري', 'المسؤول المباشر'),
                      const SizedBox(width: 16),
                      _initialsAvatar('ف.س', 'فهد السبيعي', 'مشرف المشروع'),
                      const SizedBox(width: 16),
                      _initialsAvatar('ر.ق', 'ريم القحطاني', 'فحص الجودة'),
                    ],
                  ),
                ],
              ),
            ),
            const SizedBox(height: 16),

            // Attachments - Clean UI file chips (NO brochure previews)
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
                  const Text('المرفقات والملفات الفنية', style: TextStyle(fontFamily: 'Cairo', fontWeight: FontWeight.w800, fontSize: 14, color: Color(0xFF0A2540))),
                  const SizedBox(height: 10),
                  _attachmentRow('sadad_integration_v2.pdf', '2.4 MB • وثيقة الربط المعتمدة', Icons.picture_as_pdf_rounded, const Color(0xFFEF4444)),
                  _attachmentRow('api_keys_spec.json', '48 KB • مفاتيح الربط والـ Endpoints', Icons.code_rounded, const Color(0xFF2563EB)),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }

  static Widget _initialsAvatar(String initials, String name, String role) {
    return Row(
      children: [
        Container(
          width: 32,
          height: 32,
          alignment: Alignment.center,
          decoration: BoxDecoration(
            color: const Color(0xFFECFDF5),
            shape: BoxShape.circle,
            border: Border.all(color: const Color(0xFF059669).withOpacity(0.3)),
          ),
          child: Text(initials, style: const TextStyle(fontFamily: 'Cairo', fontSize: 11, fontWeight: FontWeight.w800, color: Color(0xFF059669))),
        ),
        const SizedBox(width: 8),
        Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(name, style: const TextStyle(fontFamily: 'Cairo', fontWeight: FontWeight.w700, fontSize: 12, color: Color(0xFF0A2540))),
            Text(role, style: const TextStyle(fontFamily: 'Cairo', fontSize: 10, color: Color(0xFF64748B))),
          ],
        ),
      ],
    );
  }

  static Widget _attachmentRow(String name, String meta, IconData icon, Color color) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 8.0),
      child: Row(
        children: [
          Container(
            padding: const EdgeInsets.all(8),
            decoration: BoxDecoration(color: color.withOpacity(0.1), borderRadius: BorderRadius.circular(8)),
            child: Icon(icon, color: color, size: 18),
          ),
          const SizedBox(width: 10),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(name, style: const TextStyle(fontFamily: 'Cairo', fontWeight: FontWeight.w700, fontSize: 12.5, color: Color(0xFF0A2540))),
                Text(meta, style: const TextStyle(fontFamily: 'Cairo', fontSize: 11, color: Color(0xFF64748B))),
              ],
            ),
          ),
          IconButton(icon: const Icon(Icons.download_rounded, size: 20, color: Color(0xFF64748B)), onPressed: () {}),
        ],
      ),
    );
  }
}

// -----------------------------------------------------------------------------
// 5. NEW TASK SCREEN (مهمة جديدة)
// -----------------------------------------------------------------------------
class MarsoudNewTaskScreen extends StatelessWidget {
  const MarsoudNewTaskScreen({super.key});

  @override
  Widget build(BuildContext context) {
    return Directionality(
      textDirection: TextDirection.rtl,
      child: Scaffold(
        backgroundColor: Colors.white,
        appBar: AppBar(
          backgroundColor: Colors.white,
          elevation: 0,
          leading: IconButton(
            icon: const Icon(Icons.arrow_forward_ios_rounded, size: 20, color: Color(0xFF0A2540)),
            onPressed: () => Navigator.of(context).maybePop(),
          ),
          title: const Text('مهمة جديدة', style: TextStyle(fontFamily: 'Cairo', fontWeight: FontWeight.w800, fontSize: 17, color: Color(0xFF0A2540))),
        ),
        body: SingleChildScrollView(
          padding: const EdgeInsets.all(20),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const Text('عنوان المهمة *', style: TextStyle(fontFamily: 'Cairo', fontSize: 13, fontWeight: FontWeight.w700, color: Color(0xFF0A2540))),
              const SizedBox(height: 8),
              TextFormField(
                decoration: InputDecoration(
                  hintText: 'اكتب عنواناً واضحاً للمهمة...',
                  hintStyle: const TextStyle(fontFamily: 'Cairo', fontSize: 13, color: Color(0xFF94A3B8)),
                  filled: true,
                  fillColor: const Color(0xFFF8FAFC),
                  border: OutlineInputBorder(borderRadius: BorderRadius.circular(12), borderSide: const BorderSide(color: Color(0xFFE2E8F0))),
                ),
              ),
              const SizedBox(height: 16),

              const Text('المشروع المرتبط', style: TextStyle(fontFamily: 'Cairo', fontSize: 13, fontWeight: FontWeight.w700, color: Color(0xFF0A2540))),
              const SizedBox(height: 8),
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
                decoration: BoxDecoration(color: const Color(0xFFF8FAFC), borderRadius: BorderRadius.circular(12), border: Border.all(color: const Color(0xFFE2E8F0))),
                child: Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: const [
                    Text('بوابة التوريد الرقمية', style: TextStyle(fontFamily: 'Cairo', fontSize: 13, color: Color(0xFF0A2540))),
                    Icon(Icons.keyboard_arrow_down_rounded, color: Color(0xFF64748B)),
                  ],
                ),
              ),
              const SizedBox(height: 16),

              const Text('المسؤول المباشر والتكليف', style: TextStyle(fontFamily: 'Cairo', fontSize: 13, fontWeight: FontWeight.w700, color: Color(0xFF0A2540))),
              const SizedBox(height: 8),
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
                decoration: BoxDecoration(color: const Color(0xFFF8FAFC), borderRadius: BorderRadius.circular(12), border: Border.all(color: const Color(0xFFE2E8F0))),
                child: Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: const [
                    Text('سلمان المطيري (مدير العمليات)', style: TextStyle(fontFamily: 'Cairo', fontSize: 13, color: Color(0xFF0A2540))),
                    Icon(Icons.person_outline_rounded, color: Color(0xFF64748B), size: 20),
                  ],
                ),
              ),
              const SizedBox(height: 24),

              ElevatedButton(
                onPressed: () => Navigator.of(context).maybePop(),
                style: ElevatedButton.styleFrom(
                  backgroundColor: const Color(0xFF059669),
                  minimumSize: const Size.fromHeight(48),
                  shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
                ),
                child: const Text('إنشاء وإسناد المهمة', style: TextStyle(fontFamily: 'Cairo', fontSize: 15, fontWeight: FontWeight.w700, color: Colors.white)),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

// -----------------------------------------------------------------------------
// 6. TASK ARCHIVE SCREEN (أرشيف المهام)
// -----------------------------------------------------------------------------
class MarsoudTaskArchiveScreen extends StatelessWidget {
  const MarsoudTaskArchiveScreen({super.key});

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
          title: const Text('أرشيف المهام', style: TextStyle(fontFamily: 'Cairo', fontWeight: FontWeight.w800, fontSize: 17, color: Color(0xFF0A2540))),
        ),
        body: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            _archivedItem('تهيئة شهادة الربط ZATCA للمرحلة التجريبية', 'أُغلقت في 15 أكتوبر 2024 • مكتملة 100%'),
            _archivedItem('تسوية العهدة النقدية لشهر سبتمبر', 'أُغلقت في 30 سبتمبر 2024 • معتمدة'),
            _archivedItem('إعداد خطة التوظيف للفصل الرابع', 'أُغلقت في 25 سبتمبر 2024 • مكتملة 100%'),
          ],
        ),
      ),
    );
  }

  static Widget _archivedItem(String title, String meta) {
    return Container(
      margin: const EdgeInsets.only(bottom: 10),
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: const Color(0xFFE2E8F0)),
      ),
      child: Row(
        children: [
          const Icon(Icons.archive_outlined, color: Color(0xFF94A3B8), size: 22),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(title, style: const TextStyle(fontFamily: 'Cairo', fontWeight: FontWeight.w700, fontSize: 13, color: Color(0xFF0A2540))),
                const SizedBox(height: 2),
                Text(meta, style: const TextStyle(fontFamily: 'Cairo', fontSize: 11, color: Color(0xFF64748B))),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

// -----------------------------------------------------------------------------
// 7. PROJECTS LIST SCREEN (قائمة المشاريع)
// -----------------------------------------------------------------------------
class MarsoudProjectsListScreen extends StatelessWidget {
  const MarsoudProjectsListScreen({super.key});

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
          title: const Text('قائمة المشاريع', style: TextStyle(fontFamily: 'Cairo', fontWeight: FontWeight.w800, fontSize: 17, color: Color(0xFF0A2540))),
        ),
        body: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            // KPI Summary Row
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
              decoration: BoxDecoration(
                color: Colors.white,
                borderRadius: BorderRadius.circular(14),
                border: Border.all(color: const Color(0xFFE2E8F0)),
              ),
              child: Row(
                mainAxisAlignment: MainAxisAlignment.spaceAround,
                children: [
                  _statItem('المشاريع النشطة', '6', const Color(0xFF059669)),
                  Container(height: 28, width: 1, color: const Color(0xFFE2E8F0)),
                  _statItem('المشاريع المنجزة', '14', const Color(0xFF0A2540)),
                  Container(height: 28, width: 1, color: const Color(0xFFE2E8F0)),
                  _statItem('معدل الالتزام', '94%', const Color(0xFF2563EB)),
                ],
              ),
            ),
            const SizedBox(height: 16),

            // Projects Cards
            _projectCard(
              title: 'بوابة التوريد الرقمية (09-Gov)',
              client: 'شركة الأفق للأعمال والتجارة',
              progress: 0.68,
              progressLabel: '68%',
              daysLeft: 'متبقي 52 يوماً',
              tag: 'قيد التنفيذ',
              tagColor: const Color(0xFF059669),
              tagBg: const Color(0xFFECFDF5),
            ),
            _projectCard(
              title: 'تطبيق مرصود موبايل v2',
              client: 'إدارة التطوير والمنتجات',
              progress: 0.68,
              progressLabel: '68%',
              daysLeft: '30 نوفمبر 2024',
              tag: 'قيد التنفيذ',
              tagColor: const Color(0xFF059669),
              tagBg: const Color(0xFFECFDF5),
            ),
            _projectCard(
              title: 'الربط الضريبي ZATCA 2',
              client: 'الإدارة المالية والضرائب',
              progress: 0.85,
              progressLabel: '85%',
              daysLeft: '31 أكتوبر 2024',
              tag: 'مرحلة ختامية',
              tagColor: const Color(0xFF2563EB),
              tagBg: const Color(0xFFEFF6FF),
            ),
          ],
        ),
        floatingActionButton: FloatingActionButton.extended(
          onPressed: () {},
          backgroundColor: const Color(0xFF059669),
          icon: const Icon(Icons.add_rounded, color: Colors.white),
          label: const Text('مشروع جديد', style: TextStyle(fontFamily: 'Cairo', fontWeight: FontWeight.w700, color: Colors.white)),
        ),
      ),
    );
  }

  static Widget _statItem(String label, String val, Color col) {
    return Column(
      children: [
        Text(val, style: TextStyle(fontFamily: 'Cairo', fontSize: 16, fontWeight: FontWeight.w900, color: col)),
        Text(label, style: const TextStyle(fontFamily: 'Cairo', fontSize: 11, color: Color(0xFF64748B))),
      ],
    );
  }

  static Widget _projectCard({
    required String title,
    required String client,
    required double progress,
    required String progressLabel,
    required String daysLeft,
    required String tag,
    required Color tagColor,
    required Color tagBg,
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
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                decoration: BoxDecoration(color: tagBg, borderRadius: BorderRadius.circular(6)),
                child: Text(tag, style: TextStyle(fontFamily: 'Cairo', fontSize: 11, fontWeight: FontWeight.bold, color: tagColor)),
              ),
              Text(daysLeft, style: const TextStyle(fontFamily: 'Cairo', fontSize: 11.5, color: Color(0xFF64748B))),
            ],
          ),
          const SizedBox(height: 8),
          Text(title, style: const TextStyle(fontFamily: 'Cairo', fontWeight: FontWeight.w800, fontSize: 14.5, color: Color(0xFF0A2540))),
          const SizedBox(height: 2),
          Text(client, style: const TextStyle(fontFamily: 'Cairo', fontSize: 12, color: Color(0xFF64748B))),
          const SizedBox(height: 12),
          ClipRRect(
            borderRadius: BorderRadius.circular(4),
            child: LinearProgressIndicator(
              value: progress,
              minHeight: 6,
              backgroundColor: const Color(0xFFF1F5F9),
              valueColor: const AlwaysStoppedAnimation<Color>(Color(0xFF059669)),
            ),
          ),
        ],
      ),
    );
  }
}

// -----------------------------------------------------------------------------
// 8. PROJECT DETAIL SCREEN (تفاصيل المشروع)
// -----------------------------------------------------------------------------
class MarsoudProjectDetailScreen extends StatelessWidget {
  const MarsoudProjectDetailScreen({super.key});

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
          title: const Text('تفاصيل المشروع', style: TextStyle(fontFamily: 'Cairo', fontWeight: FontWeight.w800, fontSize: 17, color: Color(0xFF0A2540))),
          actions: [
            IconButton(icon: const Icon(Icons.share_outlined, color: Color(0xFF0A2540)), onPressed: () {}),
          ],
        ),
        body: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            // Project Header Card
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
                  Text('بوابة التوريد الرقمية (09-Gov)', style: TextStyle(fontFamily: 'Cairo', fontWeight: FontWeight.w900, fontSize: 16, color: Color(0xFF0A2540))),
                  SizedBox(height: 4),
                  Text('الجهة: شركة الأفق للأعمال والتجارة • الحالة: نشط وقيد التنفيذ', style: TextStyle(fontFamily: 'Cairo', fontSize: 12, color: Color(0xFF64748B))),
                  Divider(height: 24, color: Color(0xFFF1F5F9)),
                  Text('نسبة الإنجاز الإجمالية: 68% (24 من 35 مهمة منجزة)', style: TextStyle(fontFamily: 'Cairo', fontSize: 12.5, fontWeight: FontWeight.w700, color: Color(0xFF059669))),
                ],
              ),
            ),
            const SizedBox(height: 16),

            // Health Card
            Container(
              padding: const EdgeInsets.all(14),
              decoration: BoxDecoration(
                color: const Color(0xFFECFDF5),
                borderRadius: BorderRadius.circular(12),
                border: Border.all(color: const Color(0xFF059669).withOpacity(0.25)),
              ),
              child: Row(
                children: const [
                  Icon(Icons.health_and_safety_rounded, color: Color(0xFF059669), size: 22),
                  SizedBox(width: 10),
                  Expanded(
                    child: Text('🟢 مستقر ومنتظم (On Track) — لا توجد مخاطر تشغيلية أو انحرافات في الموازنة المعتمدة',
                        style: TextStyle(fontFamily: 'Cairo', fontSize: 12, fontWeight: FontWeight.w700, color: Color(0xFF047857))),
                  ),
                ],
              ),
            ),
            const SizedBox(height: 16),

            // Budget Summary
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
                  const Text('الميزانية والسيولة المصروفة', style: TextStyle(fontFamily: 'Cairo', fontWeight: FontWeight.w800, fontSize: 14, color: Color(0xFF0A2540))),
                  const SizedBox(height: 12),
                  Row(
                    mainAxisAlignment: MainAxisAlignment.spaceBetween,
                    children: const [
                      _MoneyItem('إجمالي الميزانية', '450,000 ر.س'),
                      _MoneyItem('المصروف الفعلي', '285,000 ر.س'),
                      _MoneyItem('الرصيد المتبقي', '165,000 ر.س'),
                    ],
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _MoneyItem extends StatelessWidget {
  final String label;
  final String amount;
  const _MoneyItem(this.label, this.amount);

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(label, style: const TextStyle(fontFamily: 'Cairo', fontSize: 11, color: Color(0xFF64748B))),
        const SizedBox(height: 2),
        Text(amount, style: const TextStyle(fontFamily: 'Cairo', fontSize: 13, fontWeight: FontWeight.w800, color: Color(0xFF0A2540))),
      ],
    );
  }
}

// -----------------------------------------------------------------------------
// 9. EMPTY STATE SYSTEM (حالات النظام الفارغة)
// -----------------------------------------------------------------------------
class MarsoudEmptyStateWidget extends StatelessWidget {
  final IconData icon;
  final String title;
  final String message;
  final String? buttonLabel;
  final VoidCallback? onAction;

  const MarsoudEmptyStateWidget({
    super.key,
    required this.icon,
    required this.title,
    required this.message,
    this.buttonLabel,
    this.onAction,
  });

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(28.0),
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Container(
              width: 80,
              height: 80,
              decoration: const BoxDecoration(
                color: Color(0xFFF1F5F9),
                shape: BoxShape.circle,
              ),
              child: Icon(icon, size: 40, color: const Color(0xFF94A3B8)),
            ),
            const SizedBox(height: 20),
            Text(title, style: const TextStyle(fontFamily: 'Cairo', fontSize: 17, fontWeight: FontWeight.w800, color: Color(0xFF0A2540))),
            const SizedBox(height: 6),
            Text(
              message,
              textAlign: TextAlign.center,
              style: const TextStyle(fontFamily: 'Cairo', fontSize: 13, color: Color(0xFF64748B), height: 1.5),
            ),
            if (buttonLabel != null) ...[
              const SizedBox(height: 24),
              ElevatedButton(
                onPressed: onAction,
                style: ElevatedButton.styleFrom(
                  backgroundColor: const Color(0xFF059669),
                  padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 12),
                  shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
                ),
                child: Text(buttonLabel!, style: const TextStyle(fontFamily: 'Cairo', fontSize: 14, fontWeight: FontWeight.w700, color: Colors.white)),
              ),
            ],
          ],
        ),
      ),
    );
  }
}
