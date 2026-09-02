// MARSOUD-MOBILE-TKT-05 (2026-08-18) — Firebase Cloud Messaging
// setup for the Marsoud employee app.
//
// Responsibilities:
//   1. Initialize FirebaseApp on app boot (called from main()).
//   2. Request notification permission on iOS (Android auto-
//      grants for API < 33; API ≥ 33 needs POST_NOTIFICATIONS
//      which we ask for on first push-token registration).
//   3. Register the current FCM token with the backend
//      (`POST /api/v1/my/push-tokens`) after the user logs in.
//   4. Handle onTokenRefresh — re-POST the new token so the
//      backend never sends to a stale token.
//   5. Show a foreground notification banner via
//      flutter_local_notifications (FCM does NOT auto-display
//      foreground notifications on Android).
//   6. Handle the tap-to-open payload: parse `data.link_url`
//      and route to the corresponding screen.
//
// Everything here is best-effort — a Firebase misconfiguration
// or a permission denial must NEVER crash the app or block the
// login flow. Every method catches broadly and logs.
import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'api_client.dart';
import 'auth_state.dart';

/// MARSOUD-MOBILE-SHIP-READY-01 (audit finding C2) — top-level
/// background handler. Fires when the app is killed / backgrounded
/// AND the incoming message is data-only (or Android delivered via
/// a data path). Must be a top-level function (not a closure) and
/// annotated `@pragma('vm:entry-point')` so tree-shaking + AOT
/// don't strip it. Without this registered, data-only pushes
/// arriving in the background never surface in the tray.
@pragma('vm:entry-point')
Future<void> _firebaseMessagingBackgroundHandler(RemoteMessage message) async {
  try {
    await Firebase.initializeApp();
  } catch (_) {}
  if (kDebugMode) {
    debugPrint('[fcm-bg] ${message.messageId} data=${message.data}');
  }
  // Best-effort: pop a local notification for data-only messages.
  // FCM's own `notification` payload is auto-displayed by the OS
  // when the app is backgrounded, so we only synthesize one when
  // there isn't a notification block.
  if (message.notification == null && message.data.isNotEmpty) {
    try {
      final plugin = FlutterLocalNotificationsPlugin();
      const androidInit = AndroidInitializationSettings('@mipmap/ic_launcher');
      const initSettings = InitializationSettings(android: androidInit);
      await plugin.initialize(initSettings);
      final title = (message.data['title'] as String?) ?? 'مرصود';
      final body = (message.data['body'] as String?) ?? '';
      await plugin.show(
        message.messageId.hashCode,
        title, body,
        NotificationDetails(
          android: AndroidNotificationDetails(
            _androidChannel.id, _androidChannel.name,
            channelDescription: _androidChannel.description,
            importance: Importance.high, priority: Priority.high,
          ),
        ),
        payload: (message.data['link_url'] as String?) ?? '',
      );
    } catch (_) {}
  }
}


/// Called ONCE from `main()` before `runApp()`. Silently no-ops
/// on any failure so the app still starts on a device where
/// Firebase isn't configured yet (dev build, missing
/// google-services.json).
Future<void> initializeFirebase() async {
  try {
    if (Firebase.apps.isEmpty) {
      await Firebase.initializeApp();
    }
    // MARSOUD-MOBILE-SHIP-READY-01 (C2) — register the top-level
    // background handler AFTER Firebase.initializeApp completes.
    // Wrapped so a Firebase misconfig doesn't stop the app.
    try {
      FirebaseMessaging.onBackgroundMessage(
          _firebaseMessagingBackgroundHandler);
    } catch (_) {}
  } catch (e) {
    if (kDebugMode) {
      debugPrint('[fcm] initializeApp failed: $e');
    }
  }
}

/// Local notifications plugin — needed to actually pop a
/// notification while the app is FOREGROUND on Android. FCM
/// only auto-displays when the app is backgrounded.
final _local = FlutterLocalNotificationsPlugin();
const _androidChannel = AndroidNotificationChannel(
  'marsoud_default',
  'إشعارات مرصود',
  description: 'الإشعارات القادمة من تطبيق مرصود',
  importance: Importance.high,
);

class PushService {
  final ApiClient _api;
  final Ref _ref;
  StreamSubscription<String>? _tokenSub;
  StreamSubscription<RemoteMessage>? _messageSub;
  StreamSubscription<RemoteMessage>? _openedSub;

  PushService(this._api, this._ref);

  /// Called from auth_state after a successful login. Requests
  /// permission (iOS + Android 13+), fetches the current FCM
  /// token, registers it with the backend, and installs the
  /// listeners.
  Future<void> onLogin() async {
    try {
      final fm = FirebaseMessaging.instance;
      // iOS + Android 13+ permission prompt.
      await fm.requestPermission(alert: true, badge: true, sound: true);
      // Foreground notification presentation on iOS.
      await fm.setForegroundNotificationPresentationOptions(
        alert: true, badge: true, sound: true,
      );
      // Local notifications channel (Android).
      await _setupLocalNotifications();

      // Register the current token — retry once on transient
      // failure (fresh install may take a couple of seconds to
      // provision the token).
      String? token;
      for (var i = 0; i < 3 && token == null; i++) {
        try {
          token = await fm.getToken();
        } catch (_) {}
        if (token == null) {
          await Future.delayed(const Duration(seconds: 2));
        }
      }
      if (token != null && token.isNotEmpty) {
        await _registerToken(token);
      }

      // Listen for FCM token refresh — happens on app data
      // clear, reinstall, or occasionally after long idle.
      _tokenSub?.cancel();
      _tokenSub = fm.onTokenRefresh.listen((t) async {
        await _registerToken(t);
      });

      // Foreground messages — pop a local banner.
      _messageSub?.cancel();
      _messageSub = FirebaseMessaging.onMessage.listen(_onMessage);

      // Cold-start tap: getInitialMessage returns the message
      // that opened the app when it was fully terminated.
      final initial = await fm.getInitialMessage();
      if (initial != null) {
        _handleOpenedPayload(initial);
      }
      // Warm-start tap: message that opened the app from
      // background.
      _openedSub?.cancel();
      _openedSub = FirebaseMessaging.onMessageOpenedApp
          .listen(_handleOpenedPayload);
    } catch (e) {
      if (kDebugMode) debugPrint('[fcm] onLogin failed: $e');
    }
  }

  /// Called from the logout flow to revoke the token
  /// server-side + cancel listeners.
  Future<void> onLogout() async {
    try {
      final token = await FirebaseMessaging.instance.getToken();
      if (token != null && token.isNotEmpty) {
        try {
          await _api.delete(
            '/api/v1/my/push-tokens/by-token',
            body: {'token': token},
          );
        } catch (_) {}
      }
      await FirebaseMessaging.instance.deleteToken();
    } catch (_) {}
    await _tokenSub?.cancel();
    await _messageSub?.cancel();
    await _openedSub?.cancel();
    _tokenSub = null;
    _messageSub = null;
    _openedSub = null;
  }

  Future<void> _registerToken(String token) async {
    try {
      await _api.post('/api/v1/my/push-tokens', body: {
        'token': token,
        'platform': Platform.isIOS ? 'ios' : 'android',
        'device_label': await _deviceLabel(),
      });
    } catch (e) {
      if (kDebugMode) {
        debugPrint('[fcm] registerToken failed: $e');
      }
    }
  }

  Future<String> _deviceLabel() async {
    // Keep it lightweight — no device_info_plus dep. Falls back
    // to the OS name.
    if (Platform.isIOS) return 'iOS';
    if (Platform.isAndroid) return 'Android';
    return 'Unknown';
  }

  Future<void> _setupLocalNotifications() async {
    const initAndroid = AndroidInitializationSettings(
        '@mipmap/ic_launcher');
    const initIos = DarwinInitializationSettings();
    await _local.initialize(
      const InitializationSettings(
          android: initAndroid, iOS: initIos),
      onDidReceiveNotificationResponse: (resp) {
        // A tap on the local banner surface — same payload
        // handler as onMessageOpenedApp.
        if (resp.payload != null && resp.payload!.isNotEmpty) {
          try {
            final data = jsonDecode(resp.payload!)
                as Map<String, dynamic>;
            _routeToLink(
                data['link_url']?.toString(),
                data['kind']?.toString());
          } catch (_) {}
        }
      },
    );
    // Register the channel on Android 8+.
    await _local
        .resolvePlatformSpecificImplementation<
            AndroidFlutterLocalNotificationsPlugin>()
        ?.createNotificationChannel(_androidChannel);
  }

  Future<void> _onMessage(RemoteMessage msg) async {
    final n = msg.notification;
    if (n == null) return;
    // MARSOUD-MOBILE-SHIP-READY-01 (L11) — was
    // `DateTime.now().millisecondsSinceEpoch.remainder(100000)`,
    // which collides every ~100s and lets a later push silently
    // replace an earlier one in the tray. Use the FCM messageId
    // hash (stable per-message) so each push occupies its own slot.
    await _local.show(
      (msg.messageId ?? msg.hashCode.toString()).hashCode,
      n.title ?? 'مرصود',
      n.body ?? '',
      NotificationDetails(
        android: AndroidNotificationDetails(
          _androidChannel.id,
          _androidChannel.name,
          channelDescription: _androidChannel.description,
          importance: Importance.high,
          priority: Priority.high,
          icon: '@mipmap/ic_launcher',
        ),
        iOS: const DarwinNotificationDetails(),
      ),
      payload: jsonEncode(msg.data),
    );
  }

  void _handleOpenedPayload(RemoteMessage msg) {
    _routeToLink(msg.data['link_url']?.toString(),
        msg.data['kind']?.toString());
  }

  void _routeToLink(String? linkUrl, String? kind) {
    if (linkUrl == null || linkUrl.isEmpty) return;
    // The backend produces web-style paths like `/tasks/42`,
    // `/leads/7`, `/vendor-bills/12`. Map only the ones the
    // employee app renders.
    String mobilePath;
    if (linkUrl.startsWith('/tasks/')) {
      mobilePath = linkUrl;
    } else if (linkUrl.startsWith('/projects/')) {
      mobilePath = linkUrl;
    } else if (linkUrl.startsWith('/leads/')) {
      mobilePath = linkUrl;
    } else if (linkUrl.startsWith('/daily-reports/')) {
      mobilePath = linkUrl;
    } else if (linkUrl.startsWith('/notifications')) {
      mobilePath = '/notifications';
    } else {
      // MARSOUD-MOBILE-SHIP-READY-01 (H4) — unknown link targets
      // (e.g. /vendor-bills/12 which the mobile app doesn't render
      // yet) used to silently redirect to /notifications, leaving
      // the user thinking the notification "did nothing". Fallback
      // still lands on the inbox — but stamp a flag so the shell
      // can show a "opened on desktop" hint at the top of the
      // inbox once we wire it. For now, keep the inbox landing.
      mobilePath = '/notifications';
      if (kDebugMode) {
        debugPrint('[fcm] unrouted deep link: $linkUrl');
      }
    }
    // Router isn't easy to reach here without a BuildContext.
    // Store the path so the shell can consume it. Riverpod
    // notifier.
    _ref.read(pendingDeepLinkProvider.notifier).state = mobilePath;
  }
}

/// Router listens for changes here and pushes the path once
/// the user is authenticated.
final pendingDeepLinkProvider = StateProvider<String?>((_) => null);

/// Constructed by main.dart after Firebase.initializeApp and
/// held by riverpod so screens can call `.onLogin()` /
/// `.onLogout()`.
final pushServiceProvider = Provider<PushService>((ref) {
  return PushService(ref.read(apiClientProvider), ref);
});
