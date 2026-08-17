// MARSOUD-MOBILE-FLUTTER — /home. Pixel-matches portal_emp/account.html.
//
// Structure, top → bottom (mirrors the web):
//   1. Identity card + horizontal tab-strip (7 anchors)
//   2. 📋 بياناتي — 2-col profile data grid
//   3. 🌴 رصيد الإجازات — grid of mini leave-type cards + past-requests table
//   4. 🕐 الحضور والانصراف — in/out summary + inline note
//   5. 💵 السلف — 4-metric grid OR "no active advance" placeholder
//   6. 💰 قسائم الرواتب — table of periods with net + PDF button
//   7. 🔒 تغيير كلمة السر — old/new/confirm form
//
// Anchors work by scrolling to their GlobalKey; matches how the web
// uses `href="#mydata"` etc. The الحضور tab navigates to /attendance
// (a dedicated screen with GPS check-in — same as the web).
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../app/theme.dart';
import '../../data/api_client.dart';
import '../../data/my_account_repository.dart';
import '../../widgets/gradient_button.dart';

final _accountProvider = FutureProvider.autoDispose<Map<String, dynamic>>(
    (ref) => ref.watch(myAccountRepoProvider).account());

class MyAccountScreen extends ConsumerStatefulWidget {
  const MyAccountScreen({super.key});

  @override
  ConsumerState<MyAccountScreen> createState() => _MyAccountScreenState();
}

class _MyAccountScreenState extends ConsumerState<MyAccountScreen> {
  // Anchor keys for the tab strip.
  final _kMydata = GlobalKey();
  final _kLeaves = GlobalKey();
  final _kAdvances = GlobalKey();
  final _kPayslips = GlobalKey();
  final _kPassword = GlobalKey();

  void _scrollTo(GlobalKey key) {
    final ctx = key.currentContext;
    if (ctx == null) return;
    Scrollable.ensureVisible(
      ctx,
      duration: const Duration(milliseconds: 320),
      curve: Curves.easeInOut,
      alignment: 0.05,
    );
  }

  @override
  Widget build(BuildContext context) {
    final async = ref.watch(_accountProvider);
    return async.when(
      loading: () =>
          const Center(child: CircularProgressIndicator()),
      error: (e, _) => _ErrorView(
        message: e is ApiException ? _friendly(e) : e.toString(),
        onRetry: () => ref.invalidate(_accountProvider),
      ),
      data: (data) {
        final emp = data['employee'] as Map<String, dynamic>;
        final tenure = data['tenure_label'] as String? ?? '—';
        final leave = data['leave'] as Map<String, dynamic>? ?? {};
        final types = (leave['types'] as List?)
                ?.cast<Map<String, dynamic>>() ??
            const [];
        final balances = (leave['balances'] as List?)
                ?.cast<Map<String, dynamic>>() ??
            const [];
        final leaveRequests = (leave['requests'] as List?)
                ?.cast<Map<String, dynamic>>() ??
            const [];
        final advance = data['advance'] as Map<String, dynamic>?;
        final activeAdv = advance?['active'] as Map<String, dynamic>?;
        final advReqs = (advance?['requests'] as List?)
                ?.cast<Map<String, dynamic>>() ??
            const [];
        final slips = (data['payslips'] as List?)
                ?.cast<Map<String, dynamic>>() ??
            const [];
        final todayCi = data['today_checkin'] as Map<String, dynamic>?;
        return RefreshIndicator(
          color: BrandColors.emerald600,
          onRefresh: () async => ref.invalidate(_accountProvider),
          child: ListView(
            padding: const EdgeInsets.fromLTRB(12, 16, 12, 32),
            children: [
              _IdentityRow(emp: emp),
              const SizedBox(height: 12),
              _TabsCard(
                onData: () => _scrollTo(_kMydata),
                onAttendance: () => context.go('/attendance'),
                onAdvances: () => _scrollTo(_kAdvances),
                onPayslips: () => _scrollTo(_kPayslips),
                onPassword: () => _scrollTo(_kPassword),
                onActivity: () => context.go('/activity'),
              ),
              const SizedBox(height: 16),
              _SectionCard(
                key: _kMydata,
                emoji: '📋',
                title: 'بياناتي',
                subtitle:
                    'تعديل الاسم/الجوال/الإيميل من إدارة الموارد البشرية',
                child: _MyDataGrid(emp: emp, tenure: tenure),
              ),
              const SizedBox(height: 12),
              _SectionCard(
                key: _kLeaves,
                emoji: '🌴',
                title: 'رصيد الإجازات',
                trailingText:
                    'السنة الحالية: ${types.length} نوع متاح',
                child: _LeaveBody(
                  types: types,
                  balances: balances,
                  requests: leaveRequests,
                ),
              ),
              const SizedBox(height: 12),
              _SectionCard(
                emoji: '🕐',
                title: 'الحضور والانصراف',
                child: _AttendanceInlineCard(todayCi: todayCi),
              ),
              const SizedBox(height: 12),
              _SectionCard(
                key: _kAdvances,
                emoji: '💵',
                title: 'السلف',
                child: _AdvancesBody(
                  advance: activeAdv,
                  requests: advReqs,
                ),
              ),
              const SizedBox(height: 12),
              _SectionCard(
                key: _kPayslips,
                emoji: '💰',
                title: 'قسائم الرواتب',
                child: _PayslipsTable(slips: slips),
              ),
              const SizedBox(height: 12),
              _SectionCard(
                key: _kPassword,
                emoji: '🔒',
                title: 'تغيير كلمة السر',
                subtitle:
                    'أدخل كلمة السر القديمة أولاً، ثم الجديدة مرتين للتأكيد. كلمة السر لازم تكون 6 أحرف على الأقل.',
                child: const _PasswordForm(),
              ),
            ],
          ),
        );
      },
    );
  }

  String _friendly(ApiException e) {
    if (e.message == 'no_employee_record') {
      return 'حسابك ليس مرتبطاً بسجل موارد بشرية في هذه الشركة — تواصل مع الإدارة.';
    }
    return e.message;
  }
}

// ═════ Section card wrapper — matches `.card p-6 mb-4` ═════════════
class _SectionCard extends StatelessWidget {
  final String? emoji;
  final String title;
  final String? subtitle;
  final String? trailingText;
  final Widget child;
  const _SectionCard({
    super.key,
    this.emoji,
    required this.title,
    this.subtitle,
    this.trailingText,
    required this.child,
  });
  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: BrandColors.slate200),
      ),
      padding: const EdgeInsets.fromLTRB(18, 18, 18, 18),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              if (emoji != null) ...[
                Text(emoji!, style: const TextStyle(fontSize: 18)),
                const SizedBox(width: 8),
              ],
              Expanded(
                child: Text(
                  title,
                  style: const TextStyle(
                    color: BrandColors.navy900,
                    fontWeight: FontWeight.w800,
                    fontSize: 15,
                  ),
                ),
              ),
              if (trailingText != null)
                Text(
                  trailingText!,
                  style: const TextStyle(
                    color: BrandColors.slate500,
                    fontSize: 11,
                  ),
                ),
            ],
          ),
          if (subtitle != null) ...[
            const SizedBox(height: 4),
            Text(
              subtitle!,
              style: const TextStyle(
                color: BrandColors.slate500,
                fontSize: 12,
                height: 1.5,
              ),
            ),
          ],
          const SizedBox(height: 14),
          child,
        ],
      ),
    );
  }
}

// ═════ Identity row — matches the col-span-1 identity card ═════════
class _IdentityRow extends StatelessWidget {
  final Map<String, dynamic> emp;
  const _IdentityRow({required this.emp});
  @override
  Widget build(BuildContext context) {
    final name = (emp['name'] as String?) ?? '—';
    final initial = name.isNotEmpty ? name.characters.first : '?';
    final jobTitle = emp['job_title'] as String? ?? '—';
    final empNum = emp['employee_number'] as String? ?? '—';
    return Container(
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: BrandColors.slate200),
      ),
      padding: const EdgeInsets.symmetric(vertical: 20, horizontal: 20),
      child: Column(
        children: [
          Container(
            width: 80,
            height: 80,
            decoration: BoxDecoration(
              color: BrandColors.emerald100,
              borderRadius: BorderRadius.circular(40),
            ),
            alignment: Alignment.center,
            child: Text(
              initial,
              style: const TextStyle(
                color: BrandColors.emerald700,
                fontSize: 34,
                fontWeight: FontWeight.w800,
              ),
            ),
          ),
          const SizedBox(height: 10),
          Text(
            name,
            style: const TextStyle(
              color: BrandColors.navy900,
              fontSize: 17,
              fontWeight: FontWeight.w800,
            ),
          ),
          const SizedBox(height: 2),
          Text(
            jobTitle,
            style: const TextStyle(
              color: BrandColors.slate500,
              fontSize: 12,
            ),
          ),
          const SizedBox(height: 10),
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 5),
            decoration: BoxDecoration(
              color: BrandColors.emerald50,
              borderRadius: BorderRadius.circular(999),
            ),
            child: Text.rich(
              TextSpan(children: [
                const TextSpan(
                  text: 'الرقم الوظيفي: ',
                  style: TextStyle(
                    color: BrandColors.emerald700,
                    fontSize: 11,
                  ),
                ),
                TextSpan(
                  text: empNum,
                  style: const TextStyle(
                    color: BrandColors.emerald700,
                    fontSize: 11,
                    fontWeight: FontWeight.w800,
                    fontFamily: 'monospace',
                  ),
                ),
              ]),
            ),
          ),
        ],
      ),
    );
  }
}

// ═════ Horizontal tabs card — matches the 7-tab strip ═════════════
class _TabsCard extends StatelessWidget {
  final VoidCallback onData;
  final VoidCallback onAttendance;
  final VoidCallback onAdvances;
  final VoidCallback onPayslips;
  final VoidCallback onPassword;
  final VoidCallback onActivity;
  const _TabsCard({
    required this.onData,
    required this.onAttendance,
    required this.onAdvances,
    required this.onPayslips,
    required this.onPassword,
    required this.onActivity,
  });
  @override
  Widget build(BuildContext context) {
    final tabs = <_TabDef>[
      _TabDef('📋 بياناتي', onData, active: true),
      _TabDef('🕐 الحضور', onAttendance),
      _TabDef('💵 السلف', onAdvances),
      _TabDef('💰 القسائم', onPayslips),
      _TabDef('📜 سجل نشاطي', onActivity),
      _TabDef('🔒 كلمة السر', onPassword),
    ];
    return Container(
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: BrandColors.slate200),
      ),
      padding: const EdgeInsets.all(8),
      child: SingleChildScrollView(
        scrollDirection: Axis.horizontal,
        // Even RTL, horizontal scroll goes physical left/right.
        reverse: false,
        child: Row(
          children: [
            for (final t in tabs)
              Padding(
                padding: const EdgeInsets.symmetric(horizontal: 3),
                child: _TabPill(def: t),
              ),
          ],
        ),
      ),
    );
  }
}

class _TabDef {
  final String label;
  final VoidCallback onTap;
  final bool active;
  const _TabDef(this.label, this.onTap, {this.active = false});
}

class _TabPill extends StatelessWidget {
  final _TabDef def;
  const _TabPill({required this.def});
  @override
  Widget build(BuildContext context) {
    return Material(
      color: Colors.transparent,
      borderRadius: BorderRadius.circular(10),
      child: InkWell(
        borderRadius: BorderRadius.circular(10),
        onTap: def.onTap,
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 11),
          decoration: BoxDecoration(
            color: def.active
                ? BrandColors.emerald50
                : BrandColors.slate50,
            borderRadius: BorderRadius.circular(10),
          ),
          child: Text(
            def.label,
            style: TextStyle(
              color: def.active
                  ? BrandColors.emerald700
                  : BrandColors.slate700,
              fontWeight: FontWeight.w700,
              fontSize: 13,
            ),
          ),
        ),
      ),
    );
  }
}

// ═════ بياناتي data grid (mirrors 2-col mobile / 3-col desktop) ═══
class _MyDataGrid extends StatelessWidget {
  final Map<String, dynamic> emp;
  final String tenure;
  const _MyDataGrid({required this.emp, required this.tenure});
  @override
  Widget build(BuildContext context) {
    final entries = <_KV>[
      _KV('الاسم الكامل', emp['name']?.toString() ?? '—'),
      _KV('رقم الجوال', emp['phone']?.toString() ?? '—', mono: true),
      _KV('البريد الإلكتروني', emp['email']?.toString() ?? '—',
          mono: true, ltr: true),
      _KV('المسمى الوظيفي', emp['job_title']?.toString() ?? '—'),
      _KV('نوع العقد', _enumLabel(emp['contract_type'])),
      _KV('تاريخ التعيين',
          (emp['start_date'] as String?)?.substring(0, 10) ?? '—',
          mono: true),
      _KV('مدة الخدمة', tenure, highlight: true),
      _KV('الراتب الأساسي', _money(emp['basic_salary']),
          mono: true, highlight: true),
      _KV('البدلات', _money(emp['allowances']), mono: true),
    ];
    return LayoutBuilder(
      builder: (context, c) {
        // Web uses 2 col on mobile, 3 on ≥sm. On a real phone (~360dp)
        // 2-col is right; on a tablet portrait (600dp) go to 3.
        final cols = c.maxWidth >= 560 ? 3 : 2;
        final rowGap = 12.0;
        final colGap = 16.0;
        final itemW = (c.maxWidth - colGap * (cols - 1)) / cols;
        return Wrap(
          spacing: colGap,
          runSpacing: rowGap,
          children: [
            for (final kv in entries)
              SizedBox(width: itemW, child: kv),
          ],
        );
      },
    );
  }

  static String _money(dynamic v) {
    final n = (v is num) ? v.toDouble() : 0.0;
    return n.toStringAsFixed(2);
  }

  static String _enumLabel(dynamic e) {
    if (e is Map) return (e['label_ar'] ?? e['value'] ?? '—').toString();
    return e?.toString() ?? '—';
  }
}

class _KV extends StatelessWidget {
  final String k;
  final String v;
  final bool mono;
  final bool ltr;
  final bool highlight;
  const _KV(this.k, this.v,
      {this.mono = false, this.ltr = false, this.highlight = false});
  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(k,
            style: const TextStyle(
              color: BrandColors.slate400,
              fontSize: 11,
            )),
        const SizedBox(height: 2),
        Directionality(
          textDirection: ltr ? TextDirection.ltr : TextDirection.rtl,
          child: Text(
            v,
            style: TextStyle(
              color:
                  highlight ? BrandColors.emerald700 : BrandColors.navy900,
              fontSize: 14,
              fontWeight: FontWeight.w700,
              fontFamily: mono ? 'monospace' : null,
            ),
          ),
        ),
      ],
    );
  }
}

// ═════ رصيد الإجازات ═══════════════════════════════════════════════
class _LeaveBody extends StatelessWidget {
  final List<Map<String, dynamic>> types;
  final List<Map<String, dynamic>> balances;
  final List<Map<String, dynamic>> requests;
  const _LeaveBody({
    required this.types,
    required this.balances,
    required this.requests,
  });
  @override
  Widget build(BuildContext context) {
    if (types.isEmpty) {
      return const Text(
        'لا توجد أنواع إجازة معرّفة.',
        style: TextStyle(color: BrandColors.slate400, fontSize: 13),
      );
    }
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        // Grid of leave-type mini-cards — mirrors `grid-cols-2 sm:grid-cols-3`.
        LayoutBuilder(builder: (context, c) {
          final cols = c.maxWidth >= 480 ? 3 : 2;
          final gap = 10.0;
          final w = (c.maxWidth - gap * (cols - 1)) / cols;
          return Wrap(
            spacing: gap,
            runSpacing: gap,
            children: [
              for (final t in types)
                SizedBox(
                  width: w,
                  child: _LeaveMiniCard(
                    name: t['name']?.toString() ?? '—',
                    remaining: _remainingFor(t['id']),
                  ),
                ),
            ],
          );
        }),
        if (requests.isNotEmpty) ...[
          const SizedBox(height: 16),
          const Divider(color: BrandColors.slate100),
          const SizedBox(height: 10),
          const Text(
            'طلباتي السابقة',
            style: TextStyle(
              color: BrandColors.navy900,
              fontWeight: FontWeight.w800,
              fontSize: 13,
            ),
          ),
          const SizedBox(height: 8),
          _RequestsMiniTable(requests: requests),
        ],
      ],
    );
  }

  double _remainingFor(dynamic typeId) {
    for (final b in balances) {
      if (b['leave_type_id'] == typeId) {
        return (b['remaining'] as num?)?.toDouble() ?? 0;
      }
    }
    return 0;
  }
}

class _LeaveMiniCard extends StatelessWidget {
  final String name;
  final double remaining;
  const _LeaveMiniCard({required this.name, required this.remaining});
  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: BrandColors.slate50,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: BrandColors.slate200),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            name,
            style: const TextStyle(
              color: BrandColors.slate500,
              fontSize: 11,
            ),
            overflow: TextOverflow.ellipsis,
          ),
          const SizedBox(height: 6),
          Text.rich(
            TextSpan(children: [
              TextSpan(
                text: remaining.toStringAsFixed(1),
                style: const TextStyle(
                  color: BrandColors.navy900,
                  fontSize: 18,
                  fontWeight: FontWeight.w800,
                  fontFamily: 'monospace',
                ),
              ),
              const TextSpan(
                text: ' يوم',
                style: TextStyle(
                  color: BrandColors.slate500,
                  fontSize: 11,
                ),
              ),
            ]),
          ),
        ],
      ),
    );
  }
}

class _RequestsMiniTable extends StatelessWidget {
  final List<Map<String, dynamic>> requests;
  const _RequestsMiniTable({required this.requests});
  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        for (final r in requests.take(6))
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 6),
            child: Row(
              children: [
                Expanded(
                  flex: 3,
                  child: Text(
                    (r['leave_type_name'] ?? '—').toString(),
                    style: const TextStyle(
                        fontSize: 12,
                        color: BrandColors.navy900,
                        fontWeight: FontWeight.w600),
                  ),
                ),
                Expanded(
                  flex: 3,
                  child: Directionality(
                    textDirection: TextDirection.ltr,
                    child: Text(
                      '${(r['start_date'] as String? ?? '').substring(0, 10)} → ${(r['end_date'] as String? ?? '').substring(0, 10)}',
                      style: const TextStyle(
                        fontSize: 11,
                        color: BrandColors.slate500,
                        fontFamily: 'monospace',
                      ),
                    ),
                  ),
                ),
                _StatusBadge(status: r['status']),
              ],
            ),
          ),
      ],
    );
  }
}

class _StatusBadge extends StatelessWidget {
  final dynamic status;
  const _StatusBadge({required this.status});
  @override
  Widget build(BuildContext context) {
    final val = status is Map ? status['value'] : status?.toString();
    // Match badge classes in base.html:120-127.
    late Color bg, fg;
    late String label;
    switch (val) {
      case 'PENDING':
        bg = BrandColors.blue100;
        fg = BrandColors.blue700;
        label = 'قيد المراجعة';
        break;
      case 'APPROVED':
        bg = BrandColors.emerald100;
        fg = BrandColors.emerald700;
        label = 'معتمدة';
        break;
      case 'REJECTED':
        bg = BrandColors.slate100;
        fg = BrandColors.slate500;
        label = 'مرفوضة';
        break;
      default:
        bg = BrandColors.slate100;
        fg = BrandColors.slate700;
        label = val?.toString() ?? '—';
    }
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
      decoration: BoxDecoration(
        color: bg,
        borderRadius: BorderRadius.circular(999),
      ),
      child: Text(
        label,
        style: TextStyle(
          color: fg,
          fontWeight: FontWeight.w700,
          fontSize: 10,
        ),
      ),
    );
  }
}

// ═════ الحضور والانصراف (inline summary card, matches web) ═════════
class _AttendanceInlineCard extends StatelessWidget {
  final Map<String, dynamic>? todayCi;
  const _AttendanceInlineCard({required this.todayCi});
  @override
  Widget build(BuildContext context) {
    final inT = _time(todayCi?['check_in_time']);
    final outT = _time(todayCi?['check_out_time']);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Row(
          children: [
            Expanded(
                child: _InOutTile(label: 'حضور اليوم', value: inT)),
            const SizedBox(width: 10),
            Expanded(
                child: _InOutTile(label: 'انصراف اليوم', value: outT)),
          ],
        ),
        const SizedBox(height: 12),
        GradientButton(
          label: 'الذهاب لصفحة الحضور',
          icon: Icons.fingerprint,
          onPressed: () => context.go('/attendance'),
        ),
      ],
    );
  }

  static String _time(dynamic iso) {
    if (iso is! String || iso.length < 16) return '—';
    return iso.substring(11, 16);
  }
}

class _InOutTile extends StatelessWidget {
  final String label;
  final String value;
  const _InOutTile({required this.label, required this.value});
  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: BrandColors.slate50,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: BrandColors.slate200),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(label,
              style: const TextStyle(
                color: BrandColors.slate500,
                fontSize: 11,
              )),
          const SizedBox(height: 4),
          Directionality(
            textDirection: TextDirection.ltr,
            child: Text(
              value,
              style: const TextStyle(
                color: BrandColors.navy900,
                fontFamily: 'monospace',
                fontSize: 18,
                fontWeight: FontWeight.w800,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

// ═════ السلف — 4-metric grid or empty state ════════════════════════
class _AdvancesBody extends StatelessWidget {
  final Map<String, dynamic>? advance;
  final List<Map<String, dynamic>> requests;
  const _AdvancesBody({required this.advance, required this.requests});
  @override
  Widget build(BuildContext context) {
    if (advance == null) {
      return Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Container(
            padding: const EdgeInsets.all(14),
            decoration: BoxDecoration(
              color: BrandColors.amber50.withValues(alpha: 0.5),
              borderRadius: BorderRadius.circular(10),
              border: Border.all(color: const Color(0xFFFDE68A)),
            ),
            child: const Row(
              children: [
                Text('💡', style: TextStyle(fontSize: 18)),
                SizedBox(width: 10),
                Expanded(
                  child: Text(
                    'لا توجد سلفة نشطة. تقديم طلب سلفة متاح قريباً من التطبيق.',
                    style: TextStyle(
                      color: BrandColors.amber700,
                      fontSize: 12,
                      height: 1.6,
                    ),
                  ),
                ),
              ],
            ),
          ),
          if (requests.isNotEmpty) ...[
            const SizedBox(height: 14),
            _AdvanceRequestsTable(requests: requests),
          ],
        ],
      );
    }
    final amount = (advance!['amount'] as num?)?.toDouble() ?? 0;
    final remaining = (advance!['remaining'] as num?)?.toDouble() ?? 0;
    final paid = (advance!['paid_amount'] as num?)?.toDouble() ?? 0;
    final next = (advance!['next_installment'] as num?)?.toDouble() ?? 0;
    final months = advance!['months'] as int?;
    final disb = advance!['disbursed_on'] as String?;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        LayoutBuilder(builder: (context, c) {
          final cols = c.maxWidth >= 480 ? 4 : 2;
          final gap = 10.0;
          final w = (c.maxWidth - gap * (cols - 1)) / cols;
          return Wrap(
            spacing: gap,
            runSpacing: gap,
            children: [
              SizedBox(
                  width: w,
                  child: _MetricBox(
                    label: 'الرصيد المتبقي',
                    value: remaining.toStringAsFixed(2),
                    tone: _MetricTone.warn,
                  )),
              SizedBox(
                  width: w,
                  child: _MetricBox(
                    label: 'القسط الشهر القادم',
                    value: next.toStringAsFixed(2),
                  )),
              SizedBox(
                  width: w,
                  child: _MetricBox(
                    label: 'إجمالي السلفة',
                    value: amount.toStringAsFixed(2),
                  )),
              SizedBox(
                  width: w,
                  child: _MetricBox(
                    label: 'المسدد حتى الآن',
                    value: paid.toStringAsFixed(2),
                  )),
            ],
          );
        }),
        const SizedBox(height: 10),
        Text(
          'صُرفت يوم ${disb?.substring(0, 10) ?? '—'} — مقسّطة على '
          '${months ?? '?'} ${months == 1 ? 'شهر' : 'شهور'}. لا يمكن '
          'طلب سلفة جديدة قبل سداد الحالية.',
          style: const TextStyle(
            color: BrandColors.slate500,
            fontSize: 11,
            height: 1.65,
          ),
        ),
      ],
    );
  }
}

enum _MetricTone { neutral, warn }

class _MetricBox extends StatelessWidget {
  final String label;
  final String value;
  final _MetricTone tone;
  const _MetricBox({
    required this.label,
    required this.value,
    this.tone = _MetricTone.neutral,
  });
  @override
  Widget build(BuildContext context) {
    final isWarn = tone == _MetricTone.warn;
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: isWarn ? BrandColors.amber50 : BrandColors.slate50,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: isWarn ? const Color(0xFFFDE68A) : BrandColors.slate200),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            label,
            style: TextStyle(
              color: isWarn ? BrandColors.amber700 : BrandColors.slate500,
              fontSize: 11,
            ),
            overflow: TextOverflow.ellipsis,
          ),
          const SizedBox(height: 5),
          Text(
            value,
            style: const TextStyle(
              color: BrandColors.navy900,
              fontSize: 17,
              fontWeight: FontWeight.w800,
              fontFamily: 'monospace',
            ),
          ),
        ],
      ),
    );
  }
}

class _AdvanceRequestsTable extends StatelessWidget {
  final List<Map<String, dynamic>> requests;
  const _AdvanceRequestsTable({required this.requests});
  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        const Text(
          'طلباتي السابقة',
          style: TextStyle(
            color: BrandColors.navy900,
            fontWeight: FontWeight.w800,
            fontSize: 13,
          ),
        ),
        const SizedBox(height: 8),
        for (final r in requests.take(6))
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 6),
            child: Row(
              children: [
                Text(
                  ((r['amount'] as num?)?.toDouble() ?? 0)
                      .toStringAsFixed(2),
                  style: const TextStyle(
                    color: BrandColors.navy900,
                    fontWeight: FontWeight.w700,
                    fontFamily: 'monospace',
                    fontSize: 13,
                  ),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: Text(
                    r['reason']?.toString() ?? '—',
                    style: const TextStyle(
                      color: BrandColors.slate500,
                      fontSize: 12,
                    ),
                    overflow: TextOverflow.ellipsis,
                  ),
                ),
                _StatusBadge(status: r['status']),
              ],
            ),
          ),
      ],
    );
  }
}

// ═════ Payslips table ══════════════════════════════════════════════
class _PayslipsTable extends StatelessWidget {
  final List<Map<String, dynamic>> slips;
  const _PayslipsTable({required this.slips});
  @override
  Widget build(BuildContext context) {
    if (slips.isEmpty) {
      return const Text(
        'لا توجد قسائم بعد.',
        style: TextStyle(color: BrandColors.slate400, fontSize: 13),
      );
    }
    return SingleChildScrollView(
      scrollDirection: Axis.horizontal,
      child: DataTable(
        columnSpacing: 22,
        headingRowHeight: 36,
        dataRowMinHeight: 42,
        dataRowMaxHeight: 46,
        headingTextStyle: const TextStyle(
          color: BrandColors.slate700,
          fontWeight: FontWeight.w700,
          fontSize: 12,
        ),
        dataTextStyle: const TextStyle(
          color: BrandColors.navy900,
          fontSize: 12,
          fontFamily: 'monospace',
        ),
        columns: const [
          DataColumn(label: Text('الفترة')),
          DataColumn(label: Text('الأساسي'), numeric: true),
          DataColumn(label: Text('البدلات'), numeric: true),
          DataColumn(label: Text('الإضافات'), numeric: true),
          DataColumn(label: Text('الخصومات'), numeric: true),
          DataColumn(label: Text('الصافي'), numeric: true),
        ],
        rows: [
          for (final s in slips.take(12))
            DataRow(cells: [
              DataCell(Text(
                  '${s['period_year']}-${(s['period_month'] as num?)?.toInt().toString().padLeft(2, '0') ?? '--'}')),
              DataCell(Text(((s['basic'] as num?)?.toDouble() ?? 0)
                  .toStringAsFixed(2))),
              DataCell(Text(((s['allowances'] as num?)?.toDouble() ?? 0)
                  .toStringAsFixed(2))),
              DataCell(Text(
                '+${_add(s).toStringAsFixed(2)}',
                style: const TextStyle(color: BrandColors.emerald700),
              )),
              DataCell(Text(
                '-${_ded(s).toStringAsFixed(2)}',
                style: const TextStyle(color: Color(0xFFBE123C)),
              )),
              DataCell(Text(
                ((s['net'] as num?)?.toDouble() ?? 0).toStringAsFixed(2),
                style: const TextStyle(fontWeight: FontWeight.w800),
              )),
            ]),
        ],
      ),
    );
  }

  static double _add(Map<String, dynamic> s) =>
      ((s['bonus'] as num?)?.toDouble() ?? 0) +
      ((s['overtime'] as num?)?.toDouble() ?? 0);
  static double _ded(Map<String, dynamic> s) =>
      ((s['deductions'] as num?)?.toDouble() ?? 0) +
      ((s['absence_deduction'] as num?)?.toDouble() ?? 0) +
      ((s['late_deduction'] as num?)?.toDouble() ?? 0) +
      ((s['advance_deduction'] as num?)?.toDouble() ?? 0);
}

// ═════ Password form ═══════════════════════════════════════════════
class _PasswordForm extends ConsumerStatefulWidget {
  const _PasswordForm();
  @override
  ConsumerState<_PasswordForm> createState() => _PasswordFormState();
}

class _PasswordFormState extends ConsumerState<_PasswordForm> {
  final _old = TextEditingController();
  final _new = TextEditingController();
  final _confirm = TextEditingController();
  bool _submitting = false;

  @override
  void dispose() {
    _old.dispose();
    _new.dispose();
    _confirm.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    if (_new.text != _confirm.text) {
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
        content: Text('كلمة السر الجديدة وتأكيدها غير متطابقين.'),
      ));
      return;
    }
    setState(() => _submitting = true);
    try {
      await ref.read(myAccountRepoProvider).changePassword(
            oldPassword: _old.text,
            newPassword: _new.text,
          );
      if (!mounted) return;
      _old.clear();
      _new.clear();
      _confirm.clear();
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
        content: Text('تم تغيير كلمة السر بنجاح.'),
      ));
    } on ApiException catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(e.message),
      ));
    } finally {
      if (mounted) setState(() => _submitting = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        TextField(
          controller: _old,
          obscureText: true,
          decoration: const InputDecoration(labelText: 'كلمة السر القديمة'),
        ),
        const SizedBox(height: 10),
        TextField(
          controller: _new,
          obscureText: true,
          decoration: const InputDecoration(labelText: 'كلمة السر الجديدة'),
        ),
        const SizedBox(height: 10),
        TextField(
          controller: _confirm,
          obscureText: true,
          decoration:
              const InputDecoration(labelText: 'تأكيد كلمة السر الجديدة'),
        ),
        const SizedBox(height: 14),
        Align(
          alignment: Alignment.centerLeft,
          child: SizedBox(
            width: 220,
            child: GradientButton(
              label: 'تغيير كلمة السر',
              icon: Icons.save,
              loading: _submitting,
              onPressed: _submitting ? null : _submit,
            ),
          ),
        ),
      ],
    );
  }
}

class _ErrorView extends StatelessWidget {
  final String message;
  final VoidCallback onRetry;
  const _ErrorView({required this.message, required this.onRetry});
  @override
  Widget build(BuildContext context) {
    return Center(
      child: Container(
        margin: const EdgeInsets.all(24),
        padding: const EdgeInsets.all(24),
        decoration: BoxDecoration(
          color: Colors.white,
          borderRadius: BorderRadius.circular(16),
          border: Border.all(color: BrandColors.slate200),
        ),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Icon(Icons.error_outline,
                color: BrandColors.red500, size: 44),
            const SizedBox(height: 12),
            Text(
              message,
              textAlign: TextAlign.center,
              style: const TextStyle(
                color: BrandColors.slate700,
                fontSize: 14,
              ),
            ),
            const SizedBox(height: 16),
            ElevatedButton(
              onPressed: onRetry,
              child: const Text('إعادة المحاولة'),
            ),
          ],
        ),
      ),
    );
  }
}
