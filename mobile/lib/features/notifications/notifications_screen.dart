// MARSOUD-MOBILE-FLUTTER — notifications feed. Card list, mint accents,
// unread ones have a bold title + emerald dot.
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../app/theme.dart';
import '../../data/my_account_repository.dart';

final _notificationsProvider =
    FutureProvider.autoDispose<Map<String, dynamic>>(
        (ref) => ref.watch(myAccountRepoProvider).notifications());

class NotificationsScreen extends ConsumerWidget {
  const NotificationsScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final async = ref.watch(_notificationsProvider);
    return async.when(
      loading: () => const Center(child: CircularProgressIndicator()),
      error: (e, _) => Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Text(
            e.toString(),
            textAlign: TextAlign.center,
            style: const TextStyle(color: BrandColors.slate500),
          ),
        ),
      ),
      data: (data) {
        final items = (data['items'] as List?)
                ?.cast<Map<String, dynamic>>() ??
            const [];
        if (items.isEmpty) {
          return const _EmptyState();
        }
        return RefreshIndicator(
          color: BrandColors.emerald600,
          onRefresh: () async => ref.invalidate(_notificationsProvider),
          child: ListView.builder(
            padding: const EdgeInsets.fromLTRB(16, 20, 16, 32),
            itemCount: items.length,
            itemBuilder: (context, i) => _NotificationTile(
              n: items[i],
              onTap: () async {
                // MARSOUD-MOBILE-NOTIF-TAP-01 (2026-09-03) — used to
                // only mark-as-read. Now also navigates to the
                // notification's `link_url` when the mobile app
                // knows how to render that surface. The same
                // whitelist as push deep-links.
                if (items[i]['is_read'] != true) {
                  try {
                    await ref
                        .read(myAccountRepoProvider)
                        .markRead(items[i]['id'] as int);
                  } catch (_) {}
                  ref.invalidate(_notificationsProvider);
                }
                final linkUrl = items[i]['link_url']?.toString() ?? '';
                if (linkUrl.isEmpty) return;
                String? target;
                if (linkUrl.startsWith('/tasks/') ||
                    linkUrl.startsWith('/projects/') ||
                    linkUrl.startsWith('/leads/') ||
                    linkUrl.startsWith('/daily-reports/')) {
                  target = linkUrl;
                }
                if (target != null && context.mounted) {
                  context.push(target);
                } else if (context.mounted) {
                  // Rendered on desktop only — tell the user so
                  // they don't think the tap is broken.
                  ScaffoldMessenger.of(context).showSnackBar(
                    const SnackBar(
                      content: Text(
                        'هذا الإشعار يفتح على نسخة الويب فقط حاليًا.'),
                    ),
                  );
                }
              },
            ),
          ),
        );
      },
    );
  }
}

class _NotificationTile extends StatelessWidget {
  final Map<String, dynamic> n;
  final VoidCallback onTap;
  const _NotificationTile({required this.n, required this.onTap});

  @override
  Widget build(BuildContext context) {
    final read = n['is_read'] == true;
    return Container(
      margin: const EdgeInsets.only(bottom: 10),
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(
          color: read ? BrandColors.slate200 : BrandColors.emerald100,
        ),
      ),
      child: Material(
        color: Colors.transparent,
        borderRadius: BorderRadius.circular(14),
        child: InkWell(
          borderRadius: BorderRadius.circular(14),
          onTap: onTap,
          child: Padding(
            padding: const EdgeInsets.all(14),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Container(
                  width: 36,
                  height: 36,
                  decoration: BoxDecoration(
                    color: read
                        ? BrandColors.slate100
                        : BrandColors.emerald50,
                    borderRadius: BorderRadius.circular(10),
                  ),
                  alignment: Alignment.center,
                  child: Icon(
                    read
                        ? Icons.notifications_none
                        : Icons.notifications_active,
                    color: read
                        ? BrandColors.slate400
                        : BrandColors.emerald700,
                    size: 20,
                  ),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Row(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Expanded(
                            child: Text(
                              (n['title'] ?? '—').toString(),
                              style: TextStyle(
                                color: BrandColors.navy900,
                                fontWeight: read
                                    ? FontWeight.w600
                                    : FontWeight.w800,
                                fontSize: 14,
                                height: 1.4,
                              ),
                            ),
                          ),
                          if (!read) ...[
                            const SizedBox(width: 6),
                            Container(
                              width: 8,
                              height: 8,
                              margin: const EdgeInsets.only(top: 5),
                              decoration: const BoxDecoration(
                                color: BrandColors.emerald500,
                                shape: BoxShape.circle,
                              ),
                            ),
                          ],
                        ],
                      ),
                      if (n['body'] != null) ...[
                        const SizedBox(height: 4),
                        Text(
                          n['body'].toString(),
                          style: const TextStyle(
                            color: BrandColors.slate500,
                            fontSize: 12,
                            height: 1.55,
                          ),
                        ),
                      ],
                      const SizedBox(height: 6),
                      Text(
                        _relative(n['created_at']),
                        style: const TextStyle(
                          color: BrandColors.slate400,
                          fontSize: 11,
                        ),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }

  static String _relative(dynamic iso) {
    if (iso is! String || iso.length < 16) return '';
    // Cheap human date — dd/MM HH:mm.
    return '${iso.substring(8, 10)}/${iso.substring(5, 7)} '
        '${iso.substring(11, 16)}';
  }
}

class _EmptyState extends StatelessWidget {
  const _EmptyState();
  @override
  Widget build(BuildContext context) {
    return Center(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Container(
            width: 80,
            height: 80,
            decoration: BoxDecoration(
              color: BrandColors.emerald50,
              borderRadius: BorderRadius.circular(24),
            ),
            alignment: Alignment.center,
            child: const Icon(Icons.notifications_none,
                color: BrandColors.emerald700, size: 40),
          ),
          const SizedBox(height: 16),
          const Text(
            'لا توجد إشعارات جديدة',
            style: TextStyle(
              color: BrandColors.navy900,
              fontWeight: FontWeight.w700,
              fontSize: 15,
            ),
          ),
          const SizedBox(height: 4),
          const Text(
            'كل شيء تحت السيطرة.',
            style: TextStyle(color: BrandColors.slate500, fontSize: 12),
          ),
        ],
      ),
    );
  }
}
