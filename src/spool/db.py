"""SQLite persistence with migrations keyed off ``PRAGMA user_version``.

There is no ORM and no migration framework. The schema is a list of numbered
steps; opening a database runs every step newer than the version recorded in
the file and then stamps the new version. Running it again is a no-op, which is
what makes ``spool init`` safe to run on an existing database.

The migration list is real history, not decoration: version 1 is the schema
that shipped with manual job entry, version 2 is what was added when printer
sync arrived and jobs needed to carry their origin so that re-syncing does not
duplicate them.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

from .models import Job, Printer, Settings, Spool

#: Default on-disk location, overridable with --db or SPOOL_DB.
DEFAULT_DB_NAME = "spool.db"

# --------------------------------------------------------------------------
# Migrations
# --------------------------------------------------------------------------

_MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS spools (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    material        TEXT    NOT NULL,
    brand           TEXT    NOT NULL DEFAULT '',
    color           TEXT    NOT NULL DEFAULT '',
    diameter_mm     REAL    NOT NULL DEFAULT 1.75,
    spool_weight_g  REAL    NOT NULL DEFAULT 1000.0,
    density_g_cm3   REAL    NOT NULL DEFAULT 1.24,
    price           REAL    NOT NULL DEFAULT 0.0,
    currency        TEXT    NOT NULL DEFAULT 'USD',
    purchased       TEXT,
    remaining_g     REAL    NOT NULL DEFAULT 0.0,
    notes           TEXT    NOT NULL DEFAULT '',
    archived        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS printers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    watts       REAL    NOT NULL DEFAULT 0.0,
    price       REAL    NOT NULL DEFAULT 0.0,
    life_hours  REAL    NOT NULL DEFAULT 0.0,
    notes       TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS jobs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT    NOT NULL,
    printer             TEXT    NOT NULL DEFAULT '',
    spool_id            INTEGER REFERENCES spools(id) ON DELETE SET NULL,
    filament_g          REAL    NOT NULL DEFAULT 0.0,
    filament_mm         REAL,
    duration_s          INTEGER NOT NULL DEFAULT 0,
    status              TEXT    NOT NULL DEFAULT 'success',
    started             TEXT,
    failed_at_fraction  REAL,
    notes               TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_jobs_spool ON jobs(spool_id);
"""

_MIGRATION_2 = """
ALTER TABLE jobs ADD COLUMN source TEXT NOT NULL DEFAULT 'manual';
ALTER TABLE jobs ADD COLUMN source_job_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_source
    ON jobs(source, source_job_id)
    WHERE source_job_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_jobs_started ON jobs(started);
"""

#: (version, sql) pairs applied in ascending order. Append, never rewrite.
MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, _MIGRATION_1),
    (2, _MIGRATION_2),
)

#: The schema version a freshly initialised database ends up at.
SCHEMA_VERSION = MIGRATIONS[-1][0]


class DBError(Exception):
    """Raised for database problems the user can act on."""


# --------------------------------------------------------------------------
# Connection handling
# --------------------------------------------------------------------------


def connect(path: str | Path, *, apply_migrations: bool = True) -> sqlite3.Connection:
    """Open (creating if needed) a spool database and bring it up to date.

    Foreign keys are enabled explicitly because SQLite leaves them off by
    default, and row_factory is set so callers get named access.
    """
    path = Path(path)
    if path.parent and str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(str(path))
    except sqlite3.Error as exc:  # pragma: no cover - depends on filesystem
        raise DBError("cannot open database at %s: %s" % (path, exc)) from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if apply_migrations:
        migrate(conn)
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    """Current ``PRAGMA user_version`` of the database."""
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0


def migrate(conn: sqlite3.Connection) -> int:
    """Apply every pending migration. Returns the resulting version.

    Idempotent: calling it on an up-to-date database runs no SQL and returns
    the same version. Each step runs inside a transaction so a failure part way
    through does not leave a half-migrated file with a bumped version.
    """
    current = schema_version(conn)
    for version, sql in MIGRATIONS:
        if version <= current:
            continue
        try:
            # executescript wraps the whole step in its own transaction. The
            # version stamp is written after the DDL succeeds, so a failure
            # part way through leaves the old version in place and the step is
            # retried on the next open.
            conn.executescript(sql)
            conn.execute("PRAGMA user_version = %d" % version)
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            raise DBError("migration to version %d failed: %s" % (version, exc)) from exc
        current = version
    return current


@contextmanager
def open_db(path: str | Path) -> Iterator[sqlite3.Connection]:
    """Context manager wrapper around :func:`connect`."""
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Row <-> dataclass mapping
# --------------------------------------------------------------------------


def _row_to_spool(row: sqlite3.Row) -> Spool:
    return Spool(
        id=row["id"],
        material=row["material"],
        brand=row["brand"],
        color=row["color"],
        diameter_mm=row["diameter_mm"],
        spool_weight_g=row["spool_weight_g"],
        density_g_cm3=row["density_g_cm3"],
        price=row["price"],
        currency=row["currency"],
        purchased=row["purchased"],
        remaining_g=row["remaining_g"],
        notes=row["notes"],
        archived=bool(row["archived"]),
    )


def _row_to_job(row: sqlite3.Row) -> Job:
    keys = row.keys()
    return Job(
        id=row["id"],
        name=row["name"],
        printer=row["printer"],
        spool_id=row["spool_id"],
        filament_g=row["filament_g"],
        filament_mm=row["filament_mm"],
        duration_s=row["duration_s"],
        status=row["status"],
        started=row["started"],
        failed_at_fraction=row["failed_at_fraction"],
        source=row["source"] if "source" in keys else "manual",
        source_job_id=row["source_job_id"] if "source_job_id" in keys else None,
        notes=row["notes"],
    )


def _row_to_printer(row: sqlite3.Row) -> Printer:
    return Printer(
        id=row["id"],
        name=row["name"],
        watts=row["watts"],
        price=row["price"],
        life_hours=row["life_hours"],
        notes=row["notes"],
    )


# --------------------------------------------------------------------------
# Spools
# --------------------------------------------------------------------------


def add_spool(conn: sqlite3.Connection, spool: Spool) -> Spool:
    """Insert a spool and return it with its assigned id."""
    cur = conn.execute(
        """
        INSERT INTO spools (material, brand, color, diameter_mm, spool_weight_g,
                            density_g_cm3, price, currency, purchased,
                            remaining_g, notes, archived)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            spool.material,
            spool.brand,
            spool.color,
            float(spool.diameter_mm),
            float(spool.spool_weight_g),
            float(spool.density_g_cm3),
            float(spool.price),
            spool.currency,
            spool.purchased,
            float(spool.remaining_g),
            spool.notes,
            1 if spool.archived else 0,
        ),
    )
    conn.commit()
    spool.id = int(cur.lastrowid)
    return spool


def get_spool(conn: sqlite3.Connection, spool_id: int) -> Optional[Spool]:
    """Fetch one spool by id, or None."""
    row = conn.execute("SELECT * FROM spools WHERE id = ?", (spool_id,)).fetchone()
    return _row_to_spool(row) if row else None


def list_spools(conn: sqlite3.Connection, *, include_archived: bool = False) -> list[Spool]:
    """All spools, newest last. Archived spools are hidden unless asked for."""
    if include_archived:
        rows = conn.execute("SELECT * FROM spools ORDER BY id").fetchall()
    else:
        rows = conn.execute("SELECT * FROM spools WHERE archived = 0 ORDER BY id").fetchall()
    return [_row_to_spool(r) for r in rows]


def set_spool_archived(conn: sqlite3.Connection, spool_id: int, archived: bool) -> bool:
    """Archive or un-archive a spool. Returns False when the id is unknown."""
    cur = conn.execute(
        "UPDATE spools SET archived = ? WHERE id = ?",
        (1 if archived else 0, spool_id),
    )
    conn.commit()
    return cur.rowcount > 0


def restock_spool(conn: sqlite3.Connection, spool_id: int, grams: Optional[float] = None) -> Optional[Spool]:
    """Refill a spool to ``grams`` (default: its as-new weight) and un-archive it.

    Used for the common case of buying the same filament again, and for
    correcting a drifted remaining figure after weighing the spool.
    """
    spool = get_spool(conn, spool_id)
    if spool is None:
        return None
    target = float(grams) if grams is not None else float(spool.spool_weight_g)
    target = max(0.0, target)
    conn.execute(
        "UPDATE spools SET remaining_g = ?, archived = 0 WHERE id = ?",
        (target, spool_id),
    )
    conn.commit()
    spool.remaining_g = target
    spool.archived = False
    return spool


def decrement_spool(conn: sqlite3.Connection, spool_id: int, grams: float) -> float:
    """Take ``grams`` off a spool. Returns the shortfall in grams.

    A spool never goes negative. If a job claims more filament than the spool
    is recorded as holding, the spool clamps to zero and the unmet amount comes
    back as a positive shortfall so the caller can tell the user their
    inventory has drifted from reality rather than silently inventing filament.
    """
    row = conn.execute("SELECT remaining_g FROM spools WHERE id = ?", (spool_id,)).fetchone()
    if row is None:
        raise DBError("no spool with id %s" % spool_id)
    remaining = float(row["remaining_g"])
    grams = max(0.0, float(grams))
    shortfall = max(0.0, grams - remaining)
    new_remaining = max(0.0, remaining - grams)
    conn.execute("UPDATE spools SET remaining_g = ? WHERE id = ?", (new_remaining, spool_id))
    conn.commit()
    return shortfall


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------


def add_job(conn: sqlite3.Connection, job: Job, *, decrement: bool = True) -> tuple[Job, float]:
    """Record a job and (by default) take its filament off the spool.

    Returns the stored job and the shortfall in grams, which is 0.0 in the
    normal case. Only the filament actually consumed is decremented, so a print
    that failed 40 percent of the way through only costs 40 percent of its
    filament.
    """
    cur = conn.execute(
        """
        INSERT INTO jobs (name, printer, spool_id, filament_g, filament_mm,
                          duration_s, status, started, failed_at_fraction,
                          notes, source, source_job_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job.name,
            job.printer,
            job.spool_id,
            float(job.filament_g),
            job.filament_mm,
            int(job.duration_s),
            job.status,
            job.started,
            job.failed_at_fraction,
            job.notes,
            job.source,
            job.source_job_id,
        ),
    )
    conn.commit()
    job.id = int(cur.lastrowid)

    shortfall = 0.0
    if decrement and job.spool_id is not None:
        shortfall = decrement_spool(conn, job.spool_id, job.grams_used())
    return job, shortfall


def get_job(conn: sqlite3.Connection, job_id: int) -> Optional[Job]:
    """Fetch one job by id, or None."""
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _row_to_job(row) if row else None


def find_job_by_source(conn: sqlite3.Connection, source: str, source_job_id: str) -> Optional[Job]:
    """Look up a previously synced job by its origin. Powers sync idempotency."""
    row = conn.execute(
        "SELECT * FROM jobs WHERE source = ? AND source_job_id = ?",
        (source, source_job_id),
    ).fetchone()
    return _row_to_job(row) if row else None


def list_jobs(
    conn: sqlite3.Connection,
    *,
    since: Optional[str] = None,
    until: Optional[str] = None,
    spool_id: Optional[int] = None,
    limit: Optional[int] = None,
) -> list[Job]:
    """Jobs in start order, oldest first, with optional ISO date bounds.

    ``since`` and ``until`` are compared as strings, which is correct for
    ISO-8601 timestamps and avoids a parse step on every row.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if since:
        clauses.append("started >= ?")
        params.append(since)
    if until:
        clauses.append("started <= ?")
        params.append(until)
    if spool_id is not None:
        clauses.append("spool_id = ?")
        params.append(spool_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = "SELECT * FROM jobs %s ORDER BY started IS NULL, started, id" % where
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_job(r) for r in rows]


def count_jobs(conn: sqlite3.Connection) -> int:
    """Total number of recorded jobs."""
    return int(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])


# --------------------------------------------------------------------------
# Printers
# --------------------------------------------------------------------------


def add_printer(conn: sqlite3.Connection, printer: Printer) -> Printer:
    """Insert or update a printer, keyed on its unique name."""
    existing = get_printer(conn, printer.name)
    if existing is not None:
        conn.execute(
            "UPDATE printers SET watts = ?, price = ?, life_hours = ?, notes = ? WHERE id = ?",
            (
                float(printer.watts),
                float(printer.price),
                float(printer.life_hours),
                printer.notes,
                existing.id,
            ),
        )
        conn.commit()
        printer.id = existing.id
        return printer
    cur = conn.execute(
        "INSERT INTO printers (name, watts, price, life_hours, notes) VALUES (?, ?, ?, ?, ?)",
        (
            printer.name,
            float(printer.watts),
            float(printer.price),
            float(printer.life_hours),
            printer.notes,
        ),
    )
    conn.commit()
    printer.id = int(cur.lastrowid)
    return printer


def get_printer(conn: sqlite3.Connection, name: str) -> Optional[Printer]:
    """Fetch one printer by name, or None."""
    row = conn.execute("SELECT * FROM printers WHERE name = ?", (name,)).fetchone()
    return _row_to_printer(row) if row else None


def list_printers(conn: sqlite3.Connection) -> list[Printer]:
    """Every registered printer, by name."""
    rows = conn.execute("SELECT * FROM printers ORDER BY name").fetchall()
    return [_row_to_printer(r) for r in rows]


def printer_map(conn: sqlite3.Connection) -> dict[str, Printer]:
    """Printers keyed by name, for cost lookups without an N+1 query."""
    return {p.name: p for p in list_printers(conn)}


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    """Write one setting. Values are stored as text and coerced on read."""
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    conn.commit()


def all_settings(conn: sqlite3.Connection) -> dict[str, str]:
    """Every stored setting as raw strings."""
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}


def load_settings(conn: sqlite3.Connection) -> Settings:
    """Build a :class:`~spool.models.Settings` from the settings table.

    Unknown keys are ignored and unparseable values fall back to the dataclass
    default, so a hand-edited database cannot crash the cost report.
    """
    settings = Settings()
    stored = all_settings(conn)
    for key, caster in Settings.FIELD_TYPES.items():
        if key not in stored:
            continue
        try:
            setattr(settings, key, caster(stored[key]))
        except (TypeError, ValueError):
            continue
    return settings


def save_settings(conn: sqlite3.Connection, settings: Settings) -> None:
    """Persist every field of a Settings object."""
    for key in Settings.FIELD_TYPES:
        set_setting(conn, key, getattr(settings, key))


def table_names(conn: sqlite3.Connection) -> Sequence[str]:
    """Names of the tables present, for diagnostics and tests."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    return [r["name"] for r in rows]
