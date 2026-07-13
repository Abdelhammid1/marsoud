#!/usr/bin/env python3
"""MARSOUD-FORM-ONCE — audit for the one-shot submit guard that
prevents comment duplication (and any other data-once form's
double-post) when the user clicks the submit button twice.

The guard is a small JS handler in base.html watching every form
submit event. Behavior:
  · On first submit, disables every submit button in the form and
    swaps its label for "جاري الإرسال...".
  · A second submit is silently prevented (event.preventDefault).

Coverage:
  1. base.html carries the MARSOUD-FORM-ONCE handler.
  2. Comment forms on /tasks/<id> and /leads/<id> carry the
     data-once attribute so the guard activates for them.
  3. The label swap uses Arabic "جاري الإرسال..." (per convention).
  4. The guard checks form.matches('form[data-once]') so unmarked
     forms are untouched.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


_BASE = (ROOT / "app" / "templates" / "base.html").read_text(encoding="utf-8")
_TASKS = (ROOT / "app" / "templates" / "tasks" / "detail.html").read_text(encoding="utf-8")
_LEADS = (ROOT / "app" / "templates" / "leads" / "detail.html").read_text(encoding="utf-8")


@check("1. base.html carries the MARSOUD-FORM-ONCE handler")
def _():
    assert "MARSOUD-FORM-ONCE" in _BASE
    # Must react to the submit event
    assert "addEventListener('submit'" in _BASE
    return "handler present"


@check("2. handler guards on form[data-once] selector")
def _():
    assert "form[data-once]" in _BASE, (
        "handler must scope by data-once so unmarked forms don't lose"
        "their submit behavior"
    )
    return "selector scoped"


@check("3. handler calls preventDefault on the second submit")
def _():
    assert "preventDefault" in _BASE
    # A flag or attribute must gate the second call
    assert "_submitted" in _BASE
    return "second submit blocked"


@check("4. handler disables submit buttons + swaps label to Arabic")
def _():
    assert "btn.disabled = true" in _BASE
    assert "جاري الإرسال" in _BASE
    return "buttons disabled + label swapped"


@check("5. task-detail comment form carries data-once")
def _():
    # The form must have data-once on its opening tag
    assert 'data-once' in _TASKS
    # And it must be on the comment-add form specifically
    idx = _TASKS.find("tasks.comment_add")
    # Search around that area for data-once
    window_ = _TASKS[max(0, idx - 300):idx + 200]
    assert "data-once" in window_, (
        "data-once must be on the comment-add form, not just somewhere "
        "else in the template"
    )
    return "task comment form guarded"


@check("6. lead-detail comment form carries data-once")
def _():
    assert 'data-once' in _LEADS
    idx = _LEADS.find("leads.comment_add")
    window_ = _LEADS[max(0, idx - 300):idx + 200]
    assert "data-once" in window_
    return "lead comment form guarded"


# MARSOUD-FORM-ONCE extension (Abdelhamid 2026-07-13) — the new-task
# form gets the same duplicate-submit guard as the comment button,
# but with a customised in-flight label ("جاري الإنشاء...") that
# fits the create action semantically. Verify both the form
# annotation and the handler's support for per-form labels.
_TASK_FORM = (ROOT / "app" / "templates" / "tasks" / "form.html").read_text(
    encoding="utf-8")


@check("7. handler supports per-form data-once-label override")
def _():
    assert "data-once-label" in _BASE or "dataset.onceLabel" in _BASE, (
        "handler must read a per-form label attribute so create forms "
        "can show 'جاري الإنشاء...' instead of the default"
    )
    assert "جاري الإنشاء" in _BASE, \
        "handler docstring should mention the create-label example"
    return "custom label mechanism present"


@check("8. new-task form carries data-once + 'جاري الإنشاء...' label")
def _():
    assert "data-once" in _TASK_FORM, \
        "tasks/form.html must carry data-once to guard fast double-clicks"
    assert 'data-once-label="جاري الإنشاء' in _TASK_FORM, (
        "tasks/form.html should override the busy label to 'جاري الإنشاء...'"
    )
    return "new-task form guarded with create-label"


def main():
    passed = failed = 0
    for label, fn in CHECKS:
        try:
            result = fn()
            print(f"PASS  {label}  ⇒ {result}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
            failed += 1
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
