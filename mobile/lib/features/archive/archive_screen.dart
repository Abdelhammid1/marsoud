// MARSOUD-MOBILE-FLUTTER — أرشيف مهامي (mirrors portal_emp/archive.html).
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/theme.dart';
import '../../data/api_client.dart';
import '../../data/my_account_repository.dart';
import '../../widgets/section_card.dart';

final _archiveProvider = FutureProvider.autoDispose<Map<String, dynamic>>(
    (ref) => ref.watch(myAccountRepoProvider).archive());

class ArchiveScreen extends ConsumerWidget {
  const ArchiveScreen({super.key});
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final async = ref.watch(_archiveProvider);
    return async.when(
      loading: () => const Center(child: CircularProgressIndicator()),
      error: (e, _) => Center(
          child: Padding(
            padding: const EdgeInsets.all(24),
            child: Text(e is ApiException ? e.message : e.toString()),
          )),
      data: (data) {
        final tasks = (data['tasks'] as List?)
                ?.cast<Map<String, dynamic>>() ??
            const [];
        return RefreshIndicator(
          color: BrandColors.emerald600,
          onRefresh: () async => ref.invalidate(_archiveProvider),
          child: ListView(
            padding: const EdgeInsets.fromLTRB(12, 16, 12, 32),
            children: [
              SectionCard(
                emoji: '🗂',
                title: 'أرشيف مهامي',
                subtitle:
                    'المهام اللي كانت مسندة لك أو أنشأتها وتم إغلاقها. '
                    'الأرشفة التلقائية تحصل بعد 30 يوم من الإتمام.',
                child: tasks.isEmpty
                    ? const EmptyState(
                        icon: Icons.inbox_outlined,
                        message: 'لا توجد مهام مؤرشفة في حسابك.\n'
                            'المهام المكتملة بيتم أرشفتها بعد 30 يوم.',
                      )
                    : Column(
                        children: [
                          for (final t in tasks)
                            _ArchivedTaskRow(
                              t: t,
                              onRestore: () async {
                                final messenger =
                                    ScaffoldMessenger.of(context);
                                try {
                                  await ref
                                      .read(myAccountRepoProvider)
                                      .restoreArchived(t['id'] as int);
                                  ref.invalidate(_archiveProvider);
                                  messenger.showSnackBar(SnackBar(
                                    content: Text(
                                        '↩ تم استعادة: ${t['title'] ?? ''}'),
                                  ));
                                } on ApiException catch (e) {
                                  messenger.showSnackBar(
                                    SnackBar(content: Text(e.message)),
                                  );
                                }
                              },
                            ),
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

class _ArchivedTaskRow extends StatelessWidget {
  final Map<String, dynamic> t;
  final Future<void> Function() onRestore;
  const _ArchivedTaskRow({required this.t, required this.onRestore});
  @override
  Widget build(BuildContext context) {
    final status = t['status'];
    final statusLabel = status is Map
        ? (status['label_ar'] ?? status['value'] ?? '').toString()
        : status?.toString() ?? '';
    final archivedAt = (t['archived_at'] as String?)?.substring(0, 16) ?? '—';
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: BrandColors.slate50,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: BrandColors.slate200),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text(
            (t['title'] ?? '—').toString(),
            style: const TextStyle(
              color: BrandColors.navy900,
              fontWeight: FontWeight.w800,
              fontSize: 14,
            ),
          ),
          const SizedBox(height: 6),
          Row(
            children: [
              StatusBadge.draft(statusLabel),
              const SizedBox(width: 8),
              Text(
                archivedAt.replaceAll('T', ' '),
                style: const TextStyle(
                  color: BrandColors.slate500,
                  fontSize: 11,
                  fontFamily: 'monospace',
                ),
              ),
            ],
          ),
          const SizedBox(height: 8),
          Align(
            alignment: Alignment.centerLeft,
            child: TextButton.icon(
              onPressed: () async {
                final ok = await showDialog<bool>(
                  context: context,
                  builder: (ctx) => AlertDialog(
                    title: const Text('استعادة المهمة'),
                    content: const Text('استعادة المهمة للبورد؟'),
                    actions: [
                      TextButton(
                        onPressed: () => Navigator.of(ctx).pop(false),
                        child: const Text('إلغاء'),
                      ),
                      FilledButton(
                        onPressed: () => Navigator.of(ctx).pop(true),
                        child: const Text('استعادة'),
                      ),
                    ],
                  ),
                );
                if (ok == true) await onRestore();
              },
              icon: const Icon(Icons.restore,
                  color: BrandColors.emerald700, size: 16),
              label: const Text(
                'استعادة',
                style: TextStyle(
                  color: BrandColors.emerald700,
                  fontWeight: FontWeight.w700,
                  fontSize: 12,
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}
