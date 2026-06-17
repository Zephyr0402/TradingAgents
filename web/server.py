"""FastAPI web UI for TradingAgents.

Mobile-first personal tool. Flow:
  1. User opens /, sees login page (or app shell if already authenticated).
  2. Login posts password; if device is new, it's auto-registered and a
     30-day session cookie is set.
  3. App shell shows the task list, with a + button to create a new task.
  4. Submitting a task kicks off a BackgroundTask that calls
     TradingAgentsGraph().propagate(). Status transitions
     pending -> running -> completed | failed.
  5. Frontend polls /api/tasks every 3s and renders the list. Clicking a
     task loads /api/tasks/{id} for the full report.

Persistence: SQLite at ~/.tradingagents/web/tasks.db (overridable via
TRADINGAGENTS_WEB_DATA_DIR). Survives restarts.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import sqlite3
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

from web import auth

logger = logging.getLogger("tradingagents.web")
logging.basicConfig(level=os.environ.get("TRADINGAGENTS_LOG_LEVEL", "INFO"))

# ---------------------------------------------------------------------------
# Concurrency: cap simultaneous analyses so the VPS doesn't die when a friend
# hits submit twice in a row.
# ---------------------------------------------------------------------------
MAX_CONCURRENT = int(os.environ.get("TRADINGAGENTS_WEB_MAX_CONCURRENT", "2"))
_run_semaphore = asyncio.Semaphore(MAX_CONCURRENT)

# Hard ceiling on a single analysis. If ollama hangs (TCP connection
# accepted but no response) we want the event loop to free the semaphore
# and mark the task failed, not block every other request forever. The
# underlying thread keeps running — Python can't kill it — but the user
# gets a clear error and the next submit isn't starved.
RUN_TIMEOUT_SECONDS = int(os.environ.get("TRADINGAGENTS_WEB_RUN_TIMEOUT", "900"))

# Dedicated pool for the blocking propagate() call. Sized at MAX_CONCURRENT
# so a queue of stuck jobs doesn't grow unbounded.
_run_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=MAX_CONCURRENT,
    thread_name_prefix="ta-propagate",
)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def _db() -> sqlite3.Connection:
    auth.WEB_DATA_DIR.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: the worker thread that runs propagate() is
    # not the event-loop thread that opened the connection. Safe because
    # each request still uses its own connection and we serialize writes
    # via _run_semaphore (plus WAL handles concurrent readers).
    conn = sqlite3.connect(auth.DB_FILE, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT    NOT NULL,
    trade_date      TEXT    NOT NULL,
    asset_type      TEXT    NOT NULL DEFAULT 'stock',
    status          TEXT    NOT NULL DEFAULT 'pending',
    created_at      TEXT    NOT NULL,
    started_at      TEXT,
    finished_at     TEXT,
    config_json     TEXT    NOT NULL,
    error           TEXT,
    rating          TEXT,
    final_decision  TEXT,
    full_state      TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_status  ON tasks(status);
"""


def init_db() -> None:
    with _db() as conn:
        conn.executescript(SCHEMA)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class LoginIn(BaseModel):
    password: str


class TaskIn(BaseModel):
    ticker: str = Field(min_length=1, max_length=20)
    trade_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    asset_type: str = Field(default="stock", pattern=r"^(stock|crypto)$")
    # LLM provider is hard-coded to a local Ollama instance. The frontend
    # hides the picker; these defaults apply if the field is missing or
    # someone POSTs a different value (we still force ollama below).
    llm_provider: str = "ollama"
    deep_think_llm: str = "minimax-m3:cloud"
    quick_think_llm: str = "minimax-m3:cloud"
    max_debate_rounds: int = Field(default=1, ge=1, le=5)
    output_language: str = "English"


class TaskSummary(BaseModel):
    id: int
    ticker: str
    trade_date: str
    asset_type: str
    status: str
    created_at: str
    started_at: Optional[str]
    finished_at: Optional[str]
    rating: Optional[str]
    error: Optional[str]


class TaskDetail(TaskSummary):
    config: dict
    final_decision: Optional[str]
    reports: dict  # extracted agent reports for the detail view


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def _row_to_summary(row: sqlite3.Row) -> TaskSummary:
    return TaskSummary(
        id=row["id"],
        ticker=row["ticker"],
        trade_date=row["trade_date"],
        asset_type=row["asset_type"],
        status=row["status"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        rating=row["rating"],
        error=row["error"],
    )


def _extract_reports(state: dict) -> dict[str, str]:
    """Flatten the final_state into a small dict the frontend can render directly."""
    debate = state.get("investment_debate_state") or {}
    risk = state.get("risk_debate_state") or {}

    return {
        "market": state.get("market_report") or "",
        "sentiment": state.get("sentiment_report") or "",
        "news": state.get("news_report") or "",
        "fundamentals": state.get("fundamentals_report") or "",
        "research_bull": debate.get("bull_history") or "",
        "research_bear": debate.get("bear_history") or "",
        "research_judge": debate.get("judge_decision") or "",
        "trader": state.get("trader_investment_plan") or "",
        "risk_aggressive": risk.get("aggressive_history") or "",
        "risk_conservative": risk.get("conservative_history") or "",
        "risk_neutral": risk.get("neutral_history") or "",
        "risk_judge": risk.get("judge_decision") or "",
    }


def _truncate(s: str, max_bytes: int) -> str:
    """Cap a string to `max_bytes` UTF-8 bytes. Used to keep the front-end
    responsive and prevent LLM blow-ups from filling SQLite.
    """
    if not s:
        return s
    b = s.encode("utf-8", errors="replace")
    if len(b) <= max_bytes:
        return s
    return b[:max_bytes].decode("utf-8", errors="replace") + "\n…(truncated)"


def _make_safe_config(user_cfg: dict) -> dict:
    """Build a TradingAgentsGraph config from the per-task settings.

    We start from DEFAULT_CONFIG so unchanged keys (data dirs, vendor chain,
    etc.) come along for the ride, then overlay the per-task overrides.
    """
    cfg = DEFAULT_CONFIG.copy()
    for k, v in user_cfg.items():
        cfg[k] = v
    return cfg


async def _run_task(task_id: int) -> None:
    """Background worker: load task, run propagate, write results back."""
    async with _run_semaphore:
        now = datetime.now(timezone.utc).isoformat()
        with _db() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not row:
                return
            conn.execute(
                "UPDATE tasks SET status='running', started_at=? WHERE id=?",
                (now, task_id),
            )

        try:
            cfg = json.loads(row["config_json"])
            ta_cfg = _make_safe_config(cfg)

            # TradingAgentsGraph() does real work; offload to a thread so the
            # event loop stays responsive (the other endpoints can serve
            # status polls while a run is in flight). wait_for releases the
            # semaphore and marks the task failed if ollama hangs longer
            # than RUN_TIMEOUT_SECONDS — the worker thread keeps running
            # in the background (Python can't kill it) but won't starve
            # the next request.
            loop = asyncio.get_running_loop()
            try:
                final_state, rating = await asyncio.wait_for(
                    loop.run_in_executor(
                        _run_executor,
                        _propagate_blocking,
                        row["ticker"],
                        row["trade_date"],
                        ta_cfg,
                        row["asset_type"],
                    ),
                    timeout=RUN_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"propagate() exceeded {RUN_TIMEOUT_SECONDS}s — likely a "
                    "hung ollama connection. The background thread is still "
                    "running but this task is being marked failed so the "
                    "queue can move on."
                )

            decision = _truncate(final_state.get("final_trade_decision") or "", 200_000)
            reports = _extract_reports(final_state)
            reports = {k: _truncate(v, 200_000) for k, v in reports.items()}
            finished = datetime.now(timezone.utc).isoformat()
            with _db() as conn:
                conn.execute(
                    """
                    UPDATE tasks
                    SET status='completed', finished_at=?, rating=?,
                        final_decision=?, full_state=?
                    WHERE id=?
                    """,
                    (finished, rating, decision, json.dumps(reports), task_id),
                )
            logger.info("Task %d completed: %s rating=%s", task_id, row["ticker"], rating)

        except Exception as exc:
            # Keep the traceback in server logs but persist a short message
            # to the DB so a runaway LLM can't dump megabytes of stack
            # frames into the API response.
            tb = traceback.format_exc(limit=4)
            logger.exception("Task %d failed: %s", task_id, exc)
            with _db() as conn:
                conn.execute(
                    "UPDATE tasks SET status='failed', finished_at=?, error=? WHERE id=?",
                    (
                        datetime.now(timezone.utc).isoformat(),
                        _truncate(f"{type(exc).__name__}: {exc}", 500),
                        task_id,
                    ),
                )


def _propagate_blocking(ticker: str, trade_date: str, cfg: dict, asset_type: str):
    """Run propagate in a worker thread. Sets debug=False to keep logs sane."""
    cfg = dict(cfg)
    cfg["debug"] = False
    ta = TradingAgentsGraph(debug=False, config=cfg)
    return ta.propagate(ticker, trade_date, asset_type=asset_type)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail fast if the password isn't set — much better than discovering it
    # on first request.
    auth.get_password()
    init_db()
    logger.info("Web UI ready. Data dir: %s", auth.WEB_DATA_DIR)
    yield


app = FastAPI(title="TradingAgents", lifespan=lifespan, docs_url=None, redoc_url=None)


# ----- static --------------------------------------------------------------


STATIC_DIR = Path(__file__).parent / "static"


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ----- auth ----------------------------------------------------------------


@app.post("/api/login")
async def login(body: LoginIn, request: Request, response: Response):
    if not auth.verify_password(body.password):
        # Constant-ish delay to make blind guessing less pleasant.
        await asyncio.sleep(0.5)
        raise HTTPException(status_code=401, detail="Wrong password")
    auth.register_device(request)
    auth.issue_session(response)
    return {"ok": True}


@app.post("/api/logout")
async def logout(response: Response):
    auth.clear_session(response)
    return {"ok": True}


@app.get("/api/me")
async def me(request: Request, _=Depends(auth.require_session)):
    return {
        "device_count": len(auth.list_devices()),
        "concurrent_limit": MAX_CONCURRENT,
        "running": _current_running_count(),
    }


def _current_running_count() -> int:
    with _db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status IN ('pending','running')"
        ).fetchone()[0]


# ----- models --------------------------------------------------------------


# Resolved at first call; OLLAMA_BASE_URL is the same env var the rest of
# TradingAgents uses, so the answer is consistent with what propagate()
# will actually call.
def _ollama_base() -> str:
    base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    # Strip the /v1 suffix used by OpenAI-compat clients; /api/tags lives
    # at the server root.
    parsed = urlparse(base)
    root = f"{parsed.scheme}://{parsed.netloc}"
    return root


# Default option shown even if ollama is unreachable. Matches the model we
# hard-code in submit_task so the dropdown is never empty.
_FALLBACK_MODELS = ["minimax-m3:cloud"]

# Cache /api/tags responses for this long. Cuts chatter when the user opens
# the new-task form repeatedly; pulls are still cheap but 200ms each adds up
# on a phone over LTE. Negative results (ollama down) are cached shorter so
# the UI recovers quickly when ollama comes back.
_CACHE_TTL_OK = 30.0
_CACHE_TTL_FAIL = 5.0
_models_cache: dict[str, Any] = {"payload": None, "expires": 0.0}


def _fmt_size(n: int) -> str:
    if not n:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


@app.get("/api/models")
async def list_models(_=Depends(auth.require_session)) -> dict:
    """Return the list of models available in the local Ollama instance.

    Each entry: ``name`` (the model tag the UI submits back) and ``size``
    (a human-readable string like ``"4.7 GB"``). The frontend groups
    local models under "Local" and any non-local fallback under
    "Recommended" using the source field.

    Ollama unreachable → return at least the fallback so the dropdown
    is never empty.
    """
    import time as _time
    now = _time.monotonic()
    if _models_cache["payload"] and _models_cache["expires"] > now:
        return _models_cache["payload"]

    url = f"{_ollama_base()}/api/tags"
    entries: list[dict] = []
    source = "fallback"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()
        for m in data.get("models", []):
            name = m.get("name")
            if not name:
                continue
            entries.append({
                "name": name,
                "size": _fmt_size(m.get("size", 0)),
            })
        source = "ollama"
    except Exception as exc:
        logger.warning("Failed to fetch ollama models from %s: %s", url, exc)

    # Always merge in the fallback so the UI never shows an empty dropdown,
    # and so newly-pulled models don't accidentally displace the configured
    # default if ollama is mid-restart.
    by_name = {e["name"]: e for e in entries}
    for n in _FALLBACK_MODELS:
        if n not in by_name:
            by_name[n] = {"name": n, "size": ""}
    # Stable order: fallbacks first, then everything else alphabetically.
    ordered = [by_name[n] for n in _FALLBACK_MODELS if n in by_name]
    ordered += sorted(
        (e for e in by_name.values() if e["name"] not in _FALLBACK_MODELS),
        key=lambda e: e["name"],
    )

    payload = {"models": ordered, "source": source}
    _models_cache["payload"] = payload
    _models_cache["expires"] = now + (_CACHE_TTL_OK if source == "ollama" else _CACHE_TTL_FAIL)
    return payload


# ----- tasks ---------------------------------------------------------------


@app.post("/api/tasks")
async def submit_task(
    body: TaskIn,
    background_tasks: BackgroundTasks,
    _=Depends(auth.require_session),
):
    # Enforce a sane limit on queued + running tasks so the queue can't grow
    # unbounded if a user mashes the button.
    if _current_running_count() >= MAX_CONCURRENT * 2:
        raise HTTPException(
            status_code=429,
            detail=f"Too many tasks in flight (limit {MAX_CONCURRENT * 2}). "
                   "Wait for an existing task to finish.",
        )

    # Defense in depth: even if the front-end fires a duplicate submit (or
    # a retry races with the first request), only one active task per
    # (ticker, trade_date) can exist. Return the existing id with 200
    # instead of 201 so callers can tell we deduplicated.
    ticker = body.ticker.upper()
    with _db() as conn:
        existing = conn.execute(
            """
            SELECT id FROM tasks
            WHERE ticker = ? AND trade_date = ? AND status IN ('pending', 'running')
            ORDER BY id DESC LIMIT 1
            """,
            (ticker, body.trade_date),
        ).fetchone()
        if existing:
            return JSONResponse(
                status_code=200,
                content={"id": existing["id"], "deduplicated": True},
            )

    # Pin the LLM stack to the local Ollama instance with the local model.
    # The frontend hides the picker; this guarantees the field can't be
    # overridden by anyone POSTing to the API directly. Swap the two
    # constants below (and the frontend defaults) to retarget.
    LOCAL_PROVIDER = "ollama"
    LOCAL_MODEL = "minimax-m3:cloud"

    cfg = {
        "llm_provider": LOCAL_PROVIDER,
        "deep_think_llm": LOCAL_MODEL,
        "quick_think_llm": LOCAL_MODEL,
        "max_debate_rounds": body.max_debate_rounds,
        "output_language": body.output_language,
    }
    now = datetime.now(timezone.utc).isoformat()
    with _db() as conn:
        cur = conn.execute(
            """
            INSERT INTO tasks (ticker, trade_date, asset_type, status,
                               created_at, config_json)
            VALUES (?, ?, ?, 'pending', ?, ?)
            """,
            (ticker, body.trade_date, body.asset_type, now, json.dumps(cfg)),
        )
        task_id = cur.lastrowid

    background_tasks.add_task(_run_task, task_id)
    return Response(
        status_code=201,
        content=json.dumps({"id": task_id}),
        media_type="application/json",
    )


@app.get("/api/tasks")
async def list_tasks(
    request: Request,
    limit: int = 50,
    _=Depends(auth.require_session),
) -> list[TaskSummary]:
    limit = max(1, min(limit, 200))
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_summary(r) for r in rows]


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: int, _=Depends(auth.require_session)) -> TaskDetail:
    with _db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")

    summary = _row_to_summary(row)
    cfg = json.loads(row["config_json"])
    reports = json.loads(row["full_state"]) if row["full_state"] else {}

    return TaskDetail(
        **summary.model_dump(),
        config=cfg,
        final_decision=row["final_decision"],
        reports=reports,
    )


# ----- devices -------------------------------------------------------------


@app.get("/api/devices")
async def get_devices(_=Depends(auth.require_session)):
    return auth.list_devices()


class DeviceLabelIn(BaseModel):
    label: str = Field(min_length=1, max_length=64)


@app.post("/api/devices/{fingerprint}/label")
async def label_device(
    fingerprint: str, body: DeviceLabelIn, _=Depends(auth.require_session)
):
    devices = auth.list_devices()
    if fingerprint not in devices:
        raise HTTPException(status_code=404, detail="Device not found")
    devices[fingerprint]["label"] = body.label
    auth._save_devices(devices)  # noqa: SLF001 — same module family
    return {"ok": True}


@app.delete("/api/devices/{fingerprint}")
async def delete_device(fingerprint: str, _=Depends(auth.require_session)):
    if not auth.remove_device(fingerprint):
        raise HTTPException(status_code=404, detail="Device not found")
    return {"ok": True}
