"""Command-line entry point for the Jira Sprint Agent.

Run `jira-agent --help` to see the available commands.
"""

from importlib.metadata import version as package_version

import typer
from pydantic import ValidationError

from jira_agent.config import get_secrets
from jira_agent.jira.client import JiraClient, JiraError

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


if __name__ == "__main__":
    app()