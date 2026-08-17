// MARSOUD-MOBILE-FLUTTER — سجل نشاطي (mirrors portal_emp/activity.html).
//
// Read-only, own actions + sessions, last 90 days — API enforces the
// user_id override so the query string can't widen the scope.
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/theme.dart';
import '../../data/api_client.dart';
import '../../data/my_account_repository.dart';
import '../../widgets/section_card.dart';

final _activityProvider = FutureProvider.autoDispose<Map<String, dynamic>>(
    (ref) => ref.watch(myAccountRepoProvider).activity());

class ActivityScreen extends ConsumerWidget {
  const ActivityScreen({super.key});
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final async = ref.watch(_activityProvider);
    return async.when(
      loading: () => const Center(child: CircularProgressIndicator()),
      error: (e, _) => Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Text(e is ApiException ? e.message : e.toString()),
        ),
      ),
      data: (data) {
        final actions = (data['activities'] as List?)
                ?.cast<Map<String, dynamic>>() ??
            const [];
        final sessions = (data['sessions'] as List?)
                ?.cast<Map<String, dynamic>>() ??
            const [];
        return RefreshIndicator(
          color: BrandColors.emerald600,
          onRefresh: () async => ref.invalidate(_activityProvider),
          child: ListView(
            padding: const EdgeInsets.fromLTRB(12, 16, 12, 32),
            children: [
              SectionCard(
                emoji: '📜',
                title: 'آخر أعمالي',
                subtitle: 'آخر 500 حركة على حسابك خلال 90 يوم.',
                child: actions.isEmpty
                    ? const EmptyState(
                        icon: Icons.timeline,
                        message: 'لا توجد أنشطة مسجّلة.',
                      )
                    : Column(
                        children: [
                          for (final a in actions.take(60))
                            _ActionRow(a: a),
                        ],
                      ),
              ),
              const SizedBox(height: 12),
              SectionCard(
                emoji: '🔐',
                title: 'جلسات الدخول',
                subtitle: 'آخر 200 جلسة، مع الجهاز والـ IP وقت الدخول.',
                child: sessions.isEmpty
                    ? const EmptyState(
                        icon: Icons.login,
                        message: 'لا توجد جلسات مسجّلة.',
                      )
                    : Column(
                        children: [
                          for (final s in sessions.take(30))
                            _SessionRow(s: s),
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

class _ActionRow extends StatelessWidget {
  final Map<String, dynamic> a;
  const _ActionRow({required this.a});
  @override
  Widget build(BuildContext context) {
    final at = (a['created_at'] as String?) ?? '';
    return Container(
      margin: const EdgeInsets.only(bottom: 6),
      padding: const EdgeInsets.symmetric(vertical: 8, horizontal: 10),
      decoration: BoxDecoration(
        color: BrandColors.slate50,
        borderRadius: BorderRadius.circular(8),
      ),
      child: Row(
        children: [
          Container(
            width: 32,
            height: 32,
            decoration: BoxDecoration(
              color: BrandColors.emerald50,
              borderRadius: BorderRadius.circular(8),
            ),
            alignment: Alignment.center,
            child: Text(
              _iconFor(a['action_type']?.toString() ?? ''),
              style: const TextStyle(fontSize: 15),
            ),
          ),
          const SizedBox(width: 10),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  '${a['action_type'] ?? '—'} · ${a['entity_type'] ?? ''}',
                  style: const TextStyle(
                    color: BrandColors.navy900,
                    fontWeight: FontWeight.w700,
                    fontSize: 12,
                  ),
                  overflow: TextOverflow.ellipsis,
                ),
                if (a['entity_label'] != null)
                  Text(
                    (a['entity_label']).toString(),
                    style: const TextStyle(
                      color: BrandColors.slate500,
                      fontSize: 11,
                    ),
                    overflow: TextOverflow.ellipsis,
                  ),
              ],
            ),
          ),
          Text(
            _short(at),
            style: const TextStyle(
              color: BrandColors.slate400,
              fontSize: 10,
              fontFamily: 'monospace',
            ),
          ),
        ],
      ),
    );
  }

  static String _iconFor(String action) {
    if (action.contains('CREATE')) return '➕';
    if (action.contains('UPDATE') || action.contains('EDIT')) return '✎';
    if (action.contains('DELETE')) return '🗑';
    if (action.contains('LOGIN')) return '🔑';
    if (action.contains('LOGOUT')) return '👋';
    if (action.contains('APPROVE')) return '✅';
    if (action.contains('REJECT')) return '✖';
    return '•';
  }

  static String _short(String iso) {
    if (iso.length < 16) return iso;
    return '${iso.substring(5, 10)} ${iso.substring(11, 16)}';
  }
}

class _SessionRow extends StatelessWidget {
  final Map<String, dynamic> s;
  const _SessionRow({required this.s});
  @override
  Widget build(BuildContext context) {
    final login = (s['login_at'] as String?) ?? '';
    final logout = (s['logout_at'] as String?);
    final ip = s['ip']?.toString();
    return Container(
      margin: const EdgeInsets.only(bottom: 6),
      padding: const EdgeInsets.symmetric(vertical: 8, horizontal: 10),
      decoration: BoxDecoration(
        color: BrandColors.slate50,
        borderRadius: BorderRadius.circular(8),
      ),
      child: Row(
        children: [
          Icon(
            logout == null ? Icons.circle : Icons.check_circle,
            color: logout == null ? BrandColors.emerald500 : BrandColors.slate400,
            size: 12,
          ),
          const SizedBox(width: 8),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Directionality(
                  textDirection: TextDirection.ltr,
                  child: Text(
                    _short(login),
                    style: const TextStyle(
                      color: BrandColors.navy900,
                      fontWeight: FontWeight.w700,
                      fontSize: 12,
                      fontFamily: 'monospace',
                    ),
                  ),
                ),
                if (ip != null)
                  Text(
                    'IP: $ip',
                    style: const TextStyle(
                      color: BrandColors.slate500,
                      fontSize: 11,
                      fontFamily: 'monospace',
                    ),
                  ),
              ],
            ),
          ),
          if (logout == null)
            const _NowChip()
          else
            Text(
              _short(logout),
              style: const TextStyle(
                color: BrandColors.slate400,
                fontSize: 10,
                fontFamily: 'monospace',
              ),
            ),
        ],
      ),
    );
  }

  static String _short(String iso) {
    if (iso.length < 16) return iso;
    return '${iso.substring(5, 10)} ${iso.substring(11, 16)}';
  }
}

class _NowChip extends StatelessWidget {
  const _NowChip();
  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
      decoration: BoxDecoration(
        color: BrandColors.emerald100,
        borderRadius: BorderRadius.circular(999),
      ),
      child: const Text(
        'نشطة',
        style: TextStyle(
          color: BrandColors.emerald700,
          fontSize: 10,
          fontWeight: FontWeight.w800,
        ),
      ),
    );
  }
}
