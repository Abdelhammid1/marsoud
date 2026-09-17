// MARSOUD-MOBILE-TKT-01 (2026-08-18) — lead detail. Basic info +
// status picker + timeline of activities + "add activity" sheet.
//
// MARSOUD-MOBILE-LEAD-FILES-01 (2026-09-17) — Batch 3 tail: the
// two file slots the web /leads/<id>/upload/<kind> endpoint already
// writes (quotation_path, contract_path) are now rendered as a
// "الملفات والمرفقات" section between the sales-action bar and the
// activities feed.  Tapping a chip opens the PDF in the phone
// browser via `url_launcher`.  Section is hidden entirely when the
// tenant has uploaded neither.  Upload from mobile is deferred to
// a follow-up (needs file_picker + a new /api/v1/my endpoint).
//
// MARSOUD-MOBILE-LEADS-DETAIL-02 (2026-09-17) — Abdelhamid's
// feedback on the 1.0.4 build: the screen was showing basic info +
// stage picker + a bare activities list. Missing (per screenshot):
//   · وصف طلب العميل  (backend already returns request_description)
//   · الملاحظات       (backend already returns notes + meeting_notes)
//   · السجل           (backend already returns history events)
//   · Activity body / result / precise time — the endpoint sends
//     `body` + `activity_date` (iso with time), the old tile only
//     used the subject + a truncated date, so a "مكالمة" tile with
//     zero context of what happened on the call
//
// All the data is on the wire already — this rewrite just adds the
// sections + polishes the activity tile.  Nothing on the backend
// changes.  Files-per-activity, contracts, and quotes require new
// endpoints; those are deferred to Batch 3.
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:url_launcher/url_launcher.dart';

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
        final history = (lead['history'] as List?)
                ?.cast<Map<String, dynamic>>() ??
            const [];
        final requestDesc =
            (lead['request_description'] ?? '').toString().trim();
        final notes = (lead['notes'] ?? '').toString().trim();
        final meetingNotes =
            (lead['meeting_notes'] ?? '').toString().trim();
        final salesAction =
            (lead['sales_action_required'] ?? '').toString().trim();
        final quotationUrl =
            (lead['quotation_url'] ?? '').toString().trim();
        final contractUrl =
            (lead['contract_url'] ?? '').toString().trim();
        final hasFiles =
            quotationUrl.isNotEmpty || contractUrl.isNotEmpty;
        return RefreshIndicator(
          color: BrandColors.emerald600,
          onRefresh: () async => ref.invalidate(_leadProvider(leadId)),
          child: ListView(
            padding: const EdgeInsets.fromLTRB(12, 12, 12, 32),
            children: [
              // ── Basic identity card ─────────────────────────
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
                          text: _fmtDateOnly(
                              lead['next_meeting'].toString())),
                    if (lead['expected_value'] != null)
                      _InfoRow(icon: Icons.attach_money,
                          text:
                              '${lead['expected_value']} ج.م (متوقّع)'),
                  ],
                ),
              ),
              const SizedBox(height: 12),

              // ── Status picker ───────────────────────────────
              _StatusPicker(
                leadId: leadId,
                currentStatus: lead['status']?.toString() ?? '',
                currentLabel:
                    lead['status_label_ar']?.toString() ?? '',
                onChanged: () => ref.invalidate(_leadProvider(leadId)),
              ),
              const SizedBox(height: 12),

              // ── وصف طلب العميل ─────────────────────────────
              // MARSOUD-MOBILE-LEADS-DETAIL-02 — new section. The
              // subtitle above shows a one-liner `service_needed`;
              // the FULL request lives in `request_description`
              // and used to be invisible on mobile.
              if (requestDesc.isNotEmpty) ...[
                SectionCard(
                  emoji: '📋',
                  title: 'وصف طلب العميل',
                  child: SelectableText(
                    requestDesc,
                    style: const TextStyle(
                      color: BrandColors.slate700,
                      fontSize: 13,
                      height: 1.7,
                    ),
                  ),
                ),
                const SizedBox(height: 12),
              ],

              // ── الملاحظات ───────────────────────────────────
              if (notes.isNotEmpty || meetingNotes.isNotEmpty) ...[
                SectionCard(
                  emoji: '📝',
                  title: 'الملاحظات',
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      if (notes.isNotEmpty)
                        _NotesBlock(
                            label: 'ملاحظات عامة', body: notes),
                      if (notes.isNotEmpty && meetingNotes.isNotEmpty)
                        const SizedBox(height: 8),
                      if (meetingNotes.isNotEmpty)
                        _NotesBlock(
                            label: 'ملاحظات آخر اجتماع',
                            body: meetingNotes),
                    ],
                  ),
                ),
                const SizedBox(height: 12),
              ],

              // ── الإجراء المطلوب من المبيعات ─────────────────
              if (salesAction.isNotEmpty) ...[
                SectionCard(
                  emoji: '⚡',
                  title: 'إجراء مطلوب',
                  child: SelectableText(
                    salesAction,
                    style: const TextStyle(
                      color: Color(0xFFB45309),
                      fontSize: 13,
                      height: 1.6,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                ),
                const SizedBox(height: 12),
              ],

              // ── الملفات والمرفقات ───────────────────────────
              // MARSOUD-MOBILE-LEAD-FILES-01 (2026-09-17) — عرض
              // السعر والعقد من الويب.  Hidden when the tenant has
              // neither so the section doesn't add noise to an
              // empty lead.
              if (hasFiles) ...[
                SectionCard(
                  emoji: '📎',
                  title: 'الملفات والمرفقات',
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      if (quotationUrl.isNotEmpty)
                        _FileChip(
                          label: 'عرض السعر',
                          emoji: '📄',
                          url: quotationUrl,
                        ),
                      if (quotationUrl.isNotEmpty &&
                          contractUrl.isNotEmpty)
                        const SizedBox(height: 8),
                      if (contractUrl.isNotEmpty)
                        _FileChip(
                          label: 'العقد',
                          emoji: '📑',
                          url: contractUrl,
                        ),
                      const SizedBox(height: 8),
                      Text(
                        'الرفع بيتم من نسخة الويب حالياً.',
                        style: const TextStyle(
                          fontSize: 11,
                          color: BrandColors.slate500,
                        ),
                      ),
                    ],
                  ),
                ),
                const SizedBox(height: 12),
              ],

              // ── الأنشطة / التعليقات ────────────────────────
              SectionCard(
                emoji: '🗓',
                title: 'الأنشطة والتعليقات',
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    Align(
                      alignment: Alignment.centerLeft,
                      child: TextButton.icon(
                        onPressed: () => _openAddActivity(context, ref),
                        icon: const Icon(Icons.add, size: 16),
                        label: const Text('إضافة نشاط / تعليق'),
                      ),
                    ),
                    if (activities.isEmpty)
                      const EmptyState(
                        icon: Icons.timeline,
                        message: 'لا يوجد أنشطة أو تعليقات بعد.',
                      )
                    else
                      for (final a in activities)
                        _ActivityCard(activity: a),
                  ],
                ),
              ),
              const SizedBox(height: 12),

              // ── السجل (Status change log) ──────────────────
              // MARSOUD-MOBILE-LEADS-DETAIL-02 — new section.  The
              // backend already sends `history`; this is a plain
              // read-only list of stage changes with the note the
              // user typed at each move.
              SectionCard(
                emoji: '📜',
                title: 'السجل',
                child: history.isEmpty
                    ? const EmptyState(
                        icon: Icons.history,
                        message: 'لم تُنقل الحالة بعد.',
                      )
                    : Column(
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        children: [
                          for (final ev in history)
                            _HistoryTile(event: ev),
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


// ══════ Helpers ═══════════════════════════════════════════════════
String _fmtDateOnly(String iso) {
  // Backend sends ISO with time; trim the T-part for date-only fields
  // (e.g. next_meeting shows in the identity card).  Falls back to
  // the raw string if the split fails.
  final t = iso.split('T');
  return t.isEmpty ? iso : t.first;
}

String _fmtActivityWhen(String? iso) {
  // Show "YYYY-MM-DD · HH:mm" so a "مكالمة" tile actually tells the
  // reader when the call happened.  Old code truncated at "." which
  // dropped the time entirely on rows that had it.
  if (iso == null || iso.isEmpty) return '—';
  final noMs = iso.split('.').first;
  final t = noMs.split('T');
  if (t.length < 2) return noMs;
  final date = t[0];
  final hm = t[1].split(':');
  if (hm.length < 2) return date;
  return '$date · ${hm[0]}:${hm[1]}';
}


// ══════ Small widgets ═════════════════════════════════════════════
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

/// MARSOUD-MOBILE-LEAD-FILES-01 (2026-09-17) — tappable chip that
/// opens the PDF (or any file the server serves) in the phone
/// browser via url_launcher.  A failed launch shows a snack bar
/// with the raw URL so the user can copy it manually — never
/// silently swallows.
class _FileChip extends StatelessWidget {
  final String label;
  final String emoji;
  final String url;
  const _FileChip({
    required this.label,
    required this.emoji,
    required this.url,
  });

  Future<void> _open(BuildContext context) async {
    final uri = Uri.tryParse(url);
    if (uri == null) return;
    final messenger = ScaffoldMessenger.of(context);
    try {
      final ok = await launchUrl(
        uri,
        mode: LaunchMode.externalApplication,
      );
      if (!ok) {
        messenger.showSnackBar(
          SnackBar(content: Text('تعذّر فتح الملف. $url')),
        );
      }
    } catch (_) {
      messenger.showSnackBar(
        SnackBar(content: Text('تعذّر فتح الملف. $url')),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    return InkWell(
      onTap: () => _open(context),
      borderRadius: BorderRadius.circular(8),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
        decoration: BoxDecoration(
          color: BrandColors.slate50,
          borderRadius: BorderRadius.circular(8),
          border: Border.all(color: BrandColors.slate200),
        ),
        child: Row(
          children: [
            Text(emoji, style: const TextStyle(fontSize: 18)),
            const SizedBox(width: 10),
            Expanded(
              child: Text(
                label,
                style: const TextStyle(
                  fontSize: 13,
                  fontWeight: FontWeight.w700,
                  color: BrandColors.navy900,
                ),
              ),
            ),
            const Icon(
              Icons.open_in_new,
              size: 16,
              color: BrandColors.slate500,
            ),
          ],
        ),
      ),
    );
  }
}

class _NotesBlock extends StatelessWidget {
  final String label;
  final String body;
  const _NotesBlock({required this.label, required this.body});
  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(
        color: BrandColors.slate50,
        borderRadius: BorderRadius.circular(8),
        border: Border.all(color: BrandColors.slate200),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(label,
              style: const TextStyle(
                fontSize: 11,
                fontWeight: FontWeight.w700,
                color: BrandColors.slate500,
              )),
          const SizedBox(height: 4),
          SelectableText(body,
              style: const TextStyle(
                color: BrandColors.slate700,
                fontSize: 13,
                height: 1.7,
              )),
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


class _ActivityCard extends StatelessWidget {
  final Map<String, dynamic> activity;
  const _ActivityCard({required this.activity});
  @override
  Widget build(BuildContext context) {
    // MARSOUD-MOBILE-LEADS-DETAIL-02 — was a dense ListTile that
    // showed the subject + a truncated date.  Now shows:
    //   · type icon + type label (مكالمة / اجتماع / …)
    //   · subject (bold)
    //   · body — the "نتيجة النشاط" the user reported missing
    //   · date · time in a subdued row
    //   · follow-up chip when set
    final typeIcon = (activity['type_icon'] ?? '📌').toString();
    final typeLabel = (activity['type_label_ar'] ?? '').toString();
    final subject = (activity['subject'] ?? '').toString().trim();
    final body = (activity['body'] ?? '').toString().trim();
    final when = _fmtActivityWhen(
        activity['activity_date']?.toString());
    final followUp = activity['follow_up_date']?.toString();
    return Container(
      margin: const EdgeInsets.only(top: 8),
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(
        color: Colors.white,
        border: Border.all(color: BrandColors.slate200),
        borderRadius: BorderRadius.circular(10),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              Text(typeIcon, style: const TextStyle(fontSize: 18)),
              const SizedBox(width: 8),
              Expanded(
                child: Text(
                  typeLabel.isEmpty ? '—' : typeLabel,
                  style: const TextStyle(
                    fontSize: 12,
                    fontWeight: FontWeight.w700,
                    color: BrandColors.emerald700,
                  ),
                ),
              ),
              Text(when,
                  style: const TextStyle(
                    fontSize: 10.5,
                    color: BrandColors.slate500,
                    fontFamily: 'monospace',
                  )),
            ],
          ),
          if (subject.isNotEmpty) ...[
            const SizedBox(height: 6),
            Text(
              subject,
              style: const TextStyle(
                fontSize: 13,
                fontWeight: FontWeight.w700,
                color: BrandColors.navy900,
              ),
            ),
          ],
          if (body.isNotEmpty) ...[
            const SizedBox(height: 4),
            SelectableText(
              body,
              style: const TextStyle(
                fontSize: 12.5,
                color: BrandColors.slate700,
                height: 1.6,
              ),
            ),
          ],
          if (followUp != null && followUp.isNotEmpty) ...[
            const SizedBox(height: 6),
            Container(
              padding: const EdgeInsets.symmetric(
                  horizontal: 8, vertical: 3),
              decoration: BoxDecoration(
                color: const Color(0xFFFEF3C7),
                borderRadius: BorderRadius.circular(6),
              ),
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  const Icon(Icons.event_note,
                      size: 12, color: Color(0xFFB45309)),
                  const SizedBox(width: 4),
                  Text(
                    'متابعة: ${_fmtDateOnly(followUp)}',
                    style: const TextStyle(
                      fontSize: 10.5,
                      color: Color(0xFFB45309),
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                ],
              ),
            ),
          ],
        ],
      ),
    );
  }
}


class _HistoryTile extends StatelessWidget {
  final Map<String, dynamic> event;
  const _HistoryTile({required this.event});
  @override
  Widget build(BuildContext context) {
    final label = (event['to_status_label_ar'] ?? '').toString();
    final note = (event['note'] ?? '').toString().trim();
    final when = _fmtActivityWhen(event['created_at']?.toString());
    return Container(
      margin: const EdgeInsets.only(top: 6),
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
      decoration: BoxDecoration(
        color: BrandColors.slate50,
        borderRadius: BorderRadius.circular(8),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              const Icon(Icons.arrow_forward,
                  size: 13, color: BrandColors.emerald600),
              const SizedBox(width: 6),
              Expanded(
                child: Text(
                  'نقل إلى: ${label.isEmpty ? "—" : label}',
                  style: const TextStyle(
                    fontSize: 12,
                    fontWeight: FontWeight.w700,
                    color: BrandColors.navy900,
                  ),
                ),
              ),
              Text(when,
                  style: const TextStyle(
                    fontSize: 10,
                    color: BrandColors.slate500,
                    fontFamily: 'monospace',
                  )),
            ],
          ),
          if (note.isNotEmpty) ...[
            const SizedBox(height: 4),
            Text(note,
                style: const TextStyle(
                  fontSize: 11.5,
                  color: BrandColors.slate500,
                  height: 1.5,
                )),
          ],
        ],
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
            decoration: const InputDecoration(
                labelText: 'النتيجة / التعليق'),
            maxLines: 4,
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
