// MARSOUD-MOBILE-FLUTTER — check-in/out screen with GPS.
//
// GPS is opportunistic (mirrors hr_self_service._coord at :860): denied
// permission means we send lat/lng as null. The server never blocks a
// check-in on location — GPS is evidence, not a gate.
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:geolocator/geolocator.dart';

import '../../data/api_client.dart';
import '../../data/my_account_repository.dart';

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
      error: (e, _) => Center(child: Text(e.toString())),
      data: (data) {
        final checkins = (data['checkins'] as List?)
                ?.cast<Map<String, dynamic>>() ??
            [];
        final todayHasCheckin = checkins.isNotEmpty &&
            checkins.first['check_in_time'] != null;
        final todayHasCheckout = checkins.isNotEmpty &&
            checkins.first['check_out_time'] != null;
        return RefreshIndicator(
          onRefresh: () async => ref.invalidate(_attendanceProvider),
          child: ListView(
            padding: const EdgeInsets.all(16),
            children: [
              _TodayCard(
                onCheckin: (!todayHasCheckin && !_submitting)
                    ? () => _do(checkin: true)
                    : null,
                onCheckout:
                    (todayHasCheckin && !todayHasCheckout && !_submitting)
                        ? () => _do(checkin: false)
                        : null,
                submitting: _submitting,
              ),
              const SizedBox(height: 16),
              _MonthlyList(checkins),
            ],
          ),
        );
      },
    );
  }
}

class _TodayCard extends StatelessWidget {
  final VoidCallback? onCheckin;
  final VoidCallback? onCheckout;
  final bool submitting;
  const _TodayCard({
    required this.onCheckin,
    required this.onCheckout,
    required this.submitting,
  });
  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text('اليوم',
                style: Theme.of(context).textTheme.titleLarge),
            const SizedBox(height: 16),
            ElevatedButton.icon(
              onPressed: onCheckin,
              icon: const Icon(Icons.login),
              label: Text(submitting ? 'جارٍ التسجيل…' : 'تسجيل الحضور'),
              style: ElevatedButton.styleFrom(
                minimumSize: const Size.fromHeight(56),
                backgroundColor: Colors.green.shade700,
              ),
            ),
            const SizedBox(height: 12),
            ElevatedButton.icon(
              onPressed: onCheckout,
              icon: const Icon(Icons.logout),
              label: Text(submitting ? 'جارٍ التسجيل…' : 'تسجيل الانصراف'),
              style: ElevatedButton.styleFrom(
                minimumSize: const Size.fromHeight(56),
                backgroundColor: Colors.orange.shade800,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _MonthlyList extends StatelessWidget {
  final List<Map<String, dynamic>> checkins;
  const _MonthlyList(this.checkins);
  @override
  Widget build(BuildContext context) {
    if (checkins.isEmpty) {
      return const Card(
        child: Padding(
          padding: EdgeInsets.all(16),
          child: Text('لا توجد تسجيلات هذا الشهر.'),
        ),
      );
    }
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text('هذا الشهر',
                style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 8),
            for (final c in checkins.take(20))
              ListTile(
                contentPadding: EdgeInsets.zero,
                title: Text((c['date'] as String?)?.substring(0, 10) ?? '—'),
                subtitle: Text(
                    'حضور: ${_time(c['check_in_time'])}   انصراف: ${_time(c['check_out_time'])}'),
                trailing: c['worked_hours'] != null
                    ? Text('${c['worked_hours']} س')
                    : null,
              ),
          ],
        ),
      ),
    );
  }

  String _time(dynamic iso) {
    if (iso is! String || iso.length < 16) return '—';
    return iso.substring(11, 16);
  }
}
