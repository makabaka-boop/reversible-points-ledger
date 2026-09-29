"""Ledger service: all write paths go through one serialised transaction."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Sequence

from .db import NegativeBalance, immediate
from .errors import BadRequest, Conflict, NotFound


def _guard(exc: Exception) -> None:
    """Translate a commit-time / trigger failure into a domain error."""
    if isinstance(exc, NegativeBalance) or "balance would become negative" in str(exc):
        raise Conflict("insufficient funds: balance would become negative") from exc
    if "period is closed" in str(exc):
        raise Conflict("no open period available") from exc
    if isinstance(exc, sqlite3.IntegrityError):
        raise Conflict(f"ledger constraint violated: {exc}") from exc
    raise exc


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _open_period(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, seq FROM periods WHERE status = 'open' ORDER BY seq DESC"
    ).fetchone()
    if row is None:  # pragma: no cover - invariant guaranteed by trigger
        raise Conflict("no open period")
    return row


def _account(conn: sqlite3.Connection, account_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, name FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()
    if row is None:
        raise NotFound(f"account {account_id} not found")
    return row


def _balance(conn: sqlite3.Connection, account_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS b FROM entries WHERE account_id = ?",
        (account_id,),
    ).fetchone()
    return int(row["b"])


def _insert_transaction(
    conn: sqlite3.Connection,
    *,
    period_id: int,
    kind: str,
    reversal_of_id: int | None = None,
    batch_id: int | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO transactions
            (period_id, batch_id, kind, reversal_of_id, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (period_id, batch_id, kind, reversal_of_id, _now()),
    )
    return int(cur.lastrowid)


def _insert_entry(
    conn: sqlite3.Connection,
    *,
    txn_id: int,
    account_id: int,
    period_id: int,
    amount: int,
) -> None:
    conn.execute(
        """
        INSERT INTO entries (txn_id, account_id, period_id, amount, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (txn_id, account_id, period_id, amount, _now()),
    )


# ---------------------------------------------------------------------------
# Accounts / issuance
# ---------------------------------------------------------------------------


def create_account(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    try:
        with immediate(conn):
            cur = conn.execute(
                "INSERT INTO accounts (name, created_at) VALUES (?, ?)",
                (name, _now()),
            )
            account_id = int(cur.lastrowid)
            row = conn.execute(
                "SELECT id, name, created_at FROM accounts WHERE id = ?",
                (account_id,),
            ).fetchone()
            return dict(row)
    except sqlite3.IntegrityError as exc:
        raise Conflict(f"account name {name!r} already exists") from exc


def issue(conn: sqlite3.Connection, account_id: int, amount: int) -> dict[str, Any]:
    """Issue new points: a single-leg positive entry (kind='issue')."""
    try:
        with immediate(conn):
            _account(conn, account_id)
            period = _open_period(conn)
            txn_id = _insert_transaction(conn, period_id=period["id"], kind="issue")
            _insert_entry(
                conn,
                txn_id=txn_id,
                account_id=account_id,
                period_id=period["id"],
                amount=amount,
            )
    except (sqlite3.IntegrityError, NegativeBalance) as exc:
        _guard(exc)
    return get_transaction(conn, txn_id)


# ---------------------------------------------------------------------------
# Transfers: balanced pairs, batch atomicity
# ---------------------------------------------------------------------------


def post_transfers(
    conn: sqlite3.Connection, transfers: Sequence[dict[str, int]]
) -> dict[str, Any]:
    """Post a batch of transfers.

    All checks and all writes happen in one BEGIN IMMEDIATE transaction:
    either every transfer is booked (two immutable entries each) or none is.
    The non-negative trigger is the final arbiter on balances.
    """
    if not transfers:
        raise BadRequest("transfers batch must not be empty")
    for t in transfers:
        if t["from_account"] == t["to_account"]:
            raise BadRequest("from_account and to_account must differ")
        if t["amount"] <= 0:
            raise BadRequest("amount must be a positive integer")

    try:
        with immediate(conn) as c:
            period = _open_period(c)
            # Validate referenced accounts up front so a batch with a bad leg
            # fails as a whole with 404.
            ids = set()
            for t in transfers:
                ids.add(t["from_account"])
                ids.add(t["to_account"])
            for aid in ids:
                if c.execute("SELECT 1 FROM accounts WHERE id = ?", (aid,)).fetchone() is None:
                    raise NotFound(f"account {aid} not found")

            cur = c.execute("INSERT INTO batches (created_at) VALUES (?)", (_now(),))
            batch_id = int(cur.lastrowid)

            txn_ids: list[int] = []
            for t in transfers:
                txn_id = _insert_transaction(
                    c, period_id=period["id"], kind="transfer", batch_id=batch_id
                )
                # Two opposite, equal entries in the SAME transaction, so the
                # transfer is balanced and indivisible. Whether the batch is
                # legal is decided by the commit-time aggregate balance check
                # in db.immediate, which permits valid balanced-chain
                # intermediate states (A->B, B->C, C->A) but rejects any
                # batch whose committed state would make an account negative.
                _insert_entry(
                    c,
                    txn_id=txn_id,
                    account_id=t["from_account"],
                    period_id=period["id"],
                    amount=-t["amount"],
                )
                _insert_entry(
                    c,
                    txn_id=txn_id,
                    account_id=t["to_account"],
                    period_id=period["id"],
                    amount=t["amount"],
                )
                txn_ids.append(txn_id)
    except (sqlite3.IntegrityError, NegativeBalance) as exc:
        _guard(exc)

    return {
        "batch_id": batch_id,
        "transaction_ids": txn_ids,
        "count": len(txn_ids),
    }


# ---------------------------------------------------------------------------
# Reversals: reference the original txn, succeed at most once
# ---------------------------------------------------------------------------


def reverse_transaction(conn: sqlite3.Connection, txn_id: int) -> dict[str, Any]:
    try:
        with immediate(conn) as c:
            original = c.execute(
                "SELECT id, kind, period_id FROM transactions WHERE id = ?", (txn_id,)
            ).fetchone()
            if original is None:
                raise NotFound(f"transaction {txn_id} not found")
            if original["kind"] != "transfer":
                raise BadRequest("only transfer transactions can be reversed")

            # Old txn reversed after close -> the correcting entries go into
            # the currently open period; the original entries are never
            # touched. Same-period reversal of an open txn is also allowed.
            period = _open_period(c)

            rev_txn_id = _insert_transaction(
                c,
                period_id=period["id"],
                kind="reversal",
                reversal_of_id=original["id"],
            )
            # The UNIQUE(original_txn_id) index makes "at most once" a
            # database decision: two concurrent reversals cannot both win.
            already = c.execute(
                "SELECT 1 FROM reversals WHERE original_txn_id = ?",
                (original["id"],),
            ).fetchone()
            if already is not None:
                raise Conflict(f"transaction {txn_id} has already been reversed")
            c.execute(
                """
                INSERT INTO reversals (original_txn_id, reversal_txn_id, created_at)
                VALUES (?, ?, ?)
                """,
                (original["id"], rev_txn_id, _now()),
            )

            original_entries = c.execute(
                "SELECT account_id, amount FROM entries WHERE txn_id = ? ORDER BY id",
                (original["id"],),
            ).fetchall()
            for e in original_entries:
                _insert_entry(
                    c,
                    txn_id=rev_txn_id,
                    account_id=e["account_id"],
                    period_id=period["id"],
                    amount=-int(e["amount"]),
                )
    except sqlite3.IntegrityError as exc:
        # Belt-and-braces for the unique index under a race not serialised
        # away (cannot happen with BEGIN IMMEDIATE but keep the mapping).
        raise Conflict(f"transaction {txn_id} has already been reversed") from exc
    except NegativeBalance as exc:
        _guard(exc)

    return get_transaction(conn, rev_txn_id)


# ---------------------------------------------------------------------------
# Period close: balance snapshots, then reopen a new period
# ---------------------------------------------------------------------------


def close_current_period(conn: sqlite3.Connection) -> dict[str, Any]:
    with immediate(conn) as c:
        period = _open_period(c)
        snapshot_at = _now()
        accounts = c.execute("SELECT id FROM accounts ORDER BY id").fetchall()
        for a in accounts:
            balance = _balance(c, a["id"])
            c.execute(
                """
                INSERT INTO snapshots (period_id, account_id, balance, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (period["id"], a["id"], balance, snapshot_at),
            )
        c.execute(
            "UPDATE periods SET status = 'closed', closed_at = ? WHERE id = ?",
            (snapshot_at, period["id"]),
        )
        next_seq = int(period["seq"]) + 1
        c.execute(
            "INSERT INTO periods (seq, status, opened_at) VALUES (?, 'open', ?)",
            (next_seq, _now()),
        )
        new_period = c.execute(
            "SELECT id, seq, status, opened_at, closed_at FROM periods WHERE seq = ?",
            (next_seq,),
        ).fetchone()
        closed = c.execute(
            "SELECT id, seq, status, opened_at, closed_at FROM periods WHERE id = ?",
            (period["id"],),
        ).fetchone()
        return {"closed_period": dict(closed), "new_period": dict(new_period)}


# ---------------------------------------------------------------------------
# Read models
# ---------------------------------------------------------------------------


def list_accounts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT a.id, a.name, a.created_at,
               COALESCE((SELECT SUM(amount) FROM entries e WHERE e.account_id = a.id), 0) AS balance
        FROM accounts a
        ORDER BY a.id
        """
    ).fetchall()
    return [
        {"id": r["id"], "name": r["name"], "created_at": r["created_at"], "balance": int(r["balance"])}
        for r in rows
    ]


def get_account(conn: sqlite3.Connection, account_id: int) -> dict[str, Any]:
    _account(conn, account_id)
    rows = list_accounts(conn)
    return next(r for r in rows if r["id"] == account_id)


def list_periods(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, seq, status, opened_at, closed_at FROM periods ORDER BY seq"
    ).fetchall()
    return [dict(r) for r in rows]


def get_snapshot(conn: sqlite3.Connection, period_id: int) -> dict[str, Any]:
    period = conn.execute(
        "SELECT id, seq, status, opened_at, closed_at FROM periods WHERE id = ?",
        (period_id,),
    ).fetchone()
    if period is None:
        raise NotFound(f"period {period_id} not found")
    if period["status"] != "closed":
        raise Conflict(f"period {period_id} is still open; no snapshot exists")
    rows = conn.execute(
        """
        SELECT s.account_id, a.name, s.balance
        FROM snapshots s JOIN accounts a ON a.id = s.account_id
        WHERE s.period_id = ?
        ORDER BY s.account_id
        """,
        (period_id,),
    ).fetchall()
    return {"period": dict(period), "balances": [dict(r) for r in rows]}


def list_entries(
    conn: sqlite3.Connection, account_id: int | None = None
) -> list[dict[str, Any]]:
    sql = """
        SELECT e.id, e.txn_id, e.account_id, a.name AS account_name,
               e.period_id, e.amount, e.created_at,
               t.kind AS txn_kind, t.batch_id, t.reversal_of_id
        FROM entries e
        JOIN accounts a ON a.id = e.account_id
        JOIN transactions t ON t.id = e.txn_id
    """
    params: tuple[Any, ...] = ()
    if account_id is not None:
        sql += " WHERE e.account_id = ?"
        params = (account_id,)
    sql += " ORDER BY e.id"
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def list_transactions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, period_id, batch_id, kind, reversal_of_id, created_at
        FROM transactions ORDER BY id
        """
    ).fetchall()
    return [dict(r) for r in rows]


def get_transaction(conn: sqlite3.Connection, txn_id: int) -> dict[str, Any]:
    txn = conn.execute(
        """
        SELECT id, period_id, batch_id, kind, reversal_of_id, created_at
        FROM transactions WHERE id = ?
        """,
        (txn_id,),
    ).fetchone()
    if txn is None:
        raise NotFound(f"transaction {txn_id} not found")
    entries = conn.execute(
        """
        SELECT e.id, e.account_id, a.name AS account_name, e.period_id, e.amount
        FROM entries e JOIN accounts a ON a.id = e.account_id
        WHERE e.txn_id = ? ORDER BY e.id
        """,
        (txn_id,),
    ).fetchall()
    result = dict(txn)
    result["entries"] = [dict(e) for e in entries]
    return result
