// MARSOUD-MOBILE-FLUTTER — مهامي (mirrors app/templates/tasks/index.html
// but scoped to /api/v1/me/tasks so it crosses companies — anywhere
// this user is an assignee).
//
// Grouped by TODO / IN_PROGRESS / REVIEW / DONE / BLOCKED with
// coloured pills matching the web `.badge-*` conventions.
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../app/theme.dart';
import '../../data/api_client.dart';
import '../../data/my_account_repository.dart';
import '../../widgets/section_card.dart';

final _tasksProvider = FutureProvider.autoDispose<Map<String, dynamic>>(
    (ref) => ref.watch(myAccountRepoProvider).myTasks());

class TasksScreen extends ConsumerWidget {
  const TasksScreen({super.key});
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final async = ref.watch(_tasksProvider);
    return async.when(
      loading: () => const Center(child: CircularProgressIndicator()),
      error: (e, _) => Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Text(e is ApiException ? e.message : e.toString()),
        ),
      ),
      data: (data) {
        final tasks = (data['tasks'] as List?)
                ?.cast<Map<String, dynamic>>() ??
            const [];
        // Group by status.
        final buckets = <String, List<Map<String, dynamic>>>{
          'TODO': [],
          'IN_PROGRESS': [],
          'REVIEW': [],
          'DONE': [],
          'BLOCKED': [],
        };
        for (final t in tasks) {
          final s = t['status']?.toString() ?? 'TODO';
          buckets.putIfAbsent(s, () => []).add(t);
        }
        return RefreshIndicator(
          color: BrandColors.emerald600,
          onRefresh: () async => ref.invalidate(_tasksProvider),
          child: ListView(
            padding: const EdgeInsets.fromLTRB(12, 16, 12, 32),
            children: [
              SectionCard(
                emoji: '✅',
                title: 'مهامي',
                subtitle:
                    'كل مهمة أنت عليها (مسؤول أو ضمن الفريق)، عبر كل شركاتك.',
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    _CountsRow(buckets: buckets),
                    const SizedBox(height: 8),
                  ],
                ),
              ),
              const SizedBox(height: 12),
              if (tasks.isEmpty)
                SectionCard(
                  child: const EmptyState(
                    icon: Icons.task_alt,
                    message: 'ما فيش مهام مسندة لك حالياً.',
                  ),
                )
              else
                // MARSOUD-MOBILE-SHIP-READY-01 (L2) — render known
                // buckets first, then any unknown status the backend
                // ships (e.g. CANCELLED). Was: only the known list.
                // A backend-added status silently disappeared from
                // the list.
                for (final bucket in [
                  ..._order,
                  ...buckets.keys.where((k) => !_order.contains(k)),
                ])
                  if ((buckets[bucket] ?? const []).isNotEmpty) ...[
                    Padding(
                      padding: const EdgeInsets.only(
                          top: 6, right: 4, bottom: 4),
                      child: Row(
                        children: [
                          Text(
                            _statusLabel(bucket),
                            style: const TextStyle(
                              color: BrandColors.slate700,
                              fontWeight: FontWeight.w800,
                              fontSize: 13,
                            ),
                          ),
                          const SizedBox(width: 6),
                          Text(
                            '(${buckets[bucket]!.length})',
                            style: const TextStyle(
                              color: BrandColors.slate500,
                              fontSize: 12,
                            ),
                          ),
                        ],
                      ),
                    ),
                    for (final t in buckets[bucket]!) _TaskRow(t: t),
                    const SizedBox(height: 12),
                  ],
            ],
          ),
        );
      },
    );
  }

  static const _order = ['TODO', 'IN_PROGRESS', 'REVIEW', 'BLOCKED', 'DONE'];
  static String _statusLabel(String s) => switch (s) {
        'TODO' => '📋 جديد',
        'IN_PROGRESS' => '⚡ قيد التنفيذ',
        'REVIEW' => '👀 مراجعة',
        'DONE' => '✅ منجزة',
        'BLOCKED' => '⛔ متوقفة',
        _ => s,
      };
}

class _CountsRow extends StatelessWidget {
  final Map<String, List<Map<String, dynamic>>> buckets;
  const _CountsRow({required this.buckets});
  @override
  Widget build(BuildContext context) {
    Widget cell(String key, String label, Color bg, Color fg) {
      final n = buckets[key]?.length ?? 0;
      return Expanded(
        child: Container(
          padding: const EdgeInsets.symmetric(vertical: 10, horizontal: 6),
          decoration: BoxDecoration(
            color: bg.withValues(alpha: 0.18),
            borderRadius: BorderRadius.circular(10),
          ),
          child: Column(
            children: [
              Text('$n',
                  style: TextStyle(
                    color: fg,
                    fontWeight: FontWeight.w800,
                    fontSize: 18,
                    fontFamily: 'monospace',
                  )),
              Text(label,
                  style: TextStyle(
                    color: fg,
                    fontSize: 10,
                    fontWeight: FontWeight.w700,
                  )),
            ],
          ),
        ),
      );
    }

    return Row(
      children: [
        cell('TODO', 'جديد', BrandColors.slate200, BrandColors.slate700),
        const SizedBox(width: 6),
        cell('IN_PROGRESS', 'جاري', BrandColors.blue100, BrandColors.blue700),
        const SizedBox(width: 6),
        cell('REVIEW', 'مراجعة', BrandColors.amber50, BrandColors.amber700),
        const SizedBox(width: 6),
        cell('BLOCKED', 'متوقف', BrandColors.red50, BrandColors.red700),
        const SizedBox(width: 6),
        cell('DONE', 'منجز', BrandColors.emerald100, BrandColors.emerald700),
      ],
    );
  }
}

class _TaskRow extends StatelessWidget {
  final Map<String, dynamic> t;
  const _TaskRow({required this.t});
  @override
  Widget build(BuildContext context) {
    final title = t['title']?.toString() ?? '—';
    final deadline = (t['deadline'] as String?)?.substring(0, 10);
    final isOverdue = t['is_overdue'] == true;
    final project = t['project'];
    final priority = t['priority']?.toString();
    return Material(
      color: Colors.transparent,
      child: InkWell(
        borderRadius: BorderRadius.circular(12),
        // MARSOUD-MOBILE-SHIP-READY-01 (H2) — push so back returns
        // to Tasks, not /home.
        onTap: () => context.push('/tasks/${t['id']}'),
        child: Container(
          margin: const EdgeInsets.only(bottom: 8),
          padding: const EdgeInsets.all(12),
          decoration: BoxDecoration(
            color: Colors.white,
            borderRadius: BorderRadius.circular(12),
            border: Border.all(color: BrandColors.slate200),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Expanded(
                    child: Text(
                      title,
                      style: const TextStyle(
                        color: BrandColors.navy900,
                        fontWeight: FontWeight.w700,
                        fontSize: 13,
                      ),
                    ),
                  ),
                  if (priority != null)
                    _prioBadge(priority),
                ],
              ),
              const SizedBox(height: 8),
              Row(
                children: [
                  if (project is Map) ...[
                    _projectChip(project['name']?.toString() ?? ''),
                    const SizedBox(width: 8),
                  ],
                  if (deadline != null)
                    Row(
                      children: [
                        Icon(
                          isOverdue
                              ? Icons.warning_amber
                              : Icons.calendar_today,
                          size: 12,
                          color: isOverdue
                              ? BrandColors.red700
                              : BrandColors.slate500,
                        ),
                        const SizedBox(width: 4),
                        Text(
                          deadline,
                          style: TextStyle(
                            color: isOverdue
                                ? BrandColors.red700
                                : BrandColors.slate500,
                            fontSize: 11,
                            fontFamily: 'monospace',
                            fontWeight:
                                isOverdue ? FontWeight.w800 : FontWeight.w600,
                          ),
                        ),
                      ],
                    ),
                  const Spacer(),
                  const Icon(Icons.chevron_left,
                      color: BrandColors.slate400, size: 18),
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _projectChip(String name) => Container(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
        decoration: BoxDecoration(
          color: BrandColors.blue100,
          borderRadius: BorderRadius.circular(999),
        ),
        child: Text(
          name,
          style: const TextStyle(
            color: BrandColors.blue700,
            fontSize: 10,
            fontWeight: FontWeight.w700,
          ),
        ),
      );

  Widget _prioBadge(String p) {
    late Color bg;
    late Color fg;
    late String label;
    switch (p) {
      case 'URGENT':
        bg = BrandColors.red50;
        fg = BrandColors.red700;
        label = 'عاجلة';
        break;
      case 'HIGH':
        bg = BrandColors.amber50;
        fg = BrandColors.amber700;
        label = 'عالية';
        break;
      case 'LOW':
        bg = BrandColors.slate100;
        fg = BrandColors.slate500;
        label = 'منخفضة';
        break;
      default:
        bg = BrandColors.slate100;
        fg = BrandColors.slate700;
        label = 'متوسطة';
    }
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
      decoration: BoxDecoration(
        color: bg,
        borderRadius: BorderRadius.circular(999),
      ),
      child: Text(label,
          style: TextStyle(
              color: fg, fontSize: 10, fontWeight: FontWeight.w700)),
    );
  }
}
