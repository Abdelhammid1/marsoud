// MARSOUD-MOBILE-FLUTTER — attendance tab (check-in/out + monthly log).
//
// Two colored gradient CTAs on top (green in / amber out) that match the
// web's `.btn-primary` gradient feel, plus a monthly summary card.
import 'package:flutter/foundation.dart';   // kDebugMode + debugPrint
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

  // Track WHY GPS acquisition failed so we can show the user a
  // specific message instead of the generic "gps مطلوب" that
  // conflates "you denied permission" with "we timed out".
  //
  // MARSOUD-MOBILE-IOS-GPS-01 (2026-09-16) — iOS cold-start GPS
  // acquisition inside a building routinely exceeds the previous
  // 8+5 s budget, and the old flow surfaced the same "gps مطلوب"
  // banner for both "permission denied" (user action needed) and
  // "GPS timed out indoors" (retry, maybe move closer to a window).
  // Now we distinguish + retry more aggressively:
  //
  //   Step 1  — Geolocator.getLastKnownPosition (INSTANT, no wait).
  //             iOS caches the last GPS fix; if fresh enough, use
  //             it. Cheap win when the user just used Maps.
  //   Step 2  — high accuracy, 12 s timeout
  //   Step 3  — medium accuracy, 10 s timeout
  //   Step 4  — low accuracy, 10 s timeout
  //   Worst-case ≈ 32 s (vs 13 s before) but success rate indoors
  //   is dramatically better on iOS, and every step catches its
  //   own error so a hardware failure doesn't kill the whole
  //   chain.
  Future<({double? lat, double? lng, String? reason})> _tryLocation() async {
    try {
      final serviceEnabled = await Geolocator.isLocationServiceEnabled();
      if (!serviceEnabled) {
        return (lat: null, lng: null, reason: 'service_off');
      }
      var perm = await Geolocator.checkPermission();
      if (perm == LocationPermission.denied) {
        perm = await Geolocator.requestPermission();
      }
      if (perm == LocationPermission.denied ||
          perm == LocationPermission.deniedForever) {
        return (lat: null, lng: null, reason: 'permission_denied');
      }

      // Step 1 — last-known position. Instant; only used if the
      // fix is recent (within 2 minutes) so we're not sending a
      // stale coordinate for a "check-in".
      try {
        final last = await Geolocator.getLastKnownPosition();
        if (last != null &&
            last.timestamp != null &&
            DateTime.now().difference(last.timestamp!).inSeconds < 120) {
          return (lat: last.latitude, lng: last.longitude, reason: null);
        }
      } catch (_) {
        // No last-known → fall through to a live fix.
      }

      // Steps 2-4 — 3-accuracy fallback chain.
      const attempts = [
        (LocationAccuracy.high, 12),
        (LocationAccuracy.medium, 10),
        (LocationAccuracy.low, 10),
      ];
      for (final (accuracy, seconds) in attempts) {
        try {
          final pos = await Geolocator.getCurrentPosition(
            locationSettings: LocationSettings(
              accuracy: accuracy,
              timeLimit: Duration(seconds: seconds),
            ),
          );
          return (lat: pos.latitude, lng: pos.longitude, reason: null);
        } catch (_) {
          // Try the next (looser) accuracy tier.
          continue;
        }
      }
      // All three attempts failed → most likely the user is deep
      // inside a building with no sky view. The server-side
      // "gps_required" gate will reject the request anyway, but
      // we want to tell them SPECIFICALLY that we timed out so
      // they know to try again (vs "permission denied" which
      // needs a settings trip).
      return (lat: null, lng: null, reason: 'timeout');
    } catch (_) {
      return (lat: null, lng: null, reason: 'unknown');
    }
  }

  Future<void> _do({required bool checkin}) async {
    if (_submitting) return;
    setState(() => _submitting = true);
    final messenger = ScaffoldMessenger.of(context);
    // MARSOUD-MOBILE-IOS-GPS-01 (2026-09-16) — cold-start GPS on
    // iOS can take 10-30 s in a building. Give the user a visible
    // "we're working on it" cue so they don't tap again thinking
    // the button did nothing. Cleared as soon as _tryLocation
    // returns.
    final progress = messenger.showSnackBar(const SnackBar(
      content: Text('📍 جاري تحديد موقعك...'),
      duration: Duration(seconds: 40),
    ));
    try {
      final loc = await _tryLocation();
      progress.close();
      if (loc.lat == null || loc.lng == null) {
        // MARSOUD-MOBILE-IOS-GPS-01 — surface the SPECIFIC reason
        // instead of the old generic banner that conflated
        // "you denied permission" with "iOS timed out indoors".
        final (String message, bool showSettings) = switch (loc.reason) {
          'service_off' => (
            'خدمة الموقع مطفية على جهازك — فعّلها من الإعدادات ثم حاول مرة أخرى.',
            true,
          ),
          'permission_denied' => (
            'التطبيق مش مصرحله يقرأ موقعك — اسمح بالإذن من إعدادات '
            'التطبيق ثم حاول مرة أخرى.',
            true,
          ),
          'timeout' => (
            'مقدرش يحدد موقعك دلوقتي (إشارة GPS ضعيفة). '
            'قرّب من نافذة أو اطلع مكان مفتوح وحاول تاني.',
            false,
          ),
          _ => (
            'مقدرش يحدد موقعك — حاول تاني بعد شوية.',
            false,
          ),
        };
        messenger.showSnackBar(SnackBar(
          duration: const Duration(seconds: 8),
          content: Text(message),
          action: showSettings
              ? SnackBarAction(
                  label: 'فتح الإعدادات',
                  onPressed: () async {
                    await Geolocator.openLocationSettings();
                  },
                )
              : null,
        ));
        return;
      }
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
    } catch (e) {
      // MARSOUD-MOBILE-SHIP-READY-01 (M9) — used to only catch
      // ApiException. A TimeoutException or a TypeError from bad
      // JSON crashed the widget's Future. Show a friendly Arabic
      // fallback instead.
      if (kDebugMode) debugPrint('[attendance] $e');
      messenger.showSnackBar(const SnackBar(
        content: Text('تعذّر تسجيل العملية — حاول مرة أخرى.'),
      ));
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
            // MARSOUD-MOBILE-TKT-04 (2026-08-18) — GPS is now
            // mandatory (was optional). Copy updated to reflect
            // that a missing/denied location blocks the check-in.
            text: 'الموقع مطلوب لتسجيل الحضور — فعّل GPS واسمح بالإذن.',
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
