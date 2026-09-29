// MARSOUD-MOBILE-DESIGN-PREVIEW-01 (2026-09-28) — a debug-only
// gallery that lets you tap through every screen from the new
// Stitch-generated design system without touching the real router.
// Wired at `/design-preview` in the ShellRoute so the existing app
// keeps working while we evaluate + migrate one screen at a time.
import 'package:flutter/material.dart';

import 'core_screens.dart';
import 'crm_screens.dart';
import 'employee_screens.dart';
import 'tasks_projects_screens.dart';

class _Entry {
  final String label;
  final String category;
  final Widget Function() build;
  final bool isSheet;
  const _Entry({
    required this.label,
    required this.category,
    required this.build,
    this.isSheet = false,
  });
}

const _entries = <_Entry>[
  // Core
  _Entry(label: 'Splash', category: 'Core', build: _splash),
  _Entry(label: 'Login', category: 'Core', build: _login),
  _Entry(label: 'Biometric Lock', category: 'Core', build: _bio),
  _Entry(label: 'Notifications', category: 'Core', build: _notif),
  _Entry(label: 'Dashboard', category: 'Core', build: _dash),

  // CRM
  _Entry(label: 'Leads List', category: 'CRM', build: _leads),
  _Entry(label: 'Lead Detail', category: 'CRM', build: _leadDetail),
  _Entry(
      label: 'Add Activity (sheet)',
      category: 'CRM',
      isSheet: true,
      build: _addActivity),
  _Entry(label: 'Meetings', category: 'CRM', build: _meetings),
  _Entry(
      label: 'New Meeting (sheet)',
      category: 'CRM',
      isSheet: true,
      build: _newMeeting),
  _Entry(label: 'Schedule', category: 'CRM', build: _schedule),

  // Employee
  _Entry(label: 'My Account', category: 'Employee', build: _myAccount),
  _Entry(label: 'Attendance', category: 'Employee', build: _attendance),
  _Entry(label: 'Daily Reports', category: 'Employee', build: _reports),
  _Entry(
      label: 'Daily Report Detail',
      category: 'Employee',
      build: _reportDetail),
  _Entry(label: 'My Requests', category: 'Employee', build: _requests),
  _Entry(label: 'Cash Custody', category: 'Employee', build: _cash),
  _Entry(label: 'Item Custody', category: 'Employee', build: _items),
  _Entry(label: 'My Files', category: 'Employee', build: _files),

  // Tasks & Projects
  _Entry(label: 'Activity Log', category: 'Tasks', build: _activity),
  _Entry(label: 'Support Tickets', category: 'Tasks', build: _support),
  _Entry(label: 'Tasks List', category: 'Tasks', build: _tasks),
  _Entry(label: 'Task Detail', category: 'Tasks', build: _taskDetail),
  _Entry(label: 'New Task', category: 'Tasks', build: _newTask),
  _Entry(label: 'Task Archive', category: 'Tasks', build: _archive),
  _Entry(label: 'Projects List', category: 'Tasks', build: _projects),
  _Entry(
      label: 'Project Detail', category: 'Tasks', build: _projectDetail),

  // System
  _Entry(label: 'Empty State — No data', category: 'System', build: _empty1),
  _Entry(label: 'Empty State — Network error', category: 'System', build: _empty2),
  _Entry(label: 'Empty State — 403 Permission', category: 'System', build: _empty3),
];

Widget _splash() => const MarsoudSplashScreen();
Widget _login() => const MarsoudLoginScreen();
Widget _bio() => const MarsoudBiometricScreen();
Widget _notif() => const MarsoudNotificationsScreen();
Widget _dash() => const Scaffold(body: SafeArea(child: MarsoudDashboardScreen()));

Widget _leads() => const MarsoudLeadsListScreen();
Widget _leadDetail() => const MarsoudLeadDetailScreen();
Widget _addActivity() => const MarsoudAddActivitySheet();
Widget _meetings() => const MarsoudMeetingsScreen();
Widget _newMeeting() => const MarsoudNewMeetingSheet();
Widget _schedule() => const MarsoudScheduleScreen();

Widget _myAccount() => const MarsoudMyAccountScreen();
Widget _attendance() => const MarsoudAttendanceScreen();
Widget _reports() => const MarsoudDailyReportsScreen();
Widget _reportDetail() => const MarsoudDailyReportDetailScreen();
Widget _requests() => const MarsoudMyRequestsScreen();
Widget _cash() => const MarsoudCashCustodyScreen();
Widget _items() => const MarsoudItemCustodyScreen();
Widget _files() => const MarsoudMyFilesScreen();

Widget _activity() => const MarsoudActivityLogScreen();
Widget _support() => const MarsoudSupportTicketsScreen();
Widget _tasks() => const MarsoudTasksListScreen();
Widget _taskDetail() => const MarsoudTaskDetailScreen();
Widget _newTask() => const MarsoudNewTaskScreen();
Widget _archive() => const MarsoudTaskArchiveScreen();
Widget _projects() => const MarsoudProjectsListScreen();
Widget _projectDetail() => const MarsoudProjectDetailScreen();

Widget _empty1() => const Scaffold(
      backgroundColor: Color(0xFFF8FAFC),
      body: MarsoudEmptyStateWidget(
        icon: Icons.inbox_outlined,
        title: 'لا توجد بيانات هنا بعد',
        message: 'ابدأ بإضافة سجل جديد وسيظهر في هذه القائمة تلقائياً.',
        buttonLabel: 'إضافة سجل جديد',
      ),
    );
Widget _empty2() => const Scaffold(
      backgroundColor: Color(0xFFF8FAFC),
      body: MarsoudEmptyStateWidget(
        icon: Icons.cloud_off_outlined,
        title: 'تعذّر الاتصال بالخادم',
        message: 'تأكد من اتصالك بالإنترنت وأعد المحاولة.',
        buttonLabel: 'إعادة المحاولة',
      ),
    );
Widget _empty3() => const Scaffold(
      backgroundColor: Color(0xFFF8FAFC),
      body: MarsoudEmptyStateWidget(
        icon: Icons.lock_outline_rounded,
        title: 'صلاحياتك لا تسمح بعرض هذه الشاشة',
        message: 'راجع مسؤول النظام لطلب الصلاحية المناسبة.',
      ),
    );

class DesignPreviewScreen extends StatelessWidget {
  const DesignPreviewScreen({super.key});

  @override
  Widget build(BuildContext context) {
    final byCategory = <String, List<_Entry>>{};
    for (final e in _entries) {
      byCategory.putIfAbsent(e.category, () => []).add(e);
    }
    return Directionality(
      textDirection: TextDirection.rtl,
      child: Scaffold(
        backgroundColor: const Color(0xFFF8FAFC),
        appBar: AppBar(
          backgroundColor: Colors.white,
          elevation: 0,
          title: const Text(
            'معاينة التصميم الجديد',
            style: TextStyle(              fontWeight: FontWeight.w800,
              fontSize: 16,
              color: Color(0xFF0A2540),
            ),
          ),
          leading: IconButton(
            icon: const Icon(Icons.arrow_back,
                color: Color(0xFF0A2540)),
            onPressed: () => Navigator.of(context).maybePop(),
          ),
        ),
        body: ListView(
          padding: const EdgeInsets.symmetric(vertical: 12),
          children: [
            _banner(),
            for (final cat in byCategory.keys)
              _section(context, cat, byCategory[cat]!),
          ],
        ),
      ),
    );
  }

  Widget _banner() => Container(
        margin: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
        padding: const EdgeInsets.all(14),
        decoration: BoxDecoration(
          color: const Color(0xFFECFDF5),
          borderRadius: BorderRadius.circular(12),
          border: Border.all(color: const Color(0xFFD1FAE5)),
        ),
        child: Row(
          children: const [
            Icon(Icons.info_outline_rounded, color: Color(0xFF047857)),
            SizedBox(width: 10),
            Expanded(
              child: Text(
                'شاشات معاينة فقط بدون ربط بيانات — البيانات كلها ثابتة للعرض. لا تمس التطبيق الفعلي.',
                style: TextStyle(                  fontSize: 12,
                  color: Color(0xFF047857),
                  height: 1.5,
                ),
              ),
            ),
          ],
        ),
      );

  Widget _section(
      BuildContext context, String category, List<_Entry> items) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 16, 16, 8),
          child: Text(
            category,
            style: const TextStyle(              fontSize: 12,
              fontWeight: FontWeight.w800,
              color: Color(0xFF64748B),
              letterSpacing: 1.2,
            ),
          ),
        ),
        Container(
          margin: const EdgeInsets.symmetric(horizontal: 16),
          decoration: BoxDecoration(
            color: Colors.white,
            borderRadius: BorderRadius.circular(12),
            border: Border.all(color: const Color(0xFFE2E8F0)),
          ),
          child: Column(
            children: [
              for (var i = 0; i < items.length; i++) ...[
                if (i > 0)
                  const Divider(
                      height: 1, color: Color(0xFFF1F5F9), indent: 16),
                _row(context, items[i]),
              ],
            ],
          ),
        ),
      ],
    );
  }

  Widget _row(BuildContext context, _Entry e) {
    return ListTile(
      title: Text(
        e.label,
        style: const TextStyle(          fontSize: 13.5,
          fontWeight: FontWeight.w700,
          color: Color(0xFF0A2540),
        ),
      ),
      trailing: Icon(
        e.isSheet ? Icons.expand_less_rounded : Icons.chevron_left,
        color: const Color(0xFF94A3B8),
      ),
      onTap: () {
        if (e.isSheet) {
          showModalBottomSheet(
            context: context,
            isScrollControlled: true,
            backgroundColor: Colors.transparent,
            builder: (_) => e.build(),
          );
        } else {
          Navigator.of(context).push(
            MaterialPageRoute(builder: (_) => e.build()),
          );
        }
      },
    );
  }
}
