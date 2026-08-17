// MARSOUD-MOBILE-FLUTTER — the login/logout HTTP layer.
//
// The AuthNotifier reads state. THIS file talks to the backend.
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'api_client.dart';
import 'auth_state.dart';

class AuthRepository {
  final ApiClient _api;
  AuthRepository(this._api);

  Future<AuthSession> login({
    required String email,
    required String password,
    String? deviceName,
  }) async {
    final body = await _api.post('/api/v1/auth/login', body: {
      'email': email.trim(),
      'password': password,
      if (deviceName != null && deviceName.isNotEmpty)
        'device_name': deviceName,
    });
    final companies = ((body['companies'] as List)
            .cast<Map<String, dynamic>>())
        .map(MarsoudCompany.fromJson)
        .toList();
    return AuthSession(
      token: body['token'] as String,
      user: MarsoudUser.fromJson(
          (body['user'] as Map).cast<String, dynamic>()),
      companies: companies,
      activeCompanyId: body['default_company_id'] as int?,
    );
  }

  Future<void> logout() async {
    // Best-effort — the token is revoked server-side; even if the call
    // fails (no network), the client-side clear proceeds anyway.
    try {
      await _api.post('/api/v1/auth/logout');
    } on ApiException {
      // ignore — clearing local state below is what actually logs out.
    }
  }

  Future<void> changePassword({
    required String oldPassword,
    required String newPassword,
  }) async {
    await _api.post('/api/v1/auth/change-password', body: {
      'old': oldPassword,
      'new': newPassword,
    });
  }
}

final authRepositoryProvider = Provider<AuthRepository>((ref) {
  return AuthRepository(ref.watch(apiClientProvider));
});
