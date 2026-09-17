// MARSOUD-MOBILE-TASK-CREATE-01 (2026-09-17) — /tasks/new on
// mobile.
//
// Abdelhamid's Batch 2 feedback (Image 46): "عاوز اقدر أضيف مهمة
// زي ما بنضيفها ع الجهاز يكون فيها كل حاجة".  Mirrors the shape
// of app/templates/tasks/form.html but scoped to the fields a
// mobile user actually types on a phone; the ambitious extras
// (schedule modes, parent-task picker, milestones) are supported
// on the backend but omitted from the form UI to keep it usable
// on a small screen. Follow-ups can add them once the primary
// flow gets validated.
//
// Fields:
//   · العنوان         (required)
//   · الوصف           (multi-line)
//   · المشروع         (dropdown, optional)
//   · المكلَّفون       (multi-select, at least one)
//   · الأولوية        (chip row: منخفضة / متوسطة / عالية)
//   · الموعد النهائي  (date picker, optional)
//
// On success: pop back to /tasks with a "تم إنشاء المهمة" snackbar
// + invalidate the tasks list provider so the new row shows.
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../app/theme.dart';
import '../../data/api_client.dart';
import '../../data/my_account_repository.dart';
import '../../widgets/gradient_button.dart';

final _projectsProvider =
    FutureProvider.autoDispose<List<Map<String, dynamic>>>((ref) async {
  final r = await ref.watch(myAccountRepoProvider).projects();
  return (r['projects'] as List?)?.cast<Map<String, dynamic>>() ?? const [];
});

final _usersProvider =
    FutureProvider.autoDispose<List<Map<String, dynamic>>>((ref) async {
  final r = await ref.watch(myAccountRepoProvider).companyUsers();
  return (r['users'] as List?)?.cast<Map<String, dynamic>>() ?? const [];
});


class TaskNewScreen extends ConsumerStatefulWidget {
  const TaskNewScreen({super.key});
  @override
  ConsumerState<TaskNewScreen> createState() => _TaskNewScreenState();
}

class _TaskNewScreenState extends ConsumerState<TaskNewScreen> {
  final _title = TextEditingController();
  final _description = TextEditingController();
  final _notes = TextEditingController();
  int? _projectId;
  final Set<int> _assigneeIds = {};
  String _priority = 'MEDIUM';
  DateTime? _deadline;
  bool _submitting = false;
  String? _error;

  @override
  void dispose() {
    _title.dispose();
    _description.dispose();
    _notes.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final projectsAsync = ref.watch(_projectsProvider);
    final usersAsync = ref.watch(_usersProvider);
    return Scaffold(
      body: SafeArea(
        child: SingleChildScrollView(
          padding: const EdgeInsets.fromLTRB(16, 12, 16, 32),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const Text(
                '✅ مهمة جديدة',
                style: TextStyle(
                  color: BrandColors.navy900,
                  fontSize: 18,
                  fontWeight: FontWeight.w800,
                ),
              ),
              const SizedBox(height: 4),
              const Text(
                'حدد العنوان + المكلَّفين على الأقل. باقي الحقول اختيارية.',
                style: TextStyle(
                    color: BrandColors.slate500, fontSize: 12),
              ),
              const SizedBox(height: 20),

              // ── العنوان ────────────────────────────────
              const _FieldLabel(text: 'العنوان *'),
              TextField(
                controller: _title,
                textInputAction: TextInputAction.next,
                decoration: const InputDecoration(
                  hintText: 'اكتب عنوان المهمة…',
                  border: OutlineInputBorder(),
                ),
              ),
              const SizedBox(height: 16),

              // ── الوصف ───────────────────────────────────
              const _FieldLabel(text: 'الوصف'),
              TextField(
                controller: _description,
                maxLines: 4,
                minLines: 3,
                decoration: const InputDecoration(
                  hintText: 'تفاصيل المهمة، خطوات التنفيذ، أي معلومة إضافية…',
                  border: OutlineInputBorder(),
                ),
              ),
              const SizedBox(height: 16),

              // ── المشروع ─────────────────────────────────
              const _FieldLabel(text: 'المشروع (اختياري)'),
              projectsAsync.when(
                loading: () => const LinearProgressIndicator(),
                error: (e, _) => Text(
                    e is ApiException ? e.message : 'تعذّر جلب المشاريع',
                    style: const TextStyle(color: BrandColors.red500)),
                data: (list) => DropdownButtonFormField<int?>(
                  initialValue: _projectId,
                  decoration: const InputDecoration(
                    border: OutlineInputBorder(),
                  ),
                  items: [
                    const DropdownMenuItem<int?>(
                      value: null,
                      child: Text('— بدون مشروع —'),
                    ),
                    for (final p in list)
                      DropdownMenuItem<int?>(
                        value: p['id'] as int,
                        child: Text((p['name'] ?? '').toString()),
                      ),
                  ],
                  onChanged: (v) => setState(() => _projectId = v),
                ),
              ),
              const SizedBox(height: 16),

              // ── الأولوية ───────────────────────────────
              const _FieldLabel(text: 'الأولوية'),
              Wrap(
                spacing: 8,
                children: [
                  _PriorityChip(
                    label: 'منخفضة',
                    value: 'LOW',
                    active: _priority == 'LOW',
                    color: BrandColors.slate500,
                    onTap: () => setState(() => _priority = 'LOW'),
                  ),
                  _PriorityChip(
                    label: 'متوسطة',
                    value: 'MEDIUM',
                    active: _priority == 'MEDIUM',
                    color: BrandColors.emerald600,
                    onTap: () => setState(() => _priority = 'MEDIUM'),
                  ),
                  _PriorityChip(
                    label: 'عالية',
                    value: 'HIGH',
                    active: _priority == 'HIGH',
                    color: BrandColors.red500,
                    onTap: () => setState(() => _priority = 'HIGH'),
                  ),
                ],
              ),
              const SizedBox(height: 16),

              // ── الموعد النهائي ─────────────────────────
              const _FieldLabel(text: 'الموعد النهائي (اختياري)'),
              InkWell(
                onTap: () async {
                  final now = DateTime.now();
                  final picked = await showDatePicker(
                    context: context,
                    firstDate: now,
                    lastDate: now.add(const Duration(days: 365 * 5)),
                    initialDate: _deadline ?? now,
                  );
                  if (picked != null) setState(() => _deadline = picked);
                },
                child: Container(
                  padding: const EdgeInsets.symmetric(
                      horizontal: 12, vertical: 14),
                  decoration: BoxDecoration(
                    border: Border.all(color: BrandColors.slate200),
                    borderRadius: BorderRadius.circular(6),
                  ),
                  child: Row(
                    children: [
                      const Icon(Icons.event,
                          size: 18, color: BrandColors.slate500),
                      const SizedBox(width: 8),
                      Expanded(
                        child: Text(
                          _deadline == null
                              ? 'اضغط لاختيار تاريخ'
                              : _fmtDate(_deadline!),
                          style: TextStyle(
                            color: _deadline == null
                                ? BrandColors.slate400
                                : BrandColors.slate700,
                            fontSize: 14,
                          ),
                        ),
                      ),
                      if (_deadline != null)
                        IconButton(
                          icon: const Icon(Icons.clear, size: 16),
                          onPressed: () =>
                              setState(() => _deadline = null),
                        ),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 16),

              // ── المكلَّفون ───────────────────────────────
              const _FieldLabel(text: 'المكلَّفون *'),
              usersAsync.when(
                loading: () => const Center(
                    child: Padding(
                        padding: EdgeInsets.all(12),
                        child: LinearProgressIndicator())),
                error: (e, _) => Text(
                    e is ApiException
                        ? e.message
                        : 'تعذّر جلب المستخدمين',
                    style: const TextStyle(color: BrandColors.red500)),
                data: (users) => Container(
                  constraints:
                      const BoxConstraints(maxHeight: 260),
                  decoration: BoxDecoration(
                    border: Border.all(color: BrandColors.slate200),
                    borderRadius: BorderRadius.circular(6),
                    color: BrandColors.slate50,
                  ),
                  child: ListView(
                    shrinkWrap: true,
                    padding: const EdgeInsets.symmetric(vertical: 4),
                    children: [
                      for (final u in users)
                        CheckboxListTile(
                          value: _assigneeIds.contains(u['id'] as int),
                          onChanged: (checked) {
                            setState(() {
                              if (checked == true) {
                                _assigneeIds.add(u['id'] as int);
                              } else {
                                _assigneeIds.remove(u['id'] as int);
                              }
                            });
                          },
                          dense: true,
                          title: Text(
                            '${u['name'] ?? ''}${(u['is_me'] as bool? ?? false) ? " (أنا)" : ""}',
                            style: const TextStyle(fontSize: 13),
                          ),
                          subtitle: Text(
                            (u['email'] ?? '').toString(),
                            style: const TextStyle(
                                color: BrandColors.slate500, fontSize: 10.5),
                          ),
                          controlAffinity: ListTileControlAffinity.leading,
                        ),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 20),

              // ── الخطأ لو حصل ───────────────────────────
              if (_error != null) ...[
                Container(
                  padding: const EdgeInsets.all(12),
                  decoration: BoxDecoration(
                    color: const Color(0xFFFEE2E2),
                    border: Border.all(color: const Color(0xFFFCA5A5)),
                    borderRadius: BorderRadius.circular(8),
                  ),
                  child: Text(
                    _error!,
                    style: const TextStyle(
                        color: Color(0xFFB91C1C),
                        fontWeight: FontWeight.w600),
                  ),
                ),
                const SizedBox(height: 12),
              ],

              // ── حفظ ─────────────────────────────────────
              GradientButton(
                onPressed: _submitting ? null : _submit,
                loading: _submitting,
                label: 'إنشاء المهمة',
              ),
              const SizedBox(height: 8),
              TextButton(
                onPressed: _submitting
                    ? null
                    : () => Navigator.of(context).maybePop(),
                child: const Text('إلغاء'),
              ),
            ],
          ),
        ),
      ),
    );
  }

  Future<void> _submit() async {
    // Client-side validation before the network round-trip.
    if (_title.text.trim().isEmpty) {
      setState(() => _error = 'العنوان مطلوب.');
      return;
    }
    if (_assigneeIds.isEmpty) {
      setState(() => _error = 'اختر مكلَّف واحد على الأقل.');
      return;
    }
    setState(() {
      _submitting = true;
      _error = null;
    });
    try {
      final r = await ref.read(myAccountRepoProvider).createTask(
            title: _title.text.trim(),
            assigneeIds: _assigneeIds.toList(),
            description: _description.text.trim(),
            projectId: _projectId,
            priority: _priority,
            deadline:
                _deadline == null ? null : _fmtDateIso(_deadline!),
          );
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
        content: Text('تم إنشاء المهمة.'),
      ));
      // Route back to /tasks; the FAB caller invalidates the
      // provider so the new row shows.
      final id = r['task_id'];
      if (id is int) {
        context.go('/tasks/$id');
      } else {
        context.go('/tasks');
      }
    } on ApiException catch (e) {
      setState(() {
        _error = e.message;
        _submitting = false;
      });
    } catch (_) {
      setState(() {
        _error = 'تعذّر الاتصال — تأكد من الإنترنت وحاول مرة أخرى.';
        _submitting = false;
      });
    }
  }

  static String _fmtDate(DateTime d) =>
      '${d.year}-${d.month.toString().padLeft(2, "0")}-${d.day.toString().padLeft(2, "0")}';
  static String _fmtDateIso(DateTime d) => _fmtDate(d);
}


class _FieldLabel extends StatelessWidget {
  final String text;
  const _FieldLabel({required this.text});
  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: Text(
        text,
        style: const TextStyle(
          color: BrandColors.slate700,
          fontWeight: FontWeight.w700,
          fontSize: 13,
        ),
      ),
    );
  }
}


class _PriorityChip extends StatelessWidget {
  final String label;
  final String value;
  final bool active;
  final Color color;
  final VoidCallback onTap;
  const _PriorityChip({
    required this.label,
    required this.value,
    required this.active,
    required this.color,
    required this.onTap,
  });
  @override
  Widget build(BuildContext context) {
    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(20),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 8),
        decoration: BoxDecoration(
          color: active
              ? color.withValues(alpha: 0.15)
              : Colors.transparent,
          border: Border.all(
              color: active ? color : BrandColors.slate200,
              width: active ? 1.5 : 1),
          borderRadius: BorderRadius.circular(20),
        ),
        child: Text(
          label,
          style: TextStyle(
            color: active ? color : BrandColors.slate500,
            fontSize: 12.5,
            fontWeight: active ? FontWeight.w800 : FontWeight.w600,
          ),
        ),
      ),
    );
  }
}
