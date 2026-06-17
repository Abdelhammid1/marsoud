#!/usr/bin/env bash
# MARSOUD — production deploy helper.
#
# Run this on the server after pushing to main.
# Safe to re-run; each step is idempotent.
#
# Required environment:
#   - FLASK_APP            (defaults to flask_app.py)
#   - SERVICE_NAME         (defaults to marsoud — used by systemctl restart)
#   - PYTHON               (defaults to .venv/bin/python — set if you use a
#                           different venv layout)
#
# Optional:
#   - DEPLOY_SKIP_PIP=1    skip the pip install step even when requirements
#                          changed (useful when iterating on Python deps locally)
#   - DEPLOY_NO_RESTART=1  skip the systemctl restart at the end

set -u   # trap unset vars; DO NOT use `set -e` — we want to keep going if a
         # non-critical step fails (per MARSOUD-54.1: "ما توقفش الـ deploy
         # لو الـ install فشل جزئياً — تطبع تحذير وتكمّل")

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR" || { echo "❌ cannot cd to $REPO_DIR"; exit 1; }

PYTHON="${PYTHON:-.venv/bin/python}"
PIP="${PIP:-.venv/bin/pip}"
FLASK_APP="${FLASK_APP:-flask_app.py}"
SERVICE_NAME="${SERVICE_NAME:-accountant}"

# Cache the requirements hash before pulling so we can detect if it changed.
HASH_BEFORE=""
if [[ -f requirements.txt ]]; then
  HASH_BEFORE="$(shasum requirements.txt 2>/dev/null | awk '{print $1}')"
fi

# ── 1. git pull main ──────────────────────────────────────────────────
echo "▶ Pulling latest from origin/main…"
if ! git pull origin main; then
  echo "⚠ git pull failed — deploy aborting"
  exit 1
fi

# ── 2. Conditional pip install ────────────────────────────────────────
#    MARSOUD-54.1: only when requirements.txt changed, so daily deploys
#    aren't slowed by a no-op install.
HASH_AFTER=""
if [[ -f requirements.txt ]]; then
  HASH_AFTER="$(shasum requirements.txt 2>/dev/null | awk '{print $1}')"
fi

if [[ "${DEPLOY_SKIP_PIP:-0}" == "1" ]]; then
  echo "⏭  DEPLOY_SKIP_PIP=1 — skipping pip install"
elif [[ -n "$HASH_BEFORE" && "$HASH_BEFORE" == "$HASH_AFTER" ]]; then
  echo "✓ requirements.txt unchanged — skipping pip install"
else
  echo "▶ requirements.txt changed (or first run) — running pip install…"
  if ! "$PIP" install -r requirements.txt; then
    echo "⚠ pip install reported a non-zero exit — continuing anyway."
    echo "   Check the log above. Common cause: WeasyPrint missing system libs."
    echo "   On Debian/Ubuntu, run:"
    echo "     sudo apt install -y libpango-1.0-0 libpangoft2-1.0-0 libcairo2"
  else
    echo "✓ pip install OK"
  fi
fi

# ── 3. Apply DB migrations ───────────────────────────────────────────
echo "▶ Running flask db upgrade…"
if ! FLASK_APP="$FLASK_APP" "$PYTHON" -m flask db upgrade; then
  echo "❌ flask db upgrade failed — refusing to restart the app with stale schema"
  exit 1
fi
echo "✓ migrations OK"

# ── 4. Restart the service ───────────────────────────────────────────
if [[ "${DEPLOY_NO_RESTART:-0}" == "1" ]]; then
  echo "⏭  DEPLOY_NO_RESTART=1 — skipping service restart"
else
  echo "▶ Restarting $SERVICE_NAME…"
  if ! sudo systemctl restart "$SERVICE_NAME"; then
    echo "⚠ systemctl restart $SERVICE_NAME failed — restart manually."
  else
    echo "✓ $SERVICE_NAME restarted"
  fi
fi

echo
echo "✅ Deploy complete."
echo "   Latest commit: $(git log -1 --oneline)"
