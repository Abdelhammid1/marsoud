// MARSOUD-MOBILE-TKT-01 (2026-08-18) — HTTP layer for the three
// mobile-only endpoints: Leads / Meetings / Schedules. Mirrors
// the shape of `MyAccountRepository` — raw Map/List returns;
// screens interpret.
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'api_client.dart';
import 'auth_state.dart';

class MobileExtrasRepository {
  final ApiClient _api;
  MobileExtrasRepository(this._api);

  // ─── Leads ────────────────────────────────────────────────
  Future<Map<String, dynamic>> leads({String? status}) async {
    final url = (status != null && status.isNotEmpty)
        ? '/api/v1/my/leads?status=$status'
        : '/api/v1/my/leads';
    return (await _api.get(url)) as Map<String, dynamic>;
  }

  Future<Map<String, dynamic>> leadStages() async =>
      (await _api.get('/api/v1/my/leads/stages'))
          as Map<String, dynamic>;

  Future<Map<String, dynamic>> leadDetail(int leadId) async =>
      (await _api.get('/api/v1/my/leads/$leadId'))
          as Map<String, dynamic>;

  Future<Map<String, dynamic>> changeLeadStatus(int leadId, {
    required String newStatus,
    String? note,
    String? lostReason,
  }) async =>
      (await _api.post('/api/v1/my/leads/$leadId/status', body: {
        'new_status': newStatus,
        if (note != null) 'note': note,
        if (lostReason != null) 'lost_reason': lostReason,
      })) as Map<String, dynamic>;

  // MARSOUD-MOBILE-LEAD-CREATE-01 (2026-09-03) — sales reps can
  // add a new prospect straight from the phone. Server maps this
  // to the same web-side "leads.new" pipeline (per-company
  // number, creation LeadStatusEvent, auto primary Contact) so
  // the lead is indistinguishable from one added on desktop.
  Future<Map<String, dynamic>> createLead({
    required String clientName,
    required String phone,
    required String serviceNeeded,
    String? email,
    String? notes,
    String? requestDescription,
    String? salesActionRequired,
    int? assignedToId,
    double? expectedValue,
    String? leadType,
    String? source,
  }) async =>
      (await _api.post('/api/v1/my/leads', body: {
        'client_name': clientName,
        'phone': phone,
        'service_needed': serviceNeeded,
        if (email != null && email.isNotEmpty) 'email': email,
        if (notes != null && notes.isNotEmpty) 'notes': notes,
        if (requestDescription != null && requestDescription.isNotEmpty)
          'request_description': requestDescription,
        if (salesActionRequired != null && salesActionRequired.isNotEmpty)
          'sales_action_required': salesActionRequired,
        if (assignedToId != null) 'assigned_to_id': assignedToId,
        if (expectedValue != null) 'expected_value': expectedValue,
        if (leadType != null && leadType.isNotEmpty)
          'lead_type': leadType,
        if (source != null && source.isNotEmpty) 'source': source,
      })) as Map<String, dynamic>;

  Future<Map<String, dynamic>> addLeadActivity(int leadId, {
    required String type,
    String? subject,
    String? body,
    String? activityDate,
    String? followUpDate,
  }) async =>
      (await _api.post('/api/v1/my/leads/$leadId/activities', body: {
        'type': type,
        if (subject != null) 'subject': subject,
        if (body != null) 'body': body,
        if (activityDate != null) 'activity_date': activityDate,
        if (followUpDate != null) 'follow_up_date': followUpDate,
      })) as Map<String, dynamic>;

  // ─── Meetings ─────────────────────────────────────────────
  Future<Map<String, dynamic>> meetings({int days = 30}) async =>
      (await _api.get('/api/v1/my/meetings?days=$days'))
          as Map<String, dynamic>;

  Future<Map<String, dynamic>> createMeeting({
    required String title,
    required String startsAt,
    String? endsAt,
    String? location,
    String? notes,
    int? leadId,
  }) async =>
      (await _api.post('/api/v1/my/meetings', body: {
        'title': title,
        'starts_at': startsAt,
        if (endsAt != null) 'ends_at': endsAt,
        if (location != null) 'location': location,
        if (notes != null) 'notes': notes,
        if (leadId != null) 'lead_id': leadId,
      })) as Map<String, dynamic>;

  // ─── Schedules ────────────────────────────────────────────
  Future<Map<String, dynamic>> schedules() async =>
      (await _api.get('/api/v1/my/schedules'))
          as Map<String, dynamic>;

  Future<Map<String, dynamic>> scheduleDetail(int scheduleId) async =>
      (await _api.get('/api/v1/my/schedules/$scheduleId'))
          as Map<String, dynamic>;
}

final mobileExtrasRepoProvider =
    Provider<MobileExtrasRepository>((ref) {
  return MobileExtrasRepository(ref.watch(apiClientProvider));
});
