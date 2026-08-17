// MARSOUD-MOBILE-FLUTTER — root widget.
//
// Locks the app to Arabic + RTL globally. `builder` wraps every route
// in a Directionality so any Material widget that reads TextDirection
// (Icons, InputDecorators, Dividers) draws right-to-left even when a
// dependency accidentally hardcodes LTR.
import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'router.dart';
import 'theme.dart';

class MarsoudApp extends ConsumerWidget {
  const MarsoudApp({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final router = ref.watch(routerProvider);
    return MaterialApp.router(
      title: 'مرصود',
      debugShowCheckedModeBanner: false,
      theme: MarsoudTheme.light(),
      routerConfig: router,
      locale: const Locale('ar', 'SA'),
      supportedLocales: const [Locale('ar', 'SA')],
      localizationsDelegates: const [
        GlobalMaterialLocalizations.delegate,
        GlobalWidgetsLocalizations.delegate,
        GlobalCupertinoLocalizations.delegate,
      ],
      builder: (context, child) => Directionality(
        textDirection: TextDirection.rtl,
        child: child ?? const SizedBox.shrink(),
      ),
    );
  }
}
