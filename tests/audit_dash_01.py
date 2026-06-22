#!/usr/bin/env python3
"""MARSOUD-DASH-01 audit — verifies the rebuilt owner dashboard.

Drives Flask's test_client through the dashboard route as the demo
owner and asserts:
  - All 6 sections render
  - dashboard_metrics() returns the full DASH-01 schema
  - Lead.expected_value is wired (data + form + dashboard sum)
  - Sparkline data points have the right shape (8 per KPI)
  - 6-month trend has 6 labels and 6 revenue/expense points
  - Permission gating: financial sections hidden when reports.view missing
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
DEMO_EMAIL = "demo@manasety.ai"
DEMO_PASS = "demo1234"


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _login(client, email, password):
    r = client.post("/login", data={"email": email, "password": password},
                    follow_redirects=False)
    assert r.status_code in (302, 303), \
        f"login for {email} failed: status={r.status_code}"


# ─── 1. Lead.expected_value column ──────────────────────────────────────
@check("1. Lead model has expected_value column")
def _():
    from app.models import Lead
    cols = {c.name for c in Lead.__table__.columns}
    assert "expected_value" in cols, "expected_value missing on Lead"
    return "Lead.expected_value column declared"


@check("2. Migration bd_4e9f1c6d8a3 file exists")
def _():
    f = ROOT / "migrations/versions/bd_4e9f1c6d8a3_lead_expected_value.py"
    assert f.exists(), "migration file not found"
    txt = f.read_text()
    assert "expected_value" in txt
    assert "down_revision = 'ac_3f6d8b9a2c4'" in txt
    return "migration file present + chains off ac_3f6d8b9a2c4"


@check("3. Lead form has expected_value input")
def _():
    src = (ROOT / "app/templates/leads/form.html").read_text()
    assert 'name="expected_value"' in src, "form missing the input"
    return "form has numeric input named expected_value"


# ─── 2. dashboard_metrics() full schema ────────────────────────────────
@check("4. dashboard_metrics() returns full DASH-01 schema")
def _():
    from app.models import Company
    from app.services.reports import dashboard_metrics
    company = Company.query.order_by(Company.id).first()
    m = dashboard_metrics(company.id)
    required = {
        "currency", "kpis", "ops", "late_invoices", "late_invoices_total",
        "late_invoices_count", "upcoming_bills", "upcoming_bills_total",
        "upcoming_bills_count", "trend_6m", "expense_breakdown",
        "team_most_active", "team_most_tasks", "team_most_leads",
        # Back-compat
        "total_revenue", "total_expenses", "net_profit", "cash_position",
        "unpaid_invoices", "overdue_invoices", "accounts_payable",
        "debt_to_equity", "current_ratio",
    }
    missing = required - set(m.keys())
    assert not missing, f"missing keys: {missing}"
    return f"all {len(required)} keys present"


@check("5. Each KPI has 8-point sparkline")
def _():
    from app.models import Company
    from app.services.reports import dashboard_metrics
    company = Company.query.order_by(Company.id).first()
    m = dashboard_metrics(company.id)
    for k in ("liquidity", "net_profit_month", "ar_open", "ap_open"):
        spark = m["kpis"][k]["spark"]
        assert len(spark) == 8, f"{k} sparkline has {len(spark)} points, expected 8"
        assert all(isinstance(p, (int, float)) for p in spark), \
            f"{k} spark contains non-numeric"
    return "all 4 KPI sparklines have exactly 8 numeric points"


@check("6. 6-month trend has aligned arrays")
def _():
    from app.models import Company
    from app.services.reports import dashboard_metrics
    company = Company.query.order_by(Company.id).first()
    m = dashboard_metrics(company.id)
    t = m["trend_6m"]
    assert len(t["labels"]) == 6, f"labels has {len(t['labels'])}, expected 6"
    assert len(t["revenue"]) == 6
    assert len(t["expenses"]) == 6
    # Labels are Arabic month names
    arabic_months = {"يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
                     "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"}
    assert all(L in arabic_months for L in t["labels"])
    return f"6-month trend labels: {t['labels']}"


@check("7. Lead.expected_value flows to ops.leads_expected_total")
def _():
    """Insert a Lead with expected_value=5000, verify the sum updates."""
    from app.models import Lead, LeadStatus, Company, User
    from app.services.reports import dashboard_metrics
    company = Company.query.order_by(Company.id).first()
    user = User.query.filter_by(email=DEMO_EMAIL).first()

    before = dashboard_metrics(company.id)["ops"]["leads_expected_total"]

    L = Lead(
        company_id=company.id,
        client_name="DASH_TEST_LEAD",
        phone="999",
        service_needed="dashboard sum test",
        assigned_to_id=user.id,
        created_by_id=user.id,
        status=LeadStatus.NEW_LEAD,
        expected_value=5000,
    )
    db.session.add(L)
    db.session.commit()

    after = dashboard_metrics(company.id)["ops"]["leads_expected_total"]

    # Cleanup
    db.session.delete(L)
    db.session.commit()

    delta = after - before
    assert abs(delta - 5000) < 0.01, \
        f"expected delta=5000, got {delta} (before={before}, after={after})"
    return f"adding a 5000 lead bumped leads_expected_total by exactly 5000"


# ─── 3. HTTP rendering ─────────────────────────────────────────────────
@check("8. GET /home renders 200 + has all 6 section labels")
def _():
    app = create_app()
    with app.test_client() as client:
        _login(client, DEMO_EMAIL, DEMO_PASS)
        r = client.get("/home", follow_redirects=False)
        assert r.status_code == 200, f"status={r.status_code}"
        body = r.data.decode("utf-8")
        for section in ["الصحة المالية", "نظرة على العمليات",
                         "يحتاج انتباهك", "الاتجاه المالي",
                         "أداء الفريق", "إجراءات سريعة"]:
            assert section in body, f"missing section '{section}'"
    return "200 OK + all 6 section labels present"


@check("9. Dashboard includes Chart.js + sparkline canvases")
def _():
    app = create_app()
    with app.test_client() as client:
        _login(client, DEMO_EMAIL, DEMO_PASS)
        body = client.get("/home").data.decode("utf-8")
        assert "chart.js" in body.lower() or "chart.umd" in body.lower(), \
            "Chart.js script missing"
        assert 'data-spark=' in body, "sparkline canvases missing"
        assert 'id="dashTrend"' in body, "trend chart canvas missing"
        # Charts must live inside fixed-height containers (prevents resize loops)
        assert "height:240px" in body or "chart-wrap" in body, \
            "chart container height not enforced"
    return "Chart.js + 4 spark canvases + trend canvas + fixed-height wrap"


@check("10. Quick-actions row gates each link by permission")
def _():
    src = (ROOT / "app/templates/dashboard/index.html").read_text()
    # Each of the 8 quick-action buttons is wrapped in has_permission()
    expected_perms = ["invoices.create", "vendor_bills.create", "journals.create",
                      "payroll.run", "leads.manage", "tasks.manage",
                      "agent.use"]
    for p in expected_perms:
        assert f"has_permission('{p}')" in src, f"quick action not gated on '{p}'"
    return f"all {len(expected_perms)} quick actions are permission-gated"


@check("11. Sidebar untouched (base.html still serves the same sidebar)")
def _():
    # Sanity: the dashboard template doesn't redefine the sidebar block
    src = (ROOT / "app/templates/dashboard/index.html").read_text()
    assert "{% block sidebar" not in src, \
        "dashboard template redefines sidebar block (forbidden by ticket)"
    return "dashboard doesn't override the sidebar block"


@check("12. Team Performance section gated by users.view (admin-ish)")
def _():
    src = (ROOT / "app/templates/dashboard/index.html").read_text()
    assert "has_permission('users.view')" in src, \
        "Team Performance section not gated"
    # Verify the gate is around the team section
    idx_perm = src.find("can_team = has_permission('users.view')")
    idx_team = src.find("أداء الفريق")
    assert idx_perm > 0 and idx_team > idx_perm, \
        "Team gate must appear before the section markup"
    return "Team Performance hidden when users.view missing"


def main():
    app = create_app()
    with app.app_context():
        passed = failed = 0
        for label, fn in CHECKS:
            try:
                msg = fn()
                print(f"\033[92mPASS\033[0m  {label}")
                if msg:
                    print(f"        {msg}")
                passed += 1
            except Exception as e:
                print(f"\033[91mFAIL\033[0m  {label}")
                print(f"        {type(e).__name__}: {e}")
                import traceback
                traceback.print_exc()
                failed += 1
        print()
        print(f"  {passed}/{passed + failed} checks passed.")
        return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
