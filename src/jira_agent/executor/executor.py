"""Applies approved, validated actions to Jira and records each one in the audit log.

Safety:
- only actions that passed validation AND were approved are applied
- dry_run=True (the default) describes what would happen and writes nothing
- start_sprint / complete_sprint also need explicit confirmation
- if an action fails, everything that depends on it is skipped
- before/after values are recorded for every change
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from jira_agent.audit.log import AuditLog
from jira_agent.board_config import BoardConfig
from jira_agent.executor.ordering import (
    OrderingError,
    _implicit_dependencies,
    order_actions,
    resolve_ref,
)
from jira_agent.executor.validator import CheckedAction
from jira_agent.jira.adf import to_adf
from jira_agent.jira.client import JiraClient, JiraError
from jira_agent.planner.schema import HIGH_IMPACT, Action

BATCH = 50  # Jira Agile API accepts at most 50 issues per move


class ExecutionError(Exception):
    """An action couldn't be applied. The message is written for humans."""


@dataclass
class ActionOutcome:
    action_id: str
    action_type: str
    target: str | None
    issue_key: str | None
    outcome: str  # applied | failed | skipped | dry-run
    message: str
    error: str | None = None
    url: str | None = None


class Executor:
    def __init__(
        self,
        jira: JiraClient,
        config: BoardConfig,
        audit: AuditLog,
        run_id: int,
        runner: str,
        *,
        dry_run: bool = True,
        confirmed_high_impact: set[str] | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.jira = jira
        self.config = config
        self.audit = audit
        self.run_id = run_id
        self.runner = runner
        self.dry_run = dry_run
        self.confirmed = confirmed_high_impact or set()
        self.log = log or (lambda message: None)
        self.tz = ZoneInfo(config.defaults.timezone)
        self.today = datetime.now(self.tz).date()
        self.created: dict[str, str] = {}          # NEW-n -> real key
        self.created_sprints: dict[str, int] = {}  # sprint name (lower) -> id
        self._sprint_targets: set[str] = set()
        self._working: dict[str, Any] | None | bool = False

    # ---- main entry point ------------------------------------------------

    def run(self, checked: list[CheckedAction], approved_ids: set[str]) -> list[ActionOutcome]:
        outcomes: list[ActionOutcome] = []
        runnable: list[CheckedAction] = []
        failed: set[str] = set()

        self._sprint_targets = {
            c.action.target or "" for c in checked
            if c.action.type in ("add_to_sprint", "move_to_backlog") and c.action.id in approved_ids
        }

        for c in checked:
            if not c.ok:
                failed.add(c.action.id)
                outcomes.append(self._record(c, "blocked", "skipped",
                                             "Blocked by validation: " + "; ".join(c.errors)))
            elif c.action.id not in approved_ids:
                failed.add(c.action.id)
                outcomes.append(self._record(c, "rejected", "skipped", "Not approved."))
            else:
                runnable.append(c)

        by_id = {c.action.id: c for c in runnable}
        all_actions = [c.action for c in checked]
        try:
            ordered = order_actions([c.action for c in runnable])
        except OrderingError as exc:
            for c in runnable:
                outcomes.append(self._record(c, "approved", "failed", str(exc), error=str(exc)))
            return outcomes

        for action in ordered:
            c = by_id[action.id]
            deps = set(action.depends_on) | _implicit_dependencies(action, all_actions)
            bad = sorted(deps & failed)
            if bad:
                failed.add(action.id)
                outcomes.append(self._record(
                    c, "approved", "skipped", f"Skipped because {', '.join(bad)} didn't run."))
                continue

            if action.type in HIGH_IMPACT and action.id not in self.confirmed:
                failed.add(action.id)
                outcomes.append(self._record(
                    c, "approved", "skipped",
                    "High-impact action not confirmed separately, so it was not run."))
                continue

            if self.dry_run:
                if action.type == "create_issue" and action.target:
                    self.created[action.target] = action.target
                if action.type == "create_sprint" and action.sprint:
                    self.created_sprints[action.sprint.lower()] = -1
                outcomes.append(self._record(c, "approved", "dry-run", self.describe(c)))
                continue

            try:
                key, before, after, message = getattr(self, f"_do_{action.type}")(c)
            except (JiraError, OrderingError, ExecutionError) as exc:
                failed.add(action.id)
                outcomes.append(self._record(c, "approved", "failed", f"Failed: {exc}", error=str(exc)))
                continue
            outcomes.append(self._record(
                c, "approved", "applied", message, issue_key=key, before=before, after=after))

        return outcomes

    # ---- recording and descriptions --------------------------------------

    def _record(
        self,
        c: CheckedAction,
        decision: str,
        outcome: str,
        message: str,
        *,
        issue_key: str | None = None,
        error: str | None = None,
        before: Any = None,
        after: Any = None,
    ) -> ActionOutcome:
        a = c.action
        if issue_key is None and a.target:
            try:
                issue_key = resolve_ref(a.target, self.created)
            except OrderingError:
                issue_key = a.target
        self.audit.record_action(
            self.run_id, action_id=a.id, action_type=a.type, target=a.target,
            issue_key=issue_key, decision=decision, outcome=outcome,
            error=error, before=before, after=after,
        )
        self.log(f"{a.id} {outcome}: {message}")
        url = None
        if issue_key and outcome == "applied" and not issue_key.startswith("NEW-"):
            url = f"{self.jira.base_url}/browse/{issue_key}"
        return ActionOutcome(a.id, a.type, a.target, issue_key, outcome, message, error, url)

    def _comment_text(self, a: Action, body: str | None = None) -> str:
        header = self.config.naming.attribution_format.format(
            member=a.on_behalf_of or self.runner,
            runner=self.runner,
            date=self.today.isoformat(),
        )
        return f"{header}\n{body}" if body else header

    def describe(self, c: CheckedAction) -> str:
        """Plain-language description of what an action would do."""
        a, r, t = c.action, c.resolved, c.action.target
        if a.type == "create_issue":
            extra = f", assigned to {r['assignee']}" if r.get("assignee") else ""
            extra += f", under {a.parent}" if a.parent else ""
            return f"Would create {r.get('issue_type', a.issue_type)} '{a.summary}'{extra}"
        if a.type == "update_fields":
            changed = [n for n in ("summary", "description", "story_points", "priority", "labels",
                                   "due_date", "parent") if getattr(a, n) not in (None, [], "")]
            return f"Would update {t}: {', '.join(changed)}"
        if a.type == "transition":
            return f"Would move {t} to {r.get('status', a.status)}"
        if a.type == "comment":
            return f"Would comment on {t}: {(a.comment or '')[:80]}"
        if a.type == "assign":
            return f"Would assign {t} to {r.get('assignee', a.assignee)}"
        if a.type == "link":
            return f"Would link: {t} {r.get('link_phrase', a.link_type)} {a.link_target}"
        if a.type == "add_to_sprint":
            return f"Would add {t} to {r.get('sprint_name', a.sprint)}"
        if a.type == "move_to_backlog":
            return f"Would move {t} to the backlog"
        if a.type == "log_work":
            return f"Would log {a.time_spent} on {t}"
        if a.type == "flag_blocked":
            return f"Would flag {t} as blocked"
        if a.type == "unflag":
            return f"Would clear the blocked flag on {t}"
        if a.type == "create_sprint":
            return f"Would create sprint '{a.sprint}'"
        if a.type == "start_sprint":
            return f"Would START sprint '{r.get('sprint_name', a.sprint)}'"
        if a.type == "complete_sprint":
            return (f"Would COMPLETE sprint '{r.get('sprint_name', a.sprint)}', "
                    f"moving unfinished work to {a.incomplete_to or 'backlog'}")
        if a.type == "set_sprint_goal":
            return f"Would set the goal of '{r.get('sprint_name', a.sprint)}' to: {a.sprint_goal}"
        return f"Would run {a.type} on {t}"

    # ---- helpers ---------------------------------------------------------

    def _key(self, ref: str | None) -> str:
        key = resolve_ref(ref, self.created)
        if not key:
            raise ExecutionError("This action has no target issue.")
        return key

    def _get(self, key: str, fields: list[str]) -> dict[str, Any]:
        data = self.jira.get(f"/rest/api/3/issue/{key}", params={"fields": ",".join(fields)})
        return data.get("fields", {})

    def _dt(self, day: str, hour: int = 9) -> str:
        return datetime.combine(date.fromisoformat(day), time(hour), tzinfo=self.tz).isoformat()

    def _working_sprint(self) -> dict[str, Any] | None:
        if self._working is False:
            sprints = list(self.jira.paginate(
                f"/rest/agile/1.0/board/{self.config.board_id}/sprint",
                params={"state": "active,future"},
            ))
            active = [s for s in sprints if s["state"] == "active"]
            future = [s for s in sprints if s["state"] == "future"]
            self._working = (active or future or [None])[0]
        return self._working  # type: ignore[return-value]

    def _current_sprints(self, key: str) -> list[dict[str, Any]]:
        field = self.config.fields.sprint
        if not field:
            return []
        values = self._get(key, [field]).get(field) or []
        return [{"id": s.get("id"), "name": s.get("name"), "state": s.get("state")}
                for s in values if isinstance(s, dict)]

    def _move_issues(self, path: str, keys: list[str]) -> None:
        for i in range(0, len(keys), BATCH):
            self.jira.post(path, json={"issues": keys[i:i + BATCH]})

    # ---- one method per action type --------------------------------------

    def _do_create_issue(self, c: CheckedAction):
        a, r, cfg = c.action, c.resolved, self.config
        fields: dict[str, Any] = {
            "project": {"key": cfg.project_key},
            "issuetype": {"id": r["issue_type_id"]},
            "summary": a.summary,
        }
        if a.description:
            fields["description"] = to_adf(a.description)
        if r.get("assignee_id"):
            fields["assignee"] = {"accountId": r["assignee_id"]}
        if a.parent:
            fields["parent"] = {"key": self._key(a.parent)}
        if r.get("priority"):
            fields["priority"] = {"name": r["priority"]}
        if a.labels:
            fields["labels"] = a.labels
        if a.due_date:
            fields["duedate"] = a.due_date

        created = self.jira.post("/rest/api/3/issue", json={"fields": fields})
        key = created["key"]
        self.created[a.target or ""] = key
        notes: list[str] = []

        if a.story_points is not None and cfg.fields.story_points:
            try:
                self.jira.put(f"/rest/api/3/issue/{key}",
                              json={"fields": {cfg.fields.story_points: a.story_points}})
            except JiraError as exc:
                notes.append(f"story points not set ({exc})")

        issue_type = cfg.issue_type(r["issue_type"])
        wants_sprint = (
            cfg.defaults.add_new_issues_to_active_sprint
            and issue_type is not None
            and not issue_type.subtask
            and issue_type.hierarchy_level == 0
            and a.target not in self._sprint_targets
        )
        if wants_sprint:
            sprint = self._working_sprint()
            if sprint:
                try:
                    self._move_issues(f"/rest/agile/1.0/sprint/{sprint['id']}/issue", [key])
                    notes.append(f"added to {sprint['name']}")
                except JiraError as exc:
                    notes.append(f"not added to sprint ({exc})")

        message = f"Created {key}: {a.summary}" + (f" ({'; '.join(notes)})" if notes else "")
        return key, None, {"key": key, "summary": a.summary}, message

    def _do_update_fields(self, c: CheckedAction):
        a, r, cfg = c.action, c.resolved, self.config
        key = self._key(a.target)
        changes: dict[str, Any] = {}
        if a.summary:
            changes["summary"] = a.summary
        if a.description:
            changes["description"] = to_adf(a.description)
        if a.story_points is not None and cfg.fields.story_points:
            changes[cfg.fields.story_points] = a.story_points
        if r.get("priority"):
            changes["priority"] = {"name": r["priority"]}
        if a.due_date:
            changes["duedate"] = a.due_date
        if a.parent:
            changes["parent"] = {"key": self._key(a.parent)}

        read_fields = list(changes) + (["labels"] if a.labels else [])
        before = self._get(key, read_fields) if read_fields else {}
        payload: dict[str, Any] = {"fields": changes}
        if a.labels:
            payload["update"] = {"labels": [{"add": label} for label in a.labels]}
        self.jira.put(f"/rest/api/3/issue/{key}", json=payload)
        after = {k: ("(updated)" if k == "description" else v) for k, v in changes.items()}
        if a.labels:
            after["labels_added"] = a.labels
        return key, before, after, f"Updated {key}: {', '.join(after)}"

    def _do_transition(self, c: CheckedAction):
        a, r = c.action, c.resolved
        key = self._key(a.target)
        current = self._get(key, ["status"])["status"]["name"]
        target = r.get("status") or a.status or ""
        if current.lower() == target.lower():
            return key, {"status": current}, {"status": current}, f"{key} was already in {current}"
        options = self.jira.get(f"/rest/api/3/issue/{key}/transitions").get("transitions", [])
        option = next((o for o in options if o.get("to", {}).get("name", "").lower() == target.lower()), None)
        if option is None:
            raise ExecutionError(f"Jira offers no way to move {key} from '{current}' to '{target}'.")
        self.jira.post(f"/rest/api/3/issue/{key}/transitions", json={"transition": {"id": option["id"]}})
        return key, {"status": current}, {"status": target}, f"Moved {key}: {current} -> {target}"

    def _do_comment(self, c: CheckedAction):
        a = c.action
        key = self._key(a.target)
        result = self.jira.post(f"/rest/api/3/issue/{key}/comment",
                                json={"body": to_adf(self._comment_text(a, a.comment))})
        return key, None, {"comment_id": (result or {}).get("id")}, f"Commented on {key}"

    def _do_assign(self, c: CheckedAction):
        a, r = c.action, c.resolved
        key = self._key(a.target)
        old = self._get(key, ["assignee"]).get("assignee") or {}
        self.jira.put(f"/rest/api/3/issue/{key}/assignee", json={"accountId": r["assignee_id"]})
        before = {"assignee_id": old.get("accountId"), "assignee": old.get("displayName")}
        after = {"assignee_id": r["assignee_id"], "assignee": r["assignee"]}
        return key, before, after, f"Assigned {key} to {r['assignee']}"

    def _do_link(self, c: CheckedAction):
        a, r = c.action, c.resolved
        key = self._key(a.target)
        other = self._key(a.link_target)
        # Verified on this site: the "from" issue goes in inwardIssue.
        self.jira.post("/rest/api/3/issueLink", json={
            "type": {"name": r["link_type"]},
            "inwardIssue": {"key": key},
            "outwardIssue": {"key": other},
        })
        after = {"link_type": r["link_type"], "from": key, "to": other}
        return key, None, after, f"Linked: {key} {r['link_phrase']} {other}"

    def _do_add_to_sprint(self, c: CheckedAction):
        a, r = c.action, c.resolved
        key = self._key(a.target)
        sprint_id = r.get("sprint_id") or self.created_sprints.get((r.get("sprint_name") or "").lower())
        if not sprint_id:
            raise ExecutionError(f"Sprint '{a.sprint}' couldn't be found.")
        before = self._current_sprints(key)
        self._move_issues(f"/rest/agile/1.0/sprint/{sprint_id}/issue", [key])
        return key, {"sprints": before}, {"sprint_id": sprint_id, "sprint": r.get("sprint_name")}, \
            f"Added {key} to {r.get('sprint_name')}"

    def _do_move_to_backlog(self, c: CheckedAction):
        key = self._key(c.action.target)
        before = self._current_sprints(key)
        self._move_issues("/rest/agile/1.0/backlog/issue", [key])
        return key, {"sprints": before}, {"sprint": None}, f"Moved {key} to the backlog"

    def _do_log_work(self, c: CheckedAction):
        a = c.action
        key = self._key(a.target)
        body = {"timeSpent": (a.time_spent or "").strip(),
                "comment": to_adf(self._comment_text(a, a.comment))}
        result = self.jira.post(f"/rest/api/3/issue/{key}/worklog", json=body)
        after = {"worklog_id": (result or {}).get("id"), "time_spent": a.time_spent}
        return key, None, after, f"Logged {a.time_spent} on {key}"

    def _do_flag_blocked(self, c: CheckedAction):
        key = self._key(c.action.target)
        field = self.config.fields.flagged or ""
        before = self._get(key, [field]).get(field)
        self.jira.put(f"/rest/api/3/issue/{key}", json={"fields": {field: [{"value": "Impediment"}]}})
        return key, {"flagged": before}, {"flagged": [{"value": "Impediment"}]}, f"Flagged {key} as blocked"

    def _do_unflag(self, c: CheckedAction):
        key = self._key(c.action.target)
        field = self.config.fields.flagged or ""
        before = self._get(key, [field]).get(field)
        self.jira.put(f"/rest/api/3/issue/{key}", json={"fields": {field: None}})
        return key, {"flagged": before}, {"flagged": None}, f"Cleared the blocked flag on {key}"

    def _do_create_sprint(self, c: CheckedAction):
        a = c.action
        body: dict[str, Any] = {"name": a.sprint, "originBoardId": self.config.board_id}
        if a.sprint_goal:
            body["goal"] = a.sprint_goal
        if a.sprint_start:
            body["startDate"] = self._dt(a.sprint_start)
        if a.sprint_end:
            body["endDate"] = self._dt(a.sprint_end, 17)
        result = self.jira.post("/rest/agile/1.0/sprint", json=body)
        self.created_sprints[(a.sprint or "").lower()] = result["id"]
        return None, None, {"sprint_id": result["id"], "name": a.sprint}, f"Created sprint '{a.sprint}'"

    def _do_start_sprint(self, c: CheckedAction):
        a, r = c.action, c.resolved
        sprint_id = r["sprint_id"]
        sprint = self.jira.get(f"/rest/agile/1.0/sprint/{sprint_id}")
        now = datetime.now(self.tz)
        start = self._dt(a.sprint_start) if a.sprint_start else (sprint.get("startDate") or now.isoformat(timespec="seconds"))
        end = self._dt(a.sprint_end, 17) if a.sprint_end else (
            sprint.get("endDate") or (now + timedelta(days=14)).isoformat(timespec="seconds"))
        self.jira.post(f"/rest/agile/1.0/sprint/{sprint_id}",
                       json={"state": "active", "startDate": start, "endDate": end})
        return None, {"state": "future"}, {"state": "active", "start": start, "end": end}, \
            f"Started sprint '{r['sprint_name']}'"

    def _do_complete_sprint(self, c: CheckedAction):
        a, r = c.action, c.resolved
        sprint_id = r["sprint_id"]
        unfinished = [
            i["key"] for i in self.jira.search_jql(
                f"sprint = {sprint_id} AND statusCategory != Done", fields=["summary"])
        ]
        destination = (a.incomplete_to or "backlog").lower()
        moved_to = "backlog"
        if unfinished:
            if destination == "next":
                future = [
                    s for s in self.jira.paginate(
                        f"/rest/agile/1.0/board/{self.config.board_id}/sprint", params={"state": "future"})
                    if s["id"] != sprint_id
                ]
                if not future:
                    raise ExecutionError(
                        "There's no future sprint to move unfinished work into. "
                        "Create one first, or choose the backlog.")
                moved_to = future[0]["name"]
                self._move_issues(f"/rest/agile/1.0/sprint/{future[0]['id']}/issue", unfinished)
            else:
                self._move_issues("/rest/agile/1.0/backlog/issue", unfinished)
        self.jira.post(f"/rest/agile/1.0/sprint/{sprint_id}", json={"state": "closed"})
        after = {"state": "closed", "unfinished_moved": unfinished, "moved_to": moved_to}
        return None, {"state": "active"}, after, \
            f"Completed sprint '{r['sprint_name']}' ({len(unfinished)} unfinished moved to {moved_to})"

    def _do_set_sprint_goal(self, c: CheckedAction):
        a, r = c.action, c.resolved
        sprint_id = r["sprint_id"]
        before = self.jira.get(f"/rest/agile/1.0/sprint/{sprint_id}").get("goal", "")
        self.jira.post(f"/rest/agile/1.0/sprint/{sprint_id}", json={"goal": a.sprint_goal})
        return None, {"goal": before}, {"goal": a.sprint_goal}, f"Set goal of '{r['sprint_name']}'"
