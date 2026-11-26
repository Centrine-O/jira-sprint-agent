"""Sends the board context and pasted updates to Claude and returns a validated plan.

Claude must answer by calling the `submit_plan` tool, whose input schema is the
ActionPlan model, so the result is always structured. If the plan fails
validation, the errors are sent back once for Claude to correct.
Nothing here writes to Jira.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import anthropic
from pydantic import ValidationError

from jira_agent.board_config import BoardConfig
from jira_agent.config import get_secrets
from jira_agent.planner.prompts import PROMPT_VERSION, SYSTEM_PROMPT, build_user_message
from jira_agent.planner.schema import ActionPlan

MAX_ATTEMPTS = 2

PLAN_TOOL = {
    "name": "submit_plan",
    "description": "Submit the proposed Jira changes and any clarifying questions.",
    "input_schema": ActionPlan.model_json_schema(),
}


class PlannerError(Exception):
    """Planning failed. The message is written for humans."""


@dataclass
class PlanResult:
    plan: ActionPlan
    model: str
    prompt_version: str
    input_tokens: int
    output_tokens: int
    attempts: int


def plan_updates(
    config: BoardConfig,
    context: dict[str, Any],
    updates: str,
    runner: str,
    update_from: str | None = None,
    today: str | None = None,
    client: anthropic.Anthropic | None = None,
) -> PlanResult:
    """Ask Claude for an action plan. Read-only: nothing is written to Jira."""
    if not updates.strip():
        raise PlannerError("There are no updates to plan from. Paste some text first.")

    if client is None:
        client = anthropic.Anthropic(
            api_key=get_secrets().anthropic_api_key.get_secret_value(),
            max_retries=3,
        )
    if today is None:
        today = datetime.now(ZoneInfo(config.defaults.timezone)).date().isoformat()

    model = config.claude.model
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": build_user_message(context, updates, today, runner, update_from)}
    ]
    input_tokens = output_tokens = 0

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=config.claude.max_tokens,
                system=SYSTEM_PROMPT,
                tools=[PLAN_TOOL],
                tool_choice={"type": "auto"},
                messages=messages,
            )
        except anthropic.NotFoundError as exc:
            raise PlannerError(
                f"The model '{model}' wasn't found. Change claude.model in your config file."
            ) from exc
        except anthropic.AuthenticationError as exc:
            raise PlannerError("Anthropic rejected the API key. Check ANTHROPIC_API_KEY in .env.") from exc
        except anthropic.APIConnectionError as exc:
            raise PlannerError("Couldn't reach the Anthropic API. Check your internet connection.") from exc
        except anthropic.APIStatusError as exc:
            raise PlannerError(f"Anthropic API error (HTTP {exc.status_code}): {exc.message}") from exc

        input_tokens += response.usage.input_tokens
        output_tokens += response.usage.output_tokens

        if response.stop_reason == "max_tokens":
            raise PlannerError(
                "The plan was too long and got cut off. Paste fewer updates at a time, "
                "or raise claude.max_tokens in your config file."
            )

        tool_use = next((block for block in response.content if block.type == "tool_use"), None)
        if tool_use is None:
            if attempt == MAX_ATTEMPTS:
                raise PlannerError("Claude didn't return a plan. Try again.")
            messages.append({"role": "assistant", "content": response.content})
            messages.append({
                "role": "user",
                "content": "Submit your answer now by calling the submit_plan tool.",
            })
            continue
        try:
            plan = ActionPlan.model_validate(tool_use.input)
        except ValidationError as exc:
            if attempt == MAX_ATTEMPTS:
                raise PlannerError(f"Claude's plan was still invalid after a retry:\n{exc}") from exc
            messages.append({"role": "assistant", "content": response.content})
            messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "is_error": True,
                    "content": (
                        "The plan failed validation. Fix exactly these problems and "
                        f"call submit_plan again:\n{exc}"
                    ),
                }],
            })
            continue

        return PlanResult(
            plan=plan,
            model=model,
            prompt_version=PROMPT_VERSION,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            attempts=attempt,
        )

    raise PlannerError("Planning failed.")  # not reached; keeps type checkers happy
    