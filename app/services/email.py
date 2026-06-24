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
from flask import current_app, render_template

logger = logging.getLogger("ledgeros.email")


def _smtp_configured():
    return bool(current_app.config.get("SMTP_HOST"))


def company_logo_email_uri(company):
    """MARSOUD-62 — Return the company logo as a data: URI for embedding in
    HTML emails. Email clients can't fetch local /static/ paths, so we
    encode the file bytes inline. Returns None if there's no logo or the
    file isn't on disk."""
    if not company or not getattr(company, "logo_path", None):
        return None
    rel = company.logo_path.lstrip("/")
    # static lives at app/static/ — same trick as services/export.py
    candidate = os.path.join(os.path.dirname(os.path.dirname(__file__)), rel)
    if not os.path.exists(candidate):
        return None
    ext = os.path.splitext(candidate)[1].lstrip(".").lower()
    mime = "image/png" if ext == "png" else f"image/{ext}"
    try:
        with open(candidate, "rb") as f:
            return f"data:{mime};base64,{base64.b64encode(f.read()).decode('ascii')}"
    except OSError:
        return None


def send_email(to, subject, html_body, attachments=None, text_body=None):
    """Send an email. Returns True on success, False on failure (logged, never raises).

    attachments: list of (filename, bytes, mimetype) tuples
    """
    if not to:
        logger.warning("send_email called with empty 'to'")
        return False

    config = current_app.config
    from_addr = config.get("SMTP_FROM", "no-reply@marsoud.app")
    from_name = config.get("SMTP_FROM_NAME", "Marsoud")

    if not _smtp_configured():
        # Log-only mode for dev
        logger.warning(
            "[EMAIL NOT SENT — SMTP_HOST not configured in .env]\n"
            "  To:      %s\n"
            "  Subject: %s\n"
            "  Body:    %s\n"
            "  Attachments: %s",
            to, subject, (text_body or html_body)[:200],
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
    smtp_port = config["SMTP_PORT"]
    smtp_user = config.get("SMTP_USER") or "(no user)"
    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            if config.get("SMTP_USE_TLS", True):
                server.starttls()
            if config.get("SMTP_USER"):
                server.login(config["SMTP_USER"], config["SMTP_PASSWORD"])
            server.send_message(msg)
        logger.info("Email sent → %s (subject=%r, host=%s:%s, user=%s)",
                    to, subject, smtp_host, smtp_port, smtp_user)
        return True
    except Exception as e:
        # Surface every detail we have — host, port, user, and the actual exception text.
        # This is the line abdelhamid asked about: previously the failure was hidden
        # under a generic message that hid which step failed (connect / TLS / login / send).
        logger.exception(
            "SMTP send failed → host=%s:%s, user=%s, to=%s — %s: %s",
            smtp_host, smtp_port, smtp_user, to, type(e).__name__, str(e)[:300],
        )
        return False


def send_invoice_email(invoice, attach_pdf=True):
    """Notify customer of a sent invoice. Attaches PDF if requested."""
    if not invoice.customer.email:
        logger.info("Skip invoice email: customer %s has no email", invoice.customer.name)
        return False
    subject = f"فاتورة جديدة #{invoice.number} من {invoice.company.name}"
    html = render_template("emails/invoice_sent.html", invoice=invoice)
    attachments = []
    if attach_pdf:
        try:
            from app.services.export import export_invoice_pdf
            pdf = export_invoice_pdf(invoice)
            attachments.append((f"invoice-{invoice.number}.pdf", pdf.getvalue(), "application/pdf"))
        except Exception as e:
            logger.warning("Could not attach invoice PDF: %s", e)
    return send_email(invoice.customer.email, subject, html, attachments=attachments)


def send_payment_received_email(invoice, payment, is_full):
    if not invoice.customer.email:
        return False
    template = "emails/payment_full.html" if is_full else "emails/payment_partial.html"
    label = "تم سداد فاتورة" if is_full else "تم تسجيل دفعة جزئية"
    subject = f"{label} #{invoice.number}"
    html = render_template(template, invoice=invoice, payment=payment)
    return send_email(invoice.customer.email, subject, html)


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
    return send_email(invoice.customer.email, subject, html)


def send_refund_email(invoice, refund):
    """Notify customer that a refund was issued for an invoice."""
    if not invoice.customer.email:
        return False
    subject = f"تأكيد استرداد — فاتورة #{invoice.number}"
    html = render_template("emails/refund_issued.html", invoice=invoice, refund=refund)
    return send_email(invoice.customer.email, subject, html)


def send_credit_note_email(invoice, credit_note):
    """Notify customer that a credit note was issued for an invoice."""
    if not invoice.customer.email:
        return False
    subject = f"إشعار دائن (Credit Note) — فاتورة #{invoice.number}"
    html = render_template("emails/credit_note_issued.html", invoice=invoice, credit_note=credit_note)
    return send_email(invoice.customer.email, subject, html)


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
    return send_email(invitation.email, subject, html)


def send_payslip_email(employee, payroll_line, payroll_run, pdf_bytes=None):
    if not employee.email:
        return False
    subject = f"كشف راتب {payroll_run.period_month}/{payroll_run.period_year} — {employee.name}"
    html = render_template("emails/payslip.html", employee=employee, line=payroll_line, run=payroll_run)
    attachments = []
    if pdf_bytes:
        attachments.append((f"payslip-{payroll_run.period_year}-{payroll_run.period_month:02d}.pdf", pdf_bytes, "application/pdf"))
    return send_email(employee.email, subject, html, attachments=attachments)


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
