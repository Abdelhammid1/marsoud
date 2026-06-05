#!/usr/bin/env python3
"""Promote or create a SUPER_ADMIN account.

Usage:
    .venv/bin/python make_superadmin.py
        → prompts interactively for email + password + full name

    .venv/bin/python make_superadmin.py --email me@x.com --password secret --name "Owner"
        → non-interactive

Behavior:
    • If a user with that email exists → sets is_superadmin = True (and
      updates the password if --password was provided).
    • If not → creates a new user with the given email/password/name, marks
      is_superadmin = True, is_active = True. No company is attached.
    • Idempotent: re-running on the same email is safe.
"""
import argparse
import getpass
import sys

from app import create_app, db
from app.models import User


def promote_or_create(email, password, full_name):
    email = email.strip().lower()
    user = User.query.filter_by(email=email).first()
    if user:
        was_already = user.is_superadmin
        user.is_superadmin = True
        user.is_active = True
        if password:
            user.set_password(password)
        db.session.commit()
        verb = "ترقية" if not was_already else "تحديث"
        print(f"✅ تم {verb} المستخدم {email} كـ SUPER_ADMIN.")
        return user
    if not full_name:
        full_name = email.split("@")[0]
    user = User(
        email=email,
        full_name=full_name,
        is_superadmin=True,
        is_active=True,
    )
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    print(f"✅ تم إنشاء حساب SUPER_ADMIN جديد: {email}")
    return user


def main():
    ap = argparse.ArgumentParser(description="Promote or create a super-admin.")
    ap.add_argument("--email")
    ap.add_argument("--password")
    ap.add_argument("--name", help="Full name (used only for new accounts).")
    args = ap.parse_args()

    email = args.email or input("Email: ").strip()
    if not email:
        print("✗ Email required.", file=sys.stderr)
        sys.exit(1)

    password = args.password
    if not password:
        password = getpass.getpass("Password: ")
        confirm = getpass.getpass("Confirm: ")
        if password != confirm:
            print("✗ Passwords don't match.", file=sys.stderr)
            sys.exit(1)
    if len(password) < 6:
        print("✗ Password must be at least 6 characters.", file=sys.stderr)
        sys.exit(1)

    full_name = args.name or input("Full name (optional): ").strip() or None

    app = create_app()
    with app.app_context():
        promote_or_create(email, password, full_name)


if __name__ == "__main__":
    main()
