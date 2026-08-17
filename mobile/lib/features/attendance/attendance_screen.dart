// MARSOUD-MOBILE-FLUTTER — attendance tab (check-in/out + monthly log).
//
// Two colored gradient CTAs on top (green in / amber out) that match the
// web's `.btn-primary` gradient feel, plus a monthly summary card.
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:geolocator/geolocator.dart';

import '../../app/theme.dart';
import '../../data/api_client.dart';
import '../../data/my_account_repository.dart';
import '../../widgets/gradient_button.dart';

final _attendanceProvider =
    FutureProvider.autoDispose<Map<String, dynamic>>(
        (ref) => ref.watch(myAccountRepoProvider).attendance());

class AttendanceScreen extends ConsumerStatefulWidget {
  const AttendanceScreen({super.key});

  @override
  ConsumerState<AttendanceScreen> createState() => _AttendanceScreenState();
}

class _AttendanceScreenState extends ConsumerState<AttendanceScreen> {
  bool _submitting = false;

  Future<({double? lat, double? lng})> _tryLocation() async {
    try {
      final serviceEnabled = await Geolocator.isLocationServiceEnabled();
      if (!serviceEnabled) return (lat: null, lng: null);
      var perm = await Geolocator.checkPermission();
      if (perm == LocationPermission.denied) {
        perm = await Geolocator.requestPermission();
      }
      if (perm == LocationPermission.denied ||
          perm == LocationPermission.deniedForever) {
        return (lat: null, lng: null);
      }
      final pos = await Geolocator.getCurrentPosition(
        locationSettings: const LocationSettings(
          accuracy: LocationAccuracy.high,
          timeLimit: Duration(seconds: 8),
        ),
      );
      return (lat: pos.latitude, lng: pos.longitude);
    } catch (_) {
      return (lat: null, lng: null);
    }
  }

  Future<void> _do({required bool checkin}) async {
    if (_submitting) return;
    setState(() => _submitting = true);
    final messenger = ScaffoldMessenger.of(context);
    try {
      final loc = await _tryLocation();
      final repo = ref.read(myAccountRepoProvider);
      final r = checkin
          ? await repo.checkin(lat: loc.lat, lng: loc.lng)
          : await repo.checkout(lat: loc.lat, lng: loc.lng);
      final lateRecorded = r['late_recorded'] == true;
      messenger.showSnackBar(SnackBar(
        content: Text(checkin
            ? (lateRecorded
                ? 'تم تسجيل الحضور — وسُجّل تأخير اليوم تلقائيًا.'
                : 'تم تسجيل الحضور.')
            : 'تم تسجيل الانصراف.'),
      ));
      ref.invalidate(_attendanceProvider);
    } on ApiException catch (e) {
      messenger.showSnackBar(SnackBar(content: Text(e.message)));
    } finally {
      if (mounted) setState(() => _submitting = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final async = ref.watch(_attendanceProvider);
    return async.when(
      loading: () => const Center(child: CircularProgressIndicator()),
      error: (e, _) => Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Text(e is ApiException ? e.message : e.toString()),
        ),
      ),
      data: (data) {
        final checkins = (data['checkins'] as List?)
                ?.cast<Map<String, dynamic>>() ??
            const [];
        final todayHasCheckin = checkins.isNotEmpty &&
            checkins.first['check_in_time'] != null;
        final todayHasCheckout = checkins.isNotEmpty &&
            checkins.first['check_out_time'] != null;
        final remainingPool = data['remaining_late_pool_min'];
        final remainingPerms = data['remaining_permits_this_month'];
        return RefreshIndicator(
          color: BrandColors.emerald600,
          onRefresh: () async => ref.invalidate(_attendanceProvider),
          child: ListView(
            padding: const EdgeInsets.fromLTRB(16, 20, 16, 32),
            children: [
              _TodayCard(
                canCheckin: !todayHasCheckin && !_submitting,
                canCheckout:
                    todayHasCheckin && !todayHasCheckout && !_submitting,
                submitting: _submitting,
                onCheckin: () => _do(checkin: true),
                onCheckout: () => _do(checkin: false),
              ),
              if (remainingPool != null || remainingPerms != null) ...[
                const SizedBox(height: 16),
                _MarginsCard(
                  remainingPoolMin: (remainingPool as num?)?.toInt(),
                  remainingPerms: (remainingPerms as num?)?.toInt(),
                ),
              ],
              const SizedBox(height: 16),
              _MonthlyCard(checkins: checkins,
                  monthLabel: data['month']?.toString() ?? ''),
            ],
          ),
        );
      },
    );
  }
}

class _TodayCard extends StatelessWidget {
  final bool canCheckin;
  final bool canCheckout;
  final bool submitting;
  final VoidCallback onCheckin;
  final VoidCallback onCheckout;
  const _TodayCard({
    required this.canCheckin,
    required this.canCheckout,
    required this.submitting,
    required this.onCheckin,
    required this.onCheckout,
  });
  @override
  Widget build(BuildContext context) {
    final now = DateTime.now();
    final today = '${now.year}-${now.month.toString().padLeft(2, '0')}-'
        '${now.day.toString().padLeft(2, '0')}';
    return Container(
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: BrandColors.slate200),
      ),
      padding: const EdgeInsets.all(20),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              const Text('🕐', style: TextStyle(fontSize: 18)),
              const SizedBox(width: 8),
              const Text(
                'اليوم',
                style: TextStyle(
                  color: BrandColors.navy900,
                  fontWeight: FontWeight.w700,
                  fontSize: 15,
                ),
              ),
              const Spacer(),
              Text(
                today,
                style: const TextStyle(
                  color: BrandColors.slate500,
                  fontSize: 12,
                  fontFamily: 'monospace',
                ),
              ),
            ],
          ),
          const SizedBox(height: 16),
          GradientButton(
            label: 'تسجيل الحضور',
            icon: Icons.login,
            onPressed: canCheckin ? onCheckin : null,
            loading: submitting && canCheckin,
            colors: const [BrandColors.emerald600, BrandColors.emerald500],
          ),
          const SizedBox(height: 12),
          GradientButton(
            label: 'تسجيل الانصراف',
            icon: Icons.logout,
            onPressed: canCheckout ? onCheckout : null,
            loading: submitting && canCheckout,
            colors: const [Color(0xFFB45309), Color(0xFFD97706)],
          ),
          const SizedBox(height: 14),
          const _HintRow(
            icon: Icons.gps_fixed,
            text: 'إن كان GPS مفعّلاً — نسجّل موقعك تلقائياً كإثبات. رفض الإذن لا يمنع التسجيل.',
          ),
        ],
      ),
    );
  }
}

class _HintRow extends StatelessWidget {
  final IconData icon;
  final String text;
  const _HintRow({required this.icon, required this.text});
  @override
  Widget build(BuildContext context) {
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Icon(icon, size: 14, color: BrandColors.slate400),
        const SizedBox(width: 6),
        Expanded(
          child: Text(
            text,
            style: const TextStyle(
              color: BrandColors.slate500,
              fontSize: 11,
              height: 1.5,
            ),
          ),
        ),
      ],
    );
  }
}

class _MarginsCard extends StatelessWidget {
  final int? remainingPoolMin;
  final int? remainingPerms;
  const _MarginsCard({
    required this.remainingPoolMin,
    required this.remainingPerms,
  });
  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: BrandColors.slate200),
      ),
      padding: const EdgeInsets.all(20),
      child: Row(
        children: [
          if (remainingPoolMin != null)
            Expanded(
              child: _MetricTile(
                emoji: '⏳',
                value: '$remainingPoolMin د',
                label: 'الرصيد المسموح للتأخير هذا الشهر',
                color: BrandColors.emerald700,
              ),
            ),
          if (remainingPoolMin != null && remainingPerms != null)
            const SizedBox(width: 12),
          if (remainingPerms != null)
            Expanded(
              child: _MetricTile(
                emoji: '📝',
                value: '$remainingPerms',
                label: 'استئذانات متبقية',
                color: BrandColors.blue700,
              ),
            ),
        ],
      ),
    );
  }
}

class _MetricTile extends StatelessWidget {
  final String emoji;
  final String value;
  final String label;
  final Color color;
  const _MetricTile({
    required this.emoji,
    required this.value,
    required this.label,
    required this.color,
  });
  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.06),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.center,
        children: [
          Text(emoji, style: const TextStyle(fontSize: 18)),
          const SizedBox(height: 4),
          Text(
            value,
            style: TextStyle(
              color: color,
              fontWeight: FontWeight.w800,
              fontFamily: 'monospace',
              fontSize: 18,
            ),
          ),
          const SizedBox(height: 4),
          Text(
            label,
            textAlign: TextAlign.center,
            style: const TextStyle(
              color: BrandColors.slate500,
              fontSize: 11,
              height: 1.4,
            ),
          ),
        ],
      ),
    );
  }
}

class _MonthlyCard extends StatelessWidget {
  final List<Map<String, dynamic>> checkins;
  final String monthLabel;
  const _MonthlyCard({required this.checkins, required this.monthLabel});
  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: BrandColors.slate200),
      ),
      padding: const EdgeInsets.all(20),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              const Text('🗓', style: TextStyle(fontSize: 18)),
              const SizedBox(width: 8),
              const Text(
                'هذا الشهر',
                style: TextStyle(
                  color: BrandColors.navy900,
                  fontWeight: FontWeight.w700,
                  fontSize: 15,
                ),
              ),
              const Spacer(),
              Text(
                monthLabel,
                style: const TextStyle(
                  color: BrandColors.slate500,
                  fontSize: 12,
                  fontFamily: 'monospace',
                ),
              ),
            ],
          ),
          const SizedBox(height: 12),
          if (checkins.isEmpty)
            const Text(
              'لا توجد تسجيلات هذا الشهر بعد.',
              style: TextStyle(
                color: BrandColors.slate500,
                fontSize: 13,
              ),
            )
          else
            Column(
              children: [
                for (final c in checkins.take(20))
                  _CheckinRow(c: c),
              ],
            ),
        ],
      ),
    );
  }
}

class _CheckinRow extends StatelessWidget {
  final Map<String, dynamic> c;
  const _CheckinRow({required this.c});
  @override
  Widget build(BuildContext context) {
    final date = (c['date'] as String?)?.substring(0, 10) ?? '—';
    final inT = _time(c['check_in_time']);
    final outT = _time(c['check_out_time']);
    final worked = c['worked_hours'];
    return Container(
      padding: const EdgeInsets.symmetric(vertical: 12, horizontal: 12),
      margin: const EdgeInsets.only(bottom: 8),
      decoration: BoxDecoration(
        color: BrandColors.slate50,
        borderRadius: BorderRadius.circular(12),
      ),
      child: Row(
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  date,
                  style: const TextStyle(
                    color: BrandColors.navy900,
                    fontFamily: 'monospace',
                    fontWeight: FontWeight.w700,
                  ),
                ),
                const SizedBox(height: 4),
                Row(
                  children: [
                    _Chip(icon: Icons.login, label: inT,
                        color: BrandColors.emerald700),
                    const SizedBox(width: 8),
                    _Chip(icon: Icons.logout, label: outT,
                        color: outT == '—'
                            ? BrandColors.slate400
                            : BrandColors.blue700),
                  ],
                ),
              ],
            ),
          ),
          if (worked != null)
            Container(
              padding: const EdgeInsets.symmetric(
                  horizontal: 10, vertical: 6),
              decoration: BoxDecoration(
                color: BrandColors.emerald50,
                borderRadius: BorderRadius.circular(999),
              ),
              child: Text(
                '$worked س',
                style: const TextStyle(
                  color: BrandColors.emerald700,
                  fontWeight: FontWeight.w800,
                  fontFamily: 'monospace',
                  fontSize: 12,
                ),
              ),
            ),
        ],
      ),
    );
  }

  static String _time(dynamic iso) {
    if (iso is! String || iso.length < 16) return '—';
    return iso.substring(11, 16);
  }
}

class _Chip extends StatelessWidget {
  final IconData icon;
  final String label;
  final Color color;
  const _Chip({required this.icon, required this.label, required this.color});
  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Icon(icon, size: 12, color: color),
        const SizedBox(width: 4),
        Text(
          label,
          style: TextStyle(
            color: color,
            fontFamily: 'monospace',
            fontSize: 12,
            fontWeight: FontWeight.w600,
          ),
        ),
      ],
    );
  }
}
