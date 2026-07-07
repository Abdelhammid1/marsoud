#!/usr/bin/env python3
"""MARSOUD-USER-FILES-TAB + MARSOUD-USER-FILES-MOBILE-PDF — audit for
the two ticket-3 / ticket-4 UI additions on /files/.

Because these are template changes with no service-layer surface,
the audit runs against the Flask test client and asserts on the
raw HTML.

Coverage:
  1. /files/ list carries the "↗ فتح في تاب جديد" link per file,
     with target=_blank AND rel=noopener noreferrer (open-redirect
     hygiene).
  2. /files/<id> detail page carries the same link in its action bar.
  3. Detail template for a PDF file emits BOTH the desktop iframe
     wrapper and the mobile CTA, guarded by a @media (min-width:
     768px) rule.
  4. The mobile CTA points at /files/<id>/raw and opens in a fresh
     tab with the same rel="noopener noreferrer".
  5. Image files still use the <img> path (not the PDF split), so
     the mobile-CTA branch doesn't fire for non-PDFs.
"""
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


class _FakeUpload:
    def __init__(self, data, filename, mimetype):
        self.stream = io.BytesIO(data)
        self.filename = filename
        self.mimetype = mimetype
    def save(self, path):
        self.stream.seek(0)
        Path(path).write_bytes(self.stream.read())


def _setup():
    from app.models import Company, User, user_companies
    from werkzeug.security import generate_password_hash
    existing = Company.query.filter_by(name="__UF_UI_AUDIT__").first()
    if existing:
        _teardown(existing.id)
    c = Company(name="__UF_UI_AUDIT__", base_currency="SAR")
    db.session.add(c); db.session.flush()
    from app.services.seed_coa import seed_default_coa
    seed_default_coa(c.id)
    u = User(email="uf-ui-audit@x.test",
              password_hash=generate_password_hash("x", method="pbkdf2:sha256"),
              full_name="UF UI Audit")
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role="owner",
    ))
    db.session.commit()
    _STATE.update(company_id=c.id, user_id=u.id)


def _teardown(company_id):
    from sqlalchemy import text, inspect
    db.session.close()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT storage_key FROM user_files WHERE company_id = :c"
        ), {"c": company_id}).fetchall()
        for (k,) in rows:
            p = Path(ROOT) / "app" / "private_uploads" / "user_files" / k
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
        conn.execute(text("DELETE FROM user_files WHERE company_id = :c"),
                     {"c": company_id})
        conn.execute(text("DELETE FROM user_companies WHERE company_id = :c"),
                     {"c": company_id})
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {c["name"] for c in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(text(f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                             {"c": company_id})
        conn.execute(text("DELETE FROM companies WHERE id = :c"),
                     {"c": company_id})
        conn.execute(text(
            "DELETE FROM users WHERE email = 'uf-ui-audit@x.test'"))


def _reset_g():
    from flask import g
    for key in ("_login_user", "active_company", "user_companies",
                 "impersonating"):
        try:
            g.pop(key, None)
        except Exception:
            pass


def _client():
    from flask import current_app
    _reset_g()
    c = current_app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(_STATE["user_id"])
        sess["_fresh"] = True
        sess["active_company_id"] = _STATE["company_id"]
    return c


# Seed one PDF and one image so both branches are exercised.
def _upload(name, mime, payload):
    from app.services.user_files import save_user_file
    return save_user_file(
        company_id=_STATE["company_id"], user_id=_STATE["user_id"],
        file_storage=_FakeUpload(payload, name, mime),
    )


@check("1. /files/ list carries new-tab link with rel=noopener noreferrer")
def _():
    pdf = _upload("audit.pdf", "application/pdf", b"%PDF-1.4\n%.")
    _STATE["pdf_id"] = pdf.id
    r = _client().get("/files/")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "فتح في تاب جديد" in body, "new-tab label missing on list"
    # target=_blank + rel="noopener noreferrer" on the raw link
    marker = (
        f'href="/files/{pdf.id}/raw"'
        + '.*target="_blank"'
    )
    import re
    assert re.search(marker, body, re.DOTALL), \
        "new-tab link missing target=_blank"
    assert 'rel="noopener noreferrer"' in body, \
        "missing rel=noopener noreferrer"
    return "list has target=_blank + rel=noopener noreferrer"


@check("2. detail page action bar has the same new-tab link")
def _():
    r = _client().get(f"/files/{_STATE['pdf_id']}")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "فتح في تاب جديد" in body, "new-tab CTA missing on detail"
    assert f'href="/files/{_STATE["pdf_id"]}/raw"' in body
    assert 'target="_blank"' in body
    return "detail page carries the new-tab link"


@check("3. PDF detail emits BOTH desktop iframe + mobile CTA")
def _():
    r = _client().get(f"/files/{_STATE['pdf_id']}")
    body = r.get_data(as_text=True)
    assert 'class="pdf-preview-desktop"' in body, \
        "desktop wrapper missing"
    assert 'class="pdf-preview-mobile"' in body, \
        "mobile CTA missing"
    # @media (min-width: 768px) breakpoint hidden inside <style>
    assert "@media (min-width: 768px)" in body, \
        "media-query breakpoint missing"
    # The desktop wrapper contains the iframe, mobile CTA opens raw.
    assert f"<iframe src=\"/files/{_STATE['pdf_id']}/raw\"" in body
    return "both preview blocks + 768px breakpoint present"


@check("4. mobile CTA links to /raw with rel=noopener")
def _():
    r = _client().get(f"/files/{_STATE['pdf_id']}")
    body = r.get_data(as_text=True)
    # Isolate the mobile CTA block and check its <a>.
    import re
    m = re.search(
        r'class="pdf-preview-mobile"(.*?)</div>\s*<style>',
        body, re.DOTALL,
    )
    assert m, "cannot isolate the mobile CTA block"
    mobile_block = m.group(1)
    assert 'target="_blank"' in mobile_block
    assert 'rel="noopener noreferrer"' in mobile_block
    assert f'href="/files/{_STATE["pdf_id"]}/raw"' in mobile_block
    return "mobile CTA is target=_blank + noopener + points at /raw"


@check("5. image files use the <img> path, not the PDF split")
def _():
    img = _upload("photo.png", "image/png",
                    b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    r = _client().get(f"/files/{img.id}")
    body = r.get_data(as_text=True)
    # PDF split shouldn't appear at all for an image
    assert 'class="pdf-preview-desktop"' not in body, \
        "image incorrectly used PDF template"
    assert 'class="pdf-preview-mobile"' not in body, \
        "image incorrectly used mobile CTA"
    # But the plain <img> tag SHOULD appear
    assert f'<img src="/files/{img.id}/raw"' in body, \
        "image tag missing"
    return "image files bypass the PDF split (correctly)"


def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        try:
            _setup()
            for label, fn in CHECKS:
                try:
                    result = fn()
                    print(f"PASS  {label}  ⇒ {result}")
                    passed += 1
                except Exception as e:  # noqa: BLE001
                    print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
                    failed += 1
                    import traceback
                    traceback.print_exc()
        finally:
            try:
                if "company_id" in _STATE:
                    _teardown(_STATE["company_id"])
                    print("\n(cleaned up fixture company)")
            except Exception as e:  # noqa: BLE001
                print(f"\n(teardown failed: {e})")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
