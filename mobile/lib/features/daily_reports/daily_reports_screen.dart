// MARSOUD-MOBILE-FLUTTER — تقاريري اليومية (mirrors portal_emp/daily_reports_list.html).
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'package:go_router/go_router.dart';

import '../../app/theme.dart';
import '../../data/api_client.dart';
import '../../data/my_account_repository.dart';
import '../../widgets/section_card.dart';

final _reportsProvider = FutureProvider.autoDispose<Map<String, dynamic>>(
    (ref) => ref.watch(myAccountRepoProvider).dailyReports());

class DailyReportsScreen extends ConsumerWidget {
  const DailyReportsScreen({super.key});
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final async = ref.watch(_reportsProvider);
    return async.when(
      loading: () => const Center(child: CircularProgressIndicator()),
      error: (e, _) => Center(
          child: Padding(
            padding: const EdgeInsets.all(24),
            child: Text(e is ApiException ? e.message : e.toString()),
          )),
      data: (data) {
        final reports = (data['reports'] as List?)
                ?.cast<Map<String, dynamic>>() ??
            const [];
        return RefreshIndicator(
          color: BrandColors.emerald600,
          onRefresh: () async => ref.invalidate(_reportsProvider),
          child: ListView(
            padding: const EdgeInsets.fromLTRB(12, 16, 12, 32),
            children: [
              SectionCard(
                emoji: '📝',
                title: 'تقاريري اليومية',
                subtitle:
                    'تقارير تلقائية بيتم بناؤها في نهاية كل يوم من نشاطك. راجعها، أضف ملاحظاتك، وأرسلها للمالك.',
                child: reports.isEmpty
                    ? const EmptyState(
                        icon: Icons.description_outlined,
                        message: 'لا توجد تقارير بعد.\n'
                            'التقارير بتتولد تلقائياً عند نهاية اليوم.',
                      )
                    : Column(
                        children: [
                          for (final r in reports)
                            InkWell(
                              onTap: () => context.go(
                                  '/daily-reports/${r['id']}'),
                              borderRadius: BorderRadius.circular(12),
                              child: _ReportRow(r: r),
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

class _ReportRow extends StatelessWidget {
  final Map<String, dynamic> r;
  const _ReportRow({required this.r});
  @override
  Widget build(BuildContext context) {
    final date = (r['report_date'] as String?)?.substring(0, 10) ?? '—';
    final status = r['status'];
    final val = status is Map ? status['value'] : status;
    final isDraft = val == 'DRAFT';
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: isDraft ? BrandColors.amber50.withValues(alpha: 0.35) : BrandColors.slate50,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(
          color: isDraft ? const Color(0xFFFDE68A) : BrandColors.slate200,
        ),
      ),
      child: Row(
        children: [
          Container(
            width: 44,
            height: 44,
            decoration: BoxDecoration(
              color: isDraft ? BrandColors.amber50 : BrandColors.emerald50,
              borderRadius: BorderRadius.circular(10),
            ),
            alignment: Alignment.center,
            child: Text(
              isDraft ? '📝' : '✅',
              style: const TextStyle(fontSize: 18),
            ),
          ),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  date,
                  style: const TextStyle(
                    color: BrandColors.navy900,
                    fontWeight: FontWeight.w800,
                    fontSize: 14,
                    fontFamily: 'monospace',
                  ),
                ),
                const SizedBox(height: 3),
                Text(
                  isDraft
                      ? 'مسودة — محتاج ترسله'
                      : 'اتبعت للمالك',
                  style: TextStyle(
                    color: isDraft
                        ? BrandColors.amber700
                        : BrandColors.emerald700,
                    fontSize: 11,
                    fontWeight: FontWeight.w700,
                  ),
                ),
              ],
            ),
          ),
          const Icon(Icons.chevron_left, color: BrandColors.slate400),
        ],
      ),
    );
  }
}
