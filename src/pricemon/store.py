"""SQLite storage for one client: runs, observations and events.

Prices are stored as decimal text, never as floats.
"""

import sqlite3
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from .models import OK, Event, Observation

_REAL_RUNS_ONLY = " AND run_id IN (SELECT id FROM runs WHERE dry_run = 0)"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    urls_total INTEGER NOT NULL DEFAULT 0,
    urls_ok INTEGER NOT NULL DEFAULT 0,
    urls_failed INTEGER NOT NULL DEFAULT 0,
    dry_run INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    url TEXT NOT NULL,
    product_name TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    status TEXT NOT NULL,
    http_status INTEGER,
    title TEXT,
    price TEXT,
    currency TEXT,
    in_stock INTEGER,
    method TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_observations_url ON observations(url, fetched_at);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    observation_id INTEGER NOT NULL REFERENCES observations(id),
    url TEXT NOT NULL,
    product_name TEXT NOT NULL,
    type TEXT NOT NULL,
    old_price TEXT,
    new_price TEXT,
    change_pct TEXT,
    verify INTEGER NOT NULL DEFAULT 0,
    alerted INTEGER NOT NULL DEFAULT 0,
    delivered INTEGER NOT NULL DEFAULT 0
);
"""


def _decimal(value) -> Decimal | None:
    return None if value is None else Decimal(value)


def _text(value) -> str | None:
    return None if value is None else str(value)


def _flag(value) -> bool | None:
    return None if value is None else bool(value)


def _observation(row: sqlite3.Row) -> Observation:
    return Observation(
        url=row["url"],
        product_name=row["product_name"],
        fetched_at=row["fetched_at"],
        status=row["status"],
        http_status=row["http_status"],
        title=row["title"],
        price=_decimal(row["price"]),
        currency=row["currency"],
        in_stock=_flag(row["in_stock"]),
        method=row["method"],
        error=row["error"],
        id=row["id"],
    )


def _event(row: sqlite3.Row) -> Event:
    return Event(
        url=row["url"],
        product_name=row["product_name"],
        type=row["type"],
        old_price=_decimal(row["old_price"]),
        new_price=_decimal(row["new_price"]),
        change_pct=_decimal(row["change_pct"]),
        verify=bool(row["verify"]),
        alerted=bool(row["alerted"]),
        currency=row["currency"],
        id=row["id"],
        run_id=row["run_id"],
    )


class Store:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)
        # A database created before delivery tracking has no such column.
        columns = [row["name"] for row in self.db.execute("PRAGMA table_info(events)")]
        if "delivered" not in columns:
            with self.db:
                self.db.execute("ALTER TABLE events ADD COLUMN delivered INTEGER NOT NULL DEFAULT 0")
                # Alerts from before the upgrade were already handled; do not resend the history.
                self.db.execute("UPDATE events SET delivered = 1")

    def close(self) -> None:
        self.db.close()

    # runs

    def start_run(self, started_at: str, dry_run: bool = False) -> int:
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO runs (started_at, dry_run) VALUES (?, ?)", (started_at, int(dry_run))
            )
        return cursor.lastrowid

    def finish_run(self, run_id: int, finished_at: str, urls_total: int, urls_ok: int, urls_failed: int) -> None:
        with self.db:
            self.db.execute(
                "UPDATE runs SET finished_at = ?, urls_total = ?, urls_ok = ?, urls_failed = ? WHERE id = ?",
                (finished_at, urls_total, urls_ok, urls_failed, run_id),
            )

    def run(self, run_id: int) -> dict:
        row = self.db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"no run with id {run_id}")
        return dict(row)

    def runs(self) -> list[dict]:
        return [dict(row) for row in self.db.execute("SELECT * FROM runs ORDER BY id")]

    # observations

    def add_observation(self, run_id: int, obs: Observation) -> Observation:
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO observations (run_id, url, product_name, fetched_at, status, http_status,"
                " title, price, currency, in_stock, method, error)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id, obs.url, obs.product_name, obs.fetched_at, obs.status, obs.http_status,
                    obs.title, _text(obs.price), obs.currency,
                    None if obs.in_stock is None else int(obs.in_stock),
                    obs.method, obs.error,
                ),
            )
        return replace(obs, id=cursor.lastrowid)

    def last_ok(
        self, url: str, before_run_id: int | None = None, include_dry_runs: bool = True
    ) -> Observation | None:
        """The most recent successful observation of a URL, optionally before a given run.

        A real run passes include_dry_runs=False so that dry runs never become its baseline.
        """
        sql = "SELECT * FROM observations WHERE url = ? AND status = ?"
        params: list = [url, OK]
        if before_run_id is not None:
            sql += " AND run_id < ?"
            params.append(before_run_id)
        if not include_dry_runs:
            sql += _REAL_RUNS_ONLY
        row = self.db.execute(sql + " ORDER BY id DESC LIMIT 1", params).fetchone()
        return None if row is None else _observation(row)

    def consecutive_failures(
        self, url: str, up_to_run_id: int | None = None, include_dry_runs: bool = True
    ) -> int:
        """How many runs in a row a URL has failed, counting back from the latest (or given) run.

        A real run passes include_dry_runs=False so that dry runs do not affect the count.
        """
        sql = "SELECT status FROM observations WHERE url = ?"
        params: list = [url]
        if up_to_run_id is not None:
            sql += " AND run_id <= ?"
            params.append(up_to_run_id)
        if not include_dry_runs:
            sql += _REAL_RUNS_ONLY
        count = 0
        for row in self.db.execute(sql + " ORDER BY id DESC", params):
            if row["status"] == OK:
                break
            count += 1
        return count

    def run_observations(self, run_id: int) -> list[Observation]:
        rows = self.db.execute("SELECT * FROM observations WHERE run_id = ? ORDER BY id", (run_id,))
        return [_observation(row) for row in rows]

    # events

    def add_event(self, run_id: int, observation_id: int, event: Event) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO events (run_id, observation_id, url, product_name, type, old_price,"
                " new_price, change_pct, verify, alerted) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id, observation_id, event.url, event.product_name, event.type,
                    _text(event.old_price), _text(event.new_price), _text(event.change_pct),
                    int(event.verify), int(event.alerted),
                ),
            )

    def run_events(self, run_id: int) -> list[Event]:
        rows = self.db.execute(
            "SELECT e.*, o.currency AS currency FROM events e"
            " JOIN observations o ON o.id = e.observation_id"
            " WHERE e.run_id = ? ORDER BY e.id",
            (run_id,),
        )
        return [_event(row) for row in rows]

    def undelivered_events(self, up_to_run_id: int) -> list[Event]:
        """Alerted events of real runs up to a run that have not reached the client yet, oldest first."""
        rows = self.db.execute(
            "SELECT e.*, o.currency AS currency FROM events e"
            " JOIN observations o ON o.id = e.observation_id"
            " WHERE e.alerted = 1 AND e.delivered = 0 AND e.run_id <= ?"
            " AND e.run_id IN (SELECT id FROM runs WHERE dry_run = 0) ORDER BY e.id",
            (up_to_run_id,),
        )
        return [_event(row) for row in rows]

    def mark_delivered(self, up_to_run_id: int) -> None:
        """Mark exactly the events undelivered_events(up_to_run_id) returns as delivered."""
        with self.db:
            self.db.execute(
                "UPDATE events SET delivered = 1"
                " WHERE alerted = 1 AND delivered = 0 AND run_id <= ?"
                " AND run_id IN (SELECT id FROM runs WHERE dry_run = 0)",
                (up_to_run_id,),
            )

    # held observations

    def latest_observation(self, url: str) -> Observation | None:
        """The newest observation of a URL from a real run, whatever its status."""
        row = self.db.execute(
            "SELECT * FROM observations WHERE url = ?" + _REAL_RUNS_ONLY + " ORDER BY id DESC LIMIT 1", (url,)
        ).fetchone()
        return None if row is None else _observation(row)

    def accept_observation(self, observation_id: int, alert_types: list[str]) -> None:
        """Make a held observation a trusted one, and queue its events of the given types for the client."""
        with self.db:
            self.db.execute(
                "UPDATE observations SET status = ?, error = NULL WHERE id = ?", (OK, observation_id)
            )
            if alert_types:
                marks = ", ".join("?" for _ in alert_types)
                self.db.execute(
                    f"UPDATE events SET alerted = 1, delivered = 0 WHERE observation_id = ? AND type IN ({marks})",
                    (observation_id, *alert_types),
                )
