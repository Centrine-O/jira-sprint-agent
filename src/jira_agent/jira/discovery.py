"""Reads a Jira project's setup: boards, issue types, statuses, fields,
link types, priorities, team members, and sprints.

Read-only: nothing here changes anything in Jira.
"""

from __future__ import annotations

from typing import Any

from jira_agent.jira.client import JiraClient

# Jira's internal project styles, in the words Jira's UI uses.
PROJECT_STYLE = {"next-gen": "team-managed", "classic": "company-managed"}

# Field names Jira uses for story points. Team-managed projects and newer
# company-managed templates use the first; older company-managed use the second.
STORY_POINT_FIELD_NAMES = ["Story point estimate", "Story Points"]

# Jira's internal type identifier for the Sprint field (stable across sites).
SPRINT_FIELD_TYPE = "com.pyxis.greenhopper.jira:gh-sprint"


def get_project(jira: JiraClient, key: str) -> dict[str, Any]:
    """Basic project info, including its type and issue types."""
    data = jira.get(f"/rest/api/3/project/{key}")
    return {
        "id": data["id"],
        "key": data["key"],
        "name": data["name"],
        "style": PROJECT_STYLE.get(data.get("style", ""), data.get("style", "unknown")),
        "issue_types": [
            {
                "id": it["id"],
                "name": it["name"],
                "subtask": bool(it.get("subtask")),
                "hierarchy_level": it.get("hierarchyLevel", 0),
            }
            for it in data.get("issueTypes", [])
        ],
    }


def get_boards(jira: JiraClient, project_key: str) -> list[dict[str, Any]]:
    """Boards attached to the project (Scrum boards are the ones with sprints)."""
    return [
        {"id": b["id"], "name": b["name"], "type": b.get("type", "unknown")}
        for b in jira.paginate(
            "/rest/agile/1.0/board", params={"projectKeyOrId": project_key}
        )
    ]


def get_statuses(jira: JiraClient, project_key: str) -> dict[str, list[dict[str, str]]]:
    """Workflow statuses available for each issue type, keyed by issue type name.

    `category` is Jira's grouping: 'new' (To Do), 'indeterminate' (In Progress), 'done'.
    """
    data = jira.get(f"/rest/api/3/project/{project_key}/statuses") or []
    result: dict[str, list[dict[str, str]]] = {}
    for issue_type in data:
        result[issue_type["name"]] = [
            {
                "id": s["id"],
                "name": s["name"],
                "category": s.get("statusCategory", {}).get("key", "unknown"),
            }
            for s in issue_type.get("statuses", [])
        ]
    return result


def get_special_fields(jira: JiraClient) -> dict[str, str | None]:
    """Find the internal IDs (e.g. customfield_10016) of fields we need, by name/type.

    Custom field IDs differ between Jira sites, so they must be looked up, never hardcoded.
    """
    fields = jira.get("/rest/api/3/field") or []
    by_name: dict[str, str] = {}
    sprint_id: str | None = None
    for f in fields:
        by_name.setdefault(f.get("name", ""), f["id"])
        if (f.get("schema") or {}).get("custom") == SPRINT_FIELD_TYPE:
            sprint_id = f["id"]

    story_points_id = next(
        (by_name[n] for n in STORY_POINT_FIELD_NAMES if n in by_name), None
    )
    return {
        "story_points": story_points_id,
        "sprint": sprint_id or by_name.get("Sprint"),
        "flagged": by_name.get("Flagged"),
        "start_date": by_name.get("Start date"),
        "due_date": "duedate",  # built-in field, same ID on every site
    }


def get_link_types(jira: JiraClient) -> list[dict[str, str]]:
    """Issue link types, e.g. Blocks (blocks / is blocked by)."""
    data = jira.get("/rest/api/3/issueLinkType") or {}
    return [
        {"id": lt["id"], "name": lt["name"], "outward": lt["outward"], "inward": lt["inward"]}
        for lt in data.get("issueLinkTypes", [])
    ]


def get_priorities(jira: JiraClient) -> list[dict[str, str]]:
    """Priority levels, e.g. Highest, High, Medium, Low, Lowest."""
    return [
        {"id": p["id"], "name": p["name"]}
        for p in jira.paginate("/rest/api/3/priority/search")
    ]


def get_team_members(jira: JiraClient, project_key: str) -> list[dict[str, str]]:
    """Real, active people who can be assigned issues in the project (bots excluded)."""
    members = []
    for user in jira.paginate(
        "/rest/api/3/user/assignable/search", params={"project": project_key}
    ):
        if user.get("accountType") != "atlassian" or not user.get("active", True):
            continue
        members.append({"account_id": user["accountId"], "name": user.get("displayName", "")})
    return sorted(members, key=lambda m: m["name"].lower())


def get_sprints(jira: JiraClient, board_id: int) -> list[dict[str, Any]]:
    """Active and future sprints on a Scrum board."""
    return [
        {
            "id": s["id"],
            "name": s["name"],
            "state": s["state"],
            "goal": s.get("goal", ""),
            "start_date": s.get("startDate"),
            "end_date": s.get("endDate"),
        }
        for s in jira.paginate(
            f"/rest/agile/1.0/board/{board_id}/sprint", params={"state": "active,future"}
        )
    ]