"""Shared fixtures for the verify suite.

Tests run against a live HTTP server (the ``api`` compose service) so they
exercise the real uvicorn + thread-pool + SQLite stack, which is what makes
the concurrency arbitration meaningful. The cross-check balance is always
recomputed from the raw entries returned by ``GET /entries``.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import db_path, reset_db  # noqa: E402

BASE_URL = os.environ.get("VERIFY_BASE_URL", "http://api:8000")
ADMIN_KEY = os.environ.get("ADMIN_KEY", "admin-secret")
ADMIN_HEADERS = {"X-Admin-Key": ADMIN_KEY}


@pytest.fixture(scope="session")
def client() -> httpx.Client:
    deadline = time.time() + 30
    last_exc: Exception | None = None
    while time.time() < deadline:
        try:
            r = httpx.get(f"{BASE_URL}/health", timeout=2)
            if r.status_code == 200:
                break
        except Exception as exc:  # server may still be starting
            last_exc = exc
        time.sleep(0.5)
    else:
        raise RuntimeError(f"API never became healthy at {BASE_URL}: {last_exc}")

    with httpx.Client(base_url=BASE_URL, timeout=60) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_ledger(client: httpx.Client):
    # Reset the ledger file directly through the shared volume instead of an
    # HTTP backdoor, so the production server image never needs to expose a
    # destructive endpoint.
    reset_db(db_path())
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_account(client: httpx.Client, name: str) -> int:
    r = client.post("/accounts", json={"name": name}, headers=ADMIN_HEADERS)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def issue(client: httpx.Client, account_id: int, amount: int) -> None:
    r = client.post(
        f"/accounts/{account_id}/issue",
        json={"amount": amount},
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 201, r.text


def transfer_batch(client: httpx.Client, items: list[dict]) -> httpx.Response:
    return client.post("/transfers", json={"transfers": items}, headers=ADMIN_HEADERS)


def balances_from_entries(
    entries: list[dict], account_ids: list[int] | None = None
) -> dict[int, int]:
    """The oracle: recompute every balance purely from immutable entries.

    Accounts with no entries are zero-balance and included when their ids are
    given in ``account_ids``.
    """
    totals: dict[int, int] = {aid: 0 for aid in (account_ids or [])}
    for e in entries:
        totals[e["account_id"]] = totals.get(e["account_id"], 0) + e["amount"]
    return totals


def api_balances(client: httpx.Client) -> dict[int, int]:
    return {a["id"]: a["balance"] for a in client.get("/accounts").json()}


def oracle_balances(client: httpx.Client) -> dict[int, int]:
    """Recompute balances from raw entries over the full account universe."""
    account_ids = list(api_balances(client))
    return balances_from_entries(client.get("/entries").json(), account_ids)
