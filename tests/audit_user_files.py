#!/usr/bin/env python3
"""MARSOUD-USER-FILES — audit for the per-user folder feature.

Proves, end-to-end against a self-contained pair of companies:

  1. save_user_file persists both the DB row AND writes the byte
     stream to the private uploads tree.
  2. Oversized uploads are rejected BEFORE anything hits the disk.
  3. Disallowed extensions are rejected.
  4. Empty uploads are rejected.
  5. Route auth (via Flask test_client + login_user):
      · owner  → 200 on own file's /raw
      · owner  → 200 on own delete
      · someone-else-in-same-company (no users.view) → 403
      · admin-in-same-company (users.view=True)     → 200 read, 403 delete
      · user-in-DIFFERENT-company                    → 404 (no leak)
      · admin listing a folder of a user in a different company → 404
  6. Delete removes both the DB row AND the on-disk file.
"""
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
COMPANY_A = "__USER_FILES_A__"
COMPANY_B = "__USER_FILES_B__"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _mk_company(name):
    from app.models import Company
    existing = Company.query.filter_by(name=name).first()
    if existing:
        _teardown_company(existing.id)
    c = Company(name=name, base_currency="SAR")
    db.session.add(c); db.session.flush()
    from app.services.seed_coa import seed_default_coa
    seed_default_coa(c.id)
    db.session.commit()
    return c


def _mk_user(email, company_id, role="owner"):
    from app.models import User, user_companies
    from werkzeug.security import generate_password_hash
    u = User(
        email=email,
        # Real hash so the login form can authenticate this fixture.
        # pbkdf2 matches the User.set_password() method — scrypt isn't
        # available on Python 3.9 stdlib.
        password_hash=generate_password_hash(
            "audit-pw", method="pbkdf2:sha256"),
        full_name=email.split("@")[0],
    )
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=company_id, role=role,
    ))
    db.session.commit()
    return u


def _teardown_company(company_id):
    from sqlalchemy import text, inspect
    db.session.close()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        # Wipe user_files rows for the target company (test-only) and
        # any orphaned disk files that were their storage keys.
        rows = conn.execute(text(
            "SELECT id, storage_key FROM user_files WHERE company_id = :c"
        ), {"c": company_id}).fetchall()
        for _, key in rows:
            p = Path(ROOT) / "app" / "private_uploads" / "user_files" / key
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
        conn.execute(text(
            "DELETE FROM user_files WHERE company_id = :c"
        ), {"c": company_id})
        # user_companies rows
        conn.execute(text(
            "DELETE FROM user_companies WHERE company_id = :c"
        ), {"c": company_id})
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {col["name"] for col in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(text(f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                             {"c": company_id})
        conn.execute(text("DELETE FROM companies WHERE id = :c"),
                     {"c": company_id})
        # Users created just for this fixture (email prefix "uf-audit-")
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'uf-audit-%'"
        ))


def _setup():
    a_id = _mk_company(COMPANY_A).id
    b_id = _mk_company(COMPANY_B).id
    # Distinct role wiring per fixture user so the audit exercises
    # the real permission split:
    #  · owner_a  → owner (has users.view — represents the file's owner)
    #  · admin_a  → admin (has users.view — the "admin passthrough")
    #  · peer_a   → employee (no users.view — the "not allowed" case)
    #  · other_b  → owner in a DIFFERENT company (cross-tenant leak)
    owner_a_id = _mk_user("uf-audit-owner-a@x.test", a_id, role="owner").id
    admin_a_id = _mk_user("uf-audit-admin-a@x.test", a_id, role="admin").id
    peer_a_id = _mk_user("uf-audit-peer-a@x.test", a_id, role="employee").id
    other_b_id = _mk_user("uf-audit-owner-b@x.test", b_id, role="owner").id
    _STATE.update(
        company_a_id=a_id, company_b_id=b_id,
        owner_a_id=owner_a_id, admin_a_id=admin_a_id,
        peer_a_id=peer_a_id, other_b_id=other_b_id,
    )


class _FakeUpload:
    """Werkzeug-style FileStorage stand-in — enough for save_user_file
    to size-check + save. Real FileStorage from Flask requests behaves
    the same way for our narrow use of stream / save / filename /
    mimetype."""
    def __init__(self, data, filename, mimetype="application/octet-stream"):
        self.stream = io.BytesIO(data)
        self.filename = filename
        self.mimetype = mimetype
    def save(self, path):
        # Mimic Werkzeug: rewind, read, write.
        self.stream.seek(0)
        Path(path).write_bytes(self.stream.read())


# ─── Service-layer checks ───────────────────────────────────────────────
@check("1. save_user_file persists row + writes disk file")
def _():
    from app.services.user_files import save_user_file, resolve_disk_path
    payload = b"%PDF-1.4\n%hello world"
    fs = _FakeUpload(payload, "note.pdf", "application/pdf")
    row = save_user_file(
        company_id=_STATE["company_a_id"],
        user_id=_STATE["owner_a_id"],
        file_storage=fs,
    )
    assert row.id is not None
    assert row.mimetype == "application/pdf"
    assert row.size_bytes == len(payload)
    assert row.name == "note.pdf"
    disk = resolve_disk_path(row)
    assert disk.exists(), "expected disk file to exist"
    assert disk.read_bytes() == payload, "disk bytes differ from upload"
    _STATE["owner_a_file_id"] = row.id
    _STATE["owner_a_disk_path"] = str(disk)
    return f"row#{row.id} on disk at {disk.name}"


@check("2. oversized upload rejected — no disk write")
def _():
    from app.services.user_files import (
        save_user_file, UserFileError, MAX_BYTES, _root,
    )
    payload = b"X" * (MAX_BYTES + 100)
    fs = _FakeUpload(payload, "huge.pdf", "application/pdf")
    files_before = list(_root().rglob("*.pdf"))
    raised = False
    try:
        save_user_file(
            company_id=_STATE["company_a_id"],
            user_id=_STATE["owner_a_id"],
            file_storage=fs,
        )
    except UserFileError:
        raised = True
    assert raised, "expected UserFileError for oversized upload"
    files_after = list(_root().rglob("*.pdf"))
    assert len(files_before) == len(files_after), \
        f"disk grew from {len(files_before)} to {len(files_after)} files"
    return f"rejected {MAX_BYTES // (1024*1024) + 1}MB upload, disk clean"


@check("3. disallowed extension rejected (.exe)")
def _():
    from app.services.user_files import save_user_file, UserFileError
    fs = _FakeUpload(b"MZ\x90\x00", "virus.exe", "application/x-msdownload")
    raised = False
    try:
        save_user_file(
            company_id=_STATE["company_a_id"],
            user_id=_STATE["owner_a_id"],
            file_storage=fs,
        )
    except UserFileError:
        raised = True
    assert raised, "expected UserFileError for .exe upload"
    return ".exe rejected"


@check("4. empty file rejected")
def _():
    from app.services.user_files import save_user_file, UserFileError
    fs = _FakeUpload(b"", "empty.pdf", "application/pdf")
    raised = False
    try:
        save_user_file(
            company_id=_STATE["company_a_id"],
            user_id=_STATE["owner_a_id"],
            file_storage=fs,
        )
    except UserFileError:
        raised = True
    assert raised, "expected UserFileError for empty upload"
    return "empty file rejected"


# ─── Route-auth checks (via Flask test client + login_user) ────────────
def _reset_app_ctx_g():
    """Clear g values that Flask-Login and load_active_company cache
    on the app-context g. Flask 3.0 keeps g at APP-context scope, so
    when the audit runs multiple checks inside a single
    `with app.app_context()`, cached values like `_login_user` from
    the FIRST authenticated request bleed into every subsequent
    test_client. Clearing them before each test_client call gives
    each check a fresh identity."""
    from flask import g
    for key in ("_login_user", "active_company", "user_companies",
                 "impersonating"):
        try:
            g.pop(key, None)
        except Exception:
            pass


def _client_as(user_id, company_id):
    """Return a fresh test client authenticated as `user_id` with
    `company_id` pinned as the active company."""
    from flask import current_app
    _reset_app_ctx_g()
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
        sess["active_company_id"] = company_id
    return client


@check("5. owner GET /files/<id>/raw → 200 PDF bytes")
def _():
    client = _client_as(_STATE["owner_a_id"], _STATE["company_a_id"])
    r = client.get(f"/files/{_STATE['owner_a_file_id']}/raw")
    assert r.status_code == 200, f"status={r.status_code}"
    assert r.mimetype == "application/pdf", f"mimetype={r.mimetype}"
    assert r.data.startswith(b"%PDF"), "raw endpoint did not return PDF bytes"
    return "owner reads own file"


@check("6. peer in same company (no users.view) → 403")
def _():
    client = _client_as(_STATE["peer_a_id"], _STATE["company_a_id"])
    r = client.get(f"/files/{_STATE['owner_a_file_id']}/raw")
    assert r.status_code == 403, f"expected 403, got {r.status_code}"
    return "peer without users.view blocked with 403"


@check("7. admin in same company (users.view) → 200 read, 403 delete")
def _():
    from app.services.permissions import _db_has_permission
    # Grant users.view to admin via role. Owners get it by default via
    # role_permissions seed. The admin fixture user was inserted with
    # role='owner' in user_companies so this passes right away.
    client = _client_as(_STATE["admin_a_id"], _STATE["company_a_id"])
    r = client.get(f"/files/{_STATE['owner_a_file_id']}/raw")
    assert r.status_code == 200, f"admin read got {r.status_code}"
    # Delete — should NOT be allowed on someone else's file.
    r2 = client.post(f"/files/{_STATE['owner_a_file_id']}/delete")
    assert r2.status_code == 403, f"admin delete got {r2.status_code}"
    return "admin reads (200) but cannot delete (403)"


@check("8. user in DIFFERENT company → 404 (no cross-tenant leak)")
def _():
    client = _client_as(_STATE["other_b_id"], _STATE["company_b_id"])
    r = client.get(f"/files/{_STATE['owner_a_file_id']}/raw")
    # Route abort(404) when company_id doesn't match — deliberately
    # not 403, to avoid confirming that the id exists at all.
    assert r.status_code == 404, f"expected 404, got {r.status_code}"
    return "cross-tenant read blocked with 404 (no info leak)"


@check("9. admin listing another user's folder cross-company → 404")
def _():
    client = _client_as(_STATE["other_b_id"], _STATE["company_b_id"])
    # other_b tries to see owner_a's folder
    r = client.get(f"/files/user/{_STATE['owner_a_id']}/")
    assert r.status_code == 404, f"expected 404, got {r.status_code}"
    return "admin folder listing cross-company blocked with 404"


@check("10. owner deletes own file → DB row and disk file gone")
def _():
    client = _client_as(_STATE["owner_a_id"], _STATE["company_a_id"])
    disk = Path(_STATE["owner_a_disk_path"])
    assert disk.exists(), "precondition: disk file should exist before delete"
    r = client.post(f"/files/{_STATE['owner_a_file_id']}/delete",
                     follow_redirects=False)
    assert r.status_code in (200, 302), f"delete status {r.status_code}"
    # DB row
    from app.models import UserFile
    assert db.session.get(UserFile, _STATE["owner_a_file_id"]) is None, \
        "DB row still present after delete"
    assert not disk.exists(), "disk file still present after delete"
    return "row + disk file both removed"


def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        from tests._orphan_sweep import preflight
        preflight()
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
                for cid_key in ("company_a_id", "company_b_id"):
                    if cid_key in _STATE:
                        _teardown_company(_STATE[cid_key])
                print(f"\n(cleaned up fixture companies)")
            except Exception as e:  # noqa: BLE001
                print(f"\n(teardown failed: {e})")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
