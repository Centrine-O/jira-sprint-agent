"""Fills a SANDBOX project with realistic sample data for testing.

Safety: refuses to run unless the project name contains "sandbox",
and refuses to run twice (seeded issues carry the label "seed-data").
"""

from __future__ import annotations

from itertools import islice
from typing import Callable

from jira_agent.board_config import BoardConfig
from jira_agent.jira.client import JiraClient, JiraError
from jira_agent.planner.context import find_working_sprint

SEED_LABEL = "seed-data"

# ref, issue type, summary, target status, team member index, story points, parent ref, add to sprint
SEED_ISSUES: list[tuple[str, str, str, str, int | None, int | None, str | None, bool]] = [
    ("EPIC",      "Epic",     "Student Portal",                     "In Progress", None, None, None,    False),
    ("LOGIN",     "Story",    "Student login with school email",    "In Progress", 0,    3,    "EPIC",  True),
    ("FORM",      "Sub-task", "Design login form",                  "Done",        0,    None, "LOGIN", False),
    ("FEES",      "Story",    "Fee statement page",                 "To Do",       1,    5,    "EPIC",  True),
    ("TIMETABLE", "Story",    "Timetable view for students",        "In Review",   2,    3,    "EPIC",  True),
    ("STAGING",   "Task",     "Set up staging server",              "In Progress", 3,    2,    None,    True),
    ("CRASH",     "Bug",      "Login page crashes on mobile",       "To Do",       1,    None, None,    True),
    ("SMS",       "Story",    "Parent notifications by SMS",        "To Do",       None, 8,    "EPIC",  False),
    ("REPORT",    "Story",    "Report card PDF export",             "To Do",       None, 5,    None,    False),
]

# (from ref, link type name, to ref): "STAGING blocks TIMETABLE", "CRASH relates to LOGIN"
SEED_LINKS = [
    ("STAGING", "Blocks", "TIMETABLE"),
    ("CRASH", "Relates", "LOGIN"),
]


class SeedError(Exception):
    """Seeding was refused or failed. The message is written for humans."""


def _transition_to(jira: JiraClient, key: str, target: str) -> None:
    issue = jira.get(f"/rest/api/3/issue/{key}", params={"fields": "status"})
    current = issue["fields"]["status"]["name"]
    if current.lower() == target.lower():
        return
    options = jira.get(f"/rest/api/3/issue/{key}/transitions").get("transitions", [])
    for option in options:
        if option.get("to", {}).get("name", "").lower() == target.lower():
            jira.post(f"/rest/api/3/issue/{key}/transitions", json={"transition": {"id": option["id"]}})
            return
    raise SeedError(f"{key}: no way to move from '{current}' to '{target}' in this workflow.")


def seed_sandbox(
    jira: JiraClient,
    config: BoardConfig,
    log: Callable[[str], None] = print,
) -> dict[str, str]:
    """Create the sample issues. Returns {ref: issue key}."""
    project = config.project_key

    if "sandbox" not in config.project_name.lower():
        raise SeedError(
            f"Refusing to seed '{config.project_name}': only projects with 'sandbox' "
            "in their name can be seeded."
        )

    already = list(islice(
        jira.search_jql(f'project = {project} AND labels = "{SEED_LABEL}"', fields=["summary"]), 1
    ))
    if already:
        raise SeedError(
            f"{project} already has seed data (label '{SEED_LABEL}'). Refusing to seed twice."
        )

    team = config.team
    keys: dict[str, str] = {}

    for ref, type_name, summary, status, member, points, parent, _ in SEED_ISSUES:
        issue_type = config.issue_type(type_name)
        if issue_type is None:
            raise SeedError(f"{project} has no issue type '{type_name}'.")

        fields: dict = {
            "project": {"key": project},
            "issuetype": {"id": issue_type.id},
            "summary": summary,
            "labels": [SEED_LABEL],
        }
        if member is not None and team:
            fields["assignee"] = {"accountId": team[member % len(team)].account_id}
        if parent:
            fields["parent"] = {"key": keys[parent]}

        created = jira.post("/rest/api/3/issue", json={"fields": fields})
        key = created["key"]
        keys[ref] = key
        log(f"Created {key}  {type_name}: {summary}")

        if points is not None and config.fields.story_points:
            try:
                jira.put(f"/rest/api/3/issue/{key}", json={"fields": {config.fields.story_points: points}})
            except JiraError as exc:
                log(f"   (couldn't set story points on {key}: {exc})")

        _transition_to(jira, key, status)
        if status != "To Do":
            log(f"   moved to {status}")

    for from_ref, link_type, to_ref in SEED_LINKS:
        jira.post("/rest/api/3/issueLink", json={
            "type": {"name": link_type},
            "inwardIssue": {"key": keys[from_ref]},
            "outwardIssue": {"key": keys[to_ref]},
        })
        log(f"Linked {keys[from_ref]} -> {link_type} -> {keys[to_ref]}")

    sprint = find_working_sprint(jira, config)
    in_sprint = [keys[ref] for ref, *_, add in SEED_ISSUES if add]
    if sprint and in_sprint:
        jira.post(f"/rest/agile/1.0/sprint/{sprint['id']}/issue", json={"issues": in_sprint})
        log(f"Added {len(in_sprint)} issues to {sprint['name']}")
    elif not sprint:
        log("No active or future sprint found; all issues left in the backlog.")

    return keys