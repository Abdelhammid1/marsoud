// MARSOUD-MOBILE-FLUTTER — عهدتي النقدية (mirrors portal_emp/custody_list.html).
//
// Three sections top → bottom: new-request form, my requests, my custodies.
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/theme.dart';
import '../../data/api_client.dart';
import '../../data/my_account_repository.dart';
import '../../widgets/gradient_button.dart';
import '../../widgets/section_card.dart';

final _custodyProvider = FutureProvider.autoDispose<Map<String, dynamic>>(
    (ref) => ref.watch(myAccountRepoProvider).custody());

class CustodyScreen extends ConsumerWidget {
  const CustodyScreen({super.key});
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final async = ref.watch(_custodyProvider);
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
        return RefreshIndicator(
          color: BrandColors.emerald600,
          onRefresh: () async => ref.invalidate(_custodyProvider),
          child: ListView(
            padding: const EdgeInsets.fromLTRB(12, 16, 12, 32),
            children: [
              SectionCard(
                emoji: '💵',
                title: 'طلب عهدة جديدة',
                child: _NewRequestForm(
                  onSubmit: () => ref.invalidate(_custodyProvider),
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
                            _CustodyRequestRow(r: r),
                        ],
                      ),
              ),
              const SizedBox(height: 12),
              SectionCard(
                emoji: '📂',
                title: 'عهدي المفتوحة والسابقة',
                child: custodies.isEmpty
                    ? const EmptyState(
                        icon: Icons.folder_open,
                        message: 'لا يوجد عهد بعد.',
                      )
                    : Column(
                        children: [
                          for (final c in custodies)
                            _CustodyRow(c: c),
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

class _NewRequestForm extends ConsumerStatefulWidget {
  final VoidCallback onSubmit;
  const _NewRequestForm({required this.onSubmit});
  @override
  ConsumerState<_NewRequestForm> createState() => _NewRequestFormState();
}

class _NewRequestFormState extends ConsumerState<_NewRequestForm> {
  final _amount = TextEditingController();
  final _purpose = TextEditingController();
  DateTime? _neededBy;
  bool _submitting = false;

  @override
  void dispose() {
    _amount.dispose();
    _purpose.dispose();
    super.dispose();
  }

  Future<void> _pickDate() async {
    final d = await showDatePicker(
      context: context,
      firstDate: DateTime.now(),
      lastDate: DateTime.now().add(const Duration(days: 365)),
      initialDate: DateTime.now(),
    );
    if (d != null) setState(() => _neededBy = d);
  }

  Future<void> _submit() async {
    final amt = double.tryParse(_amount.text);
    final purpose = _purpose.text.trim();
    if (amt == null || amt <= 0 || purpose.isEmpty) {
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
        content: Text('أدخل مبلغاً صحيحاً وحدّد الغرض.'),
      ));
      return;
    }
    setState(() => _submitting = true);
    try {
      await ref.read(myAccountRepoProvider).requestCustody(
            amount: amt,
            purpose: purpose,
            neededByDate: _neededBy != null
                ? '${_neededBy!.year}-${_neededBy!.month.toString().padLeft(2, '0')}-${_neededBy!.day.toString().padLeft(2, '0')}'
                : null,
          );
      if (!mounted) return;
      _amount.clear();
      _purpose.clear();
      setState(() => _neededBy = null);
      widget.onSubmit();
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
        content: Text('تم إرسال الطلب للاعتماد.'),
      ));
    } on ApiException catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(e.message)));
    } finally {
      if (mounted) setState(() => _submitting = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Row(
          children: [
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const _MiniLabel('المبلغ'),
                  const SizedBox(height: 4),
                  Directionality(
                    textDirection: TextDirection.ltr,
                    child: TextField(
                      controller: _amount,
                      keyboardType: const TextInputType.numberWithOptions(
                          decimal: true),
                      textAlign: TextAlign.left,
                      decoration:
                          const InputDecoration(hintText: '0.00'),
                    ),
                  ),
                ],
              ),
            ),
            const SizedBox(width: 10),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const _MiniLabel('موعد الحاجة (اختياري)'),
                  const SizedBox(height: 4),
                  OutlinedButton(
                    onPressed: _pickDate,
                    style: OutlinedButton.styleFrom(
                      minimumSize: const Size.fromHeight(52),
                      side: const BorderSide(color: BrandColors.slate200),
                      shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(12),
                      ),
                      alignment: Alignment.centerRight,
                    ),
                    child: Text(
                      _neededBy == null
                          ? 'اختر تاريخ'
                          : '${_neededBy!.year}-${_neededBy!.month.toString().padLeft(2, '0')}-${_neededBy!.day.toString().padLeft(2, '0')}',
                      style: TextStyle(
                        color: _neededBy == null
                            ? BrandColors.slate400
                            : BrandColors.navy900,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ],
        ),
        const SizedBox(height: 12),
        const _MiniLabel('الغرض'),
        const SizedBox(height: 4),
        TextField(
          controller: _purpose,
          decoration: const InputDecoration(
            hintText: 'مثلاً: مصاريف انتقال + إقامة لمشروع الفرع',
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

class _MiniLabel extends StatelessWidget {
  final String text;
  const _MiniLabel(this.text);
  @override
  Widget build(BuildContext context) => Text(
        text,
        style: const TextStyle(
          color: BrandColors.slate700,
          fontSize: 12,
          fontWeight: FontWeight.w600,
        ),
      );
}

class _CustodyRequestRow extends StatelessWidget {
  final Map<String, dynamic> r;
  const _CustodyRequestRow({required this.r});
  @override
  Widget build(BuildContext context) {
    final amount = (r['amount'] as num?)?.toDouble() ?? 0;
    final purpose = r['purpose']?.toString() ?? '—';
    final status = r['status'];
    final statusVal = status is Map ? status['value'] : status;
    final label = _statusLabel(statusVal);
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: BrandColors.slate50,
        borderRadius: BorderRadius.circular(10),
      ),
      child: Row(
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  purpose,
                  style: const TextStyle(
                    color: BrandColors.navy900,
                    fontWeight: FontWeight.w700,
                    fontSize: 12,
                  ),
                  maxLines: 2, overflow: TextOverflow.ellipsis,
                ),
                const SizedBox(height: 3),
                _StatusPill(status: statusVal?.toString(), label: label),
              ],
            ),
          ),
          Text(
            amount.toStringAsFixed(2),
            style: const TextStyle(
              color: BrandColors.navy900,
              fontFamily: 'monospace',
              fontWeight: FontWeight.w800,
              fontSize: 14,
            ),
          ),
        ],
      ),
    );
  }

  static String _statusLabel(dynamic v) {
    switch (v) {
      case 'PENDING':
        return 'قيد المراجعة';
      case 'APPROVED':
        return 'معتمد';
      case 'REJECTED':
        return 'مرفوض';
      case 'CANCELLED':
        return 'ملغى';
      default:
        return v?.toString() ?? '—';
    }
  }
}

class _CustodyRow extends StatelessWidget {
  final Map<String, dynamic> c;
  const _CustodyRow({required this.c});
  @override
  Widget build(BuildContext context) {
    final purpose = c['purpose']?.toString() ?? '—';
    final issued = (c['amount_issued'] as num?)?.toDouble() ?? 0;
    final settled = (c['amount_settled'] as num?)?.toDouble() ?? 0;
    final pending = (c['amount_pending'] as num?)?.toDouble() ?? 0;
    final due = (c['settlement_due_date'] as String?)?.substring(0, 10);
    final status = c['status'];
    final statusVal = status is Map ? status['value'] : status;
    return Container(
      margin: const EdgeInsets.only(bottom: 10),
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: BrandColors.slate50,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: BrandColors.slate200),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  purpose,
                  style: const TextStyle(
                    color: BrandColors.navy900,
                    fontWeight: FontWeight.w800,
                    fontSize: 13,
                  ),
                  maxLines: 2, overflow: TextOverflow.ellipsis,
                ),
              ),
              _StatusPill(
                status: statusVal?.toString(),
                label: _statusLabel(statusVal),
              ),
            ],
          ),
          const SizedBox(height: 10),
          Row(
            children: [
              _NumTile(label: 'مصروف', value: issued.toStringAsFixed(2)),
              const SizedBox(width: 8),
              _NumTile(
                label: 'مُنفَق',
                value: settled.toStringAsFixed(2),
                mute: true,
              ),
              const SizedBox(width: 8),
              _NumTile(
                label: 'متبقّي',
                value: pending.toStringAsFixed(2),
                bold: true,
              ),
            ],
          ),
          if (due != null) ...[
            const SizedBox(height: 8),
            Text(
              'موعد التسوية: $due',
              style: const TextStyle(
                color: BrandColors.slate500,
                fontSize: 11,
                fontFamily: 'monospace',
              ),
            ),
          ],
        ],
      ),
    );
  }

  static String _statusLabel(dynamic v) {
    switch (v) {
      case 'ISSUED':
        return 'مصروفة';
      case 'PARTIALLY_SETTLED':
        return 'قيد التسوية';
      case 'SETTLED':
        return 'مُقفلة';
      case 'CANCELLED':
        return 'ملغاة';
      default:
        return v?.toString() ?? '—';
    }
  }
}

class _NumTile extends StatelessWidget {
  final String label;
  final String value;
  final bool bold;
  final bool mute;
  const _NumTile({
    required this.label,
    required this.value,
    this.bold = false,
    this.mute = false,
  });
  @override
  Widget build(BuildContext context) {
    return Expanded(
      child: Container(
        padding: const EdgeInsets.symmetric(vertical: 8, horizontal: 8),
        decoration: BoxDecoration(
          color: Colors.white,
          borderRadius: BorderRadius.circular(8),
          border: Border.all(color: BrandColors.slate200),
        ),
        child: Column(
          children: [
            Text(
              label,
              style: const TextStyle(
                color: BrandColors.slate500,
                fontSize: 10,
              ),
            ),
            const SizedBox(height: 2),
            Text(
              value,
              style: TextStyle(
                color: mute ? BrandColors.slate500 : BrandColors.navy900,
                fontFamily: 'monospace',
                fontSize: 13,
                fontWeight: bold ? FontWeight.w800 : FontWeight.w600,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _StatusPill extends StatelessWidget {
  final String? status;
  final String label;
  const _StatusPill({required this.status, required this.label});
  @override
  Widget build(BuildContext context) {
    late final StatusBadge b;
    switch (status) {
      case 'PENDING':
      case 'ISSUED':
        b = StatusBadge.pending(label);
        break;
      case 'APPROVED':
      case 'SETTLED':
        b = StatusBadge.approved(label);
        break;
      case 'PARTIALLY_SETTLED':
        b = StatusBadge.partial(label);
        break;
      case 'REJECTED':
      case 'CANCELLED':
        b = StatusBadge.rejected(label);
        break;
      default:
        b = StatusBadge.draft(label);
    }
    return b;
  }
}
