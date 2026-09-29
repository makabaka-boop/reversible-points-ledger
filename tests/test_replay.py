"""Replay tests: rebuild balances from raw entries and compare everywhere.

These replay a full ledger history (issue, transfers, a reversal in a later
period, another close, transfers across periods) and assert at every stage
that:

* balances served by the API equal balances recomputed from ``GET /entries``;
* closing snapshots equal the entry-recomputed balance at close time and stay
  frozen afterwards;
* cumulative balances equal the sum of all period snapshots.
"""

from __future__ import annotations

import httpx

from conftest import (
    ADMIN_HEADERS,
    api_balances,
    balances_from_entries,
    issue,
    make_account,
    oracle_balances,
    transfer_batch,
)


def get_entries(client: httpx.Client) -> list[dict]:
    return client.get("/entries").json()


def assert_balances_match_entries(client: httpx.Client) -> dict[int, int]:
    expected = oracle_balances(client)
    actual = api_balances(client)
    # every account appears in both views
    assert set(actual) == set(expected)
    for account_id, balance in expected.items():
        assert actual[account_id] == balance
        assert balance >= 0
    return expected


def close_period(client: httpx.Client) -> dict:
    r = client.post("/periods/close", headers=ADMIN_HEADERS)
    assert r.status_code == 201, r.text
    return r.json()


def snapshot_map(client: httpx.Client, period_id: int) -> dict[int, int]:
    r = client.get(f"/periods/{period_id}/snapshot")
    assert r.status_code == 200, r.text
    return {row["account_id"]: row["balance"] for row in r.json()["balances"]}


def test_full_replay_history_is_consistent(client: httpx.Client):
    a = make_account(client, "alice")
    b = make_account(client, "bob")
    c = make_account(client, "carol")

    issue(client, a, 1000)
    issue(client, b, 500)

    ok = transfer_batch(
        client,
        [
            {"from_account": a, "to_account": b, "amount": 300},
            {"from_account": b, "to_account": c, "amount": 200},
            {"from_account": a, "to_account": c, "amount": 100},
        ],
    )
    assert ok.status_code == 201, ok.text
    assert_balances_match_entries(client)  # a=600 b=600 c=300

    # A transfer we will later reverse.
    r = transfer_batch(
        client, [{"from_account": a, "to_account": b, "amount": 400}]
    )
    assert r.status_code == 201, r.text
    reversed_txn = r.json()["transaction_ids"][0]
    assert_balances_match_entries(client)  # a=200 b=1000 c=300

    # --- close period 1; snapshot must equal entry-derived balances ---------
    before_close = balances_from_entries(get_entries(client))
    closed = close_period(client)
    p1 = closed["closed_period"]["id"]
    assert snapshot_map(client, p1) == before_close

    # --- period 2: reversal of an old trade lands in the open period --------
    r = client.post(f"/transactions/{reversed_txn}/reverse", headers=ADMIN_HEADERS)
    assert r.status_code == 201, r.text
    reversal = r.json()
    assert reversal["kind"] == "reversal"
    assert reversal["period_id"] != p1
    # original entries are still present and untouched
    original = client.get(f"/transactions/{reversed_txn}").json()
    assert len(original["entries"]) == 2
    assert {e["amount"] for e in original["entries"]} == {-400, 400}
    # reversal entries are the mirror image
    assert {e["amount"] for e in reversal["entries"]} == {-400, 400}

    after_reversal = assert_balances_match_entries(client)  # a=600 b=600 c=300

    # More activity in period 2.
    issue(client, c, 700)
    assert transfer_batch(
        client, [{"from_account": c, "to_account": a, "amount": 500}]
    ).status_code == 201
    balances_p2_end = assert_balances_match_entries(client)

    closed2 = close_period(client)
    p2 = closed2["closed_period"]["id"]
    # snapshot of period 2 = full history replay at that point
    assert snapshot_map(client, p2) == balances_p2_end
    # period-1 snapshot did not change
    assert snapshot_map(client, p1) == before_close

    # --- period 3: cumulative balance == latest snapshot --------------------
    issue(client, b, 10)
    final = assert_balances_match_entries(client)
    assert final[a] == after_reversal[a] + 500
    assert final[b] == after_reversal[b] + 10
    assert final[c] == after_reversal[c] + 700 - 500

    # Snapshot chain: period-N closing balance equals the replay of all
    # entries up to that point.
    entries = get_entries(client)
    history = balances_from_entries(
        [e for e in entries if e["period_id"] <= p2]
    )
    assert snapshot_map(client, p2) == history


def test_every_transfer_has_balanced_opposite_pair(client: httpx.Client):
    a = make_account(client, "a")
    b = make_account(client, "b")
    issue(client, a, 10)
    r = transfer_batch(
        client,
        [
            {"from_account": a, "to_account": b, "amount": 3},
            {"from_account": a, "to_account": b, "amount": 2},
        ],
    )
    assert r.status_code == 201
    for txn_id in r.json()["transaction_ids"]:
        txn = client.get(f"/transactions/{txn_id}").json()
        amounts = sorted(e["amount"] for e in txn["entries"])
        assert len(amounts) == 2
        assert amounts[0] == -amounts[1]
        assert amounts[0] < 0 < amounts[1]
    assert_balances_match_entries(client)


def test_amounts_must_be_integers(client: httpx.Client):
    a = make_account(client, "a")
    for bad in (1.5, "10", True, None, -1, 0):
        r = client.post(
            f"/accounts/{a}/issue", json={"amount": bad}, headers=ADMIN_HEADERS
        )
        assert r.status_code == 422, bad
    r = client.post(
        "/transfers",
        json={"transfers": [{"from_account": a, "to_account": 999, "amount": 1.0}]},
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 422
