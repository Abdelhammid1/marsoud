// MARSOUD-MOBILE-FLUTTER — task detail (title / description / assignees /
// status changer / comments). Uses /api/v1/tasks/<id>.
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/theme.dart';
import '../../data/api_client.dart';
import '../../data/my_account_repository.dart';
import '../../widgets/gradient_button.dart';
import '../../widgets/section_card.dart';

class TaskDetailScreen extends ConsumerStatefulWidget {
  final int taskId;
  const TaskDetailScreen({super.key, required this.taskId});
  @override
  ConsumerState<TaskDetailScreen> createState() => _TaskDetailScreenState();
}

class _TaskDetailScreenState extends ConsumerState<TaskDetailScreen> {
  late Future<Map<String, dynamic>> _future;
  final _commentCtrl = TextEditingController();
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    _reload();
  }

  void _reload() {
    _future = ref.read(myAccountRepoProvider).taskDetail(widget.taskId);
  }

  @override
  void dispose() {
    _commentCtrl.dispose();
    super.dispose();
  }

  Future<void> _changeStatus(String status) async {
    setState(() => _busy = true);
    try {
      await ref.read(myAccountRepoProvider).setTaskStatus(
            widget.taskId,
            status,
          );
      if (!mounted) return;
      setState(_reload);
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
        content: Text('تم تحديث الحالة.'),
      ));
    } on ApiException catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(e.message)));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _postComment() async {
    final text = _commentCtrl.text.trim();
    if (text.isEmpty) return;
    setState(() => _busy = true);
    try {
      await ref
          .read(myAccountRepoProvider)
          .addTaskComment(widget.taskId, text);
      if (!mounted) return;
      _commentCtrl.clear();
      setState(_reload);
    } on ApiException catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(e.message)));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return FutureBuilder<Map<String, dynamic>>(
      future: _future,
      builder: (context, snap) {
        if (!snap.hasData && !snap.hasError) {
          return const Center(child: CircularProgressIndicator());
        }
        if (snap.hasError) {
          return Center(child: Text(snap.error.toString()));
        }
        final t = snap.data!['task'] as Map<String, dynamic>;
        final status = t['status']?.toString() ?? 'TODO';
        final desc = t['description']?.toString() ?? '';
        final assignees = (t['assignees'] as List?)
                ?.cast<Map<String, dynamic>>() ??
            const [];
        final comments = (t['comments'] as List?)
                ?.cast<Map<String, dynamic>>() ??
            const [];
        final project = t['project'];
        final deadline = (t['deadline'] as String?)?.substring(0, 10);
        return ListView(
          padding: const EdgeInsets.fromLTRB(12, 16, 12, 32),
          children: [
            SectionCard(
              emoji: '✅',
              title: t['title']?.toString() ?? '—',
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  if (project is Map)
                    Row(
                      children: [
                        const Icon(Icons.folder_outlined,
                            size: 14, color: BrandColors.slate500),
                        const SizedBox(width: 4),
                        Text(
                          project['name']?.toString() ?? '',
                          style: const TextStyle(
                            color: BrandColors.slate700,
                            fontWeight: FontWeight.w700,
                            fontSize: 12,
                          ),
                        ),
                      ],
                    ),
                  if (deadline != null) ...[
                    const SizedBox(height: 4),
                    Row(
                      children: [
                        Icon(
                            t['is_overdue'] == true
                                ? Icons.warning_amber
                                : Icons.event,
                            size: 14,
                            color: t['is_overdue'] == true
                                ? BrandColors.red700
                                : BrandColors.slate500),
                        const SizedBox(width: 4),
                        Text(deadline,
                            style: TextStyle(
                              color: t['is_overdue'] == true
                                  ? BrandColors.red700
                                  : BrandColors.slate500,
                              fontSize: 12,
                              fontFamily: 'monospace',
                              fontWeight: FontWeight.w700,
                            )),
                      ],
                    ),
                  ],
                  if (desc.isNotEmpty) ...[
                    const SizedBox(height: 12),
                    const Divider(color: BrandColors.slate100),
                    const SizedBox(height: 10),
                    Text(desc,
                        style: const TextStyle(
                          color: BrandColors.slate700,
                          fontSize: 13,
                          height: 1.7,
                        )),
                  ],
                ],
              ),
            ),
            const SizedBox(height: 12),
            SectionCard(
              emoji: '🚦',
              title: 'الحالة',
              child: Column(
                children: [
                  for (final s in const [
                    'TODO',
                    'IN_PROGRESS',
                    'REVIEW',
                    'BLOCKED',
                    'DONE'
                  ])
                    _StatusOption(
                      status: s,
                      active: s == status,
                      disabled: _busy,
                      onTap: () => _changeStatus(s),
                    ),
                ],
              ),
            ),
            if (assignees.isNotEmpty) ...[
              const SizedBox(height: 12),
              SectionCard(
                emoji: '👥',
                title: 'المكلَّفون',
                child: Wrap(
                  spacing: 6, runSpacing: 6,
                  children: [
                    for (final a in assignees)
                      _AssigneeChip(name: (a['name'] ?? '—').toString()),
                  ],
                ),
              ),
            ],
            const SizedBox(height: 12),
            SectionCard(
              emoji: '💬',
              title: 'التعليقات',
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  if (comments.isEmpty)
                    const EmptyState(
                      icon: Icons.chat_bubble_outline,
                      message: 'لا توجد تعليقات بعد.',
                    )
                  else
                    for (final c in comments) _CommentRow(c: c),
                  const SizedBox(height: 8),
                  TextField(
                    controller: _commentCtrl,
                    minLines: 2, maxLines: 5,
                    decoration: const InputDecoration(
                      hintText: 'اكتب تعليق…',
                    ),
                  ),
                  const SizedBox(height: 10),
                  GradientButton(
                    label: 'أرسل التعليق',
                    icon: Icons.send,
                    loading: _busy,
                    onPressed: _busy ? null : _postComment,
                  ),
                ],
              ),
            ),
          ],
        );
      },
    );
  }
}

class _StatusOption extends StatelessWidget {
  final String status;
  final bool active;
  final bool disabled;
  final VoidCallback onTap;
  const _StatusOption({
    required this.status,
    required this.active,
    required this.disabled,
    required this.onTap,
  });
  @override
  Widget build(BuildContext context) {
    final (bg, fg, label) = switch (status) {
      'TODO' => (BrandColors.slate100, BrandColors.slate700, '📋 جديد'),
      'IN_PROGRESS' =>
        (BrandColors.blue100, BrandColors.blue700, '⚡ قيد التنفيذ'),
      'REVIEW' => (BrandColors.amber50, BrandColors.amber700, '👀 مراجعة'),
      'BLOCKED' => (BrandColors.red50, BrandColors.red700, '⛔ متوقفة'),
      'DONE' =>
        (BrandColors.emerald100, BrandColors.emerald700, '✅ منجزة'),
      _ => (BrandColors.slate100, BrandColors.slate700, status),
    };
    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: Material(
        color: Colors.transparent,
        borderRadius: BorderRadius.circular(10),
        child: InkWell(
          borderRadius: BorderRadius.circular(10),
          onTap: disabled ? null : onTap,
          child: Container(
            padding:
                const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
            decoration: BoxDecoration(
              color: active ? bg : Colors.white,
              borderRadius: BorderRadius.circular(10),
              border: Border.all(
                color: active ? fg.withValues(alpha: 0.4) : BrandColors.slate200,
                width: active ? 1.5 : 1,
              ),
            ),
            child: Row(
              children: [
                Expanded(
                  child: Text(
                    label,
                    style: TextStyle(
                      color: fg,
                      fontWeight:
                          active ? FontWeight.w800 : FontWeight.w600,
                      fontSize: 13,
                    ),
                  ),
                ),
                if (active)
                  Icon(Icons.check_circle, color: fg, size: 18),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _AssigneeChip extends StatelessWidget {
  final String name;
  const _AssigneeChip({required this.name});
  @override
  Widget build(BuildContext context) {
    final initial = name.isNotEmpty ? name.characters.first : '?';
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
      decoration: BoxDecoration(
        color: BrandColors.emerald50,
        borderRadius: BorderRadius.circular(999),
        border: Border.all(color: BrandColors.emerald100),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          CircleAvatar(
            radius: 10,
            backgroundColor: BrandColors.emerald700,
            child: Text(initial,
                style: const TextStyle(
                  color: Colors.white,
                  fontSize: 10,
                  fontWeight: FontWeight.w800,
                )),
          ),
          const SizedBox(width: 6),
          Text(name,
              style: const TextStyle(
                color: BrandColors.emerald700,
                fontSize: 11,
                fontWeight: FontWeight.w700,
              )),
        ],
      ),
    );
  }
}

class _CommentRow extends StatelessWidget {
  final Map<String, dynamic> c;
  const _CommentRow({required this.c});
  @override
  Widget build(BuildContext context) {
    final userName = (c['user'] is Map)
        ? (c['user']['name'] ?? '—').toString()
        : '—';
    final content = c['content']?.toString() ?? '';
    final at = (c['created_at'] as String?)?.substring(0, 16) ?? '';
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(
        color: BrandColors.slate50,
        borderRadius: BorderRadius.circular(10),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Text(userName,
                  style: const TextStyle(
                    color: BrandColors.navy900,
                    fontWeight: FontWeight.w800,
                    fontSize: 12,
                  )),
              const Spacer(),
              Text(at.replaceAll('T', ' '),
                  style: const TextStyle(
                    color: BrandColors.slate400,
                    fontSize: 10,
                    fontFamily: 'monospace',
                  )),
            ],
          ),
          const SizedBox(height: 4),
          Text(content,
              style: const TextStyle(
                color: BrandColors.slate700,
                fontSize: 13,
                height: 1.6,
              )),
        ],
      ),
    );
  }
}
