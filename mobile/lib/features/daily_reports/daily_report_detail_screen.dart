// MARSOUD-MOBILE-FLUTTER — Daily report detail (mirrors portal_emp/daily_report_detail.html).
//
// Frozen SUBMITTED reports render read-only. DRAFT reports expose an
// employee-notes text area + a "أرسل نهائياً" button that finalises
// the report (server call is idempotent — same as the web).
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/theme.dart';
import '../../data/api_client.dart';
import '../../data/my_account_repository.dart';
import '../../widgets/gradient_button.dart';
import '../../widgets/section_card.dart';

class DailyReportDetailScreen extends ConsumerStatefulWidget {
  final int reportId;
  const DailyReportDetailScreen({super.key, required this.reportId});
  @override
  ConsumerState<DailyReportDetailScreen> createState() =>
      _DailyReportDetailScreenState();
}

class _DailyReportDetailScreenState
    extends ConsumerState<DailyReportDetailScreen> {
  late Future<Map<String, dynamic>> _future;
  final _notesCtrl = TextEditingController();
  bool _saving = false;
  bool _notesLoaded = false;

  @override
  void initState() {
    super.initState();
    _future = ref.read(myAccountRepoProvider).dailyReport(widget.reportId);
  }

  @override
  void dispose() {
    _notesCtrl.dispose();
    super.dispose();
  }

  Future<void> _saveNotes() async {
    setState(() => _saving = true);
    try {
      await ref.read(myAccountRepoProvider).saveDailyReportNotes(
            widget.reportId,
            _notesCtrl.text.trim(),
          );
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
        content: Text('تم حفظ الملاحظات.'),
      ));
    } on ApiException catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(e.message)));
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  Future<void> _submitFinal() async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('إرسال التقرير نهائياً؟'),
        content: const Text(
          'بعد الإرسال ما تقدرش تعدله ثاني. متأكد؟',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(false),
            child: const Text('رجوع'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(ctx).pop(true),
            child: const Text('إرسال'),
          ),
        ],
      ),
    );
    if (ok != true) return;
    setState(() => _saving = true);
    try {
      // Save notes first, then submit.
      if (_notesCtrl.text.trim().isNotEmpty) {
        await ref.read(myAccountRepoProvider).saveDailyReportNotes(
              widget.reportId,
              _notesCtrl.text.trim(),
            );
      }
      await ref
          .read(myAccountRepoProvider)
          .submitDailyReport(widget.reportId);
      if (!mounted) return;
      setState(() {
        _future = ref
            .read(myAccountRepoProvider)
            .dailyReport(widget.reportId);
      });
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
        content: Text('تم إرسال التقرير للمالك.'),
      ));
    } on ApiException catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(e.message)));
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return FutureBuilder<Map<String, dynamic>>(
      future: _future,
      builder: (context, snap) {
        if (!snap.hasData && !snap.hasError) {
          return const Center(child: CircularProgressIndicator());
        }
        if (snap.hasError) {
          return Center(
            child: Padding(
              padding: const EdgeInsets.all(24),
              child: Text(snap.error.toString()),
            ),
          );
        }
        final data = snap.data!;
        final r = data['report'] as Map<String, dynamic>;
        final status = r['status'];
        final statusVal =
            status is Map ? status['value'] : status?.toString();
        final isDraft = statusVal == 'DRAFT';
        final body = (r['body'] ?? '').toString();
        final notes = (r['employee_notes'] ?? '').toString();
        // MARSOUD-MOBILE-SHIP-READY-01 (M6) — was mutating
        // `_notesLoaded` + `_notesCtrl.text` inside build(). Any
        // subsequent Riverpod invalidate would refetch fresh
        // notes but the controller would never update. Defer to a
        // post-frame callback + only set the controller when its
        // current text differs from the server's — safe to run on
        // every rebuild without fighting the user's edits.
        WidgetsBinding.instance.addPostFrameCallback((_) {
          if (!mounted) return;
          if (_notesCtrl.text != notes && !_notesLoaded) {
            _notesCtrl.text = notes;
            _notesLoaded = true;
          }
        });
        final date =
            (r['report_date'] as String?)?.substring(0, 10) ?? '—';
        return ListView(
          padding: const EdgeInsets.fromLTRB(12, 16, 12, 32),
          children: [
            SectionCard(
              emoji: '📝',
              title: 'تقرير يوم $date',
              subtitle: isDraft
                  ? 'مسودة — راجع، أضف ملاحظاتك، وارسل نهائياً للمالك.'
                  : 'التقرير أُرسل نهائياً. لا يمكن التعديل.',
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Container(
                    padding: const EdgeInsets.all(14),
                    decoration: BoxDecoration(
                      color: BrandColors.slate50,
                      borderRadius: BorderRadius.circular(10),
                      border: Border.all(color: BrandColors.slate200),
                    ),
                    child: Text(
                      body.isEmpty ? '— لا يوجد نشاط لهذا اليوم —' : body,
                      style: const TextStyle(
                        color: BrandColors.navy900,
                        fontSize: 13,
                        height: 1.85,
                      ),
                    ),
                  ),
                  const SizedBox(height: 16),
                  const Text(
                    'ملاحظاتي',
                    style: TextStyle(
                      color: BrandColors.slate700,
                      fontWeight: FontWeight.w700,
                      fontSize: 13,
                    ),
                  ),
                  const SizedBox(height: 6),
                  TextField(
                    controller: _notesCtrl,
                    enabled: isDraft && !_saving,
                    minLines: 3,
                    maxLines: 8,
                    decoration: InputDecoration(
                      hintText: isDraft
                          ? 'اكتب أي ملاحظة قبل الإرسال…'
                          : '—',
                      filled: true,
                      fillColor:
                          isDraft ? Colors.white : BrandColors.slate50,
                    ),
                  ),
                  if (isDraft) ...[
                    const SizedBox(height: 12),
                    Row(
                      children: [
                        Expanded(
                          child: OutlinedButton.icon(
                            onPressed: _saving ? null : _saveNotes,
                            style: OutlinedButton.styleFrom(
                              minimumSize: const Size.fromHeight(48),
                              side: const BorderSide(
                                  color: BrandColors.slate200),
                              shape: RoundedRectangleBorder(
                                borderRadius: BorderRadius.circular(12),
                              ),
                            ),
                            icon: const Icon(Icons.save,
                                color: BrandColors.slate700, size: 18),
                            label: const Text(
                              'حفظ الملاحظات',
                              style: TextStyle(
                                color: BrandColors.slate700,
                                fontWeight: FontWeight.w700,
                              ),
                            ),
                          ),
                        ),
                        const SizedBox(width: 10),
                        Expanded(
                          child: GradientButton(
                            label: 'إرسال نهائياً',
                            icon: Icons.check_circle,
                            loading: _saving,
                            onPressed: _saving ? null : _submitFinal,
                          ),
                        ),
                      ],
                    ),
                  ],
                ],
              ),
            ),
          ],
        );
      },
    );
  }
}
