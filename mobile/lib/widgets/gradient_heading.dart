// MARSOUD-MOBILE-FLUTTER — the `.gradient-heading` from the web.
//
// Web CSS uses `background-clip: text` on an emerald gradient
// (login uses navy→blue). Flutter emulates this with ShaderMask.
import 'package:flutter/material.dart';

import '../app/theme.dart';

class GradientHeading extends StatelessWidget {
  final String text;
  final double fontSize;
  final FontWeight fontWeight;
  final List<Color> colors;
  final TextAlign align;

  const GradientHeading(
    this.text, {
    super.key,
    this.fontSize = 28,
    this.fontWeight = FontWeight.w800,
    this.colors = const [BrandColors.emerald600, BrandColors.emerald500],
    this.align = TextAlign.center,
  });

  factory GradientHeading.navy(String text, {
    Key? key,
    double fontSize = 28,
    FontWeight fontWeight = FontWeight.w800,
    TextAlign align = TextAlign.center,
  }) =>
      GradientHeading(
        text,
        key: key,
        fontSize: fontSize,
        fontWeight: fontWeight,
        align: align,
        // login.html uses this exact stop pair.
        colors: const [BrandColors.navy900, BrandColors.blue600],
      );

  @override
  Widget build(BuildContext context) {
    return ShaderMask(
      shaderCallback: (rect) => LinearGradient(
        begin: Alignment.topRight,
        end: Alignment.bottomLeft,
        colors: colors,
      ).createShader(rect),
      blendMode: BlendMode.srcIn,
      child: Text(
        text,
        textAlign: align,
        style: TextStyle(
          fontSize: fontSize,
          fontWeight: fontWeight,
          // ShaderMask paints this white "under" the gradient.
          color: Colors.white,
        ),
      ),
    );
  }
}
