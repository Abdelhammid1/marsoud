// MARSOUD-MOBILE-FLUTTER — /home tab. Renders /api/v1/my/account.
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../data/api_client.dart';
import '../../data/my_account_repository.dart';

final _accountProvider = FutureProvider.autoDispose<Map<String, dynamic>>(
    (ref) => ref.watch(myAccountRepoProvider).account());

class MyAccountScreen extends ConsumerWidget {
  const MyAccountScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final async = ref.watch(_accountProvider);
    return async.when(
      loading: () => const Center(child: CircularProgressIndicator()),
      error: (e, _) => _ErrorView(
        message: e is ApiException ? e.message : e.toString(),
        onRetry: () => ref.invalidate(_accountProvider),
      ),
      data: (data) => RefreshIndicator(
        onRefresh: () async => ref.invalidate(_accountProvider),
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            _EmployeeCard(data['employee'] as Map<String, dynamic>,
                data['tenure_label'] as String? ?? '—'),
            const SizedBox(height: 16),
            _LeaveBalanceCard(
                (data['leave']?['balances'] as List?)?.cast<Map<String, dynamic>>() ??
                    []),
            const SizedBox(height: 16),
            _AdvanceCard(
                (data['advance']?['active'] as Map<String, dynamic>?)),
            const SizedBox(height: 16),
            _PayslipList(
                (data['payslips'] as List?)?.cast<Map<String, dynamic>>() ??
                    []),
          ],
        ),
      ),
    );
  }
}

class _EmployeeCard extends StatelessWidget {
  final Map<String, dynamic> emp;
  final String tenure;
  const _EmployeeCard(this.emp, this.tenure);
  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text(emp['name'] ?? '—',
                style: Theme.of(context)
                    .textTheme
                    .titleLarge
                    ?.copyWith(fontWeight: FontWeight.bold)),
            const SizedBox(height: 4),
            Text(emp['job_title'] ?? '',
                style: Theme.of(context).textTheme.bodyMedium),
            const Divider(height: 24),
            _kv('رقم الموظف', emp['employee_number'] ?? '—'),
            _kv('البريد', emp['email'] ?? '—'),
            _kv('الجوال', emp['phone'] ?? '—'),
            _kv('مدة الخدمة', tenure),
          ],
        ),
      ),
    );
  }

  Widget _kv(String k, dynamic v) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 4),
        child: Row(
          children: [
            Text('$k: ',
                style: const TextStyle(fontWeight: FontWeight.bold)),
            Expanded(child: Text('$v')),
          ],
        ),
      );
}

class _LeaveBalanceCard extends StatelessWidget {
  final List<Map<String, dynamic>> balances;
  const _LeaveBalanceCard(this.balances);
  @override
  Widget build(BuildContext context) {
    if (balances.isEmpty) {
      return const Card(
        child: Padding(
          padding: EdgeInsets.all(16),
          child: Text('لا يوجد رصيد إجازات لهذا العام.'),
        ),
      );
    }
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text('رصيد الإجازات',
                style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 8),
            ...balances.map((b) => Padding(
                  padding: const EdgeInsets.symmetric(vertical: 4),
                  child: Row(
                    children: [
                      Expanded(child: Text(b['leave_type_name'] ?? '—')),
                      Text('${b['remaining'] ?? 0} / ${b['granted'] ?? 0}'),
                    ],
                  ),
                )),
          ],
        ),
      ),
    );
  }
}

class _AdvanceCard extends StatelessWidget {
  final Map<String, dynamic>? adv;
  const _AdvanceCard(this.adv);
  @override
  Widget build(BuildContext context) {
    if (adv == null) {
      return const Card(
        child: Padding(
          padding: EdgeInsets.all(16),
          child: Text('لا توجد سلفة نشطة.'),
        ),
      );
    }
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('سلفة نشطة',
                style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 8),
            Text('المبلغ الإجمالي: ${adv!['amount']}'),
            Text('المتبقي: ${adv!['remaining']}'),
            Text('القسط الشهري: ${adv!['monthly_installment']}'),
          ],
        ),
      ),
    );
  }
}

class _PayslipList extends StatelessWidget {
  final List<Map<String, dynamic>> slips;
  const _PayslipList(this.slips);
  @override
  Widget build(BuildContext context) {
    if (slips.isEmpty) {
      return const Card(
        child: Padding(
          padding: EdgeInsets.all(16),
          child: Text('لا توجد قسائم رواتب.'),
        ),
      );
    }
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text('قسائم الرواتب',
                style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 8),
            for (final s in slips.take(6))
              ListTile(
                contentPadding: EdgeInsets.zero,
                title: Text(
                    '${s['period_year']}-${(s['period_month'] as num).toInt().toString().padLeft(2, '0')}'),
                subtitle: Text('صافي: ${s['net']}'),
                trailing: const Icon(Icons.picture_as_pdf),
              ),
          ],
        ),
      ),
    );
  }
}

class _ErrorView extends StatelessWidget {
  final String message;
  final VoidCallback onRetry;
  const _ErrorView({required this.message, required this.onRetry});
  @override
  Widget build(BuildContext context) {
    return Center(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          const Icon(Icons.error_outline, color: Colors.red, size: 48),
          const SizedBox(height: 16),
          Text(message, textAlign: TextAlign.center),
          const SizedBox(height: 16),
          ElevatedButton(onPressed: onRetry, child: const Text('إعادة المحاولة')),
        ],
      ),
    );
  }
}
