// MARSOUD-MOBILE-FLUTTER — عهدي العينية (mirrors portal_emp/items_list.html).
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/theme.dart';
import '../../data/api_client.dart';
import '../../data/my_account_repository.dart';
import '../../widgets/gradient_button.dart';
import '../../widgets/section_card.dart';

final _itemsProvider = FutureProvider.autoDispose<Map<String, dynamic>>(
    (ref) => ref.watch(myAccountRepoProvider).items());

class ItemsScreen extends ConsumerWidget {
  const ItemsScreen({super.key});
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final async = ref.watch(_itemsProvider);
    return async.when(
      loading: () => const Center(child: CircularProgressIndicator()),
      error: (e, _) => Center(
          child: Padding(
            padding: const EdgeInsets.all(24),
            child: Text(e is ApiException ? e.message : e.toString()),
          )),
      data: (data) {
        final custodies = (data['custodies'] as List?)
                ?.cast<Map<String, dynamic>>() ??
            const [];
        final requests = (data['requests'] as List?)
                ?.cast<Map<String, dynamic>>() ??
            const [];
        final available = (data['available'] as List?)
                ?.cast<Map<String, dynamic>>() ??
            const [];
        return RefreshIndicator(
          color: BrandColors.emerald600,
          onRefresh: () async => ref.invalidate(_itemsProvider),
          child: ListView(
            padding: const EdgeInsets.fromLTRB(12, 16, 12, 32),
            children: [
              SectionCard(
                emoji: '📦',
                title: 'طلب استلام عنصر',
                child: available.isEmpty
                    ? const EmptyState(
                        icon: Icons.inventory_2_outlined,
                        message: 'مافيش عناصر متاحة للطلب حالياً.',
                      )
                    : _NewItemForm(
                        available: available,
                        onSubmit: () => ref.invalidate(_itemsProvider),
                      ),
              ),
              const SizedBox(height: 12),
              SectionCard(
                emoji: '📝',
                title: 'طلباتي',
                child: requests.isEmpty
                    ? const EmptyState(
                        icon: Icons.mail_outline,
                        message: 'لا يوجد طلبات.',
                      )
                    : Column(
                        children: [
                          for (final r in requests)
                            _ItemRequestRow(r: r),
                        ],
                      ),
              ),
              const SizedBox(height: 12),
              SectionCard(
                emoji: '🗃',
                title: 'عهدي الحالية والسابقة',
                child: custodies.isEmpty
                    ? const EmptyState(
                        icon: Icons.folder_open,
                        message: 'لا يوجد عهد بعد. اطلب عنصر من الأعلى.',
                      )
                    : Column(
                        children: [
                          for (final c in custodies)
                            _ItemCustodyRow(c: c),
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

class _NewItemForm extends ConsumerStatefulWidget {
  final List<Map<String, dynamic>> available;
  final VoidCallback onSubmit;
  const _NewItemForm({required this.available, required this.onSubmit});
  @override
  ConsumerState<_NewItemForm> createState() => _NewItemFormState();
}

class _NewItemFormState extends ConsumerState<_NewItemForm> {
  int? _pickedItemId;
  final _purpose = TextEditingController();
  bool _submitting = false;

  @override
  void dispose() {
    _purpose.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    if (_pickedItemId == null || _purpose.text.trim().isEmpty) {
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
        content: Text('اختر عنصر واكتب الغرض.'),
      ));
      return;
    }
    setState(() => _submitting = true);
    try {
      await ref.read(myAccountRepoProvider).requestItem(
            itemId: _pickedItemId!,
            purpose: _purpose.text.trim(),
          );
      if (!mounted) return;
      _purpose.clear();
      setState(() => _pickedItemId = null);
      widget.onSubmit();
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
        content: Text('تم إرسال الطلب للاعتماد.'),
      ));
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
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        DropdownButtonFormField<int>(
          initialValue: _pickedItemId,
          isExpanded: true,
          decoration: const InputDecoration(labelText: 'العنصر'),
          items: [
            for (final it in widget.available)
              DropdownMenuItem(
                value: it['id'] as int,
                child: Text(
                  it['serial_number'] != null &&
                          (it['serial_number'] as String).isNotEmpty
                      ? '${it['name']} · SN: ${it['serial_number']}'
                      : (it['name'] ?? '—').toString(),
                  overflow: TextOverflow.ellipsis,
                ),
              ),
          ],
          onChanged: (v) => setState(() => _pickedItemId = v),
        ),
        const SizedBox(height: 12),
        TextField(
          controller: _purpose,
          decoration: const InputDecoration(
            labelText: 'الغرض',
            hintText: 'مثلاً: مطلوب للعمل من المنزل',
          ),
        ),
        const SizedBox(height: 14),
        GradientButton(
          label: 'إرسال الطلب للاعتماد',
          icon: Icons.send,
          loading: _submitting,
          onPressed: _submitting ? null : _submit,
        ),
      ],
    );
  }
}

class _ItemRequestRow extends StatelessWidget {
  final Map<String, dynamic> r;
  const _ItemRequestRow({required this.r});
  @override
  Widget build(BuildContext context) {
    final name = r['item_name']?.toString() ?? '—';
    final purpose = r['purpose']?.toString() ?? '—';
    final status = r['status'];
    final statusVal = status is Map ? status['value'] : status;
    late final StatusBadge b;
    switch (statusVal) {
      case 'PENDING':
        b = StatusBadge.pending('قيد الانتظار');
        break;
      case 'APPROVED':
        b = StatusBadge.approved('معتمدة');
        break;
      case 'REJECTED':
        b = StatusBadge.rejected('مرفوضة');
        break;
      default:
        b = StatusBadge.draft(statusVal?.toString() ?? '—');
    }
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: BrandColors.slate50,
        borderRadius: BorderRadius.circular(10),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  name,
                  style: const TextStyle(
                    color: BrandColors.navy900,
                    fontWeight: FontWeight.w800,
                    fontSize: 13,
                  ),
                ),
              ),
              b,
            ],
          ),
          const SizedBox(height: 3),
          Text(
            purpose,
            style: const TextStyle(
              color: BrandColors.slate500,
              fontSize: 11,
              height: 1.5,
            ),
          ),
        ],
      ),
    );
  }
}

class _ItemCustodyRow extends StatelessWidget {
  final Map<String, dynamic> c;
  const _ItemCustodyRow({required this.c});
  @override
  Widget build(BuildContext context) {
    final name = c['item_name']?.toString() ?? '—';
    final handed =
        (c['handed_over_on'] as String?)?.substring(0, 10) ?? '—';
    final settled = (c['settled_on'] as String?)?.substring(0, 10) ?? '—';
    final status = c['status'];
    final statusVal = status is Map ? status['value'] : status;
    late final StatusBadge b;
    switch (statusVal) {
      case 'ACTIVE':
        b = StatusBadge.partial('لدي حالياً');
        break;
      case 'RETURNED_GOOD':
        b = StatusBadge.approved('سُلِّمت سليمة');
        break;
      case 'RETURNED_DAMAGED':
        b = StatusBadge.overdue('تالفة');
        break;
      case 'LOST':
        b = StatusBadge.overdue('فقد');
        break;
      case 'TRANSFERRED':
        b = StatusBadge.pending('محوّلة');
        break;
      default:
        b = StatusBadge.draft(statusVal?.toString() ?? '—');
    }
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
            children: [
              Expanded(
                child: Text(
                  name,
                  style: const TextStyle(
                    color: BrandColors.navy900,
                    fontWeight: FontWeight.w800,
                    fontSize: 13,
                  ),
                ),
              ),
              b,
            ],
          ),
          const SizedBox(height: 6),
          Row(
            children: [
              const Icon(Icons.download,
                  size: 12, color: BrandColors.slate500),
              const SizedBox(width: 4),
              Text('استلمت: $handed',
                  style: const TextStyle(
                    color: BrandColors.slate500,
                    fontSize: 11,
                    fontFamily: 'monospace',
                  )),
              const SizedBox(width: 12),
              const Icon(Icons.check_circle_outline,
                  size: 12, color: BrandColors.slate500),
              const SizedBox(width: 4),
              Text('سُويت: $settled',
                  style: const TextStyle(
                    color: BrandColors.slate500,
                    fontSize: 11,
                    fontFamily: 'monospace',
                  )),
            ],
          ),
        ],
      ),
    );
  }
}
