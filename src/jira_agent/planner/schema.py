"""The action plan: the exact shape Claude must return.

One flat Action model (rather than a different model per action type) keeps
the plan easy for Claude to produce, easy to show in a review table, and easy
to edit before applying. Required fields per type are checked here; whether
the values actually exist in Jira is checked later by the validator.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ActionType = Literal[
    "create_issue",     # new issue; target is a placeholder like NEW-1
    "update_fields",    # change summary, story points, priority, labels, due date, parent
    "transition",       # move to another status
    "comment",          # add a progress comment
    "assign",           # change assignee
    "link",             # link target to link_target (e.g. target blocks link_target)
    "add_to_sprint",    # put target into a sprint
    "move_to_backlog",  # take target out of its sprint
    "log_work",         # record time spent
    "flag_blocked",     # mark as blocked (impediment flag)
    "unflag",           # clear the blocked flag
    "create_sprint",    # create a future sprint
    "start_sprint",     # start a sprint (high impact)
    "complete_sprint",  # complete a sprint (high impact)
    "set_sprint_goal",  # set or change a sprint's goal
]

Confidence = Literal["high", "medium", "low"]

# Fields each action type must have filled in (besides the common ones).
REQUIRED_FIELDS: dict[str, list[str]] = {
    "create_issue": ["target", "summary", "issue_type"],
    "update_fields": ["target"],
    "transition": ["target", "status"],
    "comment": ["target", "comment"],
    "assign": ["target", "assignee"],
    "link": ["target", "link_type", "link_target"],
    "add_to_sprint": ["target", "sprint"],
    "move_to_backlog": ["target"],
    "log_work": ["target", "time_spent"],
    "flag_blocked": ["target"],
    "unflag": ["target"],
    "create_sprint": ["sprint"],
    "start_sprint": ["sprint"],
    "complete_sprint": ["sprint"],
    "set_sprint_goal": ["sprint", "sprint_goal"],
}

HIGH_IMPACT = {"start_sprint", "complete_sprint"}


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Short unique id within this plan, e.g. A1, A2.")
    type: ActionType
    target: str | None = Field(
        default=None,
        description="Existing issue key (e.g. SM-12), or a placeholder NEW-1, NEW-2 "
        "for an issue created earlier in this same plan.",
    )

    # Issue fields
    summary: str | None = None
    issue_type: str | None = None
    parent: str | None = Field(default=None, description="Parent issue key or NEW-n placeholder.")
    description: str | None = Field(default=None, description="Short plain-text description.")
    assignee: str | None = Field(default=None, description="Team member's name exactly as listed.")
    status: str | None = Field(default=None, description="Target status name exactly as listed.")
    story_points: float | None = None
    priority: str | None = None
    labels: list[str] = Field(default_factory=list)
    due_date: str | None = Field(default=None, description="YYYY-MM-DD")

    # Comments, links, time
    comment: str | None = None
    link_type: str | None = Field(default=None, description="Link type name, e.g. Blocks, Relates.")
    link_target: str | None = Field(default=None, description="The other issue: key or NEW-n.")
    time_spent: str | None = Field(default=None, description="Jira format, e.g. 3h, 1d 2h, 30m.")

    # Sprints
    sprint: str | None = Field(
        default=None,
        description="Sprint name exactly as listed, or 'active' / 'next' / a new sprint's name.",
    )
    sprint_goal: str | None = None
    sprint_start: str | None = Field(default=None, description="YYYY-MM-DD")
    sprint_end: str | None = Field(default=None, description="YYYY-MM-DD")
    incomplete_to: str | None = Field(
        default=None, description="complete_sprint only: 'next' sprint or 'backlog'."
    )

    # Traceability
    depends_on: list[str] = Field(default_factory=list, description="Ids of actions that must run first.")
    source_text: str = Field(description="The exact words from the pasted updates this came from.")
    on_behalf_of: str | None = Field(default=None, description="Team member the update is about.")
    confidence: Confidence
    reasoning: str = Field(description="One line: why this action, and how the issue was matched.")

    @model_validator(mode="after")
    def check_required_and_impact(self) -> "Action":
        missing = [f for f in REQUIRED_FIELDS[self.type] if not getattr(self, f)]
        if missing:
            raise ValueError(f"{self.type} action {self.id} is missing: {', '.join(missing)}")
        if self.type in HIGH_IMPACT:
            self.confidence = "low"  # never bulk-approved
        return self


class Clarification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str
    options: list[str] = Field(default_factory=list)
    source_text: str


class ActionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(description="One or two sentences on what this batch does.")
    actions: list[Action] = Field(default_factory=list)
    clarifications: list[Clarification] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_ids_and_dependencies(self) -> "ActionPlan":
        ids = [a.id for a in self.actions]
        if len(ids) != len(set(ids)):
            raise ValueError("Action ids must be unique within a plan.")
        known = set(ids)
        for action in self.actions:
            unknown = [d for d in action.depends_on if d not in known]
            if unknown:
                raise ValueError(f"Action {action.id} depends on unknown action(s): {unknown}")
        return self