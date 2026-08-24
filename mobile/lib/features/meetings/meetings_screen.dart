// MARSOUD-MOBILE-TKT-01 (2026-08-18) — اجتماعاتي. Reads
// /api/v1/my/meetings (merged CalendarEvent + LeadActivity
// meetings) grouped by today / tomorrow / this week.
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/theme.dart';
import '../../data/api_client.dart';
import '../../data/mobile_extras_repository.dart';
import '../../widgets/section_card.dart';

final _meetingsProvider =
    FutureProvider.autoDispose<Map<String, dynamic>>((ref) {
  return ref.watch(mobileExtrasRepoProvider).meetings(days: 30);
});

class MeetingsScreen extends ConsumerWidget {
  const MeetingsScreen({super.key});
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final async = ref.watch(_meetingsProvider);
    return Scaffold(
      floatingActionButton: FloatingActionButton.extended(
        onPressed: () => _openCreateSheet(context, ref),
        icon: const Icon(Icons.add),
        label: const Text('اجتماع جديد'),
        backgroundColor: BrandColors.emerald600,
      ),
      body: async.when(
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (e, _) => Center(
          child: Padding(
            padding: const EdgeInsets.all(24),
            child: Text(e is ApiException ? e.message : e.toString()),
          ),
        ),
        data: (data) {
          final meetings = (data['meetings'] as List?)
                  ?.cast<Map<String, dynamic>>() ??
              const [];
          return RefreshIndicator(
            color: BrandColors.emerald600,
            onRefresh: () async => ref.invalidate(_meetingsProvider),
            child: ListView(
              padding: const EdgeInsets.fromLTRB(12, 12, 12, 80),
              children: [
                if (meetings.isEmpty)
                  SectionCard(
                    child: const EmptyState(
                      icon: Icons.event_note,
                      message: 'ما فيش اجتماعات قادمة خلال 30 يوم.',
                    ),
                  )
                else
                  for (final m in meetings) _MeetingCard(meeting: m),
              ],
            ),
          );
        },
      ),
    );
  }

  void _openCreateSheet(BuildContext context, WidgetRef ref) {
    showModalBottomSheet(
      context: context,
      isScrollControlled: true,
      builder: (_) => _CreateMeetingSheet(
        onCreated: () => ref.invalidate(_meetingsProvider),
      ),
    );
  }
}

class _MeetingCard extends StatelessWidget {
  final Map<String, dynamic> meeting;
  const _MeetingCard({required this.meeting});
  @override
  Widget build(BuildContext context) {
    final source = meeting['source']?.toString() ?? '';
    final icon = source == 'lead_activity' ? '🎯' : '📅';
    return Card(
      margin: const EdgeInsets.only(bottom: 8),
      child: ListTile(
        leading: Text(icon, style: const TextStyle(fontSize: 22)),
        title: Text(meeting['title']?.toString() ?? '',
            style: const TextStyle(
                fontWeight: FontWeight.w700, fontSize: 13)),
        subtitle: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              (meeting['starts_at']?.toString() ?? '')
                  .replaceAll('T', ' ')
                  .split('.')
                  .first,
              style: const TextStyle(
                  fontFamily: 'monospace',
                  fontSize: 11,
                  color: BrandColors.slate500),
            ),
            if ((meeting['location'] ?? '').toString().isNotEmpty)
              Text('📍 ${meeting['location']}',
                  style: const TextStyle(
                      fontSize: 11, color: BrandColors.slate500)),
          ],
        ),
      ),
    );
  }
}

class _CreateMeetingSheet extends ConsumerStatefulWidget {
  final VoidCallback onCreated;
  const _CreateMeetingSheet({required this.onCreated});
  @override
  ConsumerState<_CreateMeetingSheet> createState() =>
      _CreateMeetingSheetState();
}

class _CreateMeetingSheetState extends ConsumerState<_CreateMeetingSheet> {
  final _title = TextEditingController();
  final _location = TextEditingController();
  DateTime? _startsAt;
  bool _submitting = false;

  @override
  void dispose() {
    _title.dispose();
    _location.dispose();
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
          const Text('اجتماع جديد',
              style: TextStyle(
                  fontSize: 15, fontWeight: FontWeight.w700)),
          const SizedBox(height: 12),
          TextField(
            controller: _title,
            decoration: const InputDecoration(labelText: 'العنوان *'),
          ),
          const SizedBox(height: 8),
          TextField(
            controller: _location,
            decoration: const InputDecoration(labelText: 'المكان'),
          ),
          const SizedBox(height: 8),
          OutlinedButton.icon(
            onPressed: _pickDateTime,
            icon: const Icon(Icons.event),
            label: Text(_startsAt == null
                ? 'اختر التاريخ والوقت *'
                : _startsAt!.toIso8601String().split('.').first),
          ),
          const SizedBox(height: 12),
          FilledButton(
            onPressed: _submitting ? null : _submit,
            child: _submitting
                ? const SizedBox(
                    height: 16, width: 16,
                    child: CircularProgressIndicator(strokeWidth: 2))
                : const Text('إنشاء'),
          ),
        ],
      ),
    );
  }

  Future<void> _pickDateTime() async {
    final now = DateTime.now();
    final date = await showDatePicker(
      context: context,
      firstDate: now,
      lastDate: now.add(const Duration(days: 365)),
      initialDate: now,
    );
    if (date == null || !mounted) return;
    final time = await showTimePicker(
      context: context,
      initialTime: TimeOfDay.now(),
    );
    if (time == null || !mounted) return;
    setState(() {
      _startsAt = DateTime(
          date.year, date.month, date.day, time.hour, time.minute);
    });
  }

  Future<void> _submit() async {
    if (_title.text.trim().isEmpty || _startsAt == null) return;
    setState(() => _submitting = true);
    final messenger = ScaffoldMessenger.of(context);
    try {
      await ref.read(mobileExtrasRepoProvider).createMeeting(
        title: _title.text.trim(),
        startsAt: _startsAt!.toIso8601String(),
        location: _location.text.trim().isEmpty
            ? null : _location.text.trim(),
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
