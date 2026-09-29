"""FastAPI application: integer-only, non-negative points ledger over SQLite."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import ledger
from .db import connect, init_db
from .errors import LedgerError, Unauthorized

ADMIN_KEY = os.environ.get("ADMIN_KEY", "admin-secret")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Points Ledger",
    version="1.0.0",
    description="Integer, non-negative, append-only double-entry ledger.",
    lifespan=lifespan,
)


@app.exception_handler(LedgerError)
async def ledger_error_handler(request: Request, exc: LedgerError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": str(exc)})


@app.exception_handler(sqlite3.OperationalError)
async def operational_error_handler(request: Request, exc: sqlite3.OperationalError) -> JSONResponse:
    if "locked" in str(exc).lower() or "busy" in str(exc).lower():
        return JSONResponse(
            status_code=503, content={"error": "database is busy, please retry"}
        )
    return JSONResponse(status_code=500, content={"error": str(exc)})


# ---------------------------------------------------------------------------
# Request / response models (strict integer validation: bool, float and
# strings are all rejected)
# ---------------------------------------------------------------------------

PositiveInt = Annotated[int, Field(strict=True, gt=0)]
AccountId = Annotated[int, Field(strict=True, gt=0)]


class AccountCreate(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=100)]


class IssueRequest(BaseModel):
    amount: PositiveInt


class TransferItem(BaseModel):
    from_account: AccountId
    to_account: AccountId
    amount: PositiveInt


class BatchRequest(BaseModel):
    transfers: Annotated[list[TransferItem], Field(min_length=1, max_length=10_000)]


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


@contextmanager
def _connection() -> Iterator[sqlite3.Connection]:
    # Opened AND closed in the same worker thread. (A FastAPI sync generator
    # dependency resumes its cleanup on the event-loop thread, which violates
    # SQLite's thread affinity; the context manager keeps both in one thread.)
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def require_admin(x_admin_key: Annotated[str | None, Header()] = None) -> bool:
    if x_admin_key != ADMIN_KEY:
        raise Unauthorized("invalid or missing admin key")
    return True


ADMIN_DEP = Depends(require_admin)


# ---------------------------------------------------------------------------
# Health / meta
# ---------------------------------------------------------------------------


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


@app.post("/accounts", status_code=201, tags=["accounts"], dependencies=[ADMIN_DEP])
def create_account(body: AccountCreate) -> dict:
    with _connection() as conn:
        return ledger.create_account(conn, body.name)


@app.get("/accounts", tags=["accounts"])
def accounts() -> list[dict]:
    with _connection() as conn:
        return ledger.list_accounts(conn)


@app.get("/accounts/{account_id}", tags=["accounts"])
def account(account_id: int) -> dict:
    with _connection() as conn:
        return ledger.get_account(conn, account_id)


@app.get("/accounts/{account_id}/entries", tags=["accounts"])
def account_entries(account_id: int) -> list[dict]:
    with _connection() as conn:
        ledger.get_account(conn, account_id)  # 404 if missing
        return ledger.list_entries(conn, account_id=account_id)


# ---------------------------------------------------------------------------
# Issuance and transfers
# ---------------------------------------------------------------------------


@app.post("/accounts/{account_id}/issue", status_code=201, tags=["ledger"], dependencies=[ADMIN_DEP])
def issue(account_id: int, body: IssueRequest) -> dict:
    with _connection() as conn:
        return ledger.issue(conn, account_id, body.amount)


@app.post("/transfers", status_code=201, tags=["ledger"], dependencies=[ADMIN_DEP])
def post_transfers(body: BatchRequest) -> dict:
    with _connection() as conn:
        return ledger.post_transfers(
            conn, [t.model_dump() for t in body.transfers]
        )


@app.get("/transactions", tags=["ledger"])
def transactions() -> list[dict]:
    with _connection() as conn:
        return ledger.list_transactions(conn)


@app.get("/transactions/{txn_id}", tags=["ledger"])
def transaction(txn_id: int) -> dict:
    with _connection() as conn:
        return ledger.get_transaction(conn, txn_id)


@app.post("/transactions/{txn_id}/reverse", status_code=201, tags=["ledger"], dependencies=[ADMIN_DEP])
def reverse(txn_id: int) -> dict:
    with _connection() as conn:
        return ledger.reverse_transaction(conn, txn_id)


@app.get("/entries", tags=["ledger"])
def all_entries() -> list[dict]:
    with _connection() as conn:
        return ledger.list_entries(conn)


# ---------------------------------------------------------------------------
# Periods / closing
# ---------------------------------------------------------------------------


@app.get("/periods", tags=["periods"])
def periods() -> list[dict]:
    with _connection() as conn:
        return ledger.list_periods(conn)


@app.post("/periods/close", status_code=201, tags=["periods"], dependencies=[ADMIN_DEP])
def close_period() -> dict:
    with _connection() as conn:
        return ledger.close_current_period(conn)


@app.get("/periods/{period_id}/snapshot", tags=["periods"])
def period_snapshot(period_id: int) -> dict:
    with _connection() as conn:
        return ledger.get_snapshot(conn, period_id)
