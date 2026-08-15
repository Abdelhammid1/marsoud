// MARSOUD-MOBILE-FLUTTER — theme matching the web (Cairo font, navy/blue).
import 'package:flutter/material.dart';
import 'package:google_fonts/google_fonts.dart';

class MarsoudTheme {
  static const Color _brandNavy = Color(0xFF1E3A8A);
  static const Color _brandBlue = Color(0xFF2563EB);

  static ThemeData light() {
    final base = ThemeData(
      useMaterial3: true,
      colorScheme: ColorScheme.fromSeed(seedColor: _brandNavy),
      brightness: Brightness.light,
    );
    return base.copyWith(
      textTheme: GoogleFonts.cairoTextTheme(base.textTheme),
      appBarTheme: base.appBarTheme.copyWith(
        backgroundColor: _brandNavy,
        foregroundColor: Colors.white,
        centerTitle: true,
      ),
      elevatedButtonTheme: ElevatedButtonThemeData(
        style: ElevatedButton.styleFrom(
          backgroundColor: _brandBlue,
          foregroundColor: Colors.white,
          padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 12),
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(10),
          ),
          textStyle: GoogleFonts.cairo(
            fontSize: 16,
            fontWeight: FontWeight.w600,
          ),
        ),
      ),
      inputDecorationTheme: const InputDecorationTheme(
        border: OutlineInputBorder(),
        contentPadding:
            EdgeInsets.symmetric(horizontal: 12, vertical: 14),
      ),
    );
  }
}
