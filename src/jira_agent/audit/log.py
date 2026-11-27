"""Audit log: a local SQLite record of every run and every change made.

Stored in data/audit.db (ignored by Git). Records the pasted text, the plan,
what was approved, what happened in Jira, and before/after values for undo.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path("data/audit.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    runner          TEXT,
    project_key     TEXT,
    updates_text    TEXT,
    plan_json       TEXT,
    model           TEXT,
    prompt_version  TEXT,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    dry_run         INTEGER NOT NULL DEFAULT 1,
    status          TEXT NOT NULL DEFAULT 'started'
);
CREATE TABLE IF NOT EXISTS action_results (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES runs(id),
    action_id    TEXT,
    action_type  TEXT,
    target       TEXT,
    issue_key    TEXT,
    decision     TEXT,
    outcome      TEXT,
    error        TEXT,
    before_json  TEXT,
    after_json   TEXT,
    recorded_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_results_run ON action_results(run_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dump(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, default=str)


class AuditLog:
    def __init__(self, path: Path = DEFAULT_DB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "AuditLog":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ---- writing ---------------------------------------------------------

    def start_run(
        self,
        *,
        runner: str,
        project_key: str,
        updates_text: str,
        plan: dict[str, Any],
        model: str,
        prompt_version: str,
        input_tokens: int,
        output_tokens: int,
        dry_run: bool,
    ) -> int:
        cur = self._db.execute(
            "INSERT INTO runs (started_at, runner, project_key, updates_text, plan_json, model, "
            "prompt_version, input_tokens, output_tokens, dry_run) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (_now(), runner, project_key, updates_text, _dump(plan), model,
             prompt_version, input_tokens, output_tokens, int(dry_run)),
        )
        self._db.commit()
        return int(cur.lastrowid)

    def record_action(
        self,
        run_id: int,
        *,
        action_id: str,
        action_type: str,
        target: str | None,
        issue_key: str | None,
        decision: str,
        outcome: str,
        error: str | None = None,
        before: Any = None,
        after: Any = None,
    ) -> None:
        """decision: approved | rejected | blocked.  outcome: applied | failed | skipped | dry-run."""
        self._db.execute(
            "INSERT INTO action_results (run_id, action_id, action_type, target, issue_key, decision, "
            "outcome, error, before_json, after_json, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, action_id, action_type, target, issue_key, decision, outcome,
             error, _dump(before), _dump(after), _now()),
        )
        self._db.commit()

    def finish_run(self, run_id: int, status: str) -> None:
        """status: completed | completed_with_errors | dry_run | cancelled."""
        self._db.execute(
            "UPDATE runs SET finished_at = ?, status = ? WHERE id = ?", (_now(), status, run_id)
        )
        self._db.commit()

    # ---- reading ---------------------------------------------------------

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT r.*, "
            "  (SELECT COUNT(*) FROM action_results a WHERE a.run_id = r.id AND a.outcome = 'applied') AS applied, "
            "  (SELECT COUNT(*) FROM action_results a WHERE a.run_id = r.id AND a.outcome = 'failed') AS failed "
            "FROM runs r ORDER BY r.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        run = self._db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if run is None:
            return None
        results = self._db.execute(
            "SELECT * FROM action_results WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        data = dict(run)
        data["plan"] = json.loads(data.pop("plan_json") or "null")
        data["results"] = []
        for row in results:
            item = dict(row)
            item["before"] = json.loads(item.pop("before_json") or "null")
            item["after"] = json.loads(item.pop("after_json") or "null")
            data["results"].append(item)
        return data
