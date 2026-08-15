// MARSOUD-MOBILE-FLUTTER — the /api/v1/my/* HTTP layer.
//
// Every method here returns raw Map/List — the screens interpret them.
// We intentionally don't add heavy DTOs at this stage (freezed etc.) so
// the first-cut of the app is easy to iterate on; a follow-up ticket
// (MARSOUD-MOBILE-DTOS) can generate typed models once the payloads
// have settled.
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'api_client.dart';
import 'auth_state.dart';

class MyAccountRepository {
  final ApiClient _api;
  MyAccountRepository(this._api);

  Future<Map<String, dynamic>> account() async =>
      (await _api.get('/api/v1/my/account')) as Map<String, dynamic>;

  Future<Map<String, dynamic>> attendance() async =>
      (await _api.get('/api/v1/my/attendance')) as Map<String, dynamic>;

  Future<Map<String, dynamic>> checkin({double? lat, double? lng}) async =>
      (await _api.post('/api/v1/my/attendance/checkin', body: {
        if (lat != null) 'lat': lat,
        if (lng != null) 'lng': lng,
      })) as Map<String, dynamic>;

  Future<Map<String, dynamic>> checkout({double? lat, double? lng}) async =>
      (await _api.post('/api/v1/my/attendance/checkout', body: {
        if (lat != null) 'lat': lat,
        if (lng != null) 'lng': lng,
      })) as Map<String, dynamic>;

  Future<void> submitLeave({
    required int leaveTypeId,
    required String startDate,
    required String endDate,
    String? reason,
  }) async {
    await _api.post('/api/v1/my/leave', body: {
      'leave_type_id': leaveTypeId,
      'start_date': startDate,
      'end_date': endDate,
      if (reason != null) 'reason': reason,
    });
  }

  Future<void> submitPermission({
    required String requestDate,
    required num hoursCount,
    String? startTime,
    String? endTime,
    String? reason,
  }) async {
    await _api.post('/api/v1/my/permission', body: {
      'request_date': requestDate,
      'hours_count': hoursCount,
      if (startTime != null) 'start_time': startTime,
      if (endTime != null) 'end_time': endTime,
      if (reason != null) 'reason': reason,
    });
  }

  Future<void> submitAdvance({
    required num amount,
    String? reason,
  }) async {
    await _api.post('/api/v1/my/advance', body: {
      'amount': amount,
      if (reason != null) 'reason': reason,
    });
  }

  Future<Map<String, dynamic>> notifications({int limit = 50}) async =>
      (await _api.get('/api/v1/notifications', query: {'limit': limit}))
          as Map<String, dynamic>;

  Future<int> unreadCount() async {
    final r = await _api.get('/api/v1/notifications/unread-count')
        as Map<String, dynamic>;
    return (r['count'] as num).toInt();
  }

  Future<void> markRead(int notifId) async {
    await _api.post('/api/v1/notifications/$notifId/read');
  }
}

final myAccountRepoProvider = Provider<MyAccountRepository>((ref) {
  return MyAccountRepository(ref.watch(apiClientProvider));
});
