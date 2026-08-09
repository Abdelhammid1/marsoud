"""MARSOUD-PARENT-CHILD-TASK-HIERARCHY (2026-08-09) — walkers +
validation for the Task self-FK.

Every writer that sets parent_task_id must go through
validate_parent() so the invariants land in ONE place:

  1. same-company (no cross-tenant leak)
  2. no self-loop
  3. no cycle (parent must not be a descendant of the task)
  4. resolved parent must exist

The descendant walker mirrors routes/accounts.py::_descendants
(the only self-FK hierarchy pattern already in the codebase). See
that file's edit() route (lines ~179-231) for the same
picker-filter + POST re-validation flow.
"""


# Runaway guard. Real task trees in practice are 3-5 deep; 500 is
# a very generous ceiling that still refuses to walk forever on a
# corrupted cycle (belt against a same-branch UPDATE that would
# create one).
_MAX_DEPTH = 500


class TaskHierarchyError(Exception):
    """User-facing hierarchy violation. Message is Arabic so the
    route can flash it directly without extra formatting."""


def descendant_ids(task):
    """Iterative DFS starting at task.subtasks. Returns a set of
    task ids that appear anywhere in the subtree below `task`.
    Never includes task's own id. Caps at _MAX_DEPTH nodes to
    survive a corrupted cycle."""
    seen = set()
    stack = list(task.subtasks or [])
    while stack and len(seen) < _MAX_DEPTH:
        node = stack.pop()
        if node.id in seen:
            continue
        seen.add(node.id)
        stack.extend(node.subtasks or [])
    return seen


def ancestors(task):
    """Root-first list of ancestors (excludes `task` itself).
    Caps at _MAX_DEPTH.  Returns [] for a root task."""
    out = []
    cur = task.parent
    depth = 0
    while cur is not None and depth < _MAX_DEPTH:
        out.append(cur)
        cur = cur.parent
        depth += 1
    out.reverse()
    return out


def breadcrumb(task):
    """[{id, title}, ...] from root ancestor down to and
    INCLUDING task itself.

    Used by templates/tasks/detail.html:

        {% for crumb in breadcrumb[:-1] %}<a>{{ crumb.title }}</a>
        {% endfor %}<span>{{ breadcrumb[-1].title }}</span>
    """
    return ([{"id": a.id, "title": a.title} for a in ancestors(task)]
            + [{"id": task.id, "title": task.title}])


def available_parents_for(task_or_none, company_id, user_id,
                            full_visibility, pm_project_ids=None):
    """Tasks the user may pick as parent, excluding self +
    descendants of `task_or_none` (pass None on task-creation).

    Uses the existing visible_tasks_query so the picker never
    leaks tasks the user can't otherwise see. Archived tasks are
    excluded (a parent that isn't on the board would confuse the
    breadcrumb)."""
    from app.services.tasks_extras import visible_tasks_query
    from app.models import Task
    q = (visible_tasks_query(company_id, user_id,
                              full_visibility, pm_project_ids)
         .filter(Task.archived_at.is_(None)))
    if task_or_none is None:
        return q.order_by(Task.title).all()
    excluded = descendant_ids(task_or_none) | {task_or_none.id}
    if not excluded:
        return q.order_by(Task.title).all()
    return (q.filter(~Task.id.in_(excluded))
             .order_by(Task.title).all())


def validate_parent(task, new_parent_id):
    """Raises TaskHierarchyError on any invariant break. Returns
    the resolved Parent (or None) on success.

    Callers MUST go through this before writing parent_task_id:

        parent = validate_parent(task, request.form.get("parent_task_id"))
        task.parent_task_id = parent.id if parent else None

    Cases:
      · empty / None / "" / whitespace → returns None (unset parent)
      · non-integer → "معرّف المهمة الأب غير صالح"
      · unknown id → "لا توجد مهمة بالمعرّف المحدد"
      · different company → "المهمة الأب في شركة مختلفة"
      · self (only when task.id is set) → "لا يمكن جعل المهمة تابعة لنفسها"
      · descendant (only when task.id is set) → "حلقة دائرية"
    """
    from app import db
    from app.models import Task

    # Empty / whitespace → unset.
    if new_parent_id is None:
        return None
    if isinstance(new_parent_id, str):
        stripped = new_parent_id.strip()
        if not stripped:
            return None
        new_parent_id = stripped

    try:
        pid = int(new_parent_id)
    except (TypeError, ValueError):
        raise TaskHierarchyError("معرّف المهمة الأب غير صالح")

    parent = db.session.get(Task, pid)
    if parent is None:
        raise TaskHierarchyError("لا توجد مهمة بالمعرّف المحدد")

    # Cross-tenant guard — the picker never shows a task from
    # another company, but a crafted POST could try. Refuse.
    if parent.company_id != task.company_id:
        raise TaskHierarchyError("المهمة الأب في شركة مختلفة")

    # Self + descendant checks only make sense once the task has
    # been persisted (has an id + subtasks relationship). At
    # creation, task.id is None and this section is a no-op —
    # which is correct: a task being created can't have
    # descendants yet.
    if task.id is not None:
        if parent.id == task.id:
            raise TaskHierarchyError(
                "لا يمكن جعل المهمة تابعة لنفسها")
        if parent.id in descendant_ids(task):
            raise TaskHierarchyError(
                "لا يمكن اختيار مهمة تابعة كأب (حلقة دائرية)")

    return parent
