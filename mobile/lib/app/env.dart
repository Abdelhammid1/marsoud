import 'package:flutter/foundation.dart';  // debugPrint

// MARSOUD-MOBILE-FLUTTER — compile-time environment.
//
// Set via `--dart-define`:
//
//   flutter run \
//     --dart-define=MARSOUD_API=https://api.marsoud.example \
//     --dart-define=MARSOUD_API=http://10.0.2.2:5050   # Android emulator → host
//
// A single source of truth. Never hardcode the base URL in code —
// staging vs. prod vs. local emulator all differ, and swapping URLs
// through find-and-replace before a build is exactly how a debug build
// leaks into production.
class Env {
  static const String apiBaseUrl = String.fromEnvironment(
    'MARSOUD_API',
    defaultValue: '',
  );

  /// MARSOUD-MOBILE-SHIP-READY-01 (audit finding H9) — optional
  /// separate web host, used for links that must render on the web
  /// (forgot-password page, email verification, ToS acceptance).
  /// Falls back to `apiBaseUrl` when unset — that covers the common
  /// case where API and web share a host (dev + small prods).
  ///
  /// Split-host prod:
  ///   --dart-define=MARSOUD_API=https://api.marsoud.com
  ///   --dart-define=MARSOUD_WEB=https://app.marsoud.com
  static const String _webBaseUrl = String.fromEnvironment(
    'MARSOUD_WEB',
    defaultValue: '',
  );

  static String get webBaseUrl =>
      _webBaseUrl.isNotEmpty ? _webBaseUrl : apiBaseUrl;

  /// True when running against a local dev server. Used to relax certain
  /// UX warnings (e.g. HTTP → HTTPS) that would otherwise noise-up dev.
  ///
  /// MARSOUD-MOBILE-SHIP-READY-01 (M10) — widened the pattern beyond
  /// localhost so ad-hoc dev hosts (10.x.x.x LAN IPs, `*.local` mDNS
  /// names, `*.ngrok.io` tunnels) are correctly flagged as dev. A
  /// real staging host (`staging.marsoud.example`) still reads as
  /// prod — that's deliberate, staging must behave like prod.
  static bool get isDev {
    final u = apiBaseUrl.toLowerCase();
    if (u.contains('localhost')) return true;
    if (u.contains('127.0.0.1')) return true;
    if (u.contains('10.0.2.2')) return true;
    if (u.contains('.local')) return true;
    if (u.contains('.ngrok.io')) return true;
    if (u.contains('.ngrok-free.app')) return true;
    // LAN IPs: 10.x, 172.16-31.x, 192.168.x
    final m = RegExp(r'https?://(\d+)\.(\d+)\.').firstMatch(u);
    if (m != null) {
      final a = int.tryParse(m.group(1) ?? '') ?? 0;
      final b = int.tryParse(m.group(2) ?? '') ?? 0;
      if (a == 10) return true;
      if (a == 192 && b == 168) return true;
      if (a == 172 && b >= 16 && b <= 31) return true;
    }
    return false;
  }

  /// Called at boot. Fails loudly if the build lacks MARSOUD_API — a
  /// release APK with an empty base URL is a silent-401 bug factory,
  /// and this catches it at first-launch instead of first-login.
  static void assertConfigured() {
    if (apiBaseUrl.isEmpty) {
      // debugPrint suffices — the throw immediately after this
      // aborts the app anyway, so a release build never leaks
      // through a naked print.
      debugPrint(
        'FATAL: MARSOUD_API not defined. Rebuild with '
        '--dart-define=MARSOUD_API=https://your-backend',
      );
      throw StateError('MARSOUD_API not defined at build time.');
    }
  }
}
