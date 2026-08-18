// MARSOUD-MOBILE-TKT-03 (2026-08-18) — طلباتي. Three forms in
// one screen: leave request / permission request / advance
// request. Each form POSTs to the existing /api/v1/my/*
// endpoints — no new backend code. Employee → Submit →
// Manager (web) → Approve/Reject → Employee notification.
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/theme.dart';
import '../../data/api_client.dart';
import '../../data/my_account_repository.dart';
import '../../widgets/section_card.dart';

final _accountProvider =
    FutureProvider.autoDispose<Map<String, dynamic>>((ref) {
  return ref.watch(myAccountRepoProvider).account();
});

class RequestsScreen extends ConsumerWidget {
  const RequestsScreen({super.key});
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    // We need leave types (from /my/account bundle) to populate
    // the leave-request dropdown. Everything else is form-only.
    final acct = ref.watch(_accountProvider);
    return acct.when(
      loading: () => const Center(child: CircularProgressIndicator()),
      error: (e, _) => Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Text(e is ApiException ? e.message : e.toString()),
        ),
      ),
      data: (data) {
        final leaveTypes = ((data['leave']
                    as Map<String, dynamic>?)?['types']
                as List?)
                ?.cast<Map<String, dynamic>>() ??
            const [];
        return RefreshIndicator(
          color: BrandColors.emerald600,
          onRefresh: () async => ref.invalidate(_accountProvider),
          child: ListView(
            padding: const EdgeInsets.fromLTRB(12, 12, 12, 32),
            children: [
              const SectionCard(
                emoji: 'ℹ️',
                title: 'طلبات الموظف',
                subtitle:
                    'اختر نوع الطلب وعبّي البيانات. المدير هيراجع طلبك ويوافق أو يرفض — هتوصلك الإجابة كإشعار.',
                child: SizedBox(),
              ),
              const SizedBox(height: 12),
              _LeaveRequestCard(leaveTypes: leaveTypes),
              const SizedBox(height: 12),
              const _PermissionRequestCard(),
              const SizedBox(height: 12),
              const _AdvanceRequestCard(),
            ],
          ),
        );
      },
    );
  }
}

// ═════ Leave ═════════════════════════════════════════════════════
class _LeaveRequestCard extends ConsumerStatefulWidget {
  final List<Map<String, dynamic>> leaveTypes;
  const _LeaveRequestCard({required this.leaveTypes});
  @override
  ConsumerState<_LeaveRequestCard> createState() =>
      _LeaveRequestCardState();
}

class _LeaveRequestCardState extends ConsumerState<_LeaveRequestCard> {
  int? _typeId;
  DateTime? _start;
  DateTime? _end;
  final _reason = TextEditingController();
  bool _submitting = false;

  @override
  void dispose() {
    _reason.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return SectionCard(
      emoji: '🌴',
      title: 'طلب إجازة',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          DropdownButtonFormField<int>(
            initialValue: _typeId,
            decoration: const InputDecoration(
                labelText: 'نوع الإجازة'),
            items: [
              for (final t in widget.leaveTypes)
                DropdownMenuItem(
                  value: t['id'] as int?,
                  child: Text(t['name_ar']?.toString() ??
                              t['name']?.toString() ??
                              '—'),
                ),
            ],
            onChanged: (v) => setState(() => _typeId = v),
          ),
          const SizedBox(height: 8),
          _DateRow(
            label: 'من تاريخ',
            value: _start,
            onPick: (d) => setState(() => _start = d),
          ),
          _DateRow(
            label: 'إلى تاريخ',
            value: _end,
            onPick: (d) => setState(() => _end = d),
          ),
          const SizedBox(height: 8),
          TextField(
            controller: _reason,
            decoration: const InputDecoration(labelText: 'السبب'),
            maxLines: 2,
          ),
          const SizedBox(height: 10),
          FilledButton(
            onPressed: _submitting ? null : _submit,
            child: _submitting
                ? const SizedBox(
                    height: 16, width: 16,
                    child: CircularProgressIndicator(strokeWidth: 2))
                : const Text('إرسال الطلب'),
          ),
        ],
      ),
    );
  }

  Future<void> _submit() async {
    if (_typeId == null || _start == null || _end == null) {
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
          content: Text('عبّي النوع + التاريخين')));
      return;
    }
    setState(() => _submitting = true);
    final messenger = ScaffoldMessenger.of(context);
    try {
      await ref.read(myAccountRepoProvider).submitLeave(
            leaveTypeId: _typeId!,
            startDate: _start!.toIso8601String().split('T').first,
            endDate: _end!.toIso8601String().split('T').first,
            reason: _reason.text.trim().isEmpty
                ? null : _reason.text.trim(),
          );
      messenger.showSnackBar(const SnackBar(
          content: Text('تم إرسال طلب الإجازة.')));
      _reason.clear();
      setState(() { _typeId = null; _start = null; _end = null; });
    } on ApiException catch (e) {
      messenger.showSnackBar(SnackBar(content: Text(e.message)));
    } finally {
      if (mounted) setState(() => _submitting = false);
    }
  }
}

// ═════ Permission (استئذان) ═════════════════════════════════════
class _PermissionRequestCard extends ConsumerStatefulWidget {
  const _PermissionRequestCard();
  @override
  ConsumerState<_PermissionRequestCard> createState() =>
      _PermissionRequestCardState();
}

class _PermissionRequestCardState
    extends ConsumerState<_PermissionRequestCard> {
  DateTime? _date;
  final _hours = TextEditingController(text: '1');
  final _reason = TextEditingController();
  bool _submitting = false;

  @override
  void dispose() {
    _hours.dispose();
    _reason.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return SectionCard(
      emoji: '⏱',
      title: 'طلب استئذان',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          _DateRow(
            label: 'التاريخ',
            value: _date,
            onPick: (d) => setState(() => _date = d),
          ),
          const SizedBox(height: 8),
          TextField(
            controller: _hours,
            decoration: const InputDecoration(
                labelText: 'عدد الساعات'),
            keyboardType:
                const TextInputType.numberWithOptions(decimal: true),
          ),
          const SizedBox(height: 8),
          TextField(
            controller: _reason,
            decoration: const InputDecoration(labelText: 'السبب'),
            maxLines: 2,
          ),
          const SizedBox(height: 10),
          FilledButton(
            onPressed: _submitting ? null : _submit,
            child: _submitting
                ? const SizedBox(
                    height: 16, width: 16,
                    child: CircularProgressIndicator(strokeWidth: 2))
                : const Text('إرسال الطلب'),
          ),
        ],
      ),
    );
  }

  Future<void> _submit() async {
    if (_date == null) {
      ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('اختر التاريخ')));
      return;
    }
    final h = num.tryParse(_hours.text.trim());
    if (h == null || h <= 0) {
      ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('عدد الساعات غير صحيح')));
      return;
    }
    setState(() => _submitting = true);
    final messenger = ScaffoldMessenger.of(context);
    try {
      await ref.read(myAccountRepoProvider).submitPermission(
            requestDate:
                _date!.toIso8601String().split('T').first,
            hoursCount: h,
            reason: _reason.text.trim().isEmpty
                ? null : _reason.text.trim(),
          );
      messenger.showSnackBar(const SnackBar(
          content: Text('تم إرسال طلب الاستئذان.')));
      _reason.clear();
      setState(() { _date = null; });
    } on ApiException catch (e) {
      messenger.showSnackBar(SnackBar(content: Text(e.message)));
    } finally {
      if (mounted) setState(() => _submitting = false);
    }
  }
}

// ═════ Advance (سلفة) ══════════════════════════════════════════
class _AdvanceRequestCard extends ConsumerStatefulWidget {
  const _AdvanceRequestCard();
  @override
  ConsumerState<_AdvanceRequestCard> createState() =>
      _AdvanceRequestCardState();
}

class _AdvanceRequestCardState extends ConsumerState<_AdvanceRequestCard> {
  final _amount = TextEditingController();
  final _reason = TextEditingController();
  bool _submitting = false;

  @override
  void dispose() {
    _amount.dispose();
    _reason.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return SectionCard(
      emoji: '💵',
      title: 'طلب سلفة',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          TextField(
            controller: _amount,
            decoration: const InputDecoration(labelText: 'المبلغ'),
            keyboardType:
                const TextInputType.numberWithOptions(decimal: true),
          ),
          const SizedBox(height: 8),
          TextField(
            controller: _reason,
            decoration: const InputDecoration(labelText: 'السبب'),
            maxLines: 2,
          ),
          const SizedBox(height: 10),
          FilledButton(
            onPressed: _submitting ? null : _submit,
            child: _submitting
                ? const SizedBox(
                    height: 16, width: 16,
                    child: CircularProgressIndicator(strokeWidth: 2))
                : const Text('إرسال الطلب'),
          ),
        ],
      ),
    );
  }

  Future<void> _submit() async {
    final a = num.tryParse(_amount.text.trim());
    if (a == null || a <= 0) {
      ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('المبلغ غير صحيح')));
      return;
    }
    setState(() => _submitting = true);
    final messenger = ScaffoldMessenger.of(context);
    try {
      await ref.read(myAccountRepoProvider).submitAdvance(
            amount: a,
            reason: _reason.text.trim().isEmpty
                ? null : _reason.text.trim(),
          );
      messenger.showSnackBar(const SnackBar(
          content: Text('تم إرسال طلب السلفة.')));
      _amount.clear();
      _reason.clear();
    } on ApiException catch (e) {
      messenger.showSnackBar(SnackBar(content: Text(e.message)));
    } finally {
      if (mounted) setState(() => _submitting = false);
    }
  }
}

// ═════ Shared date row ═════════════════════════════════════════
class _DateRow extends StatelessWidget {
  final String label;
  final DateTime? value;
  final ValueChanged<DateTime> onPick;
  const _DateRow({
    required this.label,
    required this.value,
    required this.onPick,
  });
  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(
        children: [
          Expanded(
            child: Text(label,
                style: const TextStyle(
                    fontSize: 13, color: BrandColors.slate700)),
          ),
          OutlinedButton.icon(
            onPressed: () async {
              final now = DateTime.now();
              final d = await showDatePicker(
                context: context,
                firstDate: now.subtract(const Duration(days: 30)),
                lastDate: now.add(const Duration(days: 365)),
                initialDate: value ?? now,
              );
              if (d != null) onPick(d);
            },
            icon: const Icon(Icons.event, size: 14),
            label: Text(value == null
                ? 'اختر'
                : value!.toIso8601String().split('T').first),
          ),
        ],
      ),
    );
  }
}
