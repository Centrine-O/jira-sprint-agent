"""Checks every planned action against Jira before anything is applied.

Also resolves names into the IDs Jira needs (people -> account IDs, sprint
names -> sprint IDs, issue types -> type IDs). Read-only: nothing is written.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from jira_agent.board_config import BoardConfig
from jira_agent.jira.client import JiraClient, JiraError
from jira_agent.planner.schema import Action, ActionPlan

PLACEHOLDER = re.compile(r"^NEW-\d+$")
TIME_SPENT = re.compile(r"^\s*(\d+(\.\d+)?[wdhm]\s*)+$")
SPRINT_ONLY = {"create_sprint", "start_sprint", "complete_sprint", "set_sprint_goal"}


@dataclass
class CheckedAction:
    action: Action
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    resolved: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


def _is_date(value: str | None) -> bool:
    if not value:
        return True
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


class _Validator:
    def __init__(self, plan: ActionPlan, config: BoardConfig, context: dict[str, Any], jira: JiraClient):
        self.plan = plan
        self.config = config
        self.jira = jira
        self.issues: dict[str, dict[str, Any] | None] = {
            i["key"]: i
            for group in ("sprint_issues", "backlog", "epics")
            for i in context.get(group, [])
        }
        # NEW-n placeholder -> the create_issue action that makes it
        self.placeholders = {
            a.target: a for a in plan.actions if a.type == "create_issue" and a.target
        }
        self.new_sprint_names = {
            a.sprint.lower() for a in plan.actions if a.type == "create_sprint" and a.sprint
        }
        self._sprints: list[dict[str, Any]] | None = None

    # ---- lookups ---------------------------------------------------------

    def issue(self, key: str) -> dict[str, Any] | None:
        """Issue info from the snapshot, or fetched from Jira if not in it."""
        if key not in self.issues:
            try:
                raw = self.jira.get(f"/rest/api/3/issue/{key}", params={"fields": "issuetype,status,summary"})
                f = raw.get("fields", {})
                self.issues[key] = {
                    "key": raw["key"],
                    "type": f.get("issuetype", {}).get("name"),
                    "status": f.get("status", {}).get("name"),
                    "summary": f.get("summary"),
                    "flagged": None,
                }
            except JiraError:
                self.issues[key] = None
        return self.issues[key]

    def issue_type_of(self, ref: str) -> str | None:
        if PLACEHOLDER.match(ref):
            creator = self.placeholders.get(ref)
            return creator.issue_type if creator else None
        info = self.issue(ref)
        return info["type"] if info else None

    def sprints(self) -> list[dict[str, Any]]:
        if self._sprints is None:
            self._sprints = list(self.jira.paginate(
                f"/rest/agile/1.0/board/{self.config.board_id}/sprint",
                params={"state": "active,future"},
            ))
        return self._sprints

    def resolve_sprint(self, name: str) -> dict[str, Any] | None:
        sprints = self.sprints()
        active = [s for s in sprints if s["state"] == "active"]
        future = [s for s in sprints if s["state"] == "future"]
        wanted = name.strip().lower()
        if wanted in ("active", "current", "this sprint"):
            return (active or future or [None])[0]
        if wanted == "next":
            return future[0] if active and future else (future[1] if len(future) > 1 else None)
        return next((s for s in sprints if s["name"].lower() == wanted), None)

    # ---- checks ----------------------------------------------------------

    def check_ref(self, ref: str, label: str, c: CheckedAction) -> bool:
        """An issue key that exists, or a placeholder created in this plan."""
        if PLACEHOLDER.match(ref):
            creator = self.placeholders.get(ref)
            if creator is None:
                c.errors.append(f"{label} {ref} isn't created by any action in this plan.")
                return False
            if creator.id not in c.action.depends_on and creator.id != c.action.id:
                c.warnings.append(f"Will run after {creator.id}, which creates {ref}.")
            return True
        if self.issue(ref) is None:
            c.errors.append(f"{label} {ref} doesn't exist, or your account can't see it.")
            return False
        return True

    def check(self, a: Action) -> CheckedAction:
        c = CheckedAction(action=a)
        cfg = self.config

        if a.type not in SPRINT_ONLY:
            if a.type == "create_issue":
                if not PLACEHOLDER.match(a.target or ""):
                    c.errors.append("A new issue's target must be a placeholder like NEW-1.")
            else:
                self.check_ref(a.target or "", "Issue", c)

        if a.type == "create_issue":
            it = cfg.issue_type(a.issue_type or "")
            if it is None:
                c.errors.append(
                    f"'{a.issue_type}' isn't an issue type in {cfg.project_key}. "
                    f"Valid: {', '.join(t.name for t in cfg.issue_types)}"
                )
            else:
                c.resolved["issue_type_id"] = it.id
                c.resolved["issue_type"] = it.name
                if it.subtask and not a.parent:
                    c.errors.append("A sub-task must have a parent issue.")

        if a.parent:
            self.check_ref(a.parent, "Parent", c)

        if a.assignee:
            member = cfg.find_member(a.assignee)
            if member is None:
                c.errors.append(f"'{a.assignee}' isn't a team member in the config.")
            else:
                c.resolved["assignee_id"] = member.account_id
                c.resolved["assignee"] = member.name

        if a.type == "transition" and a.status:
            issue_type = self.issue_type_of(a.target or "")
            valid = cfg.status_names(issue_type) if issue_type else []
            match = next((s for s in valid if s.lower() == a.status.lower()), None)
            if issue_type and match is None:
                c.errors.append(f"'{a.status}' isn't a status for {issue_type}. Valid: {', '.join(valid)}")
            elif match:
                c.resolved["status"] = match
                info = self.issues.get(a.target or "")
                if info and (info.get("status") or "").lower() == match.lower():
                    c.errors.append(f"{a.target} is already in '{match}'. Nothing to do.")

        if a.priority:
            match = next((p for p in cfg.priorities if p.lower() == a.priority.lower()), None)
            if match is None:
                c.errors.append(f"'{a.priority}' isn't a priority. Valid: {', '.join(cfg.priorities)}")
            else:
                c.resolved["priority"] = match

        if a.type == "link":
            lt = next((l for l in cfg.link_types if l.name.lower() == (a.link_type or "").lower()), None)
            if lt is None:
                c.errors.append(
                    f"'{a.link_type}' isn't a link type. Valid: {', '.join(l.name for l in cfg.link_types)}"
                )
            else:
                c.resolved["link_type"] = lt.name
                c.resolved["link_phrase"] = lt.outward
            self.check_ref(a.link_target or "", "Linked issue", c)

        if a.type in ("add_to_sprint", "start_sprint", "complete_sprint", "set_sprint_goal"):
            sprint = self.resolve_sprint(a.sprint or "")
            if sprint is None:
                if a.type == "add_to_sprint" and (a.sprint or "").lower() in self.new_sprint_names:
                    c.resolved["sprint_name"] = a.sprint
                    c.resolved["sprint_created_in_plan"] = True
                else:
                    names = ", ".join(f"{s['name']} ({s['state']})" for s in self.sprints()) or "none"
                    c.errors.append(f"Sprint '{a.sprint}' wasn't found. Open sprints: {names}")
            else:
                c.resolved["sprint_id"] = sprint["id"]
                c.resolved["sprint_name"] = sprint["name"]
                c.resolved["sprint_state"] = sprint["state"]
                if a.type == "start_sprint" and sprint["state"] != "future":
                    c.errors.append(f"'{sprint['name']}' is already {sprint['state']}; only future sprints can be started.")
                if a.type == "complete_sprint" and sprint["state"] != "active":
                    c.errors.append(f"'{sprint['name']}' isn't active, so it can't be completed.")

        if a.type in ("start_sprint", "complete_sprint") and not cfg.safety.allow_sprint_start_complete:
            c.errors.append("Starting/completing sprints is turned off in the config (safety settings).")

        if a.type == "complete_sprint" and a.incomplete_to and a.incomplete_to.lower() not in ("next", "backlog"):
            c.errors.append("incomplete_to must be 'next' or 'backlog'.")

        if a.type == "create_sprint":
            if any(s["name"].lower() == (a.sprint or "").lower() for s in self.sprints()):
                c.errors.append(f"A sprint named '{a.sprint}' already exists.")

        if a.type == "log_work" and not TIME_SPENT.match(a.time_spent or ""):
            c.errors.append(f"'{a.time_spent}' isn't a valid time. Use Jira format like 3h, 1d 2h, 30m.")

        if a.type in ("flag_blocked", "unflag") and not cfg.fields.flagged:
            c.errors.append("This Jira site has no 'Flagged' field, so issues can't be flagged.")

        if a.story_points is not None:
            if not cfg.fields.story_points:
                c.errors.append("No story points field was found for this board.")
            elif a.story_points < 0:
                c.errors.append("Story points can't be negative.")

        for label, value in (("due_date", a.due_date), ("sprint_start", a.sprint_start), ("sprint_end", a.sprint_end)):
            if not _is_date(value):
                c.errors.append(f"{label} '{value}' isn't a date in YYYY-MM-DD format.")

        if a.type == "update_fields" and not any(
            [a.summary, a.story_points is not None, a.priority, a.labels, a.due_date, a.parent, a.description]
        ):
            c.errors.append("update_fields doesn't change anything.")

        return c


def validate_plan(
    plan: ActionPlan,
    config: BoardConfig,
    context: dict[str, Any],
    jira: JiraClient,
) -> list[CheckedAction]:
    """Check every action. Returns one CheckedAction per action, in plan order."""
    validator = _Validator(plan, config, context, jira)
    checked = [validator.check(action) for action in plan.actions]

    # An action that depends on an invalid action can't run either.
    failed = {c.action.id for c in checked if not c.ok}
    changed = True
    while changed:
        changed = False
        for c in checked:
            if c.ok and any(dep in failed for dep in c.action.depends_on):
                c.errors.append("Depends on an action that can't be applied.")
                failed.add(c.action.id)
                changed = True
    return checked