// MARSOUD-MOBILE-FLUTTER — entrypoint.
//
// Loads env config, wires Riverpod, and hands off to MarsoudApp. Everything
// interesting lives under lib/app/ (shell), lib/data/ (API), and
// lib/features/ (screens).
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'app/app.dart';
import 'app/env.dart';
import 'data/push_service.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  Env.assertConfigured();
  // MARSOUD-MOBILE-TKT-05 (2026-08-18) — Firebase must be
  // initialized before any FirebaseMessaging call. Best-effort:
  // silently no-ops if google-services.json is missing so the
  // app still starts on a dev machine.
  await initializeFirebase();
  runApp(const ProviderScope(child: MarsoudApp()));
}
