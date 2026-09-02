// MARSOUD-MOBILE-FLUTTER — project detail (matches projects/view.html).
//
// Header card (name / manager / dates / progress) + task list scoped
// to the caller (assigned_to_me=true by default).
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../app/theme.dart';
import '../../data/api_client.dart';
import '../../data/my_account_repository.dart';
import '../../widgets/section_card.dart';

class ProjectDetailScreen extends ConsumerStatefulWidget {
  final int projectId;
  const ProjectDetailScreen({super.key, required this.projectId});
  @override
  ConsumerState<ProjectDetailScreen> createState() =>
      _ProjectDetailScreenState();
}

class _ProjectDetailScreenState extends ConsumerState<ProjectDetailScreen> {
  late Future<List<Map<String, dynamic>>> _future;

  @override
  void initState() {
    super.initState();
    _future = _load();
  }

  Future<List<Map<String, dynamic>>> _load() async {
    final repo = ref.read(myAccountRepoProvider);
    // MARSOUD-MOBILE-SHIP-READY-01 (M9) — was two sequential awaits
    // (~2× latency). The two endpoints are independent — fetch in
    // parallel. Failing one still fails the whole detail card
    // (matches previous behaviour); a per-section error boundary
    // would need a wider refactor.
    final results = await Future.wait([
      repo.projectDetail(widget.projectId),
      repo.projectTasks(widget.projectId),
    ]);
    return results;
  }

  @override
  Widget build(BuildContext context) {
    return FutureBuilder<List<Map<String, dynamic>>>(
      future: _future,
      builder: (context, snap) {
        if (!snap.hasData && !snap.hasError) {
          return const Center(child: CircularProgressIndicator());
        }
        if (snap.hasError) {
          return Center(
            child: Padding(
              padding: const EdgeInsets.all(24),
              child: Text(snap.error is ApiException
                  ? (snap.error as ApiException).message
                  : snap.error.toString()),
            ),
          );
        }
        final detail = snap.data![0]['project'] as Map<String, dynamic>;
        final tasks = (snap.data![1]['tasks'] as List)
            .cast<Map<String, dynamic>>();
        final manager = detail['manager'];
        final customer = detail['customer'];
        final progress = (detail['progress_pct'] as num?)?.toDouble() ?? 0;
        return RefreshIndicator(
          color: BrandColors.emerald600,
          onRefresh: () async {
            setState(() => _future = _load());
          },
          child: ListView(
            padding: const EdgeInsets.fromLTRB(12, 16, 12, 32),
            children: [
              SectionCard(
                emoji: '📂',
                title: detail['name']?.toString() ?? '—',
                subtitle: detail['notes']?.toString(),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    _kv('العميل',
                        customer is Map
                            ? (customer['name'] ?? '—').toString()
                            : '—'),
                    _kv(
                        'مدير المشروع',
                        manager is Map
                            ? (manager['name'] ?? '—').toString()
                            : '—'),
                    _kv('يبدأ',
                        (detail['start_date'] as String?)?.substring(0, 10) ??
                            '—',
                        mono: true),
                    _kv('ينتهي',
                        (detail['end_date'] as String?)?.substring(0, 10) ??
                            '—',
                        mono: true),
                    const SizedBox(height: 10),
                    Row(
                      children: [
                        Text('${progress.toStringAsFixed(0)}%',
                            style: const TextStyle(
                              color: BrandColors.emerald700,
                              fontFamily: 'monospace',
                              fontSize: 14,
                              fontWeight: FontWeight.w800,
                            )),
                        const SizedBox(width: 10),
                        Expanded(
                          child: ClipRRect(
                            borderRadius: BorderRadius.circular(999),
                            child: LinearProgressIndicator(
                              value: progress / 100,
                              minHeight: 8,
                              backgroundColor: BrandColors.slate100,
                              valueColor: const AlwaysStoppedAnimation(
                                  BrandColors.emerald500),
                            ),
                          ),
                        ),
                      ],
                    ),
                  ],
                ),
              ),
              const SizedBox(height: 12),
              SectionCard(
                emoji: '✅',
                title: 'مهامي في المشروع',
                child: tasks.isEmpty
                    ? const EmptyState(
                        icon: Icons.task_alt,
                        message: 'ما فيش مهام مسندة لك في هذا المشروع.',
                      )
                    : Column(
                        children: [
                          for (final t in tasks) _MiniTaskRow(t: t),
                        ],
                      ),
              ),
            ],
          ),
        );
      },
    );
  }

  Widget _kv(String k, String v, {bool mono = false}) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 4),
        child: Row(
          children: [
            SizedBox(
              width: 110,
              child: Text(k,
                  style: const TextStyle(
                    color: BrandColors.slate500,
                    fontSize: 12,
                  )),
            ),
            Expanded(
              child: Text(
                v,
                style: TextStyle(
                  color: BrandColors.navy900,
                  fontSize: 13,
                  fontWeight: FontWeight.w700,
                  fontFamily: mono ? 'monospace' : null,
                ),
              ),
            ),
          ],
        ),
      );
}

class _MiniTaskRow extends StatelessWidget {
  final Map<String, dynamic> t;
  const _MiniTaskRow({required this.t});
  @override
  Widget build(BuildContext context) {
    final title = t['title']?.toString() ?? '—';
    final status = t['status'];
    final statusVal = status is Map ? status['value'] : status?.toString();
    late final StatusBadge b;
    switch (statusVal) {
      case 'DONE':
        b = StatusBadge.approved('منجزة');
        break;
      case 'IN_PROGRESS':
        b = StatusBadge.pending('جارية');
        break;
      case 'REVIEW':
        b = StatusBadge.partial('مراجعة');
        break;
      case 'BLOCKED':
        b = StatusBadge.overdue('متوقفة');
        break;
      default:
        b = StatusBadge.draft('جديد');
    }
    return Material(
      color: Colors.transparent,
      child: InkWell(
        borderRadius: BorderRadius.circular(10),
        onTap: () => context.push('/tasks/${t['id']}'),
        child: Container(
          margin: const EdgeInsets.only(bottom: 6),
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
          decoration: BoxDecoration(
            color: BrandColors.slate50,
            borderRadius: BorderRadius.circular(10),
          ),
          child: Row(
            children: [
              Expanded(
                child: Text(title,
                    style: const TextStyle(
                      color: BrandColors.navy900,
                      fontSize: 13,
                      fontWeight: FontWeight.w700,
                    )),
              ),
              b,
              const SizedBox(width: 4),
              const Icon(Icons.chevron_left,
                  color: BrandColors.slate400, size: 16),
            ],
          ),
        ),
      ),
    );
  }
}
