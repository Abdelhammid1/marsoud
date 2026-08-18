// MARSOUD-MOBILE-TKT-01 (2026-08-18) — lead detail. Basic info +
// status picker + timeline of activities + "add activity" sheet.
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/theme.dart';
import '../../data/api_client.dart';
import '../../data/mobile_extras_repository.dart';
import '../../widgets/section_card.dart';

final _leadProvider = FutureProvider.autoDispose
    .family<Map<String, dynamic>, int>((ref, id) {
  return ref.watch(mobileExtrasRepoProvider).leadDetail(id);
});

final _stagesProvider = FutureProvider.autoDispose<List<Map<String, dynamic>>>(
    (ref) async {
  final r = await ref.watch(mobileExtrasRepoProvider).leadStages();
  return (r['stages'] as List).cast<Map<String, dynamic>>();
});

class LeadDetailScreen extends ConsumerWidget {
  final int leadId;
  const LeadDetailScreen({super.key, required this.leadId});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final async = ref.watch(_leadProvider(leadId));
    return async.when(
      loading: () => const Center(child: CircularProgressIndicator()),
      error: (e, _) => Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Text(e is ApiException ? e.message : e.toString()),
        ),
      ),
      data: (data) {
        final lead = data['lead'] as Map<String, dynamic>;
        final activities = (lead['activities'] as List?)
                ?.cast<Map<String, dynamic>>() ??
            const [];
        return RefreshIndicator(
          color: BrandColors.emerald600,
          onRefresh: () async => ref.invalidate(_leadProvider(leadId)),
          child: ListView(
            padding: const EdgeInsets.fromLTRB(12, 12, 12, 32),
            children: [
              SectionCard(
                emoji: '🎯',
                title: lead['client_name']?.toString() ?? '',
                subtitle: lead['service_needed']?.toString() ?? '',
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    _InfoRow(icon: Icons.phone,
                        text: lead['phone']?.toString() ?? '—'),
                    if ((lead['email'] ?? '').toString().isNotEmpty)
                      _InfoRow(icon: Icons.email,
                          text: lead['email'].toString()),
                    if (lead['next_meeting'] != null)
                      _InfoRow(icon: Icons.event,
                          text: lead['next_meeting'].toString()),
                  ],
                ),
              ),
              const SizedBox(height: 12),
              _StatusPicker(
                leadId: leadId,
                currentStatus: lead['status']?.toString() ?? '',
                currentLabel:
                    lead['status_label_ar']?.toString() ?? '',
                onChanged: () => ref.invalidate(_leadProvider(leadId)),
              ),
              const SizedBox(height: 12),
              SectionCard(
                emoji: '🗓',
                title: 'الأنشطة',
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    Align(
                      alignment: Alignment.centerLeft,
                      child: TextButton.icon(
                        onPressed: () => _openAddActivity(context, ref),
                        icon: const Icon(Icons.add, size: 16),
                        label: const Text('إضافة نشاط'),
                      ),
                    ),
                    if (activities.isEmpty)
                      const EmptyState(
                        icon: Icons.timeline,
                        message: 'لا يوجد أنشطة بعد.',
                      )
                    else
                      for (final a in activities) _ActivityTile(activity: a),
                  ],
                ),
              ),
            ],
          ),
        );
      },
    );
  }

  void _openAddActivity(BuildContext context, WidgetRef ref) {
    showModalBottomSheet(
      context: context,
      isScrollControlled: true,
      builder: (_) => _AddActivitySheet(
        leadId: leadId,
        onCreated: () => ref.invalidate(_leadProvider(leadId)),
      ),
    );
  }
}

class _InfoRow extends StatelessWidget {
  final IconData icon;
  final String text;
  const _InfoRow({required this.icon, required this.text});
  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 3),
      child: Row(
        children: [
          Icon(icon, size: 14, color: BrandColors.slate400),
          const SizedBox(width: 6),
          Expanded(child: Text(text,
              style: const TextStyle(
                  color: BrandColors.slate700, fontSize: 12))),
        ],
      ),
    );
  }
}

class _StatusPicker extends ConsumerWidget {
  final int leadId;
  final String currentStatus;
  final String currentLabel;
  final VoidCallback onChanged;
  const _StatusPicker({
    required this.leadId,
    required this.currentStatus,
    required this.currentLabel,
    required this.onChanged,
  });
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final stagesAsync = ref.watch(_stagesProvider);
    return SectionCard(
      emoji: '📌',
      title: 'الحالة الحالية: $currentLabel',
      child: stagesAsync.maybeWhen(
        data: (stages) => Wrap(
          spacing: 6,
          runSpacing: 6,
          children: [
            for (final s in stages)
              if (s['code'] != currentStatus)
                OutlinedButton(
                  onPressed: () async {
                    final messenger = ScaffoldMessenger.of(context);
                    try {
                      await ref.read(mobileExtrasRepoProvider)
                          .changeLeadStatus(leadId,
                              newStatus: s['code']?.toString() ?? '');
                      messenger.showSnackBar(SnackBar(
                          content: Text('تم النقل إلى ${s['label_ar']}')));
                      onChanged();
                    } on ApiException catch (e) {
                      messenger.showSnackBar(
                          SnackBar(content: Text(e.message)));
                    }
                  },
                  child: Text(s['label_ar']?.toString() ?? ''),
                ),
          ],
        ),
        orElse: () => const SizedBox(),
      ),
    );
  }
}

class _ActivityTile extends StatelessWidget {
  final Map<String, dynamic> activity;
  const _ActivityTile({required this.activity});
  @override
  Widget build(BuildContext context) {
    return ListTile(
      contentPadding: EdgeInsets.zero,
      dense: true,
      leading: Text(activity['type_icon']?.toString() ?? '📌',
          style: const TextStyle(fontSize: 20)),
      title: Text(
        activity['subject']?.toString() ??
            activity['type_label_ar']?.toString() ??
            '',
        style: const TextStyle(fontSize: 13),
      ),
      subtitle: Text(
        (activity['activity_date']?.toString() ?? '').split('.').first,
        style: const TextStyle(
            fontSize: 11, color: BrandColors.slate500),
      ),
    );
  }
}

class _AddActivitySheet extends ConsumerStatefulWidget {
  final int leadId;
  final VoidCallback onCreated;
  const _AddActivitySheet({
    required this.leadId,
    required this.onCreated,
  });
  @override
  ConsumerState<_AddActivitySheet> createState() =>
      _AddActivitySheetState();
}

class _AddActivitySheetState extends ConsumerState<_AddActivitySheet> {
  String _type = 'CALL';
  final _subject = TextEditingController();
  final _body = TextEditingController();
  bool _submitting = false;

  @override
  void dispose() {
    _subject.dispose();
    _body.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: EdgeInsets.only(
        left: 16, right: 16, top: 16,
        bottom: MediaQuery.of(context).viewInsets.bottom + 16,
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          const Text('إضافة نشاط',
              style: TextStyle(
                  fontSize: 15, fontWeight: FontWeight.w700)),
          const SizedBox(height: 12),
          DropdownButtonFormField<String>(
            initialValue: _type,
            decoration: const InputDecoration(labelText: 'النوع'),
            items: const [
              DropdownMenuItem(value: 'CALL', child: Text('📞 مكالمة')),
              DropdownMenuItem(value: 'EMAIL', child: Text('✉ إيميل')),
              DropdownMenuItem(value: 'MEETING', child: Text('🤝 اجتماع')),
              DropdownMenuItem(value: 'NOTE', child: Text('📝 ملاحظة')),
              DropdownMenuItem(value: 'WHATSAPP',
                  child: Text('💬 واتساب')),
              DropdownMenuItem(value: 'VISIT', child: Text('🚶 زيارة')),
            ],
            onChanged: (v) => setState(() => _type = v ?? 'CALL'),
          ),
          const SizedBox(height: 8),
          TextField(
            controller: _subject,
            decoration: const InputDecoration(labelText: 'الموضوع'),
          ),
          const SizedBox(height: 8),
          TextField(
            controller: _body,
            decoration: const InputDecoration(labelText: 'ملاحظات'),
            maxLines: 3,
          ),
          const SizedBox(height: 12),
          FilledButton(
            onPressed: _submitting ? null : _submit,
            child: _submitting
                ? const SizedBox(
                    height: 16, width: 16,
                    child: CircularProgressIndicator(strokeWidth: 2))
                : const Text('حفظ'),
          ),
        ],
      ),
    );
  }

  Future<void> _submit() async {
    setState(() => _submitting = true);
    final messenger = ScaffoldMessenger.of(context);
    try {
      await ref.read(mobileExtrasRepoProvider).addLeadActivity(
        widget.leadId,
        type: _type,
        subject: _subject.text.trim().isEmpty
            ? null : _subject.text.trim(),
        body: _body.text.trim().isEmpty ? null : _body.text.trim(),
      );
      widget.onCreated();
      if (mounted) Navigator.of(context).pop();
    } on ApiException catch (e) {
      messenger.showSnackBar(SnackBar(content: Text(e.message)));
    } finally {
      if (mounted) setState(() => _submitting = false);
    }
  }
}
