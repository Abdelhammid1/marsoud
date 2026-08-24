// MARSOUD-MOBILE-TKT-01 (2026-08-18) — جدولي. Read-only list of
// TaskSchedule rows I own or am a member of.
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/theme.dart';
import '../../data/api_client.dart';
import '../../data/mobile_extras_repository.dart';
import '../../widgets/section_card.dart';

final _schedulesProvider =
    FutureProvider.autoDispose<Map<String, dynamic>>((ref) {
  return ref.watch(mobileExtrasRepoProvider).schedules();
});

class ScheduleScreen extends ConsumerWidget {
  const ScheduleScreen({super.key});
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final async = ref.watch(_schedulesProvider);
    return async.when(
      loading: () => const Center(child: CircularProgressIndicator()),
      error: (e, _) => Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Text(e is ApiException ? e.message : e.toString()),
        ),
      ),
      data: (data) {
        final rows = (data['schedules'] as List?)
                ?.cast<Map<String, dynamic>>() ??
            const [];
        return RefreshIndicator(
          color: BrandColors.emerald600,
          onRefresh: () async => ref.invalidate(_schedulesProvider),
          child: ListView(
            padding: const EdgeInsets.fromLTRB(12, 12, 12, 32),
            children: [
              if (rows.isEmpty)
                SectionCard(
                  child: const EmptyState(
                    icon: Icons.schedule,
                    message: 'ما فيش جدولات متكررة عندك.',
                  ),
                )
              else
                for (final s in rows) _ScheduleCard(schedule: s),
            ],
          ),
        );
      },
    );
  }
}

class _ScheduleCard extends StatelessWidget {
  final Map<String, dynamic> schedule;
  const _ScheduleCard({required this.schedule});
  @override
  Widget build(BuildContext context) {
    final active = schedule['active'] == true;
    return Card(
      margin: const EdgeInsets.only(bottom: 8),
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(
                    schedule['title']?.toString() ?? '',
                    style: const TextStyle(
                        fontWeight: FontWeight.w700,
                        fontSize: 13,
                        color: BrandColors.navy900),
                  ),
                ),
                Container(
                  padding: const EdgeInsets.symmetric(
                      horizontal: 8, vertical: 2),
                  decoration: BoxDecoration(
                    color: active
                        ? const Color(0xFFD1FAE5)
                        : BrandColors.slate100,
                    borderRadius: BorderRadius.circular(999),
                  ),
                  child: Text(
                    active ? 'نشط' : 'موقوف',
                    style: TextStyle(
                        fontSize: 10,
                        color: active
                            ? const Color(0xFF047857)
                            : BrandColors.slate500),
                  ),
                ),
              ],
            ),
            if ((schedule['description'] ?? '').toString().isNotEmpty) ...[
              const SizedBox(height: 6),
              Text(schedule['description'].toString(),
                  style: const TextStyle(
                      fontSize: 11, color: BrandColors.slate500)),
            ],
            const SizedBox(height: 6),
            Row(
              children: [
                const Icon(Icons.repeat,
                    size: 12, color: BrandColors.slate400),
                const SizedBox(width: 4),
                Text(
                  schedule['recurrence']?.toString() ?? '',
                  style: const TextStyle(
                      fontSize: 11, color: BrandColors.slate500),
                ),
                const SizedBox(width: 12),
                const Icon(Icons.play_arrow,
                    size: 12, color: BrandColors.slate400),
                const SizedBox(width: 4),
                Text(
                  '${schedule['generated_count'] ?? 0} تنفيذ',
                  style: const TextStyle(
                      fontSize: 11, color: BrandColors.slate500),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}
