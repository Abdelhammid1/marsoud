// MARSOUD-MOBILE-DASHBOARD-01 (2026-09-17) — Batch 3 tail: landing
// screen with KPI tiles + quick actions.  Users kept saying the
// old landing (My Account) buried the two questions they actually
// open the app to answer: "what do I owe today?" and "what came
// in overnight?".  This screen leads with the answers.
//
// Data sources are all existing endpoints — the dashboard is a
// read-only composite view, so nothing new lands on the wire.
// The BottomNavigationBar (see home_shell.dart) uses / as its
// canonical route; /dashboard is the alias so the intent is
// obvious from the URL.
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../app/theme.dart';
import '../../data/api_client.dart';
import '../../data/auth_state.dart';
import '../../data/mobile_extras_repository.dart';
import '../../data/my_account_repository.dart';
import '../home/home_shell.dart' show unreadCountProvider;

final _tasksSummary = FutureProvider.autoDispose<Map<String, dynamic>>((ref) {
  return ref.watch(myAccountRepoProvider).myTasks(limit: 100);
});

final _leadsSummary =
    FutureProvider.autoDispose<Map<String, dynamic>>((ref) {
  return ref.watch(mobileExtrasRepoProvider).leads();
});

final _meetingsSummary =
    FutureProvider.autoDispose<Map<String, dynamic>>((ref) {
  return ref.watch(mobileExtrasRepoProvider).meetings(days: 7);
});

class DashboardScreen extends ConsumerWidget {
  const DashboardScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final session = ref.watch(authProvider).value;
    final tasksAsync = ref.watch(_tasksSummary);
    final leadsAsync = ref.watch(_leadsSummary);
    final meetingsAsync = ref.watch(_meetingsSummary);
    final unreadAsync = ref.watch(unreadCountProvider);

    int openTasks(Map<String, dynamic>? m) {
      final rows =
          (m?['tasks'] as List?)?.cast<Map<String, dynamic>>() ?? const [];
      return rows.where((t) {
        final st = (t['status'] ?? '').toString().toUpperCase();
        return st != 'DONE' && st != 'CANCELLED' && st != 'ARCHIVED';
      }).length;
    }

    int activeLeads(Map<String, dynamic>? m) {
      final rows =
          (m?['leads'] as List?)?.cast<Map<String, dynamic>>() ?? const [];
      return rows.where((l) {
        final st = (l['status'] ?? '').toString().toUpperCase();
        return st != 'WON' && st != 'LOST' && st != 'ARCHIVED';
      }).length;
    }

    int thisWeekMeetings(Map<String, dynamic>? m) {
      return (m?['meetings'] as List?)?.length ?? 0;
    }

    return RefreshIndicator(
      color: BrandColors.emerald600,
      onRefresh: () async {
        ref.invalidate(_tasksSummary);
        ref.invalidate(_leadsSummary);
        ref.invalidate(_meetingsSummary);
        ref.invalidate(unreadCountProvider);
      },
      child: ListView(
        padding: const EdgeInsets.fromLTRB(12, 12, 12, 24),
        children: [
          // Greeting
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 4),
            child: Text(
              'أهلاً، ${session?.user.name.split(' ').first ?? ''} 👋',
              style: const TextStyle(
                fontSize: 18,
                fontWeight: FontWeight.w800,
                color: BrandColors.navy900,
              ),
            ),
          ),
          const SizedBox(height: 8),

          // KPI grid — 2×2
          GridView.count(
            crossAxisCount: 2,
            crossAxisSpacing: 10,
            mainAxisSpacing: 10,
            childAspectRatio: 1.5,
            shrinkWrap: true,
            physics: const NeverScrollableScrollPhysics(),
            children: [
              _KpiTile(
                emoji: '✅',
                label: 'مهامي المفتوحة',
                value: tasksAsync.when(
                  data: (m) => openTasks(m).toString(),
                  loading: () => '…',
                  error: (_, __) => '—',
                ),
                accent: BrandColors.emerald600,
                onTap: () => context.go('/tasks'),
              ),
              _KpiTile(
                emoji: '🎯',
                label: 'عملائي المحتملين',
                value: leadsAsync.when(
                  data: (m) => activeLeads(m).toString(),
                  loading: () => '…',
                  error: (_, __) => '—',
                ),
                accent: const Color(0xFF2563EB),
                onTap: () => context.go('/leads'),
              ),
              _KpiTile(
                emoji: '📅',
                label: 'اجتماعات هذا الأسبوع',
                value: meetingsAsync.when(
                  data: (m) => thisWeekMeetings(m).toString(),
                  loading: () => '…',
                  error: (_, __) => '—',
                ),
                accent: const Color(0xFF9333EA),
                onTap: () => context.go('/meetings'),
              ),
              _KpiTile(
                emoji: '🔔',
                label: 'إشعارات لم تُقرأ',
                value: (unreadAsync.value ?? 0).toString(),
                accent: const Color(0xFFDC2626),
                onTap: () => context.go('/notifications'),
              ),
            ],
          ),

          const SizedBox(height: 20),
          const _SectionHeader(text: 'إجراءات سريعة'),
          const SizedBox(height: 8),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              _QuickAction(
                emoji: '➕',
                label: 'مهمة جديدة',
                onTap: () => context.go('/tasks/new'),
              ),
              _QuickAction(
                emoji: '🎯',
                label: 'عميل محتمل جديد',
                onTap: () => context.go('/leads'),
              ),
              _QuickAction(
                emoji: '📅',
                label: 'اجتماعاتي',
                onTap: () => context.go('/meetings'),
              ),
              _QuickAction(
                emoji: '👤',
                label: 'حسابي',
                onTap: () => context.go('/home'),
              ),
            ],
          ),

          const SizedBox(height: 20),
          const _SectionHeader(text: 'قادم الأسبوع'),
          const SizedBox(height: 8),
          meetingsAsync.when(
            loading: () => const Padding(
              padding: EdgeInsets.symmetric(vertical: 20),
              child: Center(child: CircularProgressIndicator()),
            ),
            error: (e, _) => Text(
              e is ApiException ? e.message : e.toString(),
              style: const TextStyle(color: BrandColors.slate500, fontSize: 12),
            ),
            data: (m) {
              final meetings =
                  (m['meetings'] as List?)?.cast<Map<String, dynamic>>() ??
                      const [];
              if (meetings.isEmpty) {
                return Container(
                  padding: const EdgeInsets.all(16),
                  decoration: BoxDecoration(
                    color: Colors.white,
                    borderRadius: BorderRadius.circular(10),
                    border: Border.all(color: BrandColors.slate200),
                  ),
                  child: const Text(
                    'ما فيش اجتماعات مجدولة في الأسبوع الجاي.',
                    style: TextStyle(
                      color: BrandColors.slate500,
                      fontSize: 12.5,
                    ),
                  ),
                );
              }
              return Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  for (final mt in meetings.take(5))
                    _UpcomingRow(meeting: mt),
                ],
              );
            },
          ),
        ],
      ),
    );
  }
}

class _KpiTile extends StatelessWidget {
  final String emoji;
  final String label;
  final String value;
  final Color accent;
  final VoidCallback onTap;
  const _KpiTile({
    required this.emoji,
    required this.label,
    required this.value,
    required this.accent,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return Material(
      color: Colors.white,
      borderRadius: BorderRadius.circular(12),
      elevation: 0,
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(12),
        child: Container(
          padding: const EdgeInsets.all(12),
          decoration: BoxDecoration(
            border: Border.all(color: BrandColors.slate200),
            borderRadius: BorderRadius.circular(12),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              Row(
                children: [
                  Text(emoji, style: const TextStyle(fontSize: 20)),
                  const SizedBox(width: 6),
                  Expanded(
                    child: Text(
                      label,
                      style: const TextStyle(
                        fontSize: 11.5,
                        color: BrandColors.slate500,
                        fontWeight: FontWeight.w600,
                      ),
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                    ),
                  ),
                ],
              ),
              Align(
                alignment: Alignment.centerRight,
                child: Text(
                  value,
                  style: TextStyle(
                    fontSize: 26,
                    fontWeight: FontWeight.w800,
                    color: accent,
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _QuickAction extends StatelessWidget {
  final String emoji;
  final String label;
  final VoidCallback onTap;
  const _QuickAction({
    required this.emoji,
    required this.label,
    required this.onTap,
  });
  @override
  Widget build(BuildContext context) {
    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(999),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
        decoration: BoxDecoration(
          color: BrandColors.emerald50,
          borderRadius: BorderRadius.circular(999),
          border: Border.all(color: BrandColors.emerald100),
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(emoji, style: const TextStyle(fontSize: 15)),
            const SizedBox(width: 6),
            Text(
              label,
              style: const TextStyle(
                fontSize: 12.5,
                fontWeight: FontWeight.w700,
                color: BrandColors.emerald700,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _SectionHeader extends StatelessWidget {
  final String text;
  const _SectionHeader({required this.text});
  @override
  Widget build(BuildContext context) {
    return Text(
      text,
      style: const TextStyle(
        fontSize: 13,
        fontWeight: FontWeight.w800,
        color: BrandColors.navy900,
      ),
    );
  }
}

class _UpcomingRow extends StatelessWidget {
  final Map<String, dynamic> meeting;
  const _UpcomingRow({required this.meeting});
  @override
  Widget build(BuildContext context) {
    final title = (meeting['title'] ?? '').toString();
    final when = (meeting['starts_at'] ?? '')
        .toString()
        .replaceAll('T', ' ')
        .split('.')
        .first;
    final source = (meeting['source'] ?? '').toString();
    final icon = source == 'lead_activity' ? '🎯' : '📅';
    return Container(
      margin: const EdgeInsets.only(bottom: 6),
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: BrandColors.slate200),
      ),
      child: Row(
        children: [
          Text(icon, style: const TextStyle(fontSize: 18)),
          const SizedBox(width: 10),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  title,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                    fontSize: 12.5,
                    fontWeight: FontWeight.w700,
                    color: BrandColors.navy900,
                  ),
                ),
                Text(
                  when,
                  style: const TextStyle(
                    fontFamily: 'monospace',
                    fontSize: 11,
                    color: BrandColors.slate500,
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}
