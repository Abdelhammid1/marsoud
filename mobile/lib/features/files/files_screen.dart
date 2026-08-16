// MARSOUD-MOBILE-FLUTTER — ملفاتي (mirrors user_files/index.html for own folder).
//
// Read-only list of the caller's own uploaded files. Upload/download
// require the /docs bearer surface which isn't wired yet; when it's
// built, an FAB + tap-to-download go here.
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/theme.dart';
import '../../data/api_client.dart';
import '../../data/my_account_repository.dart';
import '../../widgets/section_card.dart';

final _filesProvider = FutureProvider.autoDispose<Map<String, dynamic>>(
    (ref) => ref.watch(myAccountRepoProvider).files());

class FilesScreen extends ConsumerWidget {
  const FilesScreen({super.key});
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final async = ref.watch(_filesProvider);
    return async.when(
      loading: () => const Center(child: CircularProgressIndicator()),
      error: (e, _) => Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Text(e is ApiException ? e.message : e.toString()),
        ),
      ),
      data: (data) {
        final files = (data['files'] as List?)
                ?.cast<Map<String, dynamic>>() ??
            const [];
        return RefreshIndicator(
          color: BrandColors.emerald600,
          onRefresh: () async => ref.invalidate(_filesProvider),
          child: ListView(
            padding: const EdgeInsets.fromLTRB(12, 16, 12, 32),
            children: [
              SectionCard(
                emoji: '📁',
                title: 'ملفاتي',
                subtitle: 'الملفات المرفوعة إلى مجلدك الشخصي فقط.',
                child: files.isEmpty
                    ? const EmptyState(
                        icon: Icons.folder_open,
                        message: 'لا يوجد ملفات في مجلدك بعد.',
                      )
                    : Column(
                        children: [
                          for (final f in files) _FileRow(f: f),
                        ],
                      ),
              ),
            ],
          ),
        );
      },
    );
  }
}

class _FileRow extends StatelessWidget {
  final Map<String, dynamic> f;
  const _FileRow({required this.f});
  @override
  Widget build(BuildContext context) {
    final name = f['filename']?.toString() ?? '—';
    final size = f['size_human']?.toString() ?? '—';
    final mime = f['mimetype']?.toString() ?? '';
    final createdAt = (f['created_at'] as String?)?.substring(0, 10) ?? '';
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: BrandColors.slate50,
        borderRadius: BorderRadius.circular(10),
      ),
      child: Row(
        children: [
          Container(
            width: 40,
            height: 40,
            decoration: BoxDecoration(
              color: _tint(mime),
              borderRadius: BorderRadius.circular(10),
            ),
            alignment: Alignment.center,
            child: Icon(_icon(mime), color: _fg(mime), size: 20),
          ),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  name,
                  style: const TextStyle(
                    color: BrandColors.navy900,
                    fontWeight: FontWeight.w700,
                    fontSize: 13,
                  ),
                  overflow: TextOverflow.ellipsis,
                ),
                const SizedBox(height: 2),
                Row(
                  children: [
                    Text(
                      size,
                      style: const TextStyle(
                        color: BrandColors.slate500,
                        fontSize: 11,
                      ),
                    ),
                    const SizedBox(width: 8),
                    Text(
                      createdAt,
                      style: const TextStyle(
                        color: BrandColors.slate400,
                        fontSize: 11,
                        fontFamily: 'monospace',
                      ),
                    ),
                  ],
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }

  static Color _tint(String m) {
    final low = m.toLowerCase();
    if (low.startsWith('image/')) return BrandColors.emerald50;
    if (low == 'application/pdf') return BrandColors.red50;
    if (low.contains('spreadsheet') || low.endsWith('xlsx') || low.endsWith('xls')) {
      return BrandColors.emerald100;
    }
    if (low.contains('word') || low.endsWith('docx') || low.endsWith('doc')) {
      return BrandColors.blue100;
    }
    return BrandColors.slate100;
  }

  static Color _fg(String m) {
    final low = m.toLowerCase();
    if (low.startsWith('image/')) return BrandColors.emerald700;
    if (low == 'application/pdf') return BrandColors.red700;
    if (low.contains('spreadsheet') || low.endsWith('xlsx') || low.endsWith('xls')) {
      return BrandColors.emerald700;
    }
    if (low.contains('word') || low.endsWith('docx') || low.endsWith('doc')) {
      return BrandColors.blue700;
    }
    return BrandColors.slate500;
  }

  static IconData _icon(String m) {
    final low = m.toLowerCase();
    if (low.startsWith('image/')) return Icons.image_outlined;
    if (low == 'application/pdf') return Icons.picture_as_pdf;
    if (low.contains('spreadsheet') || low.endsWith('xlsx') || low.endsWith('xls')) {
      return Icons.grid_on;
    }
    if (low.contains('word') || low.endsWith('docx') || low.endsWith('doc')) {
      return Icons.article;
    }
    return Icons.insert_drive_file;
  }
}
