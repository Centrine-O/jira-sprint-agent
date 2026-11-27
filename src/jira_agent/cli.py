"""Command-line entry point for the Jira Sprint Agent.

Run `jira-agent --help` to see the available commands.
"""

from importlib.metadata import version as package_version
from pathlib import Path

import typer
from pydantic import ValidationError

from jira_agent.config import get_secrets
from jira_agent.board_config import DEFAULT_CONFIG_PATH, BoardConfig, ConfigError, load_board_config, save_board_config
from jira_agent.jira.client import JiraClient, JiraError
from jira_agent.jira import discovery
import json
from jira_agent.planner.context import build_board_context
from jira_agent.sandbox import SeedError, seed_sandbox
from jira_agent.planner.planner import PlannerError, plan_updates
from jira_agent.executor.validator import validate_plan

app = typer.Typer(
    help="Jira Sprint Agent: turn pasted team updates into reviewed Jira changes.",
    no_args_is_help=True,
)

PROJECT_STYLE = {"next-gen": "team-managed", "classic": "company-managed"}


def _ok(message: str) -> None:
    typer.secho(f"  [OK]   {message}", fg=typer.colors.GREEN)


def _fail(message: str) -> None:
    typer.secho(f"  [FAIL] {message}", fg=typer.colors.RED)


def _info(message: str) -> None:
    typer.echo(f"         {message}")


@app.callback()
def main() -> None:
    """Jira Sprint Agent command-line tool."""


@app.command()
def version() -> None:
    """Show the installed version of the tool."""
    typer.echo(f"jira-sprint-agent {package_version('jira-sprint-agent')}")


@app.command()
def doctor() -> None:
    """Check your .env, Jira login, and Anthropic key, and explain any problem."""
    typer.echo("Running checks...\n")
    all_good = True

    # 1. The .env file loads and has real values.
    try:
        secrets = get_secrets()
    except ValidationError as exc:
        _fail(".env file has problems:")
        for error in exc.errors():
            field = ".".join(str(part) for part in error.get("loc", ()))
            _info(f"{field}: {error.get('msg')}")
        _info("Fix these in .env (see .env.example), then run doctor again.")
        raise typer.Exit(code=1)

    placeholders = secrets.placeholder_problems()
    if placeholders:
        _fail("These .env values are still template placeholders: " + ", ".join(placeholders))
        raise typer.Exit(code=1)
    _ok(".env file loaded, all values filled in")

    # 2. Jira login and visible projects.
    try:
        with JiraClient(secrets) as jira:
            me = jira.get("/rest/api/3/myself")
            _ok(f"Jira login works: signed in as {me.get('displayName', 'unknown')}")

            projects = list(jira.paginate("/rest/api/3/project/search"))
            if projects:
                _ok(f"Jira projects your account can see: {len(projects)}")
                for project in projects:
                    style = PROJECT_STYLE.get(project.get("style", ""), "unknown type")
                    _info(f"{project.get('key', '?'):<10} {project.get('name', '')}  ({style})")
            else:
                _fail("Your Jira account can't see any projects.")
                _info("Check you're using the right Jira site and account.")
                all_good = False
    except JiraError as exc:
        _fail(f"Jira: {exc}")
        all_good = False

    # 3. Anthropic API key is accepted.
    import anthropic  # imported here so other commands start faster

    try:
        client = anthropic.Anthropic(api_key=secrets.anthropic_api_key.get_secret_value())
        client.models.list(limit=1)
        _ok("Anthropic API key accepted")
    except anthropic.AuthenticationError:
        _fail("Anthropic rejected the API key. Check ANTHROPIC_API_KEY in .env.")
        all_good = False
    except anthropic.APIConnectionError:
        _fail("Couldn't reach the Anthropic API. Check your internet connection.")
        all_good = False
    except anthropic.APIError as exc:
        _fail(f"Anthropic API problem: {exc.__class__.__name__}: {exc}")
        all_good = False

    typer.echo("")
    if all_good:
        typer.secho("All checks passed.", fg=typer.colors.GREEN, bold=True)
    else:
        typer.secho("Some checks failed. See the messages above.", fg=typer.colors.RED, bold=True)
        raise typer.Exit(code=1)



@app.command()
def inspect(
    project_key: str = typer.Argument(..., help="Jira project key, e.g. SBK"),
) -> None:
    """Show what the tool can see about a Jira project (read-only)."""
    key = project_key.upper()
    try:
        with JiraClient() as jira:
            project = discovery.get_project(jira, key)
            typer.secho(
                f"\nProject: {project['key']} - {project['name']} ({project['style']})",
                bold=True,
            )

            typer.secho("\nIssue types:", bold=True)
            for it in sorted(project["issue_types"], key=lambda i: -i["hierarchy_level"]):
                kind = "subtask" if it["subtask"] else f"level {it['hierarchy_level']}"
                _info(f"{it['name']} ({kind})")

            typer.secho("\nWorkflow statuses:", bold=True)
            for type_name, statuses in discovery.get_statuses(jira, key).items():
                names = ", ".join(f"{s['name']} [{s['category']}]" for s in statuses)
                _info(f"{type_name}: {names}")

            typer.secho("\nSpecial fields:", bold=True)
            for name, field_id in discovery.get_special_fields(jira).items():
                _info(f"{name}: {field_id or 'NOT FOUND'}")

            typer.secho("\nLink types:", bold=True)
            for lt in discovery.get_link_types(jira):
                _info(f"{lt['name']}: '{lt['outward']}' / '{lt['inward']}'")

            typer.secho("\nPriorities:", bold=True)
            _info(", ".join(p["name"] for p in discovery.get_priorities(jira)))

            typer.secho("\nTeam members:", bold=True)
            for member in discovery.get_team_members(jira, key):
                _info(member["name"])

            typer.secho("\nBoards and sprints:", bold=True)
            boards = discovery.get_boards(jira, key)
            if not boards:
                _info("No boards found.")
            for board in boards:
                _info(f"Board {board['id']}: {board['name']} ({board['type']})")
                if board["type"] == "scrum":
                    sprints = discovery.get_sprints(jira, board["id"])
                    if not sprints:
                        _info("   No active or future sprints yet.")
                    for s in sprints:
                        _info(f"   Sprint {s['id']}: {s['name']} [{s['state']}]")
    except JiraError as exc:
        _fail(f"Jira: {exc}")
        raise typer.Exit(code=1)


@app.command()
def init(
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="Where to write the settings file."
    ),
) -> None:
    """Create config.yaml for your board by reading its setup from Jira."""
    existing_aliases: dict[str, list[str]] = {}
    existing_excluded: list[str] = []
    if config_path.exists():
        if not typer.confirm(f"{config_path} already exists. Replace it?", default=False):
            typer.echo("Nothing changed.")
            raise typer.Exit()
        try:
            old = load_board_config(config_path)
            existing_aliases = {m.account_id: m.aliases for m in old.team}
            existing_excluded = old.excluded_members
            typer.echo("Existing nicknames will be offered as defaults.")
        except ConfigError:
            pass

    try:
        with JiraClient() as jira:
            projects = list(jira.paginate("/rest/api/3/project/search"))
            if not projects:
                _fail("Your Jira account can't see any projects.")
                raise typer.Exit(code=1)

            typer.secho("\nProjects your account can see:", bold=True)
            for p in projects:
                typer.echo(f"  {p['key']:<10} {p['name']}")
            key = typer.prompt("\nWhich project? Type its key").strip().upper()
            if key not in {p["key"] for p in projects}:
                _fail(f"'{key}' isn't in the list above.")
                raise typer.Exit(code=1)

            project = discovery.get_project(jira, key)
            boards = [b for b in discovery.get_boards(jira, key) if b["type"] == "scrum"]
            if not boards:
                _fail(f"{key} has no Scrum board. The tool needs one to manage sprints.")
                raise typer.Exit(code=1)
            if len(boards) == 1:
                board = boards[0]
                _ok(f"Using board {board['id']}: {board['name']}")
            else:
                typer.secho("\nScrum boards in this project:", bold=True)
                for b in boards:
                    typer.echo(f"  {b['id']}: {b['name']}")
                board_id = typer.prompt("Which board? Type its number", type=int)
                board = next((b for b in boards if b["id"] == board_id), None)
                if board is None:
                    _fail(f"Board {board_id} isn't in the list above.")
                    raise typer.Exit(code=1)

            typer.echo("Reading statuses, fields, link types, priorities and team...")
            statuses = discovery.get_statuses(jira, key)
            fields = discovery.get_special_fields(jira)
            link_types = discovery.get_link_types(jira)
            priorities = [p["name"] for p in discovery.get_priorities(jira)]
            members = discovery.get_team_members(jira, key)
    except JiraError as exc:
        _fail(f"Jira: {exc}")
        raise typer.Exit(code=1)

    typer.secho("\nTeam members found in Jira:", bold=True)
    for m in members:
        typer.echo(f"  {m['name']}")
    raw_excluded = typer.prompt(
        "\nAnyone to leave OFF the team list? Names separated by commas (Enter for none)",
        default=", ".join(existing_excluded),
        show_default=bool(existing_excluded),
    )
    excluded = [x.strip() for x in raw_excluded.split(",") if x.strip()]
    excluded_lower = {x.lower() for x in excluded}
    members = [m for m in members if m["name"].lower() not in excluded_lower]

    typer.secho("\nNicknames", bold=True)
    typer.echo(
        "For each person, type the names your team uses for them in updates,\n"
        "separated by commas (e.g. Kev, Kevo). Press Enter to skip."
    )
    team = []
    for m in members:
        default = ", ".join(existing_aliases.get(m["account_id"], []))
        raw = typer.prompt(f"  {m['name']}", default=default, show_default=bool(default))
        aliases = [
            a.strip()
            for a in raw.split(",")
            if a.strip() and a.strip().lower() != m["name"].lower()
        ]
        team.append({"account_id": m["account_id"], "name": m["name"], "aliases": aliases})

    type_names = [t["name"] for t in project["issue_types"] if not t["subtask"]]
    suggested = "Task" if "Task" in type_names else type_names[0]
    chosen = typer.prompt(
        f"\nDefault type for new work items ({', '.join(type_names)})", default=suggested
    ).strip()
    chosen = next((n for n in type_names if n.lower() == chosen.lower()), chosen)

    data = {
        "project_key": project["key"],
        "project_name": project["name"],
        "project_style": project["style"],
        "board_id": board["id"],
        "board_name": board["name"],
        "issue_types": project["issue_types"],
        "statuses": statuses,
        "fields": fields,
        "link_types": link_types,
        "priorities": priorities,
        "team": team,
        "excluded_members": excluded,
        "defaults": {"new_issue_type": chosen},
    }
    try:
        config = BoardConfig.model_validate(data)
    except ValidationError as exc:
        _fail("The settings didn't pass validation:")
        for error in exc.errors():
            _info(f"{'.'.join(str(p) for p in error['loc'])}: {error['msg']}")
        raise typer.Exit(code=1)

    save_board_config(config, config_path)
    typer.echo("")
    _ok(f"Saved {config_path} for {config.project_key} (board {config.board_id})")
    missing = [name for name, value in config.fields.model_dump().items() if not value]
    if missing:
        _fail("These fields weren't found and related features will be off: " + ", ".join(missing))


@app.command()
def snapshot(
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="Which board settings file to use."
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Print the raw data the planner will receive."
    ),
) -> None:
    """Show the current state of the board (read-only)."""
    try:
        config = load_board_config(config_path)
    except ConfigError as exc:
        _fail(str(exc))
        raise typer.Exit(code=1)

    try:
        with JiraClient() as jira:
            context = build_board_context(jira, config)
    except JiraError as exc:
        _fail(f"Jira: {exc}")
        raise typer.Exit(code=1)

    if as_json:
        typer.echo(json.dumps(context, indent=2, ensure_ascii=False))
        return

    def show_issue(issue: dict) -> None:
        who = issue["assignee"] or "unassigned"
        points = f", {issue['story_points']} pts" if issue["story_points"] is not None else ""
        flag = "  [FLAGGED]" if issue["flagged"] else ""
        parent = f"  (under {issue['parent']})" if issue["parent"] else ""
        _info(f"{issue['key']:<9} [{issue['status']}] {issue['summary']}  - {who}{points}{flag}{parent}")
        for link in issue["links"]:
            _info(f"            {link['relation']} {link['issue']}")

    project = context["project"]
    typer.secho(f"\n{project['key']} - {project['name']}", bold=True)

    sprint = context["sprint"]
    if sprint:
        typer.secho(f"\nSprint: {sprint['name']} [{sprint['state']}]", bold=True)
        if sprint["goal"]:
            _info(f"Goal: {sprint['goal']}")
        if not context["sprint_issues"]:
            _info("No issues in this sprint yet.")
        for issue in context["sprint_issues"]:
            show_issue(issue)
    else:
        typer.secho("\nNo active or future sprint on this board.", bold=True)

    typer.secho(f"\nBacklog (not done, not in a sprint): {len(context['backlog'])}", bold=True)
    for issue in context["backlog"]:
        show_issue(issue)

    typer.secho(f"\nOpen epics: {len(context['epics'])}", bold=True)
    for epic in context["epics"]:
        _info(f"{epic['key']:<9} [{epic['status']}] {epic['summary']}")
    
@app.command("seed-sandbox")
def seed_sandbox_command(
    config_path: Path = typer.Option(
        ..., "--config", help="Settings file of the SANDBOX board, e.g. config.sbk.yaml."
    ),
) -> None:
    """Fill a sandbox project with sample issues for testing (sandbox only)."""
    try:
        config = load_board_config(config_path)
    except ConfigError as exc:
        _fail(str(exc))
        raise typer.Exit(code=1)

    typer.echo(
        f"This will create about 9 sample issues in {config.project_key} "
        f"({config.project_name})."
    )
    if not typer.confirm("Continue?", default=False):
        typer.echo("Nothing changed.")
        raise typer.Exit()

    try:
        with JiraClient() as jira:
            seed_sandbox(jira, config, log=_info)
    except SeedError as exc:
        _fail(str(exc))
        raise typer.Exit(code=1)
    except JiraError as exc:
        _fail(f"Jira: {exc}")
        _info("Some issues may already have been created. Check the sandbox in Jira.")
        raise typer.Exit(code=1)

    _ok("Sandbox seeded. Run `jira-agent snapshot --config ...` to see it.")


@app.command()
def plan(
    text: str = typer.Option(None, "--text", help="The updates, in quotes."),
    file: Path = typer.Option(None, "--file", help="A text file containing the updates."),
    update_from: str = typer.Option(None, "--from", help="Who the updates are from, if the text doesn't say."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="Which board settings file to use."),
) -> None:
    """Propose Jira changes from pasted updates. Read-only: nothing is applied."""
    if file:
        if not file.exists():
            _fail(f"{file} not found.")
            raise typer.Exit(code=1)
        text = file.read_text(encoding="utf-8")
    if not text or not text.strip():
        _fail("Give the updates with --text \"...\" or --file path.txt")
        raise typer.Exit(code=1)

    try:
        config = load_board_config(config_path)
    except ConfigError as exc:
        _fail(str(exc))
        raise typer.Exit(code=1)

    try:
        with JiraClient() as jira:
            runner = jira.get("/rest/api/3/myself").get("displayName", "Scrum Master")
            typer.echo("Reading the board...")
            context = build_board_context(jira, config)
    except JiraError as exc:
        _fail(f"Jira: {exc}")
        raise typer.Exit(code=1)

    typer.echo("Asking Claude for a plan...")
    try:
        result = plan_updates(config, context, text, runner=runner, update_from=update_from)
    except PlannerError as exc:
        _fail(str(exc))
        raise typer.Exit(code=1)

    colours = {"high": typer.colors.GREEN, "medium": typer.colors.YELLOW, "low": typer.colors.RED}
    common = {"id", "type", "target", "depends_on", "source_text", "on_behalf_of", "confidence", "reasoning"}

    typer.secho(f"\nPlan: {result.plan.summary}", bold=True)
    for action in result.plan.actions:
        typer.echo("")
        typer.secho(f"  {action.id}  {action.type}  {action.target or ''}", bold=True, nl=False)
        typer.secho(f"   [{action.confidence}]", fg=colours[action.confidence])
        details = {
            k: v for k, v in action.model_dump().items()
            if k not in common and v not in (None, [], "")
        }
        for name, value in details.items():
            _info(f"{name}: {value}")
        if action.on_behalf_of:
            _info(f"for: {action.on_behalf_of}")
        if action.depends_on:
            _info(f"after: {', '.join(action.depends_on)}")
        _info(f"from: \"{action.source_text}\"")
        _info(f"why: {action.reasoning}")

    if result.plan.clarifications:
        typer.secho("\nNeeds clarification:", bold=True, fg=typer.colors.YELLOW)
        for c in result.plan.clarifications:
            _info(f"? {c.question}")
            if c.options:
                _info(f"  options: {' | '.join(c.options)}")
            _info(f"  from: \"{c.source_text}\"")

    typer.echo(
        f"\n{len(result.plan.actions)} actions, {len(result.plan.clarifications)} questions. "
        f"Model {result.model}, prompt {result.prompt_version}, "
        f"{result.input_tokens} in / {result.output_tokens} out tokens. Nothing was applied."
    )
    
@app.command()
def plan(
    text: str = typer.Option(None, "--text", help="The updates, in quotes."),
    file: Path = typer.Option(None, "--file", help="A text file containing the updates."),
    update_from: str = typer.Option(None, "--from", help="Who the updates are from, if the text doesn't say."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="Which board settings file to use."),
) -> None:
    """Propose Jira changes from pasted updates. Read-only: nothing is applied."""
    if file:
        if not file.exists():
            _fail(f"{file} not found.")
            raise typer.Exit(code=1)
        text = file.read_text(encoding="utf-8")
    if not text or not text.strip():
        _fail("Give the updates with --text \"...\" or --file path.txt")
        raise typer.Exit(code=1)

    try:
        config = load_board_config(config_path)
    except ConfigError as exc:
        _fail(str(exc))
        raise typer.Exit(code=1)

    try:
        with JiraClient() as jira:
            runner = jira.get("/rest/api/3/myself").get("displayName", "Scrum Master")
            typer.echo("Reading the board...")
            context = build_board_context(jira, config)

            typer.echo("Asking Claude for a plan...")
            try:
                result = plan_updates(config, context, text, runner=runner, update_from=update_from)
            except PlannerError as exc:
                _fail(str(exc))
                raise typer.Exit(code=1)

            typer.echo("Checking the plan against Jira...")
            checked = validate_plan(result.plan, config, context, jira)
    except JiraError as exc:
        _fail(f"Jira: {exc}")
        raise typer.Exit(code=1)

    colours = {"high": typer.colors.GREEN, "medium": typer.colors.YELLOW, "low": typer.colors.RED}
    common = {"id", "type", "target", "depends_on", "source_text", "on_behalf_of", "confidence", "reasoning"}

    typer.secho(f"\nPlan: {result.plan.summary}", bold=True)
    for c in checked:
        action = c.action
        typer.echo("")
        verdict, verdict_colour = ("READY", typer.colors.GREEN) if c.ok else ("BLOCKED", typer.colors.RED)
        typer.secho(f"  {action.id}  {action.type}  {action.target or ''}", bold=True, nl=False)
        typer.secho(f"   [{action.confidence}]", fg=colours[action.confidence], nl=False)
        typer.secho(f"  {verdict}", fg=verdict_colour, bold=True)
        details = {
            k: v for k, v in action.model_dump().items()
            if k not in common and v not in (None, [], "")
        }
        for name, value in details.items():
            _info(f"{name}: {value}")
        if action.on_behalf_of:
            _info(f"for: {action.on_behalf_of}")
        if action.depends_on:
            _info(f"after: {', '.join(action.depends_on)}")
        _info(f"from: \"{action.source_text}\"")
        _info(f"why: {action.reasoning}")
        for error in c.errors:
            typer.secho(f"         x {error}", fg=typer.colors.RED)
        for warning in c.warnings:
            typer.secho(f"         ! {warning}", fg=typer.colors.YELLOW)

    if result.plan.clarifications:
        typer.secho("\nNeeds clarification:", bold=True, fg=typer.colors.YELLOW)
        for q in result.plan.clarifications:
            _info(f"? {q.question}")
            if q.options:
                _info(f"  options: {' | '.join(q.options)}")
            _info(f"  from: \"{q.source_text}\"")

    ready = sum(1 for c in checked if c.ok)
    typer.echo(
        f"\n{ready} ready, {len(checked) - ready} blocked, "
        f"{len(result.plan.clarifications)} questions. "
        f"Model {result.model}, prompt {result.prompt_version}, "
        f"{result.input_tokens} in / {result.output_tokens} out tokens. Nothing was applied."
    )

# ---- full workflow: plan -> review -> approve -> apply ---------------------

from jira_agent.audit.log import AuditLog  # noqa: E402
from jira_agent.executor.executor import Executor  # noqa: E402
from jira_agent.planner.schema import HIGH_IMPACT  # noqa: E402

OUTCOME_STYLE = {
    "applied": ("OK  ", typer.colors.GREEN),
    "dry-run": ("~   ", typer.colors.CYAN),
    "skipped": ("--  ", typer.colors.YELLOW),
    "failed": ("FAIL", typer.colors.RED),
}


def _show_review(result, checked) -> None:
    colours = {"high": typer.colors.GREEN, "medium": typer.colors.YELLOW, "low": typer.colors.RED}
    common = {"id", "type", "target", "depends_on", "source_text", "on_behalf_of", "confidence", "reasoning"}
    typer.secho(f"\nPlan: {result.plan.summary}", bold=True)
    for c in checked:
        a = c.action
        typer.echo("")
        verdict, vcolour = ("READY", typer.colors.GREEN) if c.ok else ("BLOCKED", typer.colors.RED)
        typer.secho(f"  {a.id}  {a.type}  {a.target or ''}", bold=True, nl=False)
        typer.secho(f"   [{a.confidence}]", fg=colours[a.confidence], nl=False)
        typer.secho(f"  {verdict}", fg=vcolour, bold=True)
        for name, value in a.model_dump().items():
            if name not in common and value not in (None, [], ""):
                _info(f"{name}: {value}")
        if a.on_behalf_of:
            _info(f"for: {a.on_behalf_of}")
        _info(f"from: \"{a.source_text}\"")
        _info(f"why: {a.reasoning}")
        for error in c.errors:
            typer.secho(f"         x {error}", fg=typer.colors.RED)
        for warning in c.warnings:
            typer.secho(f"         ! {warning}", fg=typer.colors.YELLOW)
    if result.plan.clarifications:
        typer.secho("\nNeeds clarification (not applied; re-run with a clearer update):",
                    bold=True, fg=typer.colors.YELLOW)
        for q in result.plan.clarifications:
            _info(f"? {q.question}")
            if q.options:
                _info(f"  options: {' | '.join(q.options)}")


def _choose_approvals(checked) -> set[str]:
    ready = [c.action.id for c in checked if c.ok]
    if not ready:
        typer.secho("\nNothing is READY to apply.", fg=typer.colors.YELLOW)
        return set()
    high = [c.action.id for c in checked
            if c.ok and c.action.confidence == "high" and c.action.type not in HIGH_IMPACT]
    typer.secho("\nWhich READY actions should be applied?", bold=True)
    _info(f"Enter  = the {len(high)} high-confidence ones ({', '.join(high) or 'none'})")
    _info(f"all    = every READY action ({len(ready)})")
    _info("A1,A3  = only the ones you list")
    _info("none   = cancel")
    while True:
        answer = typer.prompt("Your choice", default="", show_default=False).strip()
        if answer == "":
            return set(high)
        if answer.lower() == "all":
            return set(ready)
        if answer.lower() == "none":
            return set()
        chosen = {part.strip().upper() for part in answer.replace(" ", ",").split(",") if part.strip()}
        unknown = sorted(chosen - set(ready))
        if unknown:
            _fail(f"Not READY or not in the plan: {', '.join(unknown)}. Try again.")
            continue
        return chosen


def _confirm_high_impact(checked, approved: set[str]) -> set[str]:
    confirmed: set[str] = set()
    for c in checked:
        if c.action.id in approved and c.action.type in HIGH_IMPACT:
            name = c.resolved.get("sprint_name") or c.action.sprint or ""
            typer.secho(f"\n{c.action.id} will {c.action.type.replace('_', ' ').upper()} '{name}'.",
                        fg=typer.colors.RED, bold=True)
            typed = typer.prompt("Type the sprint name exactly to confirm, or press Enter to skip",
                                 default="", show_default=False)
            if typed.strip() == name:
                confirmed.add(c.action.id)
            else:
                _info("Not confirmed; it will be skipped.")
    return confirmed


@app.command()
def run(
    text: str = typer.Option(None, "--text", help="The updates, in quotes."),
    file: Path = typer.Option(None, "--file", help="A text file containing the updates."),
    update_from: str = typer.Option(None, "--from", help="Who the updates are from, if the text doesn't say."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="Which board settings file to use."),
    apply: bool = typer.Option(False, "--apply", help="Really write to Jira. Without this, it's a dry run."),
) -> None:
    """Plan, review, approve and apply changes from pasted updates (dry run unless --apply)."""
    if file:
        if not file.exists():
            _fail(f"{file} not found.")
            raise typer.Exit(code=1)
        text = file.read_text(encoding="utf-8")
    if not text or not text.strip():
        _fail("Give the updates with --text \"...\" or --file path.txt")
        raise typer.Exit(code=1)
    try:
        config = load_board_config(config_path)
    except ConfigError as exc:
        _fail(str(exc))
        raise typer.Exit(code=1)

    dry_run = not apply
    try:
        with JiraClient() as jira, AuditLog() as audit:
            runner = jira.get("/rest/api/3/myself").get("displayName", "Scrum Master")
            typer.echo("Reading the board...")
            context = build_board_context(jira, config)
            typer.echo("Asking Claude for a plan...")
            try:
                result = plan_updates(config, context, text, runner=runner, update_from=update_from)
            except PlannerError as exc:
                _fail(str(exc))
                raise typer.Exit(code=1)
            typer.echo("Checking the plan against Jira...")
            checked = validate_plan(result.plan, config, context, jira)

            _show_review(result, checked)
            approved = _choose_approvals(checked)
            confirmed = _confirm_high_impact(checked, approved)

            status = "dry_run" if dry_run else "completed"
            if not approved:
                status = "cancelled"
            elif not dry_run and not typer.confirm(
                f"\nApply {len(approved)} change(s) to {config.project_key} in Jira now?", default=False
            ):
                approved, status = set(), "cancelled"

            run_id = audit.start_run(
                runner=runner, project_key=config.project_key, updates_text=text,
                plan=result.plan.model_dump(mode="json"), model=result.model,
                prompt_version=result.prompt_version, input_tokens=result.input_tokens,
                output_tokens=result.output_tokens, dry_run=dry_run,
            )
            if approved:
                typer.secho("\nDRY RUN: nothing is written to Jira." if dry_run else "\nApplying changes...",
                            bold=True)
            executor = Executor(jira, config, audit, run_id, runner,
                                dry_run=dry_run, confirmed_high_impact=confirmed)
            outcomes = executor.run(checked, approved)
            if any(o.outcome == "failed" for o in outcomes) and status == "completed":
                status = "completed_with_errors"
            audit.finish_run(run_id, status)
    except JiraError as exc:
        _fail(f"Jira: {exc}")
        raise typer.Exit(code=1)

    typer.secho(f"\nResults (run #{run_id}):", bold=True)
    order = {c.action.id: i for i, c in enumerate(checked)}
    for o in sorted(outcomes, key=lambda o: order.get(o.action_id, 0)):
        label, colour = OUTCOME_STYLE.get(o.outcome, ("?   ", typer.colors.WHITE))
        typer.secho(f"  {label} ", fg=colour, bold=True, nl=False)
        typer.echo(f"{o.action_id}  {o.message}")
        if o.url:
            _info(o.url)
    counts = {k: sum(1 for o in outcomes if o.outcome == k) for k in OUTCOME_STYLE}
    typer.echo(
        f"\n{counts['applied']} applied, {counts['dry-run']} dry-run, "
        f"{counts['skipped']} skipped, {counts['failed']} failed."
    )
    if dry_run and approved:
        _info("This was a dry run. Re-run with --apply to make these changes in Jira.")


if __name__ == "__main__":
    app()
