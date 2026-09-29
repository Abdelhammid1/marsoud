import 'package:flutter/material.dart';

/// ============================================================================
/// MARSOUD ERP - SALES & CRM SCREENS (Flutter Dart Widgets)
/// ============================================================================
/// Screens included:
/// 1. Leads List (قائمة العملاء المحتملين)
/// 2. Lead Detail (تفاصيل العميل المحتمل)
/// 3. Add Activity Sheet (نموذج إضافة نشاط / تفاعل)
/// 4. Meetings List (قائمة الاجتماعات المبيعية)
/// 5. New Meeting Sheet (نموذج جدولة اجتماع جديد)
/// 6. Schedule (جدولي والمواعيد الأسبوعية)
/// ============================================================================

// -----------------------------------------------------------------------------
// 1. LEADS LIST SCREEN (قائمة العملاء المحتملين)
// -----------------------------------------------------------------------------
class MarsoudLeadsListScreen extends StatefulWidget {
  const MarsoudLeadsListScreen({super.key});

  @override
  State<MarsoudLeadsListScreen> createState() => _MarsoudLeadsListScreenState();
}

class _MarsoudLeadsListScreenState extends State<MarsoudLeadsListScreen> {
  int _selectedFilterIndex = 0;
  final List<String> _filters = ['الكل (28)', 'مؤهل (12)', 'تفاوض (8)', 'عقد معتمد (5)', 'مستبعد (3)'];

  final List<Map<String, dynamic>> _leads = [
    {
      'name': 'شركة الأفق للاستشارات المالية',
      'contactPerson': 'عبدالله السعدون',
      'role': 'المدير المالي التنفيذي',
      'dealValue': '85,000 ر.س',
      'stage': 'تفاوض متقدم',
      'stageColor': Color(0xFF2563EB),
      'stageBg': Color(0xFFEFF6FF),
      'priority': 'أولوية قصوى',
      'priorityColor': Color(0xFFEF4444),
      'initials': 'أ.ف',
      'lastActivity': 'مكالمة هاتفية • منذ 3 ساعات',
    },
    {
      'name': 'مجموعة رواسي نجد للتجارة والمقاولات',
      'contactPerson': 'م. فهد التميمي',
      'role': 'رئيس المشتريات',
      'dealValue': '140,000 ر.س',
      'stage': 'عرض سعر معتمد',
      'stageColor': Color(0xFF059669),
      'stageBg': Color(0xFFECFDF5),
      'priority': 'متوسطة',
      'priorityColor': Color(0xFF3B82F6),
      'initials': 'ر.ن',
      'lastActivity': 'إرسال مسودة العقد • أمس',
    },
    {
      'name': 'مؤسسة إمداد الحلول اللوجستية',
      'contactPerson': 'سارة الشريف',
      'role': 'مديرة العمليات',
      'dealValue': '52,000 ر.س',
      'stage': 'مؤهل مبدئياً',
      'stageColor': Color(0xFFD97706),
      'stageBg': Color(0xFFFFFBEB),
      'priority': 'عادية',
      'priorityColor': Color(0xFF64748B),
      'initials': 'إ.ح',
      'lastActivity': 'اجتماع تعريفي • 22 أكتوبر',
    },
    {
      'name': 'شركة الركائز المتحدة للتقنية',
      'contactPerson': 'خالد البواردي',
      'role': 'الرئيس التنفيذي',
      'dealValue': '210,000 ر.س',
      'stage': 'مراجعة قانونية',
      'stageColor': Color(0xFF7C3AED),
      'stageBg': Color(0xFFF5F3FF),
      'priority': 'أولوية قصوى',
      'priorityColor': Color(0xFFEF4444),
      'initials': 'ر.م',
      'lastActivity': 'تحديث شروط الدفع • 20 أكتوبر',
    },
  ];

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
          title: const Text(
            'قائمة العملاء المحتملين',
            style: TextStyle(fontWeight: FontWeight.w800, fontSize: 17, color: Color(0xFF0A2540)),
          ),
          actions: [
            IconButton(
              icon: const Icon(Icons.tune_rounded, color: Color(0xFF0A2540)),
              onPressed: () {},
            ),
          ],
        ),
        body: Column(
          children: [
            // Search Bar & Filter chips
            Container(
              color: Colors.white,
              padding: const EdgeInsets.fromLTRB(16, 8, 16, 16),
              child: Column(
                children: [
                  TextField(
                    decoration: InputDecoration(
                      hintText: 'ابحث بالشركة، الاسم، أو رقم المعاملة...',
                      hintStyle: const TextStyle(fontSize: 13, color: Color(0xFF94A3B8)),
                      prefixIcon: const Icon(Icons.search_rounded, color: Color(0xFF64748B)),
                      filled: true,
                      fillColor: const Color(0xFFF8FAFC),
                      contentPadding: const EdgeInsets.symmetric(vertical: 10),
                      border: OutlineInputBorder(borderRadius: BorderRadius.circular(12), borderSide: const BorderSide(color: Color(0xFFE2E8F0))),
                      enabledBorder: OutlineInputBorder(borderRadius: BorderRadius.circular(12), borderSide: const BorderSide(color: Color(0xFFE2E8F0))),
                    ),
                  ),
                  const SizedBox(height: 12),
                  SizedBox(
                    height: 34,
                    child: ListView.separated(
                      scrollDirection: Axis.horizontal,
                      itemCount: _filters.length,
                      separatorBuilder: (_, __) => const SizedBox(width: 8),
                      itemBuilder: (context, index) {
                        final isSelected = _selectedFilterIndex == index;
                        return ChoiceChip(
                          label: Text(_filters[index]),
                          selected: isSelected,
                          onSelected: (_) => setState(() => _selectedFilterIndex = index),
                          selectedColor: const Color(0xFF059669),
                          backgroundColor: const Color(0xFFF1F5F9),
                          labelStyle: TextStyle(                            fontSize: 12,
                            fontWeight: isSelected ? FontWeight.w700 : FontWeight.w500,
                            color: isSelected ? Colors.white : const Color(0xFF475569),
                          ),
                          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(20), side: BorderSide.none),
                          padding: const EdgeInsets.symmetric(horizontal: 10),
                        );
                      },
                    ),
                  ),
                ],
              ),
            ),

            // Pipeline Summary Metric Bar
            Container(
              margin: const EdgeInsets.all(16),
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
              decoration: BoxDecoration(
                color: Colors.white,
                borderRadius: BorderRadius.circular(14),
                border: Border.all(color: const Color(0xFFE2E8F0)),
              ),
              child: Row(
                mainAxisAlignment: MainAxisAlignment.spaceAround,
                children: [
                  _statItem('إجمالي القيمة', '487,000 ر.س', const Color(0xFF059669)),
                  Container(height: 30, width: 1, color: const Color(0xFFE2E8F0)),
                  _statItem('الفرص النشطة', '28 فرصة', const Color(0xFF0A2540)),
                  Container(height: 30, width: 1, color: const Color(0xFFE2E8F0)),
                  _statItem('معدل التحويل', '32.4%', const Color(0xFF2563EB)),
                ],
              ),
            ),

            // Leads Cards List
            Expanded(
              child: ListView.separated(
                padding: const EdgeInsets.symmetric(horizontal: 16),
                itemCount: _leads.length,
                separatorBuilder: (_, __) => const SizedBox(height: 12),
                itemBuilder: (context, index) {
                  final lead = _leads[index];
                  return Container(
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
                          children: [
                            Container(
                              width: 42,
                              height: 42,
                              alignment: Alignment.center,
                              decoration: BoxDecoration(
                                color: const Color(0xFFECFDF5),
                                borderRadius: BorderRadius.circular(10),
                              ),
                              child: Text(
                                lead['initials'] as String,
                                style: const TextStyle(fontWeight: FontWeight.w800, color: Color(0xFF059669), fontSize: 14),
                              ),
                            ),
                            const SizedBox(width: 12),
                            Expanded(
                              child: Column(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                  Text(
                                    lead['name'] as String,
                                    style: const TextStyle(fontWeight: FontWeight.w800, fontSize: 14, color: Color(0xFF0A2540)),
                                  ),
                                  const SizedBox(height: 2),
                                  Text(
                                    '${lead['contactPerson']} • ${lead['role']}',
                                    style: const TextStyle(fontSize: 12, color: Color(0xFF64748B)),
                                  ),
                                ],
                              ),
                            ),
                            Container(
                              padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                              decoration: BoxDecoration(color: lead['stageBg'] as Color, borderRadius: BorderRadius.circular(6)),
                              child: Text(
                                lead['stage'] as String,
                                style: TextStyle(fontSize: 11, fontWeight: FontWeight.w700, color: lead['stageColor'] as Color),
                              ),
                            ),
                          ],
                        ),
                        const Divider(height: 24, color: Color(0xFFF1F5F9)),
                        Row(
                          mainAxisAlignment: MainAxisAlignment.spaceBetween,
                          children: [
                            Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                const Text('قيمة الصفقة المقدرة', style: TextStyle(fontSize: 11, color: Color(0xFF94A3B8))),
                                Text(
                                  lead['dealValue'] as String,
                                  style: const TextStyle(fontSize: 15, fontWeight: FontWeight.w900, color: Color(0xFF059669)),
                                ),
                              ],
                            ),
                            Text(
                              lead['lastActivity'] as String,
                              style: const TextStyle(fontSize: 11, color: Color(0xFF64748B)),
                            ),
                          ],
                        ),
                      ],
                    ),
                  );
                },
              ),
            ),
          ],
        ),
        floatingActionButton: FloatingActionButton.extended(
          onPressed: () {},
          backgroundColor: const Color(0xFF059669),
          icon: const Icon(Icons.person_add_alt_1_rounded, color: Colors.white),
          label: const Text('عميل محتمل جديد', style: TextStyle(fontWeight: FontWeight.w700, color: Colors.white)),
        ),
      ),
    );
  }

  Widget _statItem(String label, String value, Color color) {
    return Column(
      children: [
        Text(value, style: TextStyle(fontSize: 14, fontWeight: FontWeight.w900, color: color)),
        const SizedBox(height: 2),
        Text(label, style: const TextStyle(fontSize: 11, color: Color(0xFF64748B))),
      ],
    );
  }
}

// -----------------------------------------------------------------------------
// 2. LEAD DETAIL SCREEN (تفاصيل العميل المحتمل)
// -----------------------------------------------------------------------------
class MarsoudLeadDetailScreen extends StatelessWidget {
  const MarsoudLeadDetailScreen({super.key});

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
          title: const Text('تفاصيل العميل', style: TextStyle(fontWeight: FontWeight.w800, fontSize: 17, color: Color(0xFF0A2540))),
          actions: [
            IconButton(icon: const Icon(Icons.share_outlined, color: Color(0xFF0A2540)), onPressed: () {}),
            IconButton(icon: const Icon(Icons.more_vert_rounded, color: Color(0xFF0A2540)), onPressed: () {}),
          ],
        ),
        body: SingleChildScrollView(
          padding: const EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              // Lead Profile Header Card
              Container(
                padding: const EdgeInsets.all(16),
                decoration: BoxDecoration(
                  color: Colors.white,
                  borderRadius: BorderRadius.circular(16),
                  border: Border.all(color: const Color(0xFFE2E8F0)),
                ),
                child: Column(
                  children: [
                    Row(
                      children: [
                        // Initials Avatar (NO realistic photos)
                        Container(
                          width: 54,
                          height: 54,
                          alignment: Alignment.center,
                          decoration: BoxDecoration(
                            color: const Color(0xFFECFDF5),
                            borderRadius: BorderRadius.circular(14),
                            border: Border.all(color: const Color(0xFF059669).withOpacity(0.2)),
                          ),
                          child: const Text('أ.ف', style: TextStyle(fontSize: 18, fontWeight: FontWeight.w900, color: Color(0xFF059669))),
                        ),
                        const SizedBox(width: 14),
                        Expanded(
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: const [
                              Text('شركة الأفق للاستشارات المالية', style: TextStyle(fontSize: 15, fontWeight: FontWeight.w800, color: Color(0xFF0A2540))),
                              SizedBox(height: 2),
                              Text('سجل تجاري: 1010884920 • الرياض', style: TextStyle(fontSize: 12, color: Color(0xFF64748B))),
                            ],
                          ),
                        ),
                      ],
                    ),
                    const Divider(height: 24, color: Color(0xFFF1F5F9)),
                    // Quick Action Buttons
                    Row(
                      children: [
                        _actionCircleBtn(Icons.phone_rounded, 'اتصال', () {}),
                        const SizedBox(width: 8),
                        _actionCircleBtn(Icons.chat_bubble_outline_rounded, 'واتساب', () {}),
                        const SizedBox(width: 8),
                        _actionCircleBtn(Icons.alternate_email_rounded, 'بريد', () {}),
                        const SizedBox(width: 8),
                        _actionCircleBtn(Icons.add_task_rounded, 'مهمة', () {}),
                      ],
                    ),
                  ],
                ),
              ),
              const SizedBox(height: 16),

              // Deal Stage Tracker
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
                      children: const [
                        Text('مرحلة الصفقة الحالية', style: TextStyle(fontWeight: FontWeight.w800, fontSize: 14, color: Color(0xFF0A2540))),
                        Text('85,000 ر.س', style: TextStyle(fontWeight: FontWeight.w900, fontSize: 16, color: Color(0xFF059669))),
                      ],
                    ),
                    const SizedBox(height: 16),
                    // Progress Stepper Pills
                    Row(
                      children: [
                        _stageStep('تأهيل', true, true),
                        _stageStep('عرض سعر', true, true),
                        _stageStep('تفاوض', true, false),
                        _stageStep('إغلاق عقد', false, false),
                      ],
                    ),
                  ],
                ),
              ),
              const SizedBox(height: 16),

              // Key Contact Person Card
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
                    const Text('مسؤول التواصل المباشر', style: TextStyle(fontWeight: FontWeight.w800, fontSize: 14, color: Color(0xFF0A2540))),
                    const SizedBox(height: 12),
                    Row(
                      children: [
                        Container(
                          width: 40,
                          height: 40,
                          alignment: Alignment.center,
                          decoration: const BoxDecoration(color: Color(0xFFF1F5F9), shape: BoxShape.circle),
                          child: const Text('ع.س', style: TextStyle(fontWeight: FontWeight.w700, color: Color(0xFF0A2540))),
                        ),
                        const SizedBox(width: 12),
                        Expanded(
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: const [
                              Text('عبدالله السعدون', style: TextStyle(fontWeight: FontWeight.w700, fontSize: 13.5, color: Color(0xFF0A2540))),
                              Text('المدير المالي • a.saadoun@alofooq.sa', style: TextStyle(fontSize: 11.5, color: Color(0xFF64748B))),
                            ],
                          ),
                        ),
                        const Text('+966 50 123 4567', style: TextStyle(fontSize: 12, fontWeight: FontWeight.bold, color: Color(0xFF059669))),
                      ],
                    ),
                  ],
                ),
              ),
              const SizedBox(height: 16),

              // Activity Log Timeline for this Lead
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
                        const Text('سجل التفاعلات والأنشطة', style: TextStyle(fontWeight: FontWeight.w800, fontSize: 14, color: Color(0xFF0A2540))),
                        TextButton.icon(
                          onPressed: () {},
                          icon: const Icon(Icons.add_rounded, size: 16, color: Color(0xFF059669)),
                          label: const Text('إضافة نشاط', style: TextStyle(fontSize: 12, color: Color(0xFF059669), fontWeight: FontWeight.w700)),
                        ),
                      ],
                    ),
                    const SizedBox(height: 8),
                    _timelineItem(
                      'مكالمة متابعة شروط الفاتورة الإلكترونية',
                      'اليوم • 11:30 ص',
                      'سلمان المطيري',
                      Icons.phone_in_talk_rounded,
                      const Color(0xFF059669),
                    ),
                    _timelineItem(
                      'إرسال عرض السعر المالي المحدث v2',
                      '22 أكتوبر • 03:15 م',
                      'سلمان المطيري',
                      Icons.mail_outline_rounded,
                      const Color(0xFF2563EB),
                    ),
                    _timelineItem(
                      'اجتماع عرض المنظومة التوضيحي (Demo)',
                      '19 أكتوبر • 10:00 ص',
                      'فريق المبيعات والحلول',
                      Icons.groups_rounded,
                      const Color(0xFF7C3AED),
                    ),
                  ],
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  static Widget _actionCircleBtn(IconData icon, String label, VoidCallback onTap) {
    return Expanded(
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(10),
        child: Container(
          padding: const EdgeInsets.symmetric(vertical: 8),
          decoration: BoxDecoration(
            color: const Color(0xFFF8FAFC),
            borderRadius: BorderRadius.circular(10),
            border: Border.all(color: const Color(0xFFE2E8F0)),
          ),
          child: Column(
            children: [
              Icon(icon, size: 18, color: const Color(0xFF0A2540)),
              const SizedBox(height: 4),
              Text(label, style: const TextStyle(fontSize: 11.5, fontWeight: FontWeight.w700, color: Color(0xFF0A2540))),
            ],
          ),
        ),
      ),
    );
  }

  static Widget _stageStep(String title, bool isCompleted, bool isCurrent) {
    return Expanded(
      child: Column(
        children: [
          Container(
            height: 6,
            margin: const EdgeInsets.symmetric(horizontal: 2),
            decoration: BoxDecoration(
              color: isCompleted ? const Color(0xFF059669) : const Color(0xFFE2E8F0),
              borderRadius: BorderRadius.circular(3),
            ),
          ),
          const SizedBox(height: 6),
          Text(
            title,
            style: TextStyle(              fontSize: 10.5,
              fontWeight: isCurrent ? FontWeight.w800 : FontWeight.w500,
              color: isCompleted ? const Color(0xFF059669) : const Color(0xFF94A3B8),
            ),
          ),
        ],
      ),
    );
  }

  static Widget _timelineItem(String title, String time, String by, IconData icon, Color color) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 8.0),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Container(
            padding: const EdgeInsets.all(8),
            decoration: BoxDecoration(color: color.withOpacity(0.12), shape: BoxShape.circle),
            child: Icon(icon, size: 16, color: color),
          ),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(title, style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 13, color: Color(0xFF0A2540))),
                const SizedBox(height: 2),
                Text('$time • بواسطة $by', style: const TextStyle(fontSize: 11, color: Color(0xFF64748B))),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

// -----------------------------------------------------------------------------
// 3. ADD ACTIVITY SHEET (إضافة نشاط وتفاعل)
// -----------------------------------------------------------------------------
class MarsoudAddActivitySheet extends StatefulWidget {
  const MarsoudAddActivitySheet({super.key});

  @override
  State<MarsoudAddActivitySheet> createState() => _MarsoudAddActivitySheetState();
}

class _MarsoudAddActivitySheetState extends State<MarsoudAddActivitySheet> {
  int _selectedActivityType = 0;
  final List<Map<String, dynamic>> _types = [
    {'label': 'مكالمة', 'icon': Icons.phone_rounded},
    {'label': 'اجتماع', 'icon': Icons.groups_rounded},
    {'label': 'بريد', 'icon': Icons.mail_outline_rounded},
    {'label': 'ملاحظة', 'icon': Icons.edit_note_rounded},
  ];

  @override
  Widget build(BuildContext context) {
    return Directionality(
      textDirection: TextDirection.rtl,
      child: Container(
        padding: const EdgeInsets.all(20),
        decoration: const BoxDecoration(
          color: Colors.white,
          borderRadius: BorderRadius.vertical(top: Radius.circular(24)),
        ),
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Center(
                child: Container(width: 40, height: 4, decoration: BoxDecoration(color: const Color(0xFFCBD5E1), borderRadius: BorderRadius.circular(2))),
              ),
              const SizedBox(height: 16),
              const Text('تسجيل نشاط تفاعلي', style: TextStyle(fontSize: 18, fontWeight: FontWeight.w800, color: Color(0xFF0A2540))),
              const SizedBox(height: 4),
              const Text('أدخل تفاصيل التفاعل مع شركة الأفق للاستشارات', style: TextStyle(fontSize: 13, color: Color(0xFF64748B))),
              const SizedBox(height: 20),

              // Activity Type Selector
              Row(
                children: List.generate(_types.length, (index) {
                  final isSelected = _selectedActivityType == index;
                  return Expanded(
                    child: Padding(
                      padding: const EdgeInsets.symmetric(horizontal: 4),
                      child: InkWell(
                        onTap: () => setState(() => _selectedActivityType = index),
                        borderRadius: BorderRadius.circular(10),
                        child: Container(
                          padding: const EdgeInsets.symmetric(vertical: 10),
                          decoration: BoxDecoration(
                            color: isSelected ? const Color(0xFFECFDF5) : const Color(0xFFF8FAFC),
                            borderRadius: BorderRadius.circular(10),
                            border: Border.all(color: isSelected ? const Color(0xFF059669) : const Color(0xFFE2E8F0)),
                          ),
                          child: Column(
                            children: [
                              Icon(_types[index]['icon'] as IconData, color: isSelected ? const Color(0xFF059669) : const Color(0xFF64748B), size: 20),
                              const SizedBox(height: 4),
                              Text(
                                _types[index]['label'] as String,
                                style: TextStyle(                                  fontSize: 12,
                                  fontWeight: isSelected ? FontWeight.w800 : FontWeight.w500,
                                  color: isSelected ? const Color(0xFF059669) : const Color(0xFF64748B),
                                ),
                              ),
                            ],
                          ),
                        ),
                      ),
                    ),
                  );
                }),
              ),
              const SizedBox(height: 20),

              const Text('ملخص النشاط *', style: TextStyle(fontSize: 13, fontWeight: FontWeight.w700, color: Color(0xFF0A2540))),
              const SizedBox(height: 8),
              TextFormField(
                style: const TextStyle(fontSize: 14),
                decoration: InputDecoration(
                  hintText: 'مثال: مناقشة موافقة مجلس الإدارة على بنود الدفع',
                  hintStyle: const TextStyle(fontSize: 13, color: Color(0xFF94A3B8)),
                  filled: true,
                  fillColor: const Color(0xFFF8FAFC),
                  border: OutlineInputBorder(borderRadius: BorderRadius.circular(12), borderSide: const BorderSide(color: Color(0xFFE2E8F0))),
                  enabledBorder: OutlineInputBorder(borderRadius: BorderRadius.circular(12), borderSide: const BorderSide(color: Color(0xFFE2E8F0))),
                ),
              ),
              const SizedBox(height: 16),

              const Text('ملاحظات وتفاصيل التفاعل', style: TextStyle(fontSize: 13, fontWeight: FontWeight.w700, color: Color(0xFF0A2540))),
              const SizedBox(height: 8),
              TextFormField(
                maxLines: 3,
                style: const TextStyle(fontSize: 14),
                decoration: InputDecoration(
                  hintText: 'اكتب مخرجات المكالمة أو الاجتماع...',
                  hintStyle: const TextStyle(fontSize: 13, color: Color(0xFF94A3B8)),
                  filled: true,
                  fillColor: const Color(0xFFF8FAFC),
                  border: OutlineInputBorder(borderRadius: BorderRadius.circular(12), borderSide: const BorderSide(color: Color(0xFFE2E8F0))),
                  enabledBorder: OutlineInputBorder(borderRadius: BorderRadius.circular(12), borderSide: const BorderSide(color: Color(0xFFE2E8F0))),
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
                child: const Text('حفظ النشاط في السجل', style: TextStyle(fontSize: 15, fontWeight: FontWeight.w700, color: Colors.white)),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

// -----------------------------------------------------------------------------
// 4. MEETINGS SCREEN (الاجتماعات المبيعية)
// -----------------------------------------------------------------------------
class MarsoudMeetingsScreen extends StatelessWidget {
  const MarsoudMeetingsScreen({super.key});

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
          title: const Text('الاجتماعات المبيعية', style: TextStyle(fontWeight: FontWeight.w800, fontSize: 17, color: Color(0xFF0A2540))),
          actions: [
            IconButton(icon: const Icon(Icons.calendar_today_rounded, color: Color(0xFF0A2540)), onPressed: () {}),
          ],
        ),
        body: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            const Text('اليوم (24 أكتوبر)', style: TextStyle(fontSize: 14, fontWeight: FontWeight.w800, color: Color(0xFF0A2540))),
            const SizedBox(height: 10),
            _meetingCard(
              title: 'مراجعة عقد شركة الأفق المالية',
              time: '10:30 ص - 11:30 ص (بعد ساعة)',
              location: 'الرياض - المقر الرئيسي (القاعة 4)',
              status: 'مؤكد',
              statusColor: const Color(0xFF2563EB),
              statusBg: const Color(0xFFEFF6FF),
              attendees: 'عبدالله السعدون، سلمان المطيري',
              isVirtual: false,
            ),
            const SizedBox(height: 12),
            _meetingCard(
              title: 'جلسة نقاش مع العميل المحتمل (رواسي نجد)',
              time: '02:00 م - 02:45 م',
              location: 'عبر رابط Google Meet المباشر',
              status: 'عن بُعد',
              statusColor: const Color(0xFF059669),
              statusBg: const Color(0xFFECFDF5),
              attendees: 'م. فهد التميمي، سارة المنصور',
              isVirtual: true,
            ),
            const SizedBox(height: 24),
            const Text('غداً (25 أكتوبر)', style: TextStyle(fontSize: 14, fontWeight: FontWeight.w800, color: Color(0xFF0A2540))),
            const SizedBox(height: 10),
            _meetingCard(
              title: 'اجتماع فريق المبيعات الأسبوعي',
              time: '09:00 ص - 10:00 ص',
              location: 'القاعة الرئيسية - الطابق 3',
              status: 'داخلي',
              statusColor: const Color(0xFF475569),
              statusBg: const Color(0xFFF1F5F9),
              attendees: 'كافة أعضاء فريق تطوير الأعمال',
              isVirtual: false,
            ),
          ],
        ),
        floatingActionButton: FloatingActionButton.extended(
          onPressed: () {},
          backgroundColor: const Color(0xFF059669),
          icon: const Icon(Icons.add_rounded, color: Colors.white),
          label: const Text('جدولة اجتماع', style: TextStyle(fontWeight: FontWeight.w700, color: Colors.white)),
        ),
      ),
    );
  }

  static Widget _meetingCard({
    required String title,
    required String time,
    required String location,
    required String status,
    required Color statusColor,
    required Color statusBg,
    required String attendees,
    required bool isVirtual,
  }) {
    return Container(
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
                decoration: BoxDecoration(color: statusBg, borderRadius: BorderRadius.circular(6)),
                child: Text(status, style: TextStyle(fontSize: 11, fontWeight: FontWeight.bold, color: statusColor)),
              ),
              Row(
                children: [
                  Icon(isVirtual ? Icons.videocam_outlined : Icons.location_on_outlined, size: 16, color: const Color(0xFF64748B)),
                  const SizedBox(width: 4),
                  Text(isVirtual ? 'افتراضي' : 'حضوري', style: const TextStyle(fontSize: 11.5, color: Color(0xFF64748B))),
                ],
              ),
            ],
          ),
          const SizedBox(height: 10),
          Text(title, style: const TextStyle(fontWeight: FontWeight.w800, fontSize: 14.5, color: Color(0xFF0A2540))),
          const SizedBox(height: 6),
          Row(
            children: [
              const Icon(Icons.access_time_rounded, size: 15, color: Color(0xFF64748B)),
              const SizedBox(width: 6),
              Text(time, style: const TextStyle(fontSize: 12, color: Color(0xFF64748B))),
            ],
          ),
          const SizedBox(height: 4),
          Row(
            children: [
              const Icon(Icons.place_outlined, size: 15, color: Color(0xFF64748B)),
              const SizedBox(width: 6),
              Text(location, style: const TextStyle(fontSize: 12, color: Color(0xFF64748B))),
            ],
          ),
          const Divider(height: 20, color: Color(0xFFF1F5F9)),
          Row(
            children: [
              const Icon(Icons.people_outline_rounded, size: 15, color: Color(0xFF94A3B8)),
              const SizedBox(width: 6),
              Expanded(
                child: Text(attendees, style: const TextStyle(fontSize: 11.5, color: Color(0xFF64748B)), overflow: TextOverflow.ellipsis),
              ),
              TextButton(
                onPressed: () {},
                style: TextButton.styleFrom(visualDensity: VisualDensity.compact),
                child: const Text('التفاصيل', style: TextStyle(fontSize: 12, fontWeight: FontWeight.w700, color: Color(0xFF059669))),
              ),
            ],
          ),
        ],
      ),
    );
  }
}

// -----------------------------------------------------------------------------
// 5. NEW MEETING SHEET (جدولة اجتماع جديد)
// -----------------------------------------------------------------------------
class MarsoudNewMeetingSheet extends StatelessWidget {
  const MarsoudNewMeetingSheet({super.key});

  @override
  Widget build(BuildContext context) {
    return Directionality(
      textDirection: TextDirection.rtl,
      child: Container(
        padding: const EdgeInsets.all(20),
        decoration: const BoxDecoration(
          color: Colors.white,
          borderRadius: BorderRadius.vertical(top: Radius.circular(24)),
        ),
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Center(
                child: Container(width: 40, height: 4, decoration: BoxDecoration(color: const Color(0xFFCBD5E1), borderRadius: BorderRadius.circular(2))),
              ),
              const SizedBox(height: 16),
              const Text('جدولة اجتماع جديد', style: TextStyle(fontSize: 18, fontWeight: FontWeight.w800, color: Color(0xFF0A2540))),
              const SizedBox(height: 20),

              const Text('عنوان الاجتماع *', style: TextStyle(fontSize: 13, fontWeight: FontWeight.w700, color: Color(0xFF0A2540))),
              const SizedBox(height: 8),
              TextFormField(
                style: const TextStyle(fontSize: 14),
                decoration: InputDecoration(
                  hintText: 'مثال: مراجعة العقد الفني وتفاصيل الربط',
                  hintStyle: const TextStyle(fontSize: 13, color: Color(0xFF94A3B8)),
                  filled: true,
                  fillColor: const Color(0xFFF8FAFC),
                  border: OutlineInputBorder(borderRadius: BorderRadius.circular(12), borderSide: const BorderSide(color: Color(0xFFE2E8F0))),
                ),
              ),
              const SizedBox(height: 16),

              // Date & Time Row
              Row(
                children: [
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        const Text('التاريخ', style: TextStyle(fontSize: 13, fontWeight: FontWeight.w700, color: Color(0xFF0A2540))),
                        const SizedBox(height: 8),
                        Container(
                          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 12),
                          decoration: BoxDecoration(color: const Color(0xFFF8FAFC), borderRadius: BorderRadius.circular(12), border: Border.all(color: const Color(0xFFE2E8F0))),
                          child: Row(
                            children: const [
                              Icon(Icons.calendar_month_rounded, size: 18, color: Color(0xFF64748B)),
                              SizedBox(width: 8),
                              Text('25 أكتوبر 2024', style: TextStyle(fontSize: 13, color: Color(0xFF0A2540))),
                            ],
                          ),
                        ),
                      ],
                    ),
                  ),
                  const SizedBox(width: 12),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        const Text('الوقت', style: TextStyle(fontSize: 13, fontWeight: FontWeight.w700, color: Color(0xFF0A2540))),
                        const SizedBox(height: 8),
                        Container(
                          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 12),
                          decoration: BoxDecoration(color: const Color(0xFFF8FAFC), borderRadius: BorderRadius.circular(12), border: Border.all(color: const Color(0xFFE2E8F0))),
                          child: Row(
                            children: const [
                              Icon(Icons.access_time_rounded, size: 18, color: Color(0xFF64748B)),
                              SizedBox(width: 8),
                              Text('11:00 ص', style: TextStyle(fontSize: 13, color: Color(0xFF0A2540))),
                            ],
                          ),
                        ),
                      ],
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 16),

              const Text('العميل أو الجهة المرتبطة', style: TextStyle(fontSize: 13, fontWeight: FontWeight.w700, color: Color(0xFF0A2540))),
              const SizedBox(height: 8),
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 12),
                decoration: BoxDecoration(color: const Color(0xFFF8FAFC), borderRadius: BorderRadius.circular(12), border: Border.all(color: const Color(0xFFE2E8F0))),
                child: Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: const [
                    Text('شركة الأفق للاستشارات المالية', style: TextStyle(fontSize: 13, fontWeight: FontWeight.w600, color: Color(0xFF0A2540))),
                    Icon(Icons.keyboard_arrow_down_rounded, color: Color(0xFF64748B)),
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
                child: const Text('تأكيد وحفظ الموعد', style: TextStyle(fontSize: 15, fontWeight: FontWeight.w700, color: Colors.white)),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

// -----------------------------------------------------------------------------
// 6. SCHEDULE SCREEN (جدولي والمواعيد)
// -----------------------------------------------------------------------------
class MarsoudScheduleScreen extends StatelessWidget {
  const MarsoudScheduleScreen({super.key});

  @override
  Widget build(BuildContext context) {
    final days = [
      {'day': 'الأحد', 'num': '20', 'isToday': false},
      {'day': 'الإثنين', 'num': '21', 'isToday': false},
      {'day': 'الثلاثاء', 'num': '22', 'isToday': false},
      {'day': 'الأربعاء', 'num': '23', 'isToday': false},
      {'day': 'الخميس', 'num': '24', 'isToday': true},
      {'day': 'الجمعة', 'num': '25', 'isToday': false},
      {'day': 'السبت', 'num': '26', 'isToday': false},
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
          title: const Text('جدولي الأسبوعي', style: TextStyle(fontWeight: FontWeight.w800, fontSize: 17, color: Color(0xFF0A2540))),
        ),
        body: Column(
          children: [
            // Calendar Days Ribbon
            Container(
              color: Colors.white,
              padding: const EdgeInsets.symmetric(vertical: 12, horizontal: 8),
              child: Row(
                mainAxisAlignment: MainAxisAlignment.spaceAround,
                children: days.map((d) {
                  final isToday = d['isToday'] as bool;
                  return Container(
                    padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
                    decoration: BoxDecoration(
                      color: isToday ? const Color(0xFF059669) : Colors.transparent,
                      borderRadius: BorderRadius.circular(12),
                    ),
                    child: Column(
                      children: [
                        Text(d['day'] as String, style: TextStyle(fontSize: 11, color: isToday ? Colors.white : const Color(0xFF64748B))),
                        const SizedBox(height: 4),
                        Text(d['num'] as String, style: TextStyle(fontSize: 15, fontWeight: FontWeight.w800, color: isToday ? Colors.white : const Color(0xFF0A2540))),
                      ],
                    ),
                  );
                }).toList(),
              ),
            ),
            const Divider(height: 1, color: Color(0xFFE2E8F0)),

            // Time Slots List
            Expanded(
              child: ListView(
                padding: const EdgeInsets.all(16),
                children: [
                  _timeSlot('09:00 ص', 'مراجعة المهام الصباحية والبريد', 'مكتملة', const Color(0xFF059669)),
                  _timeSlot('10:30 ص', 'اجتماع مناقشة عقد الأفق (القاعة 4)', 'جاري الآن', const Color(0xFF2563EB)),
                  _timeSlot('01:00 م', 'فترة استراحة وصلاة الظهر', '', const Color(0xFF94A3B8)),
                  _timeSlot('02:00 م', 'مكالمة استعراض المنظومة مع رواسي نجد', 'قادمة', const Color(0xFFD97706)),
                  _timeSlot('04:00 م', 'إقفال التقارير اليومية وتسجيل الانصراف', 'مجدولة', const Color(0xFF64748B)),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }

  static Widget _timeSlot(String time, String title, String tag, Color tagColor) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 12.0),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SizedBox(
            width: 60,
            child: Text(time, style: const TextStyle(fontSize: 12, fontWeight: FontWeight.w700, color: Color(0xFF64748B))),
          ),
          const SizedBox(width: 8),
          Expanded(
            child: Container(
              padding: const EdgeInsets.all(12),
              decoration: BoxDecoration(
                color: Colors.white,
                borderRadius: BorderRadius.circular(12),
                border: Border.all(color: const Color(0xFFE2E8F0)),
              ),
              child: Row(
                mainAxisAlignment: MainAxisAlignment.spaceBetween,
                children: [
                  Expanded(
                    child: Text(title, style: const TextStyle(fontSize: 13, fontWeight: FontWeight.w700, color: Color(0xFF0A2540))),
                  ),
                  if (tag.isNotEmpty)
                    Container(
                      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                      decoration: BoxDecoration(color: tagColor.withOpacity(0.12), borderRadius: BorderRadius.circular(4)),
                      child: Text(tag, style: TextStyle(fontSize: 10.5, fontWeight: FontWeight.bold, color: tagColor)),
                    ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }
}
