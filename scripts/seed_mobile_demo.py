"""MARSOUD-MOBILE-FLUTTER — seed a demo Employee for the mobile app.

Creates a user with the `employee` role in the demo company, linked to
an Employee HR record, seeded with one paid leave type + balance, one
active advance, and one payroll run so every section on `/my/account`
renders with real data.

Idempotent — re-runs skip anything already there.

    python scripts/seed_mobile_demo.py
"""
from datetime import date, datetime, timedelta
from decimal import Decimal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app, db
from app.models import (
    User, Company, Employee, EmployeeStatus, ContractType,
    LeaveType, LeaveBalance,
    PayrollRun, PayrollLine,
    Task, TaskStatus, TaskPriority, Project, ProjectStatus,
    Notification, NotificationKind,
    EmployeeAdvance, AdvanceStatus, AdvanceSource,
    CashCustody, CustodyStatus, CustodyHolderType,
    CustodyItem, ItemCustody, ItemCustodyStatus,
)
from app.models.support import (
    SupportTicket, STATUS_OPEN, PRIORITY_MEDIUM,
)
from app.models.user import user_companies


DEMO_EMAIL = "mobile@marsoud.local"
DEMO_PASSWORD = "Mobile1234!"


def _get_or_create_leave_type(company_id, name, is_paid):
    lt = LeaveType.query.filter_by(company_id=company_id, name=name).first()
    if lt:
        return lt, False
    lt = LeaveType(
        company_id=company_id, name=name,
        is_active=True, is_paid=is_paid,
        accrual_per_month=Decimal("1.75") if is_paid else Decimal("0"),
        max_balance=Decimal("21") if is_paid else Decimal("0"),
    )
    db.session.add(lt)
    db.session.flush()
    return lt, True


def main():
    app = create_app()
    with app.app_context():
        # Use the demo company. Anchoring by the demo owner keeps this
        # tied to the seed demo — we're never touching a real tenant.
        owner = User.query.filter_by(email="demo@manasety.ai").first()
        if not owner or not owner.companies:
            print("ERROR: run `python seed.py` first to create the demo "
                  "company + owner.")
            sys.exit(1)
        company = owner.companies[0]
        print(f"→ Using company: {company.name} (id={company.id})")

        # ─── User ────────────────────────────────────────────────────
        user = User.query.filter_by(email=DEMO_EMAIL).first()
        if user:
            print(f"→ User {DEMO_EMAIL} already exists (id={user.id}).")
        else:
            user = User(
                email=DEMO_EMAIL,
                full_name="سارة أحمد — تجربة الموبايل",
                is_active=True,
                # Match the demo owner's terms version so the
                # require_current_terms_version gate lets us in.
                terms_version=owner.terms_version,
                terms_accepted_at=owner.terms_accepted_at,
            )
            user.set_password(DEMO_PASSWORD)
            db.session.add(user)
            db.session.flush()
            print(f"→ Created user id={user.id}")

        # ─── Link user to company with role=employee ─────────────────
        link = db.session.execute(
            user_companies.select().where(
                (user_companies.c.user_id == user.id)
                & (user_companies.c.company_id == company.id)
            )
        ).first()
        if link:
            print(f"→ Membership exists (role={link.role}).")
            if link.role != "employee":
                db.session.execute(
                    user_companies.update().where(
                        (user_companies.c.user_id == user.id)
                        & (user_companies.c.company_id == company.id)
                    ).values(role="employee")
                )
                print("  fixed role → employee")
        else:
            db.session.execute(user_companies.insert().values(
                user_id=user.id, company_id=company.id, role="employee"
            ))
            print("→ Linked to company as employee.")

        # ─── Employee HR record ──────────────────────────────────────
        emp = Employee.query.filter_by(
            company_id=company.id, user_id=user.id
        ).first()
        if emp:
            print(f"→ Employee row exists (id={emp.id}).")
        else:
            emp = Employee(
                company_id=company.id,
                user_id=user.id,
                employee_number="EMP-M001",
                name=user.full_name,
                email=user.email,
                phone="+966501234567",
                job_title="مطوّرة تطبيقات",
                start_date=date.today() - timedelta(days=540),  # ~1.5 yrs
                contract_type=ContractType.FULL_TIME,
                status=EmployeeStatus.ACTIVE,
                basic_salary=Decimal("12000.00"),
                allowances=Decimal("2500.00"),
                deductions=Decimal("300.00"),
                national_id="1099887766",
                nationality="سعودية",
            )
            db.session.add(emp)
            db.session.flush()
            print(f"→ Created Employee id={emp.id}")

        # ─── One paid leave type + balance ───────────────────────────
        lt_annual, _ = _get_or_create_leave_type(company.id, "إجازة سنوية", is_paid=True)
        _get_or_create_leave_type(company.id, "إجازة مرضية", is_paid=True)
        _get_or_create_leave_type(company.id, "بدون راتب", is_paid=False)
        year = date.today().year
        # LeaveType has an auto-seed hook that creates a zero-row on
        # first sight of a new (employee, type, year); we deliberately
        # UPDATE rather than skip so a re-run always ends at 18 days.
        bal = LeaveBalance.query.filter_by(
            employee_id=emp.id, leave_type_id=lt_annual.id, year=year
        ).first()
        if bal is None:
            bal = LeaveBalance(
                employee_id=emp.id, leave_type_id=lt_annual.id,
                year=year,
            )
            db.session.add(bal)
        bal.balance_days = Decimal("21.00")
        bal.used_days = Decimal("3.00")
        print("→ Leave balance set to 21 - 3 = 18 remaining.")

        # ─── One payroll run + this employee's line ──────────────────
        # Prior month, so it looks like a real historical payslip.
        today = date.today()
        py, pm = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
        run = PayrollRun.query.filter_by(
            company_id=company.id, period_year=py, period_month=pm
        ).first()
        if not run:
            run = PayrollRun(
                company_id=company.id,
                number=f"PAYROLL-M{py}{pm:02d}",
                period_year=py, period_month=pm,
                total_gross=Decimal("14500.00"),
                total_net=Decimal("13850.00"),
            )
            db.session.add(run)
            db.session.flush()
            print(f"→ Created payroll run {run.number}.")

        line = PayrollLine.query.filter_by(
            run_id=run.id, employee_id=emp.id
        ).first()
        if not line:
            line = PayrollLine(
                run_id=run.id, employee_id=emp.id,
                working_days=30,
                basic=Decimal("12000.00"),
                allowances=Decimal("2500.00"),
                overtime=Decimal("350.00"),
                bonus=Decimal("500.00"),
                deductions=Decimal("300.00"),
                absence_deduction=Decimal("0"),
                late_deduction=Decimal("0"),
                advance_deduction=Decimal("1200.00"),
                insurance_deduction=Decimal("0"),
                income_tax_deduction=Decimal("0"),
                net=Decimal("13850.00"),
                amount_paid=Decimal("13850.00"),
                payment_method="تحويل بنكي",
                payment_date=date(py, pm, 28),
            )
            db.session.add(line)
            print(f"→ Added payslip line for {emp.name}.")

        # ─── Project + tasks (for /tasks + /projects screens) ────────
        from app.models import Customer
        cust = Customer.query.filter_by(company_id=company.id).first()
        if not cust:
            cust = Customer(company_id=company.id, name="عميل تجريبي")
            db.session.add(cust); db.session.flush()

        project = Project.query.filter_by(
            company_id=company.id, name="مشروع تطبيق الموبايل"
        ).first()
        if not project:
            project = Project(
                company_id=company.id,
                customer_id=cust.id,
                name="مشروع تطبيق الموبايل",
                type="INTERNAL",
                status=ProjectStatus.IN_PROGRESS,
                manager_id=owner.id,
                start_date=date.today() - timedelta(days=30),
                end_date=date.today() + timedelta(days=90),
                notes="بناء تطبيق مرصود على الهاتف — تجربة داخلية.",
                progress_pct=Decimal("35.00"),
            )
            db.session.add(project)
            db.session.flush()
            print(f"→ Created project id={project.id}")

        # A few tasks in different statuses, all assigned to our
        # mobile-demo user so /api/v1/me/tasks returns them.
        task_seeds = [
            ("تصميم شاشة تسجيل الدخول", TaskStatus.DONE, TaskPriority.MEDIUM, -12),
            ("مراجعة شاشة الحساب", TaskStatus.REVIEW, TaskPriority.HIGH, 2),
            ("ربط شاشة الحضور بـ GPS", TaskStatus.IN_PROGRESS, TaskPriority.URGENT, 1),
            ("كتابة اختبار وحدة للـ auth", TaskStatus.TODO, TaskPriority.LOW, 7),
            ("إصلاح مشكلة تسجيل الخروج", TaskStatus.BLOCKED, TaskPriority.HIGH, -1),
        ]
        for title, status, prio, day_offset in task_seeds:
            existing = Task.query.filter_by(
                company_id=company.id, project_id=project.id, title=title,
            ).first()
            if existing:
                continue
            dl = date.today() + timedelta(days=day_offset)
            t = Task(
                company_id=company.id,
                project_id=project.id,
                title=title,
                description="مهمة تجريبية لعرض التطبيق على الموبايل.",
                status=status, priority=prio,
                assigned_to_id=user.id,
                created_by_id=owner.id,
                deadline=dl,
            )
            db.session.add(t)
        print(f"→ Seeded {len(task_seeds)} demo tasks.")

        # ─── Active advance (populates حسابي's السلف section) ────────
        adv = EmployeeAdvance.query.filter_by(
            company_id=company.id, employee_id=emp.id,
            status=AdvanceStatus.ACTIVE,
        ).first()
        if not adv:
            adv = EmployeeAdvance(
                company_id=company.id, employee_id=emp.id,
                amount=Decimal("6000.00"),
                remaining=Decimal("4800.00"),
                months=6,
                monthly_installment=Decimal("1000.00"),
                disbursed_on=date.today() - timedelta(days=45),
                status=AdvanceStatus.ACTIVE,
                source=AdvanceSource.DIRECT,
                approved_by=owner.id,
                created_by=owner.id,
                note="سلفة تجريبية.",
            )
            db.session.add(adv)
            print("→ Seeded an active advance (6000, remaining 4800).")

        # ─── Cash custody (open custody + settled one) ───────────────
        # SETTLED — historical row.
        past_custody = CashCustody.query.filter_by(
            company_id=company.id, employee_id=emp.id,
            purpose="مصاريف انتقال — أغسطس",
        ).first()
        if not past_custody:
            db.session.add(CashCustody(
                company_id=company.id,
                holder_type=CustodyHolderType.EMPLOYEE,
                employee_id=emp.id,
                amount_issued=Decimal("2000.00"),
                amount_settled=Decimal("1850.00"),
                amount_returned=Decimal("150.00"),
                purpose="مصاريف انتقال — أغسطس",
                issued_on=date.today() - timedelta(days=25),
                status=CustodyStatus.SETTLED,
                created_by=owner.id, approved_by=owner.id,
            ))
        # ISSUED — still open.
        open_custody = CashCustody.query.filter_by(
            company_id=company.id, employee_id=emp.id,
            purpose="عهدة مشروع الفرع الجديد",
        ).first()
        if not open_custody:
            db.session.add(CashCustody(
                company_id=company.id,
                holder_type=CustodyHolderType.EMPLOYEE,
                employee_id=emp.id,
                amount_issued=Decimal("3500.00"),
                purpose="عهدة مشروع الفرع الجديد",
                issued_on=date.today() - timedelta(days=6),
                settlement_due_date=date.today() + timedelta(days=14),
                status=CustodyStatus.ISSUED,
                created_by=owner.id, approved_by=owner.id,
            ))
            print("→ Seeded 2 cash custodies (1 settled, 1 open).")

        # ─── Item custody (an item held + one available to request) ──
        laptop = CustodyItem.query.filter_by(
            company_id=company.id, name="لابتوب Dell Latitude 5540",
        ).first()
        if not laptop:
            laptop = CustodyItem(
                company_id=company.id,
                name="لابتوب Dell Latitude 5540",
                serial_number="DL-5540-A012",
                category="أجهزة",
                estimated_value=Decimal("6500.00"),
                is_active=True,
                created_by=owner.id,
            )
            db.session.add(laptop)
            db.session.flush()
        # An available (unheld) item.
        keyboard = CustodyItem.query.filter_by(
            company_id=company.id, name="لوحة مفاتيح ميكانيكية Keychron K8",
        ).first()
        if not keyboard:
            keyboard = CustodyItem(
                company_id=company.id,
                name="لوحة مفاتيح ميكانيكية Keychron K8",
                serial_number="KEY-K8-77",
                category="ملحقات",
                estimated_value=Decimal("450.00"),
                is_active=True,
                created_by=owner.id,
            )
            db.session.add(keyboard)
            db.session.flush()
        # Assign the laptop to our user.
        held = ItemCustody.query.filter_by(
            company_id=company.id, item_id=laptop.id, employee_id=emp.id,
            status=ItemCustodyStatus.ACTIVE,
        ).first()
        if not held:
            db.session.add(ItemCustody(
                company_id=company.id, item_id=laptop.id,
                holder_type=CustodyHolderType.EMPLOYEE,
                employee_id=emp.id,
                handed_over_on=date.today() - timedelta(days=90),
                condition_at_handover="جديد — بدون خدوش.",
                status=ItemCustodyStatus.ACTIVE,
            ))
            print(f"→ Seeded item custody: {laptop.name} → {emp.name}.")

        # ─── Support ticket ─────────────────────────────────────────
        ticket = SupportTicket.query.filter_by(
            company_id=company.id, created_by_id=user.id,
        ).first()
        if not ticket:
            db.session.add(SupportTicket(
                company_id=company.id,
                created_by_id=user.id,
                title="ملاحظة على شاشة الحضور",
                description="أحياناً بيطلع GPS بطيء على الأندرويد الأحدث. "
                            "هل ممكن تخفيف مدة الانتظار؟",
                status=STATUS_OPEN,
                priority=PRIORITY_MEDIUM,
            ))
            print("→ Seeded 1 support ticket.")

        # ─── Notifications (mix of read + unread) ───────────────────
        existing_notifs = Notification.query.filter_by(
            company_id=company.id, user_id=user.id,
        ).count()
        if existing_notifs == 0:
            notifs = [
                (NotificationKind.TASK_ASSIGNED,
                 "🎯 مهمة جديدة: مراجعة شاشة الحساب",
                 "من إدارة المشروع", "/tasks", False),
                (NotificationKind.TASK_DEADLINE_24H,
                 "⏰ موعد نهائي بعد 24 ساعة",
                 "المهمة: ربط شاشة الحضور بـ GPS", "/tasks", False),
                (NotificationKind.MENTION,
                 "💬 تم ذكرك في تعليق",
                 "Ibrahim: راجعي التصميم قبل التسليم لو سمحتِ", "/tasks", True),
            ]
            for kind, title, body, link, is_read in notifs:
                n = Notification(
                    company_id=company.id,
                    user_id=user.id,
                    kind=kind, title=title, body=body,
                    link_url=link,
                )
                if is_read:
                    n.read_at = datetime.utcnow() - timedelta(days=2)
                db.session.add(n)
            print(f"→ Seeded {len(notifs)} notifications.")

        db.session.commit()

        print()
        print("═" * 55)
        print("✅ Mobile demo employee ready.")
        print()
        print(f"  Email:    {DEMO_EMAIL}")
        print(f"  Password: {DEMO_PASSWORD}")
        print(f"  Company:  {company.name}")
        print(f"  Role:     employee")
        print(f"  Emp #:    {emp.employee_number}")
        print()
        print("  → Sign in from the mobile app to see the full account page.")
        print("═" * 55)


if __name__ == "__main__":
    main()
