// MARSOUD-MOBILE-FLUTTER — entrypoint.
//
// Loads env config, wires Riverpod, and hands off to MarsoudApp. Everything
// interesting lives under lib/app/ (shell), lib/data/ (API), and
// lib/features/ (screens).
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'app/app.dart';
import 'app/env.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  Env.assertConfigured();
  runApp(const ProviderScope(child: MarsoudApp()));
}
