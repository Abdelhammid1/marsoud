"""Seed a demo company with sample data for testing."""
from datetime import date, timedelta
from app import create_app, db
from app.models import User, Company, Customer, Vendor, Employee, EmployeeStatus, ContractType
from app.services.seed_coa import seed_default_coa
from app.services.numbering import next_number


def seed():
    app = create_app()
    with app.app_context():
        db.create_all()

        if User.query.filter_by(email="demo@manasety.ai").first():
            print("Demo user already exists. Skipping.")
            return

        # Demo user
        user = User(email="demo@manasety.ai", full_name="مؤسس تجريبي")
        user.set_password("demo1234")

        # Demo company
        company = Company(
            name="شركة الأمل التجارية",
            base_currency="SAR",
            tax_number="300123456700003",
            vat_rate=15.00,
            address="الرياض، المملكة العربية السعودية",
        )
        user.companies.append(company)
        db.session.add(user)
        db.session.commit()

        seed_default_coa(company.id)

        # MARSOUD-ROLE-SYNC — the append above leaves role_id NULL.
        from app.services.roles_seed import ensure_roles_ready_for_company
        from app.services.roles import set_membership_role
        ensure_roles_ready_for_company(company.id)
        set_membership_role(user.id, company.id, "owner")

        # Sample customers
        for name, email in [
            ("شركة النور", "info@alnoor.sa"),
            ("مؤسسة الفجر", "sales@alfajr.sa"),
            ("متجر السلام", "shop@alsalam.sa"),
        ]:
            db.session.add(Customer(company_id=company.id, name=name, email=email))

        # Sample vendors
        for name in ["مورد المعدات", "شركة التموين", "موزع المكاتب"]:
            db.session.add(Vendor(company_id=company.id, name=name))

        # Sample employees
        for name, salary in [("أحمد محمد", 8000), ("فاطمة علي", 12000), ("خالد إبراهيم", 6500)]:
            emp_no = next_number(company.id, "EMPLOYEE")
            db.session.add(Employee(
                company_id=company.id, name=name,
                employee_number=emp_no,
                basic_salary=salary, allowances=salary * 0.15, deductions=salary * 0.10,
                job_title="موظف",
                status=EmployeeStatus.ACTIVE,
                contract_type=ContractType.FULL_TIME,
                start_date=date(2024, 1, 1),
            ))

        db.session.commit()
        print("✓ Demo seeded:")
        print("  Email:    demo@manasety.ai")
        print("  Password: demo1234")
        print(f"  Company:  {company.name}")
        print("  Customers: 3, Vendors: 3, Employees: 3")
        print("  Chart of Accounts: 38 accounts seeded")


if __name__ == "__main__":
    seed()
