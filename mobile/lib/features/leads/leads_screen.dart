// MARSOUD-MOBILE-TKT-01 (2026-08-18) — عملائي المحتملين. Reads
// /api/v1/my/leads. Shows a filter chip row (stages from
// /api/v1/my/leads/stages) + a card per lead. Tap → detail.
// MARSOUD-MOBILE-LEAD-CREATE-01 (2026-09-03) — floating "+" now
// opens an in-page bottom sheet to add a new prospect straight
// from the phone (hits POST /api/v1/my/leads, same web pipeline).
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../app/theme.dart';
import '../../data/api_client.dart';
import '../../data/mobile_extras_repository.dart';
import '../../widgets/section_card.dart';

final _stagesProvider = FutureProvider.autoDispose<List<Map<String, dynamic>>>(
    (ref) async {
  final r = await ref.watch(mobileExtrasRepoProvider).leadStages();
  return (r['stages'] as List).cast<Map<String, dynamic>>();
});

final _leadsProvider = FutureProvider.autoDispose
    .family<Map<String, dynamic>, String?>((ref, status) {
  return ref.watch(mobileExtrasRepoProvider).leads(status: status);
});

class LeadsScreen extends ConsumerStatefulWidget {
  const LeadsScreen({super.key});
  @override
  ConsumerState<LeadsScreen> createState() => _LeadsScreenState();
}

class _LeadsScreenState extends ConsumerState<LeadsScreen> {
  String? _status;

  @override
  Widget build(BuildContext context) {
    final leadsAsync = ref.watch(_leadsProvider(_status));
    final stagesAsync = ref.watch(_stagesProvider);
    return Stack(
      children: [
        leadsAsync.when(
          loading: () => const Center(child: CircularProgressIndicator()),
          error: (e, _) => Center(
            child: Padding(
              padding: const EdgeInsets.all(24),
              child: Text(e is ApiException ? e.message : e.toString()),
            ),
          ),
          data: (data) {
            final leads = (data['leads'] as List?)
                    ?.cast<Map<String, dynamic>>() ??
                const [];
            return RefreshIndicator(
              color: BrandColors.emerald600,
              onRefresh: () async =>
                  ref.invalidate(_leadsProvider(_status)),
              child: ListView(
                padding: const EdgeInsets.fromLTRB(12, 12, 12, 96),
                children: [
                  // Filter chips
                  stagesAsync.maybeWhen(
                    data: (stages) => _StageChips(
                      stages: stages,
                      selected: _status,
                      onChanged: (s) => setState(() => _status = s),
                    ),
                    orElse: () => const SizedBox.shrink(),
                  ),
                  const SizedBox(height: 8),
                  if (leads.isEmpty)
                    SectionCard(
                      child: const EmptyState(
                        icon: Icons.person_search,
                        message: 'ما فيش عملاء محتملين حالياً.',
                      ),
                    )
                  else
                    for (final l in leads)
                      _LeadCard(lead: l),
                ],
              ),
            );
          },
        ),
        // MARSOUD-MOBILE-LEAD-CREATE-01 — anchored bottom-left in RTL
        // (LTR-adjusted by Directionality). Positioned instead of a
        // Scaffold.floatingActionButton because LeadsScreen is
        // embedded inside HomeShell's own Scaffold body.
        Positioned(
          right: 16,
          bottom: 16,
          child: FloatingActionButton.extended(
            heroTag: 'leads_add_fab',
            backgroundColor: BrandColors.emerald600,
            foregroundColor: Colors.white,
            icon: const Icon(Icons.person_add_alt_1),
            label: const Text('عميل جديد',
                style: TextStyle(fontWeight: FontWeight.w700)),
            onPressed: () => _openCreateSheet(context),
          ),
        ),
      ],
    );
  }

  Future<void> _openCreateSheet(BuildContext context) async {
    final created = await showModalBottomSheet<bool>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      backgroundColor: Colors.white,
      shape: const RoundedRectangleBorder(
        borderRadius:
            BorderRadius.vertical(top: Radius.circular(20)),
      ),
      builder: (ctx) => Padding(
        padding: EdgeInsets.only(
          bottom: MediaQuery.of(ctx).viewInsets.bottom,
        ),
        child: const _AddLeadSheet(),
      ),
    );
    if (created == true && mounted) {
      ref.invalidate(_leadsProvider(_status));
    }
  }
}

class _AddLeadSheet extends ConsumerStatefulWidget {
  const _AddLeadSheet();
  @override
  ConsumerState<_AddLeadSheet> createState() => _AddLeadSheetState();
}

class _AddLeadSheetState extends ConsumerState<_AddLeadSheet> {
  final _formKey = GlobalKey<FormState>();
  final _clientNameCtl = TextEditingController();
  final _phoneCtl = TextEditingController();
  final _serviceCtl = TextEditingController();
  final _notesCtl = TextEditingController();
  bool _saving = false;

  @override
  void dispose() {
    _clientNameCtl.dispose();
    _phoneCtl.dispose();
    _serviceCtl.dispose();
    _notesCtl.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    if (!_formKey.currentState!.validate()) return;
    setState(() => _saving = true);
    try {
      await ref.read(mobileExtrasRepoProvider).createLead(
            clientName: _clientNameCtl.text.trim(),
            phone: _phoneCtl.text.trim(),
            serviceNeeded: _serviceCtl.text.trim(),
            notes: _notesCtl.text.trim(),
          );
      if (!mounted) return;
      Navigator.of(context).pop(true);
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text('تم إضافة العميل المحتمل'),
          backgroundColor: BrandColors.emerald600,
        ),
      );
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() => _saving = false);
      final msg = e.status == 403
          ? 'ما عندكش صلاحية إضافة عميل محتمل — كلّم مدير المبيعات.'
          : e.message;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(msg),
            backgroundColor: Colors.red.shade600),
      );
    } catch (e) {
      if (!mounted) return;
      setState(() => _saving = false);
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('حدث خطأ: $e'),
            backgroundColor: Colors.red.shade600),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    return SingleChildScrollView(
      padding: const EdgeInsets.fromLTRB(20, 4, 20, 24),
      child: Form(
        key: _formKey,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            const Text('إضافة عميل محتمل جديد',
                textAlign: TextAlign.center,
                style: TextStyle(
                    fontSize: 17,
                    fontWeight: FontWeight.w800,
                    color: BrandColors.navy900)),
            const SizedBox(height: 16),
            TextFormField(
              controller: _clientNameCtl,
              textInputAction: TextInputAction.next,
              decoration: const InputDecoration(
                labelText: 'اسم العميل *',
                prefixIcon: Icon(Icons.person_outline),
                border: OutlineInputBorder(),
              ),
              validator: (v) =>
                  (v == null || v.trim().isEmpty) ? 'حقل مطلوب' : null,
            ),
            const SizedBox(height: 12),
            TextFormField(
              controller: _phoneCtl,
              keyboardType: TextInputType.phone,
              textInputAction: TextInputAction.next,
              decoration: const InputDecoration(
                labelText: 'رقم الجوال *',
                prefixIcon: Icon(Icons.phone_outlined),
                border: OutlineInputBorder(),
              ),
              validator: (v) =>
                  (v == null || v.trim().isEmpty) ? 'حقل مطلوب' : null,
            ),
            const SizedBox(height: 12),
            TextFormField(
              controller: _serviceCtl,
              textInputAction: TextInputAction.next,
              decoration: const InputDecoration(
                labelText: 'الخدمة المطلوبة *',
                prefixIcon: Icon(Icons.work_outline),
                border: OutlineInputBorder(),
              ),
              validator: (v) =>
                  (v == null || v.trim().isEmpty) ? 'حقل مطلوب' : null,
            ),
            const SizedBox(height: 12),
            TextFormField(
              controller: _notesCtl,
              maxLines: 3,
              decoration: const InputDecoration(
                labelText: 'ملاحظات (اختياري)',
                alignLabelWithHint: true,
                border: OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 20),
            SizedBox(
              height: 48,
              child: FilledButton.icon(
                onPressed: _saving ? null : _submit,
                style: FilledButton.styleFrom(
                  backgroundColor: BrandColors.emerald600,
                ),
                icon: _saving
                    ? const SizedBox(
                        height: 16,
                        width: 16,
                        child: CircularProgressIndicator(
                            strokeWidth: 2, color: Colors.white))
                    : const Icon(Icons.check),
                label: Text(
                  _saving ? 'جاري الحفظ…' : 'حفظ العميل',
                  style: const TextStyle(fontWeight: FontWeight.w700),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _StageChips extends StatelessWidget {
  final List<Map<String, dynamic>> stages;
  final String? selected;
  final ValueChanged<String?> onChanged;
  const _StageChips({
    required this.stages,
    required this.selected,
    required this.onChanged,
  });
  @override
  Widget build(BuildContext context) {
    return SingleChildScrollView(
      scrollDirection: Axis.horizontal,
      child: Row(
        children: [
          ChoiceChip(
            label: const Text('الكل'),
            selected: selected == null,
            onSelected: (_) => onChanged(null),
          ),
          const SizedBox(width: 6),
          for (final s in stages) ...[
            ChoiceChip(
              label: Text(s['label_ar']?.toString() ?? s['code']),
              selected: selected == s['code'],
              onSelected: (_) => onChanged(s['code']?.toString()),
            ),
            const SizedBox(width: 6),
          ],
        ],
      ),
    );
  }
}

class _LeadCard extends StatelessWidget {
  final Map<String, dynamic> lead;
  const _LeadCard({required this.lead});
  @override
  Widget build(BuildContext context) {
    final id = lead['id'];
    return Card(
      margin: const EdgeInsets.only(bottom: 8),
      child: InkWell(
        onTap: () => context.push('/leads/$id'),
        borderRadius: BorderRadius.circular(12),
        child: Padding(
          padding: const EdgeInsets.all(14),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Row(
                children: [
                  Expanded(
                    child: Text(
                      lead['client_name']?.toString() ?? '—',
                      style: const TextStyle(
                          fontWeight: FontWeight.w700,
                          fontSize: 14,
                          color: BrandColors.navy900),
                    ),
                  ),
                  _StatusBadge(
                    label: lead['status_label_ar']?.toString() ?? '',
                  ),
                ],
              ),
              const SizedBox(height: 6),
              if ((lead['service_needed'] ?? '').toString().isNotEmpty)
                Text(
                  lead['service_needed'].toString(),
                  style: const TextStyle(
                      color: BrandColors.slate500, fontSize: 12),
                ),
              const SizedBox(height: 6),
              Row(
                children: [
                  const Icon(Icons.phone,
                      size: 14, color: BrandColors.slate400),
                  const SizedBox(width: 4),
                  Text(lead['phone']?.toString() ?? '—',
                      style: const TextStyle(
                          color: BrandColors.slate500,
                          fontSize: 12,
                          fontFamily: 'monospace')),
                  const Spacer(),
                  if (lead['next_meeting'] != null) ...[
                    const Icon(Icons.event,
                        size: 14, color: BrandColors.slate400),
                    const SizedBox(width: 4),
                    Text(
                      lead['next_meeting'].toString().split('T').first,
                      style: const TextStyle(
                          color: BrandColors.slate500,
                          fontSize: 12,
                          fontFamily: 'monospace'),
                    ),
                  ],
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _StatusBadge extends StatelessWidget {
  final String label;
  const _StatusBadge({required this.label});
  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
      decoration: BoxDecoration(
        color: BrandColors.slate100,
        borderRadius: BorderRadius.circular(999),
      ),
      child: Text(
        label,
        style: const TextStyle(
            color: BrandColors.slate700,
            fontSize: 11,
            fontWeight: FontWeight.w600),
      ),
    );
  }
}
