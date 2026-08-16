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

  Future<void> changePassword({
    required String oldPassword,
    required String newPassword,
  }) async {
    await _api.post('/api/v1/my/account/password', body: {
      'old': oldPassword,
      'new': newPassword,
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

  // ─── Daily reports ─────────────────────────────────────────────
  Future<Map<String, dynamic>> dailyReports() async =>
      (await _api.get('/api/v1/my/daily-reports')) as Map<String, dynamic>;

  Future<Map<String, dynamic>> dailyReport(int id) async =>
      (await _api.get('/api/v1/my/daily-reports/$id'))
          as Map<String, dynamic>;

  Future<void> submitDailyReport(int id) async {
    await _api.post('/api/v1/my/daily-reports/$id/submit');
  }

  Future<void> saveDailyReportNotes(int id, String notes) async {
    await _api.post('/api/v1/my/daily-reports/$id/notes',
        body: {'employee_notes': notes});
  }

  // ─── My archive ────────────────────────────────────────────────
  Future<Map<String, dynamic>> archive() async =>
      (await _api.get('/api/v1/my/archive')) as Map<String, dynamic>;

  Future<void> restoreArchived(int taskId) async {
    await _api.post('/api/v1/my/archive/$taskId/restore');
  }

  // ─── Cash custody ──────────────────────────────────────────────
  Future<Map<String, dynamic>> custody() async =>
      (await _api.get('/api/v1/my/custody')) as Map<String, dynamic>;

  Future<void> requestCustody({
    required num amount,
    required String purpose,
    String? neededByDate,
  }) async {
    await _api.post('/api/v1/my/custody/request', body: {
      'amount': amount,
      'purpose': purpose,
      if (neededByDate != null) 'needed_by_date': neededByDate,
    });
  }

  // ─── Item custody ──────────────────────────────────────────────
  Future<Map<String, dynamic>> items() async =>
      (await _api.get('/api/v1/my/items')) as Map<String, dynamic>;

  Future<void> requestItem({
    required int itemId,
    required String purpose,
  }) async {
    await _api.post('/api/v1/my/items/request', body: {
      'item_id': itemId,
      'purpose': purpose,
    });
  }

  // ─── Activity log ──────────────────────────────────────────────
  Future<Map<String, dynamic>> activity() async =>
      (await _api.get('/api/v1/my/activity')) as Map<String, dynamic>;

  // ─── Files (my own uploads) ────────────────────────────────────
  Future<Map<String, dynamic>> files() async =>
      (await _api.get('/api/v1/misc/files')) as Map<String, dynamic>;

  // ─── Support tickets ───────────────────────────────────────────
  Future<Map<String, dynamic>> supportTickets() async =>
      (await _api.get('/api/v1/misc/support/tickets'))
          as Map<String, dynamic>;

  Future<Map<String, dynamic>> supportTicket(int id) async =>
      (await _api.get('/api/v1/misc/support/tickets/$id'))
          as Map<String, dynamic>;

  Future<Map<String, dynamic>> createSupportTicket({
    required String title,
    required String description,
    String priority = 'MEDIUM',
  }) async =>
      (await _api.post('/api/v1/misc/support/tickets', body: {
        'title': title,
        'description': description,
        'priority': priority,
      })) as Map<String, dynamic>;

  Future<Map<String, dynamic>> supportComment({
    required int ticketId,
    required String content,
  }) async =>
      (await _api.post(
        '/api/v1/misc/support/tickets/$ticketId/comments',
        body: {'content': content},
      )) as Map<String, dynamic>;

  // ─── Tasks + projects (existing /api/v1/* endpoints) ──────────
  Future<Map<String, dynamic>> myTasks({
    String? status,
    bool includeArchived = false,
    int limit = 100,
  }) async =>
      (await _api.get('/api/v1/me/tasks', query: {
        if (status != null) 'status': status,
        if (includeArchived) 'include_archived': 1,
        'limit': limit,
      })) as Map<String, dynamic>;

  Future<Map<String, dynamic>> taskDetail(int id) async =>
      (await _api.get('/api/v1/tasks/$id')) as Map<String, dynamic>;

  Future<Map<String, dynamic>> setTaskStatus(int id, String status) async =>
      (await _api.post('/api/v1/tasks/$id/status', body: {'status': status}))
          as Map<String, dynamic>;

  Future<Map<String, dynamic>> addTaskComment(int id, String content) async =>
      (await _api.post('/api/v1/tasks/$id/comments',
              body: {'content': content}))
          as Map<String, dynamic>;

  Future<Map<String, dynamic>> projects({String? q, int limit = 50}) async =>
      (await _api.get('/api/v1/projects', query: {
        if (q != null && q.isNotEmpty) 'q': q,
        'limit': limit,
      })) as Map<String, dynamic>;

  Future<Map<String, dynamic>> projectDetail(int id) async =>
      (await _api.get('/api/v1/projects/$id')) as Map<String, dynamic>;

  Future<Map<String, dynamic>> projectTasks(int id,
      {bool assignedToMe = true}) async =>
      (await _api.get('/api/v1/projects/$id/tasks',
              query: {'assigned_to_me': assignedToMe.toString()}))
          as Map<String, dynamic>;

  // ─── Notifications ─────────────────────────────────────────────
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
