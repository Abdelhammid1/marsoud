// MARSOUD-MOBILE-FLUTTER — shared `.card p-6` equivalent used by every
// list/detail screen. Keeps the visual grammar identical.
import 'package:flutter/material.dart';

import '../app/theme.dart';

class SectionCard extends StatelessWidget {
  final String? title;
  final String? emoji;
  final String? subtitle;
  final Widget child;
  final EdgeInsets padding;
  const SectionCard({
    super.key,
    this.title,
    this.emoji,
    this.subtitle,
    required this.child,
    this.padding = const EdgeInsets.all(18),
  });
  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: BrandColors.slate200),
      ),
      padding: padding,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          if (title != null) ...[
            Row(
              children: [
                if (emoji != null) ...[
                  Text(emoji!, style: const TextStyle(fontSize: 18)),
                  const SizedBox(width: 8),
                ],
                Expanded(
                  child: Text(
                    title!,
                    style: const TextStyle(
                      color: BrandColors.navy900,
                      fontWeight: FontWeight.w800,
                      fontSize: 15,
                    ),
                  ),
                ),
              ],
            ),
            if (subtitle != null) ...[
              const SizedBox(height: 6),
              Text(
                subtitle!,
                style: const TextStyle(
                  color: BrandColors.slate500,
                  fontSize: 12,
                  height: 1.6,
                ),
              ),
            ],
            const SizedBox(height: 14),
          ],
          child,
        ],
      ),
    );
  }
}

/// Small centered empty-state — matches the web `text-slate-400 py-8`.
class EmptyState extends StatelessWidget {
  final String message;
  final IconData icon;
  const EmptyState({
    super.key,
    required this.message,
    this.icon = Icons.inbox_outlined,
  });
  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 32),
      child: Column(
        children: [
          Container(
            width: 60,
            height: 60,
            decoration: BoxDecoration(
              color: BrandColors.slate100,
              borderRadius: BorderRadius.circular(18),
            ),
            alignment: Alignment.center,
            child: Icon(icon, color: BrandColors.slate400, size: 32),
          ),
          const SizedBox(height: 10),
          Text(
            message,
            textAlign: TextAlign.center,
            style: const TextStyle(
              color: BrandColors.slate400,
              fontSize: 13,
              height: 1.6,
            ),
          ),
        ],
      ),
    );
  }
}

/// Small pill matching `.badge-*` from base.html.
class StatusBadge extends StatelessWidget {
  final String text;
  final Color bg;
  final Color fg;
  const StatusBadge({
    super.key,
    required this.text,
    required this.bg,
    required this.fg,
  });

  /// Preset factories keyed to the web badge classes.
  factory StatusBadge.pending(String text) => StatusBadge(
        text: text, bg: BrandColors.blue100, fg: BrandColors.blue700,
      );
  factory StatusBadge.approved(String text) => StatusBadge(
        text: text, bg: BrandColors.emerald100, fg: BrandColors.emerald700,
      );
  factory StatusBadge.rejected(String text) => StatusBadge(
        text: text, bg: BrandColors.slate100, fg: BrandColors.slate500,
      );
  factory StatusBadge.partial(String text) => StatusBadge(
        text: text, bg: BrandColors.amber50, fg: BrandColors.amber700,
      );
  factory StatusBadge.overdue(String text) => StatusBadge(
        text: text, bg: BrandColors.red50, fg: BrandColors.red700,
      );
  factory StatusBadge.draft(String text) => StatusBadge(
        text: text, bg: BrandColors.slate100, fg: BrandColors.slate700,
      );

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
      decoration: BoxDecoration(
        color: bg,
        borderRadius: BorderRadius.circular(999),
      ),
      child: Text(
        text,
        style: TextStyle(
          color: fg,
          fontWeight: FontWeight.w700,
          fontSize: 10,
        ),
      ),
    );
  }
}
