"""Invariant rules enforced by the database, exercised through the API and
directly against SQLite."""

from __future__ import annotations

import os
import sqlite3

import httpx
import pytest

from conftest import ADMIN_HEADERS, issue, make_account, transfer_batch


def close(client: httpx.Client) -> dict:
    r = client.post("/periods/close", headers=ADMIN_HEADERS)
    assert r.status_code == 201, r.text
    return r.json()


def test_negative_and_zero_balances_rejected(client: httpx.Client):
    a = make_account(client, "a")
    b = make_account(client, "b")
    r = transfer_batch(
        client, [{"from_account": a, "to_account": b, "amount": 1}]
    )
    assert r.status_code == 409
    issue(client, a, 3)
    assert (
        transfer_batch(
            client, [{"from_account": a, "to_account": b, "amount": 4}]
        ).status_code
        == 409
    )
    assert (
        transfer_batch(
            client, [{"from_account": a, "to_account": b, "amount": 3}]
        ).status_code
        == 201
    )


def test_batch_is_all_or_nothing_even_with_chain(client: httpx.Client):
    a = make_account(client, "a")
    b = make_account(client, "b")
    # a gives b its only unit, then b tries to pass on 2 it never had:
    # the committed state would be b=-1, so the whole batch rolls back.
    issue(client, a, 1)
    r = client.post(
        "/transfers",
        json={
            "transfers": [
                {"from_account": a, "to_account": b, "amount": 1},
                {"from_account": b, "to_account": a, "amount": 2},
            ]
        },
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 409, r.text
    accounts = {x["name"]: x["balance"] for x in client.get("/accounts").json()}
    assert accounts == {"a": 1, "b": 0}


def test_reversal_rules(client: httpx.Client):
    a = make_account(client, "a")
    b = make_account(client, "b")
    issue(client, a, 50)
    txn = transfer_batch(
        client, [{"from_account": a, "to_account": b, "amount": 20}]
    ).json()["transaction_ids"][0]

    # unknown transaction
    assert client.post("/transactions/9999/reverse", headers=ADMIN_HEADERS).status_code == 404
    # cannot reverse an issuance
    issue_txn = client.get("/transactions").json()[0]["id"]
    assert (
        client.post(f"/transactions/{issue_txn}/reverse", headers=ADMIN_HEADERS).status_code
        == 400
    )
    # first reversal succeeds
    assert client.post(f"/transactions/{txn}/reverse", headers=ADMIN_HEADERS).status_code == 201
    # second reversal is rejected (even though it is a different request)
    r = client.post(f"/transactions/{txn}/reverse", headers=ADMIN_HEADERS)
    assert r.status_code == 409
    # and a reversal cannot itself be reversed
    rev_id = [
        t["id"]
        for t in client.get("/transactions").json()
        if t["kind"] == "reversal"
    ][0]
    assert client.post(f"/transactions/{rev_id}/reverse", headers=ADMIN_HEADERS).status_code == 400


def test_reversal_that_would_overdraw_is_rejected(client: httpx.Client):
    a = make_account(client, "a")
    b = make_account(client, "b")
    issue(client, a, 10)
    txn = transfer_batch(
        client, [{"from_account": a, "to_account": b, "amount": 10}]
    ).json()["transaction_ids"][0]
    # b spends its 10 on to a new holder so it cannot give it back.
    c = make_account(client, "c")
    transfer_batch(client, [{"from_account": b, "to_account": c, "amount": 10}])
    r = client.post(f"/transactions/{txn}/reverse", headers=ADMIN_HEADERS)
    assert r.status_code == 409
    # b=0 c=10 a=0 still consistent
    balances = {x["id"]: x["balance"] for x in client.get("/accounts").json()}
    assert balances[b] == 0
    assert balances[c] == 10


def test_closed_period_cannot_be_backfilled(client: httpx.Client):
    a = make_account(client, "a")
    b = make_account(client, "b")
    issue(client, a, 10)
    closed = close(client)
    p1 = closed["closed_period"]["id"]

    # issuance and transfer attempts now go to the new open period or fail;
    # they never touch period 1.
    issue(client, a, 5)
    transfer_batch(client, [{"from_account": a, "to_account": b, "amount": 2}])
    entries = client.get("/entries").json()
    assert all(
        e["period_id"] != p1 or e["amount"] != 0 for e in entries
    )
    period1_entries = [e for e in entries if e["period_id"] == p1]
    assert {e["amount"] for e in period1_entries} == {10}  # only the original issue

    # the snapshot still shows the at-close state
    snap = client.get(f"/periods/{p1}/snapshot").json()["balances"]
    assert {row["balance"] for row in snap if row["name"] in ("a", "b")} == {10, 0}

    # closing again closes the *new* open period; periods only accumulate
    close(client)
    statuses = [p["status"] for p in client.get("/periods").json()]
    assert statuses.count("open") == 1
    assert statuses.count("closed") == 2


def test_exactly_one_open_period(client: httpx.Client):
    statuses = [p["status"] for p in client.get("/periods").json()]
    assert statuses == ["open"]
    close(client)
    statuses = [p["status"] for p in client.get("/periods").json()]
    assert statuses == ["closed", "open"]


def test_admin_required(client: httpx.Client):
    assert client.post("/accounts", json={"name": "x"}).status_code == 401
    assert (
        client.post(
            "/accounts", json={"name": "x"}, headers={"X-Admin-Key": "wrong"}
        ).status_code
        == 401
    )


@pytest.mark.parametrize(
    "table,stmt",
    [
        ("entries", "UPDATE entries SET amount = 999 WHERE id = (SELECT MIN(id) FROM entries)"),
        ("entries", "DELETE FROM entries"),
        ("transactions", "DELETE FROM transactions"),
        ("snapshots", "DELETE FROM snapshots"),
    ],
)
def test_database_rejects_mutation_or_deletion_directly(client, table, stmt):
    a = make_account(client, "a")
    issue(client, a, 7)
    close(client)  # creates snapshots

    db_path = os.environ.get("LEDGER_DB", "/data/ledger.db")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(stmt)
    finally:
        conn.close()

    # balances unchanged after the refused mutation
    accounts = {x["id"]: x["balance"] for x in client.get("/accounts").json()}
    assert accounts[a] == 7


def test_database_rejects_insert_into_closed_period_directly(client):
    a = make_account(client, "a")
    issue(client, a, 1)
    closed = close(client)
    p1 = closed["closed_period"]["id"]

    db_path = os.environ.get("LEDGER_DB", "/data/ledger.db")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        # An entry pointing at a closed period trips trg_entries_period_open.
        with pytest.raises(sqlite3.IntegrityError, match="period is closed"):
            conn.execute(
                "INSERT INTO entries (txn_id, account_id, period_id, amount, created_at) "
                "VALUES (1, 1, ?, 1, 't')",
                (p1,),
            )
    finally:
        conn.close()


def test_duplicate_reversal_unique_constraint_directly(client):
    # Two reversals rows pointing at the same original txn cannot exist even
    # if someone bypasses the service layer.
    a = make_account(client, "a")
    b = make_account(client, "b")
    issue(client, a, 5)
    txn = transfer_batch(
        client, [{"from_account": a, "to_account": b, "amount": 1}]
    ).json()["transaction_ids"][0]
    assert client.post(f"/transactions/{txn}/reverse", headers=ADMIN_HEADERS).status_code == 201

    db_path = os.environ.get("LEDGER_DB", "/data/ledger.db")
    conn = sqlite3.connect(db_path)
    try:
        rev = conn.execute(
            "INSERT INTO transactions (period_id, kind, created_at) "
            "SELECT period_id, 'reversal', 't' FROM transactions WHERE id = ?",
            (txn,),
        )
        new_id = rev.lastrowid
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            conn.execute(
                "INSERT INTO reversals (original_txn_id, reversal_txn_id, created_at) "
                "VALUES (?, ?, 't')",
                (txn, new_id),
            )
    finally:
        conn.close()
