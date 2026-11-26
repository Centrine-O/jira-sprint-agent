"""Command-line entry point for the Jira Sprint Agent.

Run `jira-agent --help` to see the available commands.
"""

from importlib.metadata import version as package_version

import typer
from pydantic import ValidationError

from jira_agent.config import get_secrets
from jira_agent.jira.client import JiraClient, JiraError
from jira_agent.jira import discovery

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
        
if __name__ == "__main__":
    app()