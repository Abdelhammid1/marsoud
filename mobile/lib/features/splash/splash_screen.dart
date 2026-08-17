// Shown while AuthNotifier restores the token from secure storage.
import 'package:flutter/material.dart';

import '../../app/theme.dart';

class SplashScreen extends StatelessWidget {
  const SplashScreen({super.key});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: ScaffoldGradient(
        child: Center(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Container(
                width: 84,
                height: 84,
                decoration: BoxDecoration(
                  color: BrandColors.emerald50,
                  borderRadius: BorderRadius.circular(24),
                  border: Border.all(
                      color: BrandColors.emerald100, width: 2),
                ),
                alignment: Alignment.center,
                child: const Text(
                  'م',
                  style: TextStyle(
                    color: BrandColors.emerald700,
                    fontSize: 40,
                    fontWeight: FontWeight.w800,
                  ),
                ),
              ),
              const SizedBox(height: 20),
              const CircularProgressIndicator(
                strokeWidth: 3,
                color: BrandColors.emerald600,
              ),
            ],
          ),
        ),
      ),
    );
  }
}
