// MARSOUD-MOBILE-FLUTTER — auth session (token + user + companies).
//
// Loaded from secure storage at boot. Written on login, cleared on
// logout. Riverpod broadcasts changes so the router redirect guard and
// the API client pick them up automatically.
import 'dart:convert';

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'api_client.dart';

class MarsoudUser {
  final int id;
  final String name;
  final String email;
  const MarsoudUser({required this.id, required this.name, required this.email});
  factory MarsoudUser.fromJson(Map<String, dynamic> j) => MarsoudUser(
        id: j['id'] as int,
        name: (j['name'] ?? '') as String,
        email: (j['email'] ?? '') as String,
      );
  Map<String, dynamic> toJson() =>
      {'id': id, 'name': name, 'email': email};
}

class MarsoudCompany {
  final int id;
  final String name;
  /// One of: owner, admin, hr_manager, sales_manager, sales_rep,
  /// project_manager, accountant, team_member, viewer, employee, client,
  /// ceo. Same source of truth as the web sidebar.
  final String role;
  const MarsoudCompany(
      {required this.id, required this.name, required this.role});
  factory MarsoudCompany.fromJson(Map<String, dynamic> j) => MarsoudCompany(
        id: j['id'] as int,
        name: (j['name'] ?? '') as String,
        role: (j['role'] ?? 'employee') as String,
      );
  Map<String, dynamic> toJson() =>
      {'id': id, 'name': name, 'role': role};
}

class AuthSession {
  final String token;
  final MarsoudUser user;
  final List<MarsoudCompany> companies;
  final int? activeCompanyId;

  const AuthSession({
    required this.token,
    required this.user,
    required this.companies,
    required this.activeCompanyId,
  });

  MarsoudCompany? get activeCompany {
    if (activeCompanyId == null) return null;
    for (final c in companies) {
      if (c.id == activeCompanyId) return c;
    }
    return companies.isNotEmpty ? companies.first : null;
  }

  String get activeRole => activeCompany?.role ?? 'employee';

  AuthSession copyWith({int? activeCompanyId}) => AuthSession(
        token: token,
        user: user,
        companies: companies,
        activeCompanyId: activeCompanyId ?? this.activeCompanyId,
      );
}

// ─── Riverpod plumbing ────────────────────────────────────────────
final _storage =
    const FlutterSecureStorage(aOptions: AndroidOptions(encryptedSharedPreferences: true));

const _kTokenKey = 'marsoud.token';
const _kUserKey = 'marsoud.user';
const _kCompaniesKey = 'marsoud.companies';
const _kActiveCidKey = 'marsoud.active_cid';

/// Broadcasts the current session — null when logged out.
class AuthNotifier extends StateNotifier<AsyncValue<AuthSession?>> {
  AuthNotifier() : super(const AsyncValue.loading()) {
    _restore();
  }

  Future<void> _restore() async {
    try {
      final tok = await _storage.read(key: _kTokenKey);
      if (tok == null || tok.isEmpty) {
        state = const AsyncValue.data(null);
        return;
      }
      final userJson = await _storage.read(key: _kUserKey);
      final companiesJson = await _storage.read(key: _kCompaniesKey);
      final prefs = await SharedPreferences.getInstance();
      final cid = prefs.getInt(_kActiveCidKey);
      final user = MarsoudUser.fromJson(json.decode(userJson ?? '{}'));
      final companies = ((json.decode(companiesJson ?? '[]') as List)
              .cast<Map<String, dynamic>>())
          .map(MarsoudCompany.fromJson)
          .toList();
      state = AsyncValue.data(AuthSession(
        token: tok,
        user: user,
        companies: companies,
        activeCompanyId: cid ?? (companies.isNotEmpty ? companies.first.id : null),
      ));
    } catch (e, st) {
      state = AsyncValue.error(e, st);
    }
  }

  Future<void> setSession(AuthSession s) async {
    await _storage.write(key: _kTokenKey, value: s.token);
    await _storage.write(
        key: _kUserKey, value: json.encode(s.user.toJson()));
    await _storage.write(
        key: _kCompaniesKey,
        value: json.encode(s.companies.map((c) => c.toJson()).toList()));
    final prefs = await SharedPreferences.getInstance();
    if (s.activeCompanyId != null) {
      await prefs.setInt(_kActiveCidKey, s.activeCompanyId!);
    }
    state = AsyncValue.data(s);
  }

  Future<void> switchCompany(int cid) async {
    final cur = state.value;
    if (cur == null) return;
    final ns = cur.copyWith(activeCompanyId: cid);
    final prefs = await SharedPreferences.getInstance();
    await prefs.setInt(_kActiveCidKey, cid);
    state = AsyncValue.data(ns);
  }

  Future<void> clear() async {
    await _storage.delete(key: _kTokenKey);
    await _storage.delete(key: _kUserKey);
    await _storage.delete(key: _kCompaniesKey);
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove(_kActiveCidKey);
    state = const AsyncValue.data(null);
  }
}

final authProvider =
    StateNotifierProvider<AuthNotifier, AsyncValue<AuthSession?>>(
        (ref) => AuthNotifier());

/// The one ApiClient the app uses. Reads token + company_id lazily from
/// the auth provider, so login/logout/switch flow doesn't need a rebuild.
final apiClientProvider = Provider<ApiClient>((ref) {
  return ApiClient(
    readToken: () => ref.read(authProvider).value?.token,
    readCompanyId: () => ref.read(authProvider).value?.activeCompanyId,
    onUnauthorized: () {
      // 401 → nuke session and let the router bounce to /login.
      ref.read(authProvider.notifier).clear();
    },
  );
});
