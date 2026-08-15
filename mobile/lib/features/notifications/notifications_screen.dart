// MARSOUD-MOBILE-FLUTTER — notifications feed + mark-read.
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

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
      error: (e, _) => Center(child: Text(e.toString())),
      data: (data) {
        final items = (data['items'] as List?)
                ?.cast<Map<String, dynamic>>() ??
            [];
        if (items.isEmpty) {
          return const Center(child: Text('لا توجد إشعارات.'));
        }
        return RefreshIndicator(
          onRefresh: () async => ref.invalidate(_notificationsProvider),
          child: ListView.separated(
            padding: const EdgeInsets.all(8),
            itemCount: items.length,
            separatorBuilder: (_, __) => const Divider(height: 1),
            itemBuilder: (context, i) {
              final n = items[i];
              final isRead = n['is_read'] == true;
              return ListTile(
                leading: Icon(
                  isRead
                      ? Icons.notifications_none
                      : Icons.notifications_active,
                  color: isRead ? Colors.grey : Colors.blue,
                ),
                title: Text(n['title'] ?? '—',
                    style: TextStyle(
                        fontWeight: isRead
                            ? FontWeight.normal
                            : FontWeight.bold)),
                subtitle: n['body'] != null ? Text(n['body']) : null,
                trailing: Text(
                  _shortDate(n['created_at']),
                  style: const TextStyle(fontSize: 11, color: Colors.grey),
                ),
                onTap: () async {
                  if (!isRead) {
                    await ref
                        .read(myAccountRepoProvider)
                        .markRead(n['id'] as int);
                    ref.invalidate(_notificationsProvider);
                  }
                },
              );
            },
          ),
        );
      },
    );
  }

  String _shortDate(dynamic iso) {
    if (iso is! String || iso.length < 10) return '';
    return iso.substring(0, 10);
  }
}
