"""Builds a snapshot of the board's current state for Claude to reason over.

Read-only. Deliberately excludes issue descriptions and comments: they aren't
needed to match updates to issues, they make requests bigger, and text inside
Jira must never be treated as instructions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from itertools import islice
from typing import Any

from jira_agent.board_config import BoardConfig
from jira_agent.jira.client import JiraClient


def _issue_fields(config: BoardConfig) -> list[str]:
    fields = [
        "summary", "issuetype", "status", "assignee", "parent",
        "priority", "labels", "updated", "issuelinks", "subtasks",
    ]
    for extra in (config.fields.story_points, config.fields.sprint, config.fields.flagged):
        if extra:
            fields.append(extra)
    return fields


def _simplify_issue(raw: dict[str, Any], config: BoardConfig) -> dict[str, Any]:
    """Turn Jira's large issue JSON into the small shape the planner needs."""
    f = raw.get("fields", {})
    assignee = f.get("assignee") or {}
    parent = f.get("parent") or {}
    status = f.get("status") or {}

    links = []
    for link in f.get("issuelinks", []) or []:
        link_type = link.get("type", {})
        if "outwardIssue" in link:
            links.append({"type": link_type.get("name"),
                          "relation": link_type.get("outward"),
                          "issue": link["outwardIssue"].get("key")})
        elif "inwardIssue" in link:
            links.append({"type": link_type.get("name"),
                          "relation": link_type.get("inward"),
                          "issue": link["inwardIssue"].get("key")})

    sprints = []
    if config.fields.sprint:
        for s in f.get(config.fields.sprint) or []:
            if isinstance(s, dict):
                sprints.append({"id": s.get("id"), "name": s.get("name"), "state": s.get("state")})

    flagged = bool(f.get(config.fields.flagged)) if config.fields.flagged else False

    return {
        "key": raw.get("key"),
        "summary": f.get("summary", ""),
        "type": (f.get("issuetype") or {}).get("name"),
        "status": status.get("name"),
        "status_category": (status.get("statusCategory") or {}).get("key"),
        "assignee": assignee.get("displayName"),
        "assignee_id": assignee.get("accountId"),
        "parent": parent.get("key"),
        "parent_summary": (parent.get("fields") or {}).get("summary"),
        "priority": (f.get("priority") or {}).get("name"),
        "labels": f.get("labels", []),
        "story_points": f.get(config.fields.story_points) if config.fields.story_points else None,
        "flagged": flagged,
        "sprints": sprints,
        "subtasks": [st.get("key") for st in f.get("subtasks", []) or []],
        "links": links,
        "updated": f.get("updated"),
    }


def _search(jira: JiraClient, config: BoardConfig, jql: str, limit: int | None = None) -> list[dict[str, Any]]:
    results = jira.search_jql(jql, fields=_issue_fields(config))
    if limit is not None:
        results = islice(results, limit)
    return [_simplify_issue(raw, config) for raw in results]


def find_working_sprint(jira: JiraClient, config: BoardConfig) -> dict[str, Any] | None:
    """The active sprint, or if none is active, the next future sprint."""
    sprints = list(jira.paginate(
        f"/rest/agile/1.0/board/{config.board_id}/sprint",
        params={"state": "active,future"},
    ))
    active = [s for s in sprints if s.get("state") == "active"]
    future = [s for s in sprints if s.get("state") == "future"]
    chosen = (active or future or [None])[0]
    if chosen is None:
        return None
    return {
        "id": chosen["id"],
        "name": chosen["name"],
        "state": chosen["state"],
        "goal": chosen.get("goal", ""),
        "start_date": chosen.get("startDate"),
        "end_date": chosen.get("endDate"),
    }


def build_board_context(
    jira: JiraClient,
    config: BoardConfig,
    backlog_limit: int = 50,
) -> dict[str, Any]:
    """Everything the planner needs to know about the board right now."""
    key = config.project_key
    sprint = find_working_sprint(jira, config)

    sprint_issues: list[dict[str, Any]] = []
    if sprint:
        sprint_issues = _search(
            jira, config, f"project = {key} AND sprint = {sprint['id']} ORDER BY Rank ASC"
        )

    backlog = _search(
        jira,
        config,
        f"project = {key} AND (sprint is EMPTY OR sprint not in openSprints()) "
        f"AND statusCategory != Done AND issuetype != Epic ORDER BY Rank ASC",
        limit=backlog_limit,
    )

    epics = _search(
        jira, config,
        f"project = {key} AND issuetype = Epic AND statusCategory != Done ORDER BY Rank ASC",
    )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "project": {"key": key, "name": config.project_name, "style": config.project_style},
        "sprint": sprint,
        "sprint_issues": sprint_issues,
        "backlog": backlog,
        "epics": epics,
        "team": [{"name": m.name, "aliases": m.aliases} for m in config.team],
        "issue_types": [t.name for t in config.issue_types],
        "statuses": {name: config.status_names(name) for name in config.statuses},
        "link_types": [
            {"name": lt.name, "outward": lt.outward, "inward": lt.inward}
            for lt in config.link_types
        ],
        "priorities": config.priorities,
    }