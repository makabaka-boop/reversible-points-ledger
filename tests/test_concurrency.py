"""Concurrency tests: the database is the arbiter.

Real parallel HTTP requests through uvicorn's thread pool:

* N clients racing to overdraw one account -> total never negative;
* N clients racing to reverse the same transaction -> exactly one wins;
* transfer racing against period close -> nothing lands in a closed period
  and snapshots always match the entries.
"""

from __future__ import annotations

import threading

import httpx

from conftest import (
    ADMIN_HEADERS,
    BASE_URL,
    api_balances,
    balances_from_entries,
    issue,
    make_account,
    oracle_balances,
)


def assert_consistent(client: httpx.Client) -> None:
    expected = oracle_balances(client)
    actual = api_balances(client)
    assert set(actual) == set(expected)
    for account_id, balance in actual.items():
        assert balance == expected[account_id]
        assert balance >= 0


def test_concurrent_transfers_cannot_overdraw(client: httpx.Client):
    a = make_account(client, "a")
    n = 12
    others = [make_account(client, f"dst{i}") for i in range(n)]
    issue(client, a, 5)  # only five of the twelve transfers can succeed

    results: list[int] = []
    barrier = threading.Barrier(n)
    errors: list[Exception] = []

    def worker(i: int):
        try:
            with httpx.Client(base_url=BASE_URL, timeout=60) as c:
                barrier.wait()
                r = c.post(
                    "/transfers",
                    json={
                        "transfers": [
                            {"from_account": a, "to_account": others[i], "amount": 1}
                        ]
                    },
                    headers=ADMIN_HEADERS,
                )
                results.append(r.status_code)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert results.count(201) == 5
    assert results.count(409) == 7
    balances = api_balances(client)
    assert balances[a] == 0
    assert sum(balances[d] for d in others) == 5
    assert_consistent(client)


def test_concurrent_reversal_of_same_txn_succeeds_exactly_once(client: httpx.Client):
    a = make_account(client, "a")
    b = make_account(client, "b")
    issue(client, a, 100)
    r = client.post(
        "/transfers",
        json={"transfers": [{"from_account": a, "to_account": b, "amount": 40}]},
        headers=ADMIN_HEADERS,
    )
    txn_id = r.json()["transaction_ids"][0]

    n = 10
    statuses: list[int] = []
    barrier = threading.Barrier(n)
    errors: list[Exception] = []

    def worker():
        try:
            with httpx.Client(base_url=BASE_URL, timeout=60) as c:
                barrier.wait()
                resp = c.post(
                    f"/transactions/{txn_id}/reverse", headers=ADMIN_HEADERS
                )
                statuses.append(resp.status_code)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert statuses.count(201) == 1, statuses
    assert statuses.count(409) == n - 1, statuses

    # Balances are back to the pre-transfer state; original entries survive.
    balances = api_balances(client)
    assert balances[a] == 100
    assert balances[b] == 0
    txn = client.get(f"/transactions/{txn_id}").json()
    assert len(txn["entries"]) == 2
    reversals = [
        t for t in client.get("/transactions").json() if t["kind"] == "reversal"
    ]
    assert len(reversals) == 1
    assert reversals[0]["reversal_of_id"] == txn_id
    assert_consistent(client)


def test_transfer_racing_period_close_never_backfills(client: httpx.Client):
    a = make_account(client, "a")
    b = make_account(client, "b")
    issue(client, a, 1000)

    # A guaranteed transfer booked before the race, so we always have a
    # closed-period transaction whose reversal we can demand.
    pre = client.post(
        "/transfers",
        json={"transfers": [{"from_account": a, "to_account": b, "amount": 100}]},
        headers=ADMIN_HEADERS,
    )
    assert pre.status_code == 201
    guaranteed_txn = pre.json()["transaction_ids"][0]

    outcome: dict[str, int] = {}
    lock = threading.Lock()
    start = threading.Event()

    def do_transfer():
        start.wait()
        with httpx.Client(base_url=BASE_URL, timeout=60) as c:
            r = c.post(
                "/transfers",
                json={
                    "transfers": [{"from_account": a, "to_account": b, "amount": 7}]
                },
                headers=ADMIN_HEADERS,
            )
            with lock:
                outcome["transfer"] = r.status_code
                if r.status_code == 201:
                    outcome["entry_period"] = r.json()["transaction_ids"][0]

    def do_close():
        start.wait()
        with httpx.Client(base_url=BASE_URL, timeout=60) as c:
            r = c.post("/periods/close", headers=ADMIN_HEADERS)
            with lock:
                outcome["close"] = r.status_code

    t1 = threading.Thread(target=do_transfer)
    t2 = threading.Thread(target=do_close)
    t1.start()
    t2.start()
    start.set()
    t1.join()
    t2.join()

    assert outcome["close"] == 201, outcome
    assert outcome["transfer"] in (201, 409), outcome

    periods = client.get("/periods").json()
    closed = [p for p in periods if p["status"] == "closed"]
    assert len(closed) == 1
    open_period = next(p for p in periods if p["status"] == "open")
    closed_period = closed[0]

    # Every entry of the closed period is inside the snapshot, and no entry
    # belonging to a transfer outside the period leaked into it.
    entries = client.get("/entries").json()
    closed_entries = [e for e in entries if e["period_id"] == closed_period["id"]]
    snapshot = {
        row["account_id"]: row["balance"]
        for row in client.get(f"/periods/{closed_period['id']}/snapshot").json()[
            "balances"
        ]
    }
    replay_closed = balances_from_entries(closed_entries, list(snapshot))
    assert snapshot == replay_closed

    if outcome["transfer"] == 201:
        txn = client.get(
            f"/transactions/{outcome['entry_period']}"
        ).json()
        # If the transfer won it was either booked before close (snapshot
        # reflects it) or after close (new open period) — never back-filled.
        entry_periods = {e["period_id"] for e in txn["entries"]}
        assert entry_periods <= {closed_period["id"], open_period["id"]}
        assert len(entry_periods) == 1  # both legs in the same period

    # Nothing can ever be posted to the closed period: reversing a
    # closed-period trade must book the correcting legs into the open period.
    r = client.post(
        f"/transactions/{guaranteed_txn}/reverse",
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 201, r.text
    rev = r.json()
    assert rev["period_id"] == open_period["id"]
    assert {e["period_id"] for e in rev["entries"]} == {open_period["id"]}
    # old snapshot frozen
    snapshot_after = {
        row["account_id"]: row["balance"]
        for row in client.get(f"/periods/{closed_period['id']}/snapshot").json()[
            "balances"
        ]
    }
    assert snapshot_after == snapshot
    assert_consistent(client)
