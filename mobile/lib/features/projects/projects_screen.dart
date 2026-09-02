// MARSOUD-MOBILE-FLUTTER — المشاريع (mirrors projects/index.html).
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../app/theme.dart';
import '../../data/api_client.dart';
import '../../data/my_account_repository.dart';
import '../../widgets/section_card.dart';

final _projectsProvider = FutureProvider.autoDispose<Map<String, dynamic>>(
    (ref) => ref.watch(myAccountRepoProvider).projects());

class ProjectsScreen extends ConsumerWidget {
  const ProjectsScreen({super.key});
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final async = ref.watch(_projectsProvider);
    return async.when(
      loading: () => const Center(child: CircularProgressIndicator()),
      error: (e, _) => Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Text(e is ApiException ? e.message : e.toString()),
        ),
      ),
      data: (data) {
        final projects = (data['projects'] as List?)
                ?.cast<Map<String, dynamic>>() ??
            const [];
        return RefreshIndicator(
          color: BrandColors.emerald600,
          onRefresh: () async => ref.invalidate(_projectsProvider),
          child: ListView(
            padding: const EdgeInsets.fromLTRB(12, 16, 12, 32),
            children: [
              SectionCard(
                emoji: '📂',
                title: 'المشاريع',
                subtitle:
                    'كل مشاريع الشركة النشطة. اضغط أي واحد لتشوف مهامه.',
                child: projects.isEmpty
                    ? const EmptyState(
                        icon: Icons.folder_open,
                        message: 'لا يوجد مشاريع.',
                      )
                    : Column(
                        children: [
                          for (final p in projects)
                            _ProjectRow(p: p),
                        ],
                      ),
              ),
            ],
          ),
        );
      },
    );
  }
}

class _ProjectRow extends StatelessWidget {
  final Map<String, dynamic> p;
  const _ProjectRow({required this.p});
  @override
  Widget build(BuildContext context) {
    final name = p['name']?.toString() ?? '—';
    final status = p['status']?.toString() ?? 'ACTIVE';
    final progress = (p['progress_pct'] as num?)?.toDouble() ?? 0;
    final counts = (p['task_counts'] as Map?) ?? const {};
    final done = (counts['done'] as num?)?.toInt() ?? 0;
    final todo = (counts['todo'] as num?)?.toInt() ?? 0;
    final progresss = (counts['in_progress'] as num?)?.toInt() ?? 0;
    final total = done + todo + progresss;
    final endDate = (p['end_date'] as String?)?.substring(0, 10);
    return Material(
      color: Colors.transparent,
      child: InkWell(
        borderRadius: BorderRadius.circular(12),
        onTap: () => context.push('/projects/${p['id']}'),
        child: Container(
          margin: const EdgeInsets.only(bottom: 10),
          padding: const EdgeInsets.all(14),
          decoration: BoxDecoration(
            color: Colors.white,
            borderRadius: BorderRadius.circular(12),
            border: Border.all(color: BrandColors.slate200),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Row(
                children: [
                  Expanded(
                    child: Text(name,
                        style: const TextStyle(
                          color: BrandColors.navy900,
                          fontWeight: FontWeight.w800,
                          fontSize: 14,
                        )),
                  ),
                  _statusBadge(status),
                ],
              ),
              const SizedBox(height: 8),
              Row(
                children: [
                  Text('${progress.toStringAsFixed(0)}%',
                      style: const TextStyle(
                        color: BrandColors.emerald700,
                        fontWeight: FontWeight.w800,
                        fontFamily: 'monospace',
                        fontSize: 13,
                      )),
                  const SizedBox(width: 10),
                  Expanded(
                    child: ClipRRect(
                      borderRadius: BorderRadius.circular(999),
                      child: LinearProgressIndicator(
                        value: progress / 100,
                        minHeight: 6,
                        backgroundColor: BrandColors.slate100,
                        valueColor: const AlwaysStoppedAnimation(
                            BrandColors.emerald500),
                      ),
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 8),
              Row(
                children: [
                  Icon(Icons.task, size: 12, color: BrandColors.slate500),
                  const SizedBox(width: 4),
                  Text('$done / $total مهمة منجزة',
                      style: const TextStyle(
                        color: BrandColors.slate500,
                        fontSize: 11,
                      )),
                  const Spacer(),
                  if (endDate != null) ...[
                    const Icon(Icons.calendar_month,
                        size: 12, color: BrandColors.slate500),
                    const SizedBox(width: 4),
                    Text(endDate,
                        style: const TextStyle(
                          color: BrandColors.slate500,
                          fontSize: 11,
                          fontFamily: 'monospace',
                        )),
                  ],
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }

  static Widget _statusBadge(String s) {
    switch (s) {
      case 'PLANNING':
        return StatusBadge.draft('تخطيط');
      case 'IN_PROGRESS':
        return StatusBadge.pending('قيد التنفيذ');
      case 'REVIEW':
        return StatusBadge.partial('مراجعة');
      case 'DELIVERED':
        return StatusBadge.approved('مُسلَّم');
      case 'CLIENT_FEEDBACK':
        return StatusBadge.partial('تعليق العميل');
      case 'CLOSED':
        return StatusBadge.approved('مُغلق');
      default:
        return StatusBadge.draft(s);
    }
  }
}
