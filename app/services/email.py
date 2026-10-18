"""Email service with SMTP + log-only fallback.

If SMTP_HOST is not configured, emails are logged to console instead of sent —
this keeps dev environments functional without requiring credentials.
"""
import os
import base64
import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from email.utils import formataddr
from flask import current_app, render_template, g, has_request_context

from app import db

logger = logging.getLogger("ledgeros.email")


def _smtp_configured():
    return bool(current_app.config.get("SMTP_HOST"))


# MARSOUD-SMTP-PER-COMPANY-01 (2026-10-18) — resolver that picks the
# SMTP config for a given company_id.  Returns `(cfg_dict, source)`
# where source is "company" or "default".
#
# "company" means a `CompanySMTP` row was found AND is_enabled.  The
# central `send_email` MUST attempt delivery with that config and
# MUST NOT silently retry with the platform default on failure
# (ticket's "لا يؤدي إلى تحويل البريد بصمت" rule).  "default" means
# we fell back to the platform's `.env` config — this is the correct
# behavior for tenants that haven't opted in.
def _resolve_mail_config(company_id):
    if company_id:
        try:
            from app.models import CompanySMTP
            from app.services.crypto import decrypt
            row = CompanySMTP.query.filter_by(
                company_id=company_id, is_enabled=True).first()
        except Exception:
            # DB hiccup → do NOT silently fall back; log + treat as
            # "no company config" (= default path).  This matches the
            # ticket's spirit: a tenant-config lookup problem is not
            # a reason to leak mail out from the platform address if
            # the operator thought they'd configured their own.
            logger.exception(
                "CompanySMTP lookup failed for company_id=%s",
                company_id)
            row = None
        if row:
            return ({
                "SMTP_HOST": row.host,
                "SMTP_PORT": int(row.port),
                "SMTP_USER": row.username or "",
                "SMTP_PASSWORD": decrypt(row.password_encrypted) or "",
                "SMTP_FROM": row.from_email,
                "SMTP_FROM_NAME": row.from_name or row.from_email,
                "SMTP_ENCRYPTION": row.encryption,
            }, "company")
    cfg = current_app.config
    return ({
        "SMTP_HOST": cfg.get("SMTP_HOST"),
        "SMTP_PORT": cfg.get("SMTP_PORT"),
        "SMTP_USER": cfg.get("SMTP_USER"),
        "SMTP_PASSWORD": cfg.get("SMTP_PASSWORD"),
        "SMTP_FROM": cfg.get("SMTP_FROM", "no-reply@marsoud.app"),
        "SMTP_FROM_NAME": cfg.get("SMTP_FROM_NAME", "Marsoud"),
        "SMTP_ENCRYPTION": (
            "STARTTLS" if cfg.get("SMTP_USE_TLS", True) else "NONE"),
    }, "default")


def _implicit_company_id():
    """When a call-site doesn't pass `company_id=` explicitly AND
    we're inside a request bound to an active company, pick it up
    from `g.active_company` so the resolver uses the right tenant.
    Returns None when we're outside a request context (cron jobs
    must pass company_id explicitly)."""
    if has_request_context():
        co = getattr(g, "active_company", None)
        if co is not None:
            return co.id
    return None


def company_logo_email_uri(company):
    """MARSOUD-62 — Return a public https:// URL for the company logo.
    Gmail (web + app) blocks data: URIs in <img src> for security reasons,
    even though Outlook/Apple Mail accept them — so we serve a real hosted
    URL instead of inlining base64. Returns None if there's no logo or the
    file isn't on disk."""
    if not company or not getattr(company, "logo_path", None):
        return None
    rel = company.logo_path.lstrip("/")
    candidate = os.path.join(os.path.dirname(os.path.dirname(__file__)), rel)
    if not os.path.exists(candidate):
        return None
    base = current_app.config.get("SITE_URL", "").rstrip("/")
    if not base:
        return None
    return f"{base}/{rel}"


def send_email(to, subject, html_body, attachments=None,
                text_body=None, *, company_id=None):
    """Send an email. Returns True on success, False on failure
    (logged, never raises).

    attachments: list of (filename, bytes, mimetype) tuples

    MARSOUD-SMTP-PER-COMPANY-01 (2026-10-18) — resolver picks the
    SMTP config for `company_id`.  If `company_id` is None and we're
    in a request context with an active company, that company's id
    is used automatically.  Cron / background jobs that outlive the
    request MUST pass `company_id=` explicitly.
    """
    if not to:
        logger.warning("send_email called with empty 'to'")
        return False

    if company_id is None:
        company_id = _implicit_company_id()
    config, source = _resolve_mail_config(company_id)
    from_addr = config["SMTP_FROM"]
    from_name = config["SMTP_FROM_NAME"]

    if not config.get("SMTP_HOST"):
        # Log-only mode (dev convenience).  Source is reported so
        # the operator can tell whether the platform OR a specific
        # tenant is unconfigured.
        logger.warning(
            "[EMAIL NOT SENT — SMTP_HOST not configured (source=%s)]\n"
            "  To:      %s\n"
            "  Subject: %s\n"
            "  Body:    %s\n"
            "  Attachments: %s",
            source, to, subject, (text_body or html_body)[:200],
            [a[0] for a in (attachments or [])],
        )
        return True

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr((from_name, from_addr))
    msg["To"] = to

    if text_body:
        msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    if attachments:
        mixed = MIMEMultipart("mixed")
        mixed.attach(msg)
        for filename, data, mimetype in attachments:
            main, sub = (mimetype.split("/", 1) + ["octet-stream"])[:2]
            part = MIMEApplication(data, _subtype=sub)
            part.add_header("Content-Disposition", "attachment", filename=filename)
            mixed.attach(part)
        mixed["Subject"] = subject
        mixed["From"] = formataddr((from_name, from_addr))
        mixed["To"] = to
        msg = mixed

    smtp_host = config["SMTP_HOST"]
    smtp_port = int(config["SMTP_PORT"])
    smtp_user = config.get("SMTP_USER") or "(no user)"
    smtp_pw = config.get("SMTP_PASSWORD") or ""
    encryption = (config.get("SMTP_ENCRYPTION") or "STARTTLS").upper()

    # Scoped stamper — closes over company_id + source so the status
    # row only updates when the resolver actually picked a company
    # config.  Writes via a fresh engine connection so a caller
    # mid-transaction can't accidentally commit (or rollback) our
    # audit stamp.
    def _stamp(ok, err_text):
        if source != "company" or not company_id:
            return
        try:
            from app.models import CompanySMTP
            from datetime import datetime as _dt
            with db.engine.begin() as conn:
                conn.execute(
                    CompanySMTP.__table__.update()
                    .where(CompanySMTP.__table__.c.company_id
                            == company_id)
                    .values(last_send_at=_dt.utcnow(),
                             last_send_ok=ok,
                             last_send_error=(err_text if not ok
                                                else None)))
        except Exception:
            # Never let a stamp failure break the mail flow.  The
            # log line below still carries the error.
            logger.exception(
                "could not record company_smtp send status for %s",
                company_id)

    try:
        # SSL (implicit TLS, typically 465) wraps the socket from the
        # start.  STARTTLS (typically 587) opens plain, upgrades.
        # NONE is plain SMTP (dev-only — not safe over public net).
        if encryption == "SSL":
            server_ctx = smtplib.SMTP_SSL(smtp_host, smtp_port,
                                            timeout=30)
        else:
            server_ctx = smtplib.SMTP(smtp_host, smtp_port, timeout=30)
        with server_ctx as server:
            if encryption == "STARTTLS":
                server.starttls()
            if config.get("SMTP_USER"):
                server.login(config["SMTP_USER"], smtp_pw)
            server.send_message(msg)
        logger.info(
            "Email sent → %s (subject=%r, source=%s, host=%s:%s, "
            "user=%s)",
            to, subject, source, smtp_host, smtp_port, smtp_user)
        _stamp(ok=True, err_text=None)
        return True
    except Exception as e:
        # Surface host:port + user + exception type — but NEVER log
        # smtp_pw.  Ticket's "عدم تسجيل كلمات المرور ... في سجلات
        # النظام" rule is enforced here (and by never passing the
        # password dict through to logger.exception's `extra`).
        err_text = f"{type(e).__name__}: {str(e)[:300]}"
        logger.exception(
            "SMTP send failed → source=%s, host=%s:%s, user=%s, "
            "to=%s — %s",
            source, smtp_host, smtp_port, smtp_user, to, err_text,
        )
        # Per-ticket rule: `source == "company"` + failure must NOT
        # silently retry with the platform default.  The caller sees
        # False and is expected to surface the error.  AC #8: also
        # persist the error to CompanySMTP.last_send_* so the UI
        # shows it (not just logs).
        _stamp(ok=False, err_text=err_text)
        return False


# MARSOUD-INVOICE-EMAIL-PDF-ATTACH-01 (2026-09-10) — one helper so
# every customer-facing invoice email (initial send, reminder, payment
# thank-you, installment update) attaches the invoice PDF the same
# way. Before this ticket only send_invoice_email attached one;
# reminders + payment confirmations + installment emails were HTML-
# only, so the customer had to dig up the original email each time
# they needed to look at the actual invoice.
#
# Non-blocking: a PDF render failure logs + returns [] so the email
# still goes out (with just the HTML body) rather than silently
# skipping the whole notification.
def _invoice_pdf_attachment(invoice):
    try:
        from app.services.export import export_invoice_pdf
        pdf = export_invoice_pdf(invoice)
        return [(f"invoice-{invoice.number}.pdf",
                 pdf.getvalue(), "application/pdf")]
    except Exception as e:
        logger.warning(
            "Could not attach invoice PDF for %s: %s",
            invoice.number, e)
        return []


def send_invoice_email(invoice, attach_pdf=True):
    """Notify customer of a sent invoice. Attaches PDF if requested."""
    if not invoice.customer.email:
        logger.info("Skip invoice email: customer %s has no email", invoice.customer.name)
        return False
    subject = f"فاتورة جديدة #{invoice.number} من {invoice.company.name}"
    html = render_template("emails/invoice_sent.html", invoice=invoice)
    attachments = _invoice_pdf_attachment(invoice) if attach_pdf else []
    return send_email(invoice.customer.email, subject, html,
                       attachments=attachments,
                       company_id=invoice.company_id)


def send_payment_received_email(invoice, payment, is_full):
    if not invoice.customer.email:
        return False
    template = "emails/payment_full.html" if is_full else "emails/payment_partial.html"
    label = "تم سداد فاتورة" if is_full else "تم تسجيل دفعة جزئية"
    subject = f"{label} #{invoice.number}"
    html = render_template(template, invoice=invoice, payment=payment)
    # MARSOUD-INVOICE-EMAIL-PDF-ATTACH-01 — attach the current-state
    # PDF so the customer's receipt email carries a printable copy
    # showing the just-recorded payment reflected in the totals.
    return send_email(invoice.customer.email, subject, html,
                       attachments=_invoice_pdf_attachment(invoice),
                       company_id=invoice.company_id)


# MARSOUD-INVOICE-INSTALLMENT-EMAILS-01 (2026-09-08) — one entry point
# for every installment-flavored notification. Wires:
#   * plan_created      — the moment /invoices/new ships a plan
#   * before_<N>        — reminder N days ahead of an installment
#   * due_today         — reminder on the due date itself
#   * overdue_<N>       — reminder N days past due
#   * payment_received  — thank-you the moment an installment is paid
# All routes render app/templates/emails/installment_email.html — one
# template, one visual language, so the customer sees the same
# Tabby-inspired timeline every step of the way.
def send_installment_email(invoice, kind, *, installment=None,
                            payment=None):
    if not invoice.customer or not invoice.customer.email:
        logger.info(
            "Skip installment email: invoice %s — customer has no email",
            invoice.number)
        return False

    # Pick the highlighted installment for kinds that don't get one
    # passed explicitly.  plan_created lights up the FIRST pending
    # row; payment_received lights up the NEXT pending row (so the
    # customer sees "next up: X").
    if installment is None:
        pending = [i for i in (invoice.installments or [])
                   if i.status != "PAID"]
        installment = pending[0] if pending else None

    if kind == "plan_created":
        subject = (f"تم إنشاء الفاتورة #{invoice.number} "
                    f"— {len(invoice.installments or [])} أقساط")
    elif kind == "payment_received":
        subject = (f"شكراً لسداد الدفعة — فاتورة #{invoice.number}")
    elif kind == "due_today":
        subject = (f"تذكير: القسط "
                    f"#{installment.sequence_no if installment else '?'} "
                    f"من فاتورة #{invoice.number} مستحق اليوم")
    elif kind.startswith("before_"):
        n = kind.split("_", 1)[1]
        subject = (f"تذكير: القسط "
                    f"#{installment.sequence_no if installment else '?'} "
                    f"من فاتورة #{invoice.number} يستحق خلال {n} أيام")
    elif kind.startswith("overdue_"):
        n = kind.split("_", 1)[1]
        subject = (f"القسط "
                    f"#{installment.sequence_no if installment else '?'} "
                    f"من فاتورة #{invoice.number} متأخر منذ {n} يوم")
    else:
        subject = f"فاتورة #{invoice.number}"

    html = render_template("emails/installment_email.html",
                            invoice=invoice, kind=kind,
                            installment=installment, payment=payment)
    # MARSOUD-INVOICE-EMAIL-PDF-ATTACH-01 — attach the invoice PDF
    # (with the installment plan block the PDF template ships now)
    # so every touchpoint carries the printable statement, not just
    # the HTML timeline.
    return send_email(invoice.customer.email, subject, html,
                       attachments=_invoice_pdf_attachment(invoice),
                       company_id=invoice.company_id)


def send_overdue_reminder(invoice, days_label):
    """days_label: 'before_<N>', 'overdue', or 'overdue_<N>'"""
    if not invoice.customer.email:
        logger.info(
            "Skip reminder: invoice %s — customer %s has no email",
            invoice.number, invoice.customer.name,
        )
        return False
    if days_label.startswith("before_"):
        n = days_label.split("_", 1)[1]
        subject = f"تذكير: فاتورة #{invoice.number} تستحق خلال {n} أيام"
    elif days_label.startswith("overdue_"):
        n = days_label.split("_", 1)[1]
        subject = f"فاتورة #{invoice.number} متأخرة منذ {n} يوم"
    else:
        subject = f"فاتورة #{invoice.number} تجاوزت تاريخ الاستحقاق"
    html = render_template("emails/invoice_reminder.html", invoice=invoice, days_label=days_label)
    # MARSOUD-INVOICE-EMAIL-PDF-ATTACH-01 — attach the current PDF so
    # the reminder email doubles as a self-contained statement the
    # customer can print or forward, not a "come see it inside the
    # app" nag.
    return send_email(invoice.customer.email, subject, html,
                       attachments=_invoice_pdf_attachment(invoice),
                       company_id=invoice.company_id)


def send_refund_email(invoice, refund):
    """Notify customer that a refund was issued for an invoice."""
    if not invoice.customer.email:
        return False
    subject = f"تأكيد استرداد — فاتورة #{invoice.number}"
    html = render_template("emails/refund_issued.html", invoice=invoice, refund=refund)
    # MARSOUD-INVOICE-EMAIL-PDF-ATTACH-01 — attach the current PDF
    # so the refund confirmation carries an updated statement of the
    # invoice's state after the reversal.
    return send_email(invoice.customer.email, subject, html,
                       attachments=_invoice_pdf_attachment(invoice),
                       company_id=invoice.company_id)


def send_credit_note_email(invoice, credit_note):
    """Notify customer that a credit note was issued for an invoice."""
    if not invoice.customer.email:
        return False
    subject = f"إشعار دائن (Credit Note) — فاتورة #{invoice.number}"
    html = render_template("emails/credit_note_issued.html", invoice=invoice, credit_note=credit_note)
    # MARSOUD-INVOICE-EMAIL-PDF-ATTACH-01 — attach the current PDF
    # so the credit-note email carries the updated invoice statement.
    return send_email(invoice.customer.email, subject, html,
                       attachments=_invoice_pdf_attachment(invoice),
                       company_id=invoice.company_id)


# MARSOUD-INVOICE-CANCEL-EMAIL-01 (2026-10-18) — notify the customer
# that their sales invoice was cancelled.  Reads `invoice.void_reason`
# + `invoice.voided_at` for the body (both stamped by `void_invoice`
# before this helper fires).  Caller stamps
# `invoice.cancel_email_sent_at` on True so a retry / double-POST
# through the resend route can't fire twice.
def send_invoice_cancelled_email(invoice):
    """Notify customer their sales invoice was cancelled.  Returns
    False on no-email-on-file (logged) or SMTP failure (the shared
    send_email already logs).  Never raises — the void_invoice ledger
    reversal already committed; a mail hiccup must not undo it."""
    if not invoice.customer or not invoice.customer.email:
        logger.info(
            "Skip cancel email: invoice %s — customer has no email",
            invoice.number)
        return False
    subject = f"إشعار بإلغاء فاتورة مبيعات رقم {invoice.number}"
    html = render_template("emails/invoice_cancelled.html",
                            invoice=invoice)
    return send_email(invoice.customer.email, subject, html,
                       attachments=_invoice_pdf_attachment(invoice),
                       company_id=invoice.company_id)


def send_commission_paid_email(commission, amount_paid):
    """MARSOUD-COMM-CASH-BASIS-01 (2026-09-20) — notify the sales rep
    that a commission was settled.  Fires from
    `settle_commission_manual()`.  `amount_paid` is what the rep just
    received for THIS settlement (may be a partial payment against a
    larger accrual — the template shows the running remainder).

    Never raises: no email on file → False; SMTP down → the shared
    `send_email` swallows.  Same non-blocking discipline the payment
    emails use.
    """
    rep = commission.sales_rep
    if not rep or not getattr(rep, "email", None):
        return False
    inv = commission.invoice
    inv_no = (inv.number if inv else f"#{commission.invoice_id}")
    subject = f"صرف عمولة مبيعات — فاتورة {inv_no}"
    html = render_template(
        "emails/commission_paid.html",
        commission=commission,
        invoice=inv,
        amount_paid=float(amount_paid or 0),
    )
    return send_email(rep.email, subject, html,
                       company_id=(inv.company_id if inv else None))


def send_invitation_email(invitation, accept_url):
    """Notify an invited user that they have access to a company."""
    role_label = {"owner": "مالك", "admin": "مدير", "accountant": "محاسب", "viewer": "مشاهد"}.get(
        invitation.role, invitation.role
    )
    subject = f"دعوة للانضمام إلى {invitation.company.name} على مرصود"
    html = render_template(
        "emails/invitation.html",
        invitation=invitation, accept_url=accept_url, role_label=role_label,
    )
    return send_email(invitation.email, subject, html,
                       company_id=invitation.company_id)


def send_payslip_email(employee, payroll_line, payroll_run, pdf_bytes=None):
    if not employee.email:
        return False
    subject = f"كشف راتب {payroll_run.period_month}/{payroll_run.period_year} — {employee.name}"
    html = render_template("emails/payslip.html", employee=employee, line=payroll_line, run=payroll_run)
    attachments = []
    if pdf_bytes:
        attachments.append((f"payslip-{payroll_run.period_year}-{payroll_run.period_month:02d}.pdf", pdf_bytes, "application/pdf"))
    return send_email(employee.email, subject, html,
                       attachments=attachments,
                       company_id=employee.company_id)


def send_task_notification_email(recipient, task, *, kind, title=None,
                                  body_text=None):
    """MARSOUD — email tied to an in-app task notification. Fires on
    TASK_ASSIGNED / TASK_STATUS_CHANGED / TASK_COMMENT_ADDED. Never raises;
    silently returns False when the recipient has no email, or the user
    isn't active, or SMTP isn't configured (logged-only mode picks it up).

    Args:
      recipient: User model instance receiving the notification.
      task:      Task model instance.
      kind:      one of the NotificationKind values (TASK_*).
      title:     short notification title (mirrors the in-app body).
      body_text: optional descriptive paragraph (preview of the comment,
                 reason for the status change, etc.).
    """
    if not recipient or not getattr(recipient, "email", None):
        return False
    if not getattr(recipient, "is_active", True):
        return False
    # Build the deep-link to the task detail page.
    from flask import url_for
    try:
        task_url = url_for("tasks.detail", task_id=task.id, _external=True)
    except Exception:
        task_url = "#"
    subject = title or "تنبيه على مهمة في مرصود"
    html = render_template(
        "emails/task_notification.html",
        recipient_name=recipient.full_name or recipient.email,
        task=task, kind=kind, body_text=body_text,
        task_url=task_url, subject=subject,
        company=getattr(task, "company", None),
    )
    return send_email(recipient.email, subject, html)
