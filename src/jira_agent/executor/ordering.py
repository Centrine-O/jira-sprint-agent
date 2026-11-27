"""Puts approved actions into a safe order and resolves placeholders.

Dependencies come from two places:
- explicit: each action's `depends_on` list
- implicit: worked out here, so a missed depends_on can't break a run:
    * an action that mentions NEW-n (as target, parent or link_target)
      runs after the create_issue that makes NEW-n
    * add_to_sprint into a sprint created in this plan runs after create_sprint
Otherwise the original plan order is kept.
"""

from __future__ import annotations

import re

from jira_agent.planner.schema import Action

PLACEHOLDER = re.compile(r"^NEW-\d+$")


class OrderingError(Exception):
    """The actions can't be ordered (e.g. they depend on each other in a loop)."""


def _implicit_dependencies(action: Action, actions: list[Action]) -> set[str]:
    deps: set[str] = set()
    creators = {a.target: a.id for a in actions if a.type == "create_issue" and a.target}
    for ref in (action.target, action.parent, action.link_target):
        if ref and PLACEHOLDER.match(ref) and ref in creators and creators[ref] != action.id:
            deps.add(creators[ref])
    if action.type == "add_to_sprint" and action.sprint:
        for other in actions:
            if (
                other.type == "create_sprint"
                and other.sprint
                and other.sprint.lower() == action.sprint.lower()
            ):
                deps.add(other.id)
    return deps


def order_actions(actions: list[Action]) -> list[Action]:
    """Return the actions in an order where every dependency runs first.

    Dependencies on actions not in `actions` (e.g. rejected ones) are ignored here;
    the validator already blocks actions whose dependencies can't run.
    """
    ids = {a.id for a in actions}
    position = {a.id: i for i, a in enumerate(actions)}
    deps = {
        a.id: ({d for d in a.depends_on if d in ids} | _implicit_dependencies(a, actions)) & ids
        for a in actions
    }

    ordered: list[Action] = []
    done: set[str] = set()
    remaining = list(actions)
    while remaining:
        ready = [a for a in remaining if deps[a.id] <= done]
        if not ready:
            stuck = ", ".join(a.id for a in remaining)
            raise OrderingError(f"These actions depend on each other in a loop: {stuck}")
        next_action = min(ready, key=lambda a: position[a.id])
        ordered.append(next_action)
        done.add(next_action.id)
        remaining.remove(next_action)
    return ordered


def resolve_ref(ref: str | None, created: dict[str, str]) -> str | None:
    """Swap a placeholder like NEW-1 for the real key once it has been created."""
    if ref and PLACEHOLDER.match(ref):
        if ref not in created:
            raise OrderingError(f"{ref} hasn't been created yet.")
        return created[ref]
    return ref
