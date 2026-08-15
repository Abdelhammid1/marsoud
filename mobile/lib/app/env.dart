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

  /// True when running against a local dev server. Used to relax certain
  /// UX warnings (e.g. HTTP → HTTPS) that would otherwise noise-up dev.
  static bool get isDev =>
      apiBaseUrl.contains('10.0.2.2') ||
      apiBaseUrl.contains('localhost') ||
      apiBaseUrl.contains('127.0.0.1');

  /// Called at boot. Fails loudly if the build lacks MARSOUD_API — a
  /// release APK with an empty base URL is a silent-401 bug factory,
  /// and this catches it at first-launch instead of first-login.
  static void assertConfigured() {
    if (apiBaseUrl.isEmpty) {
      // ignore: avoid_print
      print(
        'FATAL: MARSOUD_API not defined. Rebuild with '
        '--dart-define=MARSOUD_API=https://your-backend',
      );
      throw StateError('MARSOUD_API not defined at build time.');
    }
  }
}
