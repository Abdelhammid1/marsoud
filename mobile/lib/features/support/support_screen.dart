// MARSOUD-MOBILE-FLUTTER — الدعم الفني (mirrors support/index.html).
//
// List my tickets + a floating action button to open a new one.
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/theme.dart';
import '../../data/api_client.dart';
import '../../data/my_account_repository.dart';
import '../../widgets/gradient_button.dart';
import '../../widgets/section_card.dart';

final _ticketsProvider = FutureProvider.autoDispose<Map<String, dynamic>>(
    (ref) => ref.watch(myAccountRepoProvider).supportTickets());

class SupportScreen extends ConsumerWidget {
  const SupportScreen({super.key});
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final async = ref.watch(_ticketsProvider);
    return async.when(
      loading: () => const Center(child: CircularProgressIndicator()),
      error: (e, _) => Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Text(e is ApiException ? e.message : e.toString()),
        ),
      ),
      data: (data) {
        final items = (data['tickets'] as List?)
                ?.cast<Map<String, dynamic>>() ??
            const [];
        return RefreshIndicator(
          color: BrandColors.emerald600,
          onRefresh: () async => ref.invalidate(_ticketsProvider),
          child: ListView(
            padding: const EdgeInsets.fromLTRB(12, 16, 12, 32),
            children: [
              SectionCard(
                emoji: '🆘',
                title: 'الدعم الفني',
                subtitle:
                    'افتح تذكرة لفريق دعم مرصود لأي مشكلة أو سؤال. متوسط الردّ خلال يوم عمل.',
                child: GradientButton(
                  label: 'فتح تذكرة جديدة',
                  icon: Icons.add,
                  onPressed: () async {
                    final created = await showModalBottomSheet<bool>(
                      context: context,
                      isScrollControlled: true,
                      backgroundColor: Colors.transparent,
                      builder: (_) => const _NewTicketSheet(),
                    );
                    if (created == true) ref.invalidate(_ticketsProvider);
                  },
                ),
              ),
              const SizedBox(height: 12),
              SectionCard(
                emoji: '📋',
                title: 'تذاكري',
                child: items.isEmpty
                    ? const EmptyState(
                        icon: Icons.support_agent,
                        message: 'لا يوجد تذاكر مفتوحة.',
                      )
                    : Column(
                        children: [
                          for (final t in items) _TicketRow(t: t),
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

class _TicketRow extends StatelessWidget {
  final Map<String, dynamic> t;
  const _TicketRow({required this.t});
  @override
  Widget build(BuildContext context) {
    final title = t['title']?.toString() ?? '—';
    final status = t['status']?.toString() ?? 'OPEN';
    final priority = t['priority']?.toString() ?? 'MEDIUM';
    final commentCount = t['comment_count'] ?? 0;
    final createdAt = (t['created_at'] as String?)?.substring(0, 10) ?? '';
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: BrandColors.slate50,
        borderRadius: BorderRadius.circular(10),
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
                    fontWeight: FontWeight.w800,
                    fontSize: 14,
                  ),
                ),
              ),
              const SizedBox(width: 8),
              _statusBadge(status),
            ],
          ),
          const SizedBox(height: 8),
          Row(
            children: [
              Icon(_prioIcon(priority),
                  size: 12, color: _prioColor(priority)),
              const SizedBox(width: 4),
              Text(
                _prioLabel(priority),
                style: TextStyle(
                  color: _prioColor(priority),
                  fontSize: 11,
                  fontWeight: FontWeight.w700,
                ),
              ),
              const SizedBox(width: 12),
              const Icon(Icons.comment,
                  size: 12, color: BrandColors.slate500),
              const SizedBox(width: 4),
              Text('$commentCount',
                  style: const TextStyle(
                    color: BrandColors.slate500,
                    fontSize: 11,
                  )),
              const Spacer(),
              Text(
                createdAt,
                style: const TextStyle(
                  color: BrandColors.slate400,
                  fontSize: 11,
                  fontFamily: 'monospace',
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }

  static Widget _statusBadge(String s) {
    switch (s) {
      case 'OPEN':
        return StatusBadge.pending('مفتوحة');
      case 'IN_PROGRESS':
      case 'ONGOING':
        return StatusBadge.partial('جاري العمل');
      case 'RESOLVED':
      case 'CLOSED':
        return StatusBadge.approved('مغلقة');
      default:
        return StatusBadge.draft(s);
    }
  }

  static String _prioLabel(String p) {
    switch (p) {
      case 'LOW':
        return 'أولوية عادية';
      case 'MEDIUM':
        return 'متوسطة';
      case 'HIGH':
        return 'عالية';
      case 'URGENT':
        return 'عاجلة';
      default:
        return p;
    }
  }

  static IconData _prioIcon(String p) {
    if (p == 'URGENT') return Icons.priority_high;
    if (p == 'HIGH') return Icons.warning_amber;
    return Icons.flag_outlined;
  }

  static Color _prioColor(String p) {
    if (p == 'URGENT') return BrandColors.red700;
    if (p == 'HIGH') return BrandColors.amber700;
    return BrandColors.slate500;
  }
}

class _NewTicketSheet extends ConsumerStatefulWidget {
  const _NewTicketSheet();
  @override
  ConsumerState<_NewTicketSheet> createState() => _NewTicketSheetState();
}

class _NewTicketSheetState extends ConsumerState<_NewTicketSheet> {
  final _title = TextEditingController();
  final _desc = TextEditingController();
  String _priority = 'MEDIUM';
  bool _submitting = false;

  @override
  void dispose() {
    _title.dispose();
    _desc.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    if (_title.text.trim().isEmpty || _desc.text.trim().isEmpty) {
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
        content: Text('اكتب العنوان والوصف.'),
      ));
      return;
    }
    setState(() => _submitting = true);
    try {
      await ref.read(myAccountRepoProvider).createSupportTicket(
            title: _title.text.trim(),
            description: _desc.text.trim(),
            priority: _priority,
          );
      if (!mounted) return;
      Navigator.of(context).pop(true);
    } on ApiException catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(e.message)));
    } finally {
      if (mounted) setState(() => _submitting = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final bottom = MediaQuery.of(context).viewInsets.bottom;
    return Padding(
      padding: EdgeInsets.only(bottom: bottom),
      child: Container(
        decoration: const BoxDecoration(
          color: Colors.white,
          borderRadius: BorderRadius.vertical(top: Radius.circular(24)),
        ),
        padding: const EdgeInsets.all(20),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Center(
              child: Container(
                width: 44, height: 4,
                margin: const EdgeInsets.only(bottom: 14),
                decoration: BoxDecoration(
                  color: BrandColors.slate200,
                  borderRadius: BorderRadius.circular(4),
                ),
              ),
            ),
            const Text(
              '🆘 فتح تذكرة جديدة',
              style: TextStyle(
                color: BrandColors.navy900,
                fontSize: 17,
                fontWeight: FontWeight.w800,
              ),
            ),
            const SizedBox(height: 14),
            TextField(
              controller: _title,
              decoration: const InputDecoration(
                labelText: 'عنوان مختصر للمشكلة',
              ),
              textInputAction: TextInputAction.next,
            ),
            const SizedBox(height: 10),
            TextField(
              controller: _desc,
              minLines: 4, maxLines: 8,
              decoration: const InputDecoration(
                labelText: 'وصف مفصّل',
                alignLabelWithHint: true,
              ),
            ),
            const SizedBox(height: 10),
            DropdownButtonFormField<String>(
              initialValue: _priority,
              decoration: const InputDecoration(labelText: 'الأولوية'),
              items: const [
                DropdownMenuItem(value: 'LOW', child: Text('عادية')),
                DropdownMenuItem(value: 'MEDIUM', child: Text('متوسطة')),
                DropdownMenuItem(value: 'HIGH', child: Text('عالية')),
                DropdownMenuItem(value: 'URGENT', child: Text('عاجلة')),
              ],
              onChanged: (v) => setState(() => _priority = v ?? 'MEDIUM'),
            ),
            const SizedBox(height: 16),
            GradientButton(
              label: 'إرسال التذكرة',
              icon: Icons.send,
              loading: _submitting,
              onPressed: _submitting ? null : _submit,
            ),
          ],
        ),
      ),
    );
  }
}
