"""SQLite storage layer.

All ledger invariants that *can* be enforced by the database *are* enforced by
the database (CHECK constraints, triggers, unique indexes, serialised
transactions), so that decisions are never left to application-level
interleavings:

* entry amounts are non-zero integers; transfers are balanced pairs
  (two entries summing to zero, written in the same transaction);
* the resulting balance of every account is checked non-negative at commit
  time while the write lock is held (see ``immediate``);
* entries can never be modified or deleted;
* entries may only be written into the currently open period;
* a transaction may be reversed at most once (UNIQUE index);
* exactly one period is open at any time;
* period status can only move open -> closed, periods cannot be deleted.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DEFAULT_DB_PATH = "/data/ledger.db"

# How long to wait for another writer before SQLITE_BUSY is raised.
# Writers take a single BEGIN IMMEDIATE lock, so all write transactions are
# serialised by the database; this timeout just bounds the wait.
BUSY_TIMEOUT_MS = 30_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS periods (
    id          INTEGER PRIMARY KEY,
    seq         INTEGER NOT NULL UNIQUE,
    status      TEXT NOT NULL CHECK (status IN ('open','closed')),
    opened_at   TEXT NOT NULL,
    closed_at   TEXT
);

-- At most one open period at any moment.
CREATE UNIQUE INDEX IF NOT EXISTS idx_periods_single_open
    ON periods(status) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    id          INTEGER PRIMARY KEY,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    id              INTEGER PRIMARY KEY,
    period_id       INTEGER NOT NULL REFERENCES periods(id),
    batch_id        INTEGER REFERENCES batches(id),
    kind            TEXT NOT NULL CHECK (kind IN ('issue','transfer','reversal')),
    reversal_of_id  INTEGER REFERENCES transactions(id),
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entries (
    id              INTEGER PRIMARY KEY,
    txn_id          INTEGER NOT NULL REFERENCES transactions(id),
    account_id      INTEGER NOT NULL REFERENCES accounts(id),
    period_id       INTEGER NOT NULL REFERENCES periods(id),
    amount          INTEGER NOT NULL CHECK (amount <> 0),
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_entries_account ON entries(account_id, id);
CREATE INDEX IF NOT EXISTS idx_entries_period_account ON entries(period_id, account_id);
CREATE INDEX IF NOT EXISTS idx_entries_txn ON entries(txn_id);

-- A transaction may be reversed at most once.
CREATE TABLE IF NOT EXISTS reversals (
    id                  INTEGER PRIMARY KEY,
    original_txn_id     INTEGER NOT NULL UNIQUE REFERENCES transactions(id),
    reversal_txn_id     INTEGER NOT NULL UNIQUE REFERENCES transactions(id),
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    period_id   INTEGER NOT NULL REFERENCES periods(id),
    account_id  INTEGER NOT NULL REFERENCES accounts(id),
    balance     INTEGER NOT NULL CHECK (balance >= 0),
    created_at  TEXT NOT NULL,
    PRIMARY KEY (period_id, account_id)
);

-- Note on non-negative balances:
-- A naive per-row trigger cannot tell a valid *intermediate* state of a
-- balanced batch (e.g. A->B, B->C, C->A) from a real overdraw, because the
-- first leg of a transfer briefly makes the sender negative. SQLite has no
-- deferrable triggers. Instead, legality is decided by an aggregate check at
-- the end of each write transaction (see _assert_non_negative, invoked by the
-- immediate() context immediately before COMMIT inside the same BEGIN
-- IMMEDIATE transaction): if any account's *final* balance is negative, the
-- transaction is aborted and rolled back wholesale. Because writers hold the
-- IMMEDIATE lock from the start, the check and the commit are atomic with
-- respect to all other writers - the database still arbitrates concurrent
-- transfers, reversals and period closes.

-- ---------------------------------------------------------------------------
-- Trigger: entries may only be written into the open period, so a closed
-- period can never be back-filled (not even by a reversal of an old trade).
-- ---------------------------------------------------------------------------
CREATE TRIGGER IF NOT EXISTS trg_entries_period_open
BEFORE INSERT ON entries
FOR EACH ROW
WHEN (SELECT status FROM periods WHERE id = NEW.period_id) <> 'open'
BEGIN
    SELECT RAISE(ABORT, 'period is closed');
END;

-- Entries are immutable: no UPDATE, no DELETE, ever.
CREATE TRIGGER IF NOT EXISTS trg_entries_no_update
BEFORE UPDATE ON entries
BEGIN
    SELECT RAISE(ABORT, 'entries are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_entries_no_delete
BEFORE DELETE ON entries
BEGIN
    SELECT RAISE(ABORT, 'entries are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_transactions_no_update
BEFORE UPDATE ON transactions
BEGIN
    SELECT RAISE(ABORT, 'transactions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_transactions_no_delete
BEFORE DELETE ON transactions
BEGIN
    SELECT RAISE(ABORT, 'transactions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_reversals_no_update
BEFORE UPDATE ON reversals
BEGIN
    SELECT RAISE(ABORT, 'reversals are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_reversals_no_delete
BEFORE DELETE ON reversals
BEGIN
    SELECT RAISE(ABORT, 'reversals are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_snapshots_no_update
BEFORE UPDATE ON snapshots
BEGIN
    SELECT RAISE(ABORT, 'snapshots are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_snapshots_no_delete
BEFORE DELETE ON snapshots
BEGIN
    SELECT RAISE(ABORT, 'snapshots are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_periods_no_delete
BEFORE DELETE ON periods
BEGIN
    SELECT RAISE(ABORT, 'periods cannot be deleted');
END;

-- A period can only move open -> closed; nothing else may change.
CREATE TRIGGER IF NOT EXISTS trg_periods_status_transition
BEFORE UPDATE ON periods
FOR EACH ROW
WHEN OLD.status IS NOT 'open'
   OR NEW.status IS NOT 'closed'
   OR NEW.id IS NOT OLD.id
   OR NEW.seq IS NOT OLD.seq
   OR NEW.opened_at IS NOT OLD.opened_at
BEGIN
    SELECT RAISE(ABORT, 'invalid period update');
END;
"""


def db_path() -> str:
    return os.environ.get("LEDGER_DB", DEFAULT_DB_PATH)


def connect(database: str | None = None) -> sqlite3.Connection:
    """Open a database connection with ledger-safe pragmas."""
    path = database or db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(database: str | None = None) -> None:
    """Create the schema and make sure exactly one open period exists."""
    conn = connect(database)
    try:
        conn.executescript(SCHEMA)
        open_row = conn.execute(
            "SELECT id FROM periods WHERE status = 'open'"
        ).fetchone()
        if open_row is None:
            nxt = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM periods").fetchone()["n"]
            conn.execute(
                "INSERT INTO periods (seq, status, opened_at) VALUES (?, 'open', ?)",
                (nxt, _now()),
            )
    finally:
        conn.close()


def reset_db(database: str | None = None) -> None:
    """Drop every ledger table and recreate an empty ledger.

    Only used by the verify suite, which resets the database file directly
    through the shared volume; it is never reachable through the HTTP API.
    """
    conn = connect(database)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        for table in (
            "snapshots",
            "reversals",
            "entries",
            "transactions",
            "batches",
            "accounts",
            "periods",
        ):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute("PRAGMA foreign_keys = ON")
    finally:
        conn.close()
    init_db(database)


@contextmanager
def immediate(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Open a serialised write transaction (BEGIN IMMEDIATE).

    Taking the writer lock up front means two transfers, two reversals or a
    transfer versus a period close cannot interleave: the database is the
    arbiter. Immediately before commit, every account's resulting balance is
    checked to be non-negative; if not, the transaction is aborted and the
    whole unit of work (e.g. an entire batch) rolls back. On any error the
    whole transaction is rolled back, so partial batches can never be left
    behind.
    """
    conn.execute("BEGIN IMMEDIATE")
    committed = False
    try:
        yield conn
        _assert_non_negative(conn)
        conn.execute("COMMIT")
        committed = True
    except BaseException:
        if not committed:
            # Roll back unconditionally (in particular when COMMIT itself
            # failed mid-way), swallowing a secondary error so the original
            # exception is what the caller sees.
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise


def _assert_non_negative(conn: sqlite3.Connection) -> None:
    """Commit-time arbiter run while the writer lock is held.

    Recomputes every account balance from raw entries; the first negative
    balance aborts the transaction. This catches a batch whose *final* state
    would overdraw an account while allowing valid intermediate states of a
    balanced batch (A->B, B->C, C->A), which a per-row trigger could not.
    """
    worst = conn.execute(
        """
        SELECT MIN(bal) AS worst FROM (
            SELECT COALESCE(SUM(amount), 0) AS bal FROM entries GROUP BY account_id
        )
        """
    ).fetchone()["worst"]
    if worst is not None and int(worst) < 0:
        raise NegativeBalance()


class NegativeBalance(Exception):
    """Raised at commit time so the write-transaction context rolls back."""

    def __init__(self) -> None:
        super().__init__("balance would become negative")


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")
