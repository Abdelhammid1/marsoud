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
    return _sessionFromLoginBody(body);
  }

  // MARSOUD-MOBILE-REACCEPT-TERMS-01 (2026-09-12) — companion to
  // login(). When the login call returns 403 terms_acceptance_
  // required, the ReacceptTermsScreen collects the checkbox tick +
  // re-sends the same credentials with agreed=true. The backend
  // updates user.terms_version + records consent + mints the
  // bearer, so the caller reuses the same session type it would
  // have gotten from login().
  Future<AuthSession> acceptTermsAndLogin({
    required String email,
    required String password,
    String? deviceName,
  }) async {
    final body = await _api.post('/api/v1/auth/accept-terms', body: {
      'email': email.trim(),
      'password': password,
      'agreed': true,
      if (deviceName != null && deviceName.isNotEmpty)
        'device_name': deviceName,
    });
    return _sessionFromLoginBody(body);
  }

  // MARSOUD-MOBILE-REACCEPT-TERMS-01 — fetch the current published
  // terms + privacy HTML so the accept-terms screen can display them.
  // Non-fatal: an empty response means the super-admin hasn't
  // published anything yet, in which case the screen shows a plain
  // fallback notice.
  Future<Map<String, String>> fetchLegalContent() async {
    final body = await _api.get('/api/v1/auth/legal');
    return {
      'terms_version': (body['terms_version'] ?? '').toString(),
      'terms_html': (body['terms_html'] ?? '').toString(),
      'privacy_html': (body['privacy_html'] ?? '').toString(),
    };
  }

  AuthSession _sessionFromLoginBody(dynamic body) {
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

  // MARSOUD-MOBILE-SHIP-READY-01 (M3) — the old `changePassword`
  // method here posted to /api/v1/auth/change-password and had
  // zero call sites; the actual change-password flow lives in
  // MyAccountRepository.changePassword (posts to /api/v1/my/
  // account/password). Kept the method deleted to force any new
  // caller to go through the my_account flow, which already has
  // the UI + validation.
}

final authRepositoryProvider = Provider<AuthRepository>((ref) {
  return AuthRepository(ref.watch(apiClientProvider));
});
