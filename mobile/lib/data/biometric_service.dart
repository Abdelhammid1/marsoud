// MARSOUD-MOBILE-BIOMETRIC-01 (2026-09-17) — Face ID on iOS +
// Fingerprint on Android to lock the app between opens.  Opt-in
// through the setting inside حسابي; when off (default), the app
// behaves exactly like before (session token → auto-restore →
// home).
//
// How it fits together:
//
//   1. The user turns the toggle on inside حسابي → we prompt the
//      OS auth once (to fail loud if the device has no biometric)
//      and, on success, store `biometric_enabled = true` in
//      shared_preferences.
//
//   2. On next cold-start, `router.dart` sees the session restored
//      AND `biometric_enabled == true` AND `_unlocked == false`,
//      and redirects to `/lock`.  The lock screen prompts biometric
//      → success flips `_unlocked` in this service → the redirect
//      lets the user through to /home.
//
//   3. Toggling off runs the exact same prompt (as an "are you
//      sure it's really you?" gate) before clearing the preference.
//
// Credentials never leave the device.  We never store the biometric
// template; the OS does that.  All we hold is a bool preference +
// the "session unlocked in this app run" latch.
import 'package:flutter/services.dart' show PlatformException;
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:local_auth/local_auth.dart';
import 'package:shared_preferences/shared_preferences.dart';

const String _kBiometricEnabledKey = 'marsoud.biometric_enabled';


class BiometricService {
  // Latched to true after a successful in-app unlock (either from
  // the lock screen at boot, or from a fresh login).  Cleared on
  // logout so the next login has to re-authenticate.  This is
  // deliberately kept in-memory only — a killed/backgrounded app
  // has to re-auth on the next open, which is the whole point.
  bool _unlocked = false;

  final _auth = LocalAuthentication();

  /// Does this device have any biometric or device-credential lock
  /// that we can prompt through?  Returns false on a stock emulator
  /// or a device with no enrolled fingerprints/face — the toggle
  /// stays hidden in that case so the user isn't offered something
  /// they can't use.
  Future<bool> isDeviceSupported() async {
    try {
      final supported = await _auth.isDeviceSupported();
      if (!supported) return false;
      final canCheck = await _auth.canCheckBiometrics;
      final available = await _auth.getAvailableBiometrics();
      // Either real biometric OR the device's own passcode fallback
      // is fine — the OS prompt lets the user choose.
      return canCheck || available.isNotEmpty;
    } catch (_) {
      return false;
    }
  }

  Future<bool> isEnabled() async {
    final prefs = await SharedPreferences.getInstance();
    return prefs.getBool(_kBiometricEnabledKey) ?? false;
  }

  Future<void> setEnabled(bool enabled) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setBool(_kBiometricEnabledKey, enabled);
    // Turning ON = the user just proved their identity; treat the
    // current session as unlocked so we don't immediately bounce
    // them to /lock.
    if (enabled) _unlocked = true;
  }

  bool get isUnlocked => _unlocked;

  /// Prompt the OS auth sheet.  Returns true on success, false on
  /// any failure (user cancelled, wrong finger, hardware error,
  /// etc.).  Never throws — the UI can just show a "حاول تاني"
  /// action on false.
  Future<bool> authenticate({
    String reason = 'افتح تطبيق مرصود',
  }) async {
    try {
      final ok = await _auth.authenticate(
        localizedReason: reason,
        options: const AuthenticationOptions(
          biometricOnly: false, // fall back to device passcode
          stickyAuth: true,
          useErrorDialogs: true,
        ),
      );
      if (ok) _unlocked = true;
      return ok;
    } on PlatformException {
      return false;
    } catch (_) {
      return false;
    }
  }

  /// Called from the logout flow so the next login has to
  /// re-authenticate.  The stored preference is untouched — a user
  /// who had biometric on before logout still has it on for the
  /// next account.
  void clearUnlockLatch() {
    _unlocked = false;
  }
}


final biometricServiceProvider = Provider<BiometricService>((ref) {
  return BiometricService();
});


// MARSOUD-MOBILE-BIOMETRIC-01 — synchronous "should we gate this
// user with biometric right now?" signal for the router redirect.
// Set to true by the splash screen when it detects
// `biometric_enabled=true` in shared_preferences and a valid
// session was restored; flipped to false the moment the OS auth
// prompt succeeds (or the user hits "logout and log in with
// password"). Router reads this in redirect() as a plain bool —
// no async work in the hot path.
final biometricLockedProvider = StateProvider<bool>((ref) => false);
