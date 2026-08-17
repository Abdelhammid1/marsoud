// MARSOUD-MOBILE-FLUTTER — visual language matched to the web.
//
// Tokens taken directly from app/templates/base.html: emerald primary
// (#047857 → #059669 gradient), navy headings (#0A2540), blue accent
// (#2563EB), sky background gradient (#FFFFFF → #F0F7FF), slate text,
// Cairo type at weights 400/600/700/800.
import 'package:flutter/material.dart';
import 'package:google_fonts/google_fonts.dart';

class BrandColors {
  // Emerald / primary
  static const emerald500 = Color(0xFF059669);
  static const emerald600 = Color(0xFF047857);
  static const emerald50 = Color(0xFFECFDF5);
  static const emerald100 = Color(0xFFD1FAE5);
  static const emerald700 = Color(0xFF047857);

  // Navy / headings
  static const navy900 = Color(0xFF0A2540);
  static const navy700 = Color(0xFF1B3A5C);
  static const navy800 = Color(0xFF102A43);

  // Blue / accent
  static const blue600 = Color(0xFF2563EB);
  static const blue700 = Color(0xFF1D4ED8);
  static const blue100 = Color(0xFFDBEAFE);

  // Slate / body copy
  static const slate50 = Color(0xFFF8FAFC);
  static const slate100 = Color(0xFFF1F5F9);
  static const slate200 = Color(0xFFE2E8F0);
  static const slate400 = Color(0xFF94A3B8);
  static const slate500 = Color(0xFF64748B);
  static const slate700 = Color(0xFF334155);
  static const slate900 = Color(0xFF0F172A);

  // Feedback
  static const red500 = Color(0xFFEF4444);
  static const red50 = Color(0xFFFEE2E2);
  static const red700 = Color(0xFFB91C1C);
  static const amber50 = Color(0xFFFEF3C7);
  static const amber700 = Color(0xFF92400E);

  // Page background stops (linear-gradient(180deg, #FFF, #F0F7FF))
  static const bgTop = Color(0xFFFFFFFF);
  static const bgBottom = Color(0xFFF0F7FF);
}

/// Gradient the whole page sits on top of — same 180deg stops the web
/// uses in `body { background: linear-gradient(...) }`.
const pageBackgroundGradient = LinearGradient(
  begin: Alignment.topCenter,
  end: Alignment.bottomCenter,
  colors: [BrandColors.bgTop, BrandColors.bgBottom],
);

class MarsoudTheme {
  static ThemeData light() {
    final baseText = GoogleFonts.cairoTextTheme(
      ThemeData.light().textTheme.apply(
            bodyColor: BrandColors.slate900,
            displayColor: BrandColors.navy900,
          ),
    );

    return ThemeData(
      useMaterial3: true,
      colorScheme: ColorScheme.fromSeed(
        seedColor: BrandColors.emerald500,
        primary: BrandColors.emerald600,
        secondary: BrandColors.blue600,
        surface: Colors.white,
      ),
      // The Scaffold's background is transparent so the outer
      // gradient (painted via a Container in ScaffoldGradient below)
      // shows through. Every scaffold in the app wraps its body in
      // that gradient container.
      scaffoldBackgroundColor: Colors.transparent,
      textTheme: baseText,
      appBarTheme: AppBarTheme(
        backgroundColor: Colors.white,
        foregroundColor: BrandColors.navy900,
        elevation: 0,
        scrolledUnderElevation: 0,
        centerTitle: false,
        titleTextStyle: GoogleFonts.cairo(
          color: BrandColors.navy900,
          fontSize: 18,
          fontWeight: FontWeight.w700,
        ),
        iconTheme: const IconThemeData(color: BrandColors.navy900),
      ),
      cardTheme: CardThemeData(
        color: Colors.white,
        elevation: 0,
        shape: RoundedRectangleBorder(
          side: const BorderSide(color: BrandColors.slate200),
          borderRadius: BorderRadius.circular(16),
        ),
        margin: EdgeInsets.zero,
      ),
      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: Colors.white,
        contentPadding: const EdgeInsets.symmetric(
            horizontal: 16, vertical: 14),
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(12),
          borderSide: const BorderSide(color: BrandColors.slate200),
        ),
        enabledBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(12),
          borderSide: const BorderSide(color: BrandColors.slate200),
        ),
        focusedBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(12),
          borderSide:
              const BorderSide(color: BrandColors.blue600, width: 1.5),
        ),
        errorBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(12),
          borderSide: const BorderSide(color: BrandColors.red500),
        ),
        labelStyle: GoogleFonts.cairo(
          color: BrandColors.slate700,
          fontWeight: FontWeight.w600,
          fontSize: 14,
        ),
        floatingLabelStyle: GoogleFonts.cairo(
          color: BrandColors.blue600,
          fontWeight: FontWeight.w600,
        ),
      ),
      elevatedButtonTheme: ElevatedButtonThemeData(
        // The web's `.btn-primary` is a gradient; ElevatedButton doesn't
        // paint gradients, so we use the primary color as the base tone
        // and wrap explicit CTA buttons in `PrimaryButton` (widgets/
        // gradient_button.dart) where the gradient matters.
        style: ElevatedButton.styleFrom(
          backgroundColor: BrandColors.emerald600,
          foregroundColor: Colors.white,
          padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 14),
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(12),
          ),
          elevation: 0,
          textStyle: GoogleFonts.cairo(
            fontSize: 16, fontWeight: FontWeight.w700),
        ),
      ),
      dividerTheme: const DividerThemeData(
        color: BrandColors.slate100,
        space: 1,
        thickness: 1,
      ),
      snackBarTheme: SnackBarThemeData(
        behavior: SnackBarBehavior.floating,
        backgroundColor: BrandColors.navy900,
        contentTextStyle: GoogleFonts.cairo(
          color: Colors.white, fontWeight: FontWeight.w600),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(12),
        ),
      ),
    );
  }
}

/// Wraps a Scaffold body in the page gradient so every screen inherits
/// the same background without repeating the container each time.
class ScaffoldGradient extends StatelessWidget {
  final Widget child;
  const ScaffoldGradient({super.key, required this.child});
  @override
  Widget build(BuildContext context) {
    return DecoratedBox(
      decoration: const BoxDecoration(gradient: pageBackgroundGradient),
      child: child,
    );
  }
}
