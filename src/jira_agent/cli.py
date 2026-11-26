"""Command-line entry point for the Jira Sprint Agent.

Run `jira-agent --help` to see the available commands.
"""

from importlib.metadata import version as package_version

import typer

app = typer.Typer(
    help="Jira Sprint Agent: turn pasted team updates into reviewed Jira changes.",
    no_args_is_help=True,
)


@app.callback()
def main() -> None:
    """Jira Sprint Agent command-line tool."""


@app.command()
def version() -> None:
    """Show the installed version of the tool."""
    typer.echo(f"jira-sprint-agent {package_version('jira-sprint-agent')}")


if __name__ == "__main__":
    app()