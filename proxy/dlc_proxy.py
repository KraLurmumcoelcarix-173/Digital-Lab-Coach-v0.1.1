"""
DLC course proxy: key custody + machine-keyed limits + telemetry.

One small server the instructor user runs; students' tools point at it via
`proxy_url` in their config.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(app):
    _startup_sanity()
    yield


app = FastAPI(title="DLC course proxy", lifespan=_lifespan)

os.environ["DLC_PROXY_SELF"] = "1"

CALL_BUDGETS = {"modeA": 4, "modeB": 4, "grade": 20, "explain": 20}
_DEFAULT_BUDGET = 12


def _global_daily_calls() -> int:
    try:
        return int(os.environ.get("DLC_GLOBAL_DAILY_CALLS", "") or 600)
    except ValueError:
        return 600


def _global_daily_usd() -> float:
    try:
        return float(os.environ.get("DLC_GLOBAL_DAILY_USD", "") or 20.0)
    except ValueError:
        return 20.0

_PRICES = {
    "claude-sonnet-4-6": (3.0, 15.0), "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0), "gpt-5": (1.25, 10.0),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS machines (
    install_id  TEXT PRIMARY KEY,
    first_seen  TEXT NOT NULL,
    issued_client TEXT,
    id_source   TEXT,
    app_version TEXT,
    last_seen   REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    install_id TEXT NOT NULL,
    client_row_id INTEGER NOT NULL,
    session_id TEXT,
    kind TEXT NOT NULL,
    client_ts REAL,
    stored_at REAL,
    received_at REAL NOT NULL,
    props TEXT NOT NULL,
    UNIQUE(install_id, client_row_id)
);
CREATE INDEX IF NOT EXISTS idx_ev_kind ON events(kind);
CREATE INDEX IF NOT EXISTS idx_ev_machine ON events(install_id);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    install_id TEXT NOT NULL,
    day TEXT NOT NULL,
    ts REAL NOT NULL,
    feature TEXT NOT NULL,
    model TEXT,
    ok INTEGER,
    in_tokens INTEGER,
    out_tokens INTEGER,
    ms INTEGER,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_llm_budget ON llm_calls(install_id, feature, day);
"""


def _db() -> sqlite3.Connection:
    p = Path(os.environ.get("DLC_PROXY_DB", "dlc_proxy.db"))
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.executescript(_SCHEMA)
    try:
        conn.execute("ALTER TABLE llm_calls ADD COLUMN response TEXT")
    except sqlite3.OperationalError:
        pass
    return conn


def _check_course_token(tok: str | None) -> None:
    want = os.environ.get("DLC_COURSE_TOKEN")
    if want and (tok or "") != want:
        raise HTTPException(status_code=401, detail="bad course token")


def _check_admin(token: str | None, header_token: str | None = None) -> None:
    want = os.environ.get("DLC_ADMIN_TOKEN")
    got = header_token or token or ""
    if not want or got != want:
        raise HTTPException(status_code=401, detail="bad admin token")


def _est_usd(conn, day: str | None = None) -> float:
    q = ("SELECT model, COALESCE(SUM(in_tokens),0),"
         " COALESCE(SUM(out_tokens),0) FROM llm_calls")
    args: tuple = ()
    if day:
        q += " WHERE day = ?"
        args = (day,)
    q += " GROUP BY model"
    spend = 0.0
    for model, i, o in conn.execute(q, args):
        pin, pout = _PRICES.get(model, (5.0, 25.0))
        spend += (i * pin + o * pout) / 1e6
    return spend


def _touch_machine(conn, install_id: str, issued=None, source=None,
                   version=None) -> None:
    with conn:
        conn.execute(
            "INSERT INTO machines (install_id, first_seen, issued_client,"
            " id_source, app_version, last_seen) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(install_id) DO UPDATE SET last_seen = ?,"
            " app_version = COALESCE(excluded.app_version, app_version)",
            (install_id, date.today().isoformat(), issued, source,
             version, time.time(), time.time()))


class EventsIn(BaseModel):
    install_id: str
    issued: str | None = None
    id_source: str | None = None
    app_version: str | None = None
    events: list[dict] = []


@app.post("/v1/events")
def ingest(req: EventsIn,
           x_dlc_token: str | None = Header(default=None)) -> dict:
    _check_course_token(x_dlc_token)
    if not req.install_id:
        raise HTTPException(status_code=400, detail="install_id required")
    conn = _db()
    try:
        _touch_machine(conn, req.install_id, req.issued, req.id_source,
                       req.app_version)
        stored = 0
        now = time.time()
        for ev in req.events[:1000]:
            if not isinstance(ev, dict) or not ev.get("kind"):
                continue
            try:
                with conn:
                    conn.execute(
                        "INSERT OR IGNORE INTO events (install_id,"
                        " client_row_id, session_id, kind, client_ts,"
                        " stored_at, received_at, props)"
                        " VALUES (?,?,?,?,?,?,?,?)",
                        (req.install_id,
                         int(ev.get("client_row_id") or 0),
                         ev.get("session_id"),
                         str(ev.get("kind"))[:64],
                         ev.get("client_ts"), ev.get("stored_at"), now,
                         json.dumps(ev.get("props") or {})[:20000]))
                stored += 1
            except (sqlite3.Error, TypeError, ValueError):
                continue
        return {"ok": True, "stored": stored}
    finally:
        conn.close()


class LlmIn(BaseModel):
    install_id: str
    feature: str = "other"
    model: str
    prompt: str
    system: str | None = None
    max_tokens: int = 3000
    effort: str | None = None


@app.post("/v1/llm")
def relay(req: LlmIn,
          x_dlc_token: str | None = Header(default=None)) -> dict:
    _check_course_token(x_dlc_token)
    if not req.install_id:
        raise HTTPException(status_code=400, detail="install_id required")
    day = date.today().isoformat()
    budget = CALL_BUDGETS.get(req.feature, _DEFAULT_BUDGET)
    conn = _db()
    try:
        _touch_machine(conn, req.install_id)
        conn.execute("BEGIN IMMEDIATE")
        (day_calls,) = conn.execute(
            "SELECT COUNT(*) FROM llm_calls WHERE day = ?",
            (day,)).fetchone()
        if (day_calls >= _global_daily_calls()
                or _est_usd(conn, day) >= _global_daily_usd()):
            conn.execute("ROLLBACK")
            return {"ok": False, "text": None,
                    "error": ("The course server has reached its daily"
                              " capacity — please try again tomorrow. All"
                              " deterministic checks still work."),
                    "limit_hit": True, "capacity_hit": True,
                    "usage": None, "model": req.model}
        (used,) = conn.execute(
            "SELECT COUNT(*) FROM llm_calls WHERE install_id = ?"
            " AND feature = ? AND day = ?",
            (req.install_id, req.feature, day)).fetchone()
        if used >= budget:
            conn.execute("ROLLBACK")
            return {"ok": False, "text": None,
                    "error": (f"Daily limit reached for {req.feature} on"
                              f" this machine — it resets tomorrow."),
                    "limit_hit": True, "usage": None, "model": req.model}
        cur = conn.execute(
            "INSERT INTO llm_calls (install_id, day, ts, feature,"
            " model, ok, in_tokens, out_tokens, ms, error)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (req.install_id, day, time.time(), req.feature, req.model,
             0, None, None, None, "pending"))
        row_id = cur.lastrowid
        conn.execute("COMMIT")
        t0 = time.monotonic()
        from dlc.llm.client import call_llm
        r = call_llm(req.prompt, model=req.model,
                     max_tokens=req.max_tokens, system=req.system,
                     effort=req.effort)
        ms = int((time.monotonic() - t0) * 1000)
        u = r.get("usage") or {}
        with conn:
            conn.execute(
                "UPDATE llm_calls SET ts = ?, ok = ?, in_tokens = ?,"
                " out_tokens = ?, ms = ?, error = ?, response = ?"
                " WHERE id = ?",
                (time.time(), 1 if r.get("ok") else 0,
                 u.get("input_tokens"), u.get("output_tokens"), ms,
                 (r.get("error") or "")[:300] or None,
                 (r.get("text") or "")[:20000] or None, row_id))
        r["limit"] = {"feature": req.feature, "used": used + 1,
                      "budget": budget}
        return r
    finally:
        conn.close()


def _effective_key() -> str:
    from dlc.llm.client import get_api_key
    return get_api_key("anthropic") or ""


def _startup_sanity() -> None:
    key = _effective_key()
    if key and not key.startswith("sk-"):
        print("WARNING: ANTHROPIC_API_KEY does not look like a real key"
              " (expected it to start with 'sk-'). LLM relays will fail"
              " with 401 until it is fixed.")
    if not os.environ.get("DLC_COURSE_TOKEN"):
        print("WARNING: DLC_COURSE_TOKEN is not set — the proxy will"
              " accept requests from ANYONE who finds the URL.")


@app.get("/v1/health")
def health() -> dict:
    conn = _db()
    try:
        (m,) = conn.execute("SELECT COUNT(*) FROM machines").fetchone()
        (e,) = conn.execute("SELECT COUNT(*) FROM events").fetchone()
        day = date.today().isoformat()
        (day_calls,) = conn.execute(
            "SELECT COUNT(*) FROM llm_calls WHERE day = ?",
            (day,)).fetchone()
        key = _effective_key()
        db_path = str(Path(os.environ.get("DLC_PROXY_DB",
                                          "dlc_proxy.db")).resolve())
        return {"ok": True, "machines": m, "events": e,
                "db_path": db_path,
                "key_configured": bool(key),
                "key_format_ok": key.startswith("sk-") if key else False,
                "course_token_set": bool(os.environ.get("DLC_COURSE_TOKEN")),
                "today_calls": day_calls,
                "today_est_usd": round(_est_usd(conn, day), 2),
                "global_daily_calls": _global_daily_calls(),
                "global_daily_usd": _global_daily_usd()}
    finally:
        conn.close()


@app.get("/admin/summary")
def summary(token: str | None = Query(default=None),
            x_dlc_admin_token: str | None = Header(default=None)) -> dict:
    _check_admin(token, x_dlc_admin_token)
    conn = _db()
    try:
        machines = [dict(zip(
            ("install_id", "first_seen", "issued_client", "id_source",
             "app_version", "last_seen"), row))
            for row in conn.execute(
                "SELECT install_id, first_seen, issued_client, id_source,"
                " app_version, last_seen FROM machines"
                " ORDER BY first_seen")]
        for m in machines:
            if m["last_seen"]:
                m["last_seen"] = datetime.fromtimestamp(
                    m["last_seen"]).isoformat(timespec="seconds")
            (m["events"],) = conn.execute(
                "SELECT COUNT(*) FROM events WHERE install_id = ?",
                (m["install_id"],)).fetchone()
            (m["llm_calls"],) = conn.execute(
                "SELECT COUNT(*) FROM llm_calls WHERE install_id = ?",
                (m["install_id"],)).fetchone()
        kinds = conn.execute(
            "SELECT kind, COUNT(*) FROM events GROUP BY kind"
            " ORDER BY COUNT(*) DESC LIMIT 30").fetchall()
        spend = 0.0
        for model, i, o in conn.execute(
                "SELECT model, COALESCE(SUM(in_tokens),0),"
                " COALESCE(SUM(out_tokens),0) FROM llm_calls"
                " GROUP BY model"):
            pin, pout = _PRICES.get(model, (5.0, 25.0))
            spend += (i * pin + o * pout) / 1e6
        return {"machines": machines,
                "event_kinds": [{"kind": k, "n": n} for k, n in kinds],
                "llm_spend_est_usd": round(spend, 2)}
    finally:
        conn.close()


@app.get("/admin/daily")
def daily(token: str | None = Query(default=None),
          x_dlc_admin_token: str | None = Header(default=None)) -> dict:
    _check_admin(token, x_dlc_admin_token)
    conn = _db()
    try:
        llm = [dict(zip(("day", "install_id", "feature", "calls",
                         "ok_calls", "in_tokens", "out_tokens"), row))
               for row in conn.execute(
                   "SELECT day, install_id, feature, COUNT(*),"
                   " COALESCE(SUM(ok),0), COALESCE(SUM(in_tokens),0),"
                   " COALESCE(SUM(out_tokens),0) FROM llm_calls"
                   " GROUP BY day, install_id, feature"
                   " ORDER BY day DESC, install_id")]
        spend = {}
        for d, model, i, o in conn.execute(
                "SELECT day, model, COALESCE(SUM(in_tokens),0),"
                " COALESCE(SUM(out_tokens),0) FROM llm_calls"
                " GROUP BY day, model"):
            pin, pout = _PRICES.get(model, (5.0, 25.0))
            spend[d] = spend.get(d, 0.0) + (i * pin + o * pout) / 1e6
        activity = [dict(zip(("day", "install_id", "events"), row))
                    for row in conn.execute(
                        "SELECT date(received_at, 'unixepoch'), install_id,"
                        " COUNT(*) FROM events"
                        " GROUP BY date(received_at, 'unixepoch'), install_id"
                        " ORDER BY 1 DESC, install_id")]
        return {"llm": llm,
                "spend_by_day": [{"day": d, "est_usd": round(v, 2)}
                                 for d, v in sorted(spend.items(),
                                                    reverse=True)],
                "activity": activity}
    finally:
        conn.close()


@app.get("/admin/events")
def admin_events(token: str | None = Query(default=None),
                 x_dlc_admin_token: str | None = Header(default=None),
                 day: str | None = Query(default=None),
                 install_id: str | None = Query(default=None),
                 kind: str | None = Query(default=None),
                 page: int = Query(default=1, ge=1),
                 per_page: int = Query(default=50, ge=1, le=200)) -> dict:
    _check_admin(token, x_dlc_admin_token)
    conn = _db()
    try:
        where, args = [], []
        if day:
            where.append("date(received_at, 'unixepoch') = ?")
            args.append(day)
        if install_id:
            where.append("install_id = ?")
            args.append(install_id)
        if kind:
            where.append("kind = ?")
            args.append(kind)
        w = (" WHERE " + " AND ".join(where)) if where else ""
        (total,) = conn.execute(
            "SELECT COUNT(*) FROM events" + w, args).fetchone()
        rows = [dict(zip(("id", "install_id", "kind", "ts",
                          "session_id", "props"), r))
                for r in conn.execute(
                    "SELECT id, install_id, kind,"
                    " COALESCE(client_ts, received_at), session_id, props"
                    " FROM events" + w +
                    " ORDER BY id DESC LIMIT ? OFFSET ?",
                    (*args, per_page, (page - 1) * per_page))]
        for r in rows:
            try:
                r["props"] = json.loads(r["props"])
            except (TypeError, ValueError):
                r["props"] = {}
            r["time"] = datetime.fromtimestamp(
                r.pop("ts")).isoformat(sep=" ", timespec="seconds")
        days = [d for (d,) in conn.execute(
            "SELECT DISTINCT date(received_at, 'unixepoch') FROM events"
            " ORDER BY 1 DESC LIMIT 120")]
        kinds = [k for (k,) in conn.execute(
            "SELECT DISTINCT kind FROM events ORDER BY kind LIMIT 100")]
        machines = [m for (m,) in conn.execute(
            "SELECT DISTINCT install_id FROM events ORDER BY 1 LIMIT 200")]
        return {"total": total, "page": page, "per_page": per_page,
                "rows": rows, "days": days, "kinds": kinds,
                "machines": machines}
    finally:
        conn.close()


@app.get("/admin/llm_texts")
def admin_llm_texts(token: str | None = Query(default=None),
                    x_dlc_admin_token: str | None = Header(default=None),
                    day: str | None = Query(default=None),
                    install_id: str | None = Query(default=None),
                    feature: str | None = Query(default=None),
                    page: int = Query(default=1, ge=1),
                    per_page: int = Query(default=20, ge=1,
                                          le=100)) -> dict:
    _check_admin(token, x_dlc_admin_token)
    conn = _db()
    try:
        where, args = ["response IS NOT NULL"], []
        if day:
            where.append("day = ?")
            args.append(day)
        if install_id:
            where.append("install_id = ?")
            args.append(install_id)
        if feature:
            where.append("feature = ?")
            args.append(feature)
        w = " WHERE " + " AND ".join(where)
        (total,) = conn.execute(
            "SELECT COUNT(*) FROM llm_calls" + w, args).fetchone()
        rows = [dict(zip(("id", "install_id", "feature", "model", "ok",
                          "in_tokens", "out_tokens", "ms", "ts",
                          "response"), r))
                for r in conn.execute(
                    "SELECT id, install_id, feature, model, ok,"
                    " in_tokens, out_tokens, ms, ts, response"
                    " FROM llm_calls" + w +
                    " ORDER BY id DESC LIMIT ? OFFSET ?",
                    (*args, per_page, (page - 1) * per_page))]
        for r in rows:
            r["time"] = datetime.fromtimestamp(
                r.pop("ts")).isoformat(sep=" ", timespec="seconds")
        days = [d for (d,) in conn.execute(
            "SELECT DISTINCT day FROM llm_calls ORDER BY 1 DESC"
            " LIMIT 120")]
        features = [f for (f,) in conn.execute(
            "SELECT DISTINCT feature FROM llm_calls ORDER BY 1")]
        machines = [m for (m,) in conn.execute(
            "SELECT DISTINCT install_id FROM llm_calls ORDER BY 1"
            " LIMIT 200")]
        return {"total": total, "page": page, "per_page": per_page,
                "rows": rows, "days": days, "features": features,
                "machines": machines}
    finally:
        conn.close()


@app.get("/admin/stats")
def admin_stats(token: str | None = Query(default=None),
                x_dlc_admin_token: str | None = Header(default=None),
                range_days: int = Query(default=7, ge=1,
                                        le=366)) -> dict:
    _check_admin(token, x_dlc_admin_token)
    from datetime import timedelta
    since = (date.today() - timedelta(days=range_days - 1)).isoformat()
    conn = _db()
    try:
        ev_day = "date(received_at, 'unixepoch')"
        active_by_day = [dict(zip(("day", "machines", "events"), r))
                         for r in conn.execute(
            f"SELECT {ev_day}, COUNT(DISTINCT install_id), COUNT(*)"
            f" FROM events WHERE {ev_day} >= ?"
            f" GROUP BY {ev_day} ORDER BY 1", (since,))]
        (active, ev_total) = conn.execute(
            f"SELECT COUNT(DISTINCT install_id), COUNT(*) FROM events"
            f" WHERE {ev_day} >= ?", (since,)).fetchone()
        (new_machines,) = conn.execute(
            "SELECT COUNT(*) FROM machines WHERE first_seen >= ?",
            (since,)).fetchone()
        by_feature = {}
        spend_by_feature = {}
        for feat, model, calls, ok, i, o in conn.execute(
                "SELECT feature, model, COUNT(*), COALESCE(SUM(ok),0),"
                " COALESCE(SUM(in_tokens),0), COALESCE(SUM(out_tokens),0)"
                " FROM llm_calls WHERE day >= ?"
                " GROUP BY feature, model", (since,)):
            f = by_feature.setdefault(feat, {"feature": feat, "calls": 0,
                                             "ok_calls": 0,
                                             "in_tokens": 0,
                                             "out_tokens": 0})
            f["calls"] += calls
            f["ok_calls"] += ok
            f["in_tokens"] += i
            f["out_tokens"] += o
            pin, pout = _PRICES.get(model, (5.0, 25.0))
            spend_by_feature[feat] = spend_by_feature.get(feat, 0.0) +                 (i * pin + o * pout) / 1e6
        features = []
        for feat, f in sorted(by_feature.items()):
            f["est_usd"] = round(spend_by_feature.get(feat, 0.0), 2)
            features.append(f)
        top_kinds = [dict(zip(("kind", "n"), r)) for r in conn.execute(
            f"SELECT kind, COUNT(*) FROM events WHERE {ev_day} >= ?"
            f" GROUP BY kind ORDER BY COUNT(*) DESC LIMIT 15", (since,))]
        (a_runs, a_confirmed, a_cards) = conn.execute(
            f"SELECT COUNT(*),"
            f" COALESCE(SUM(json_extract(props, '$.confirmed')), 0),"
            f" COALESCE(SUM(json_extract(props, '$.cards')), 0)"
            f" FROM events WHERE kind = 'l3_modeA_result_server'"
            f" AND {ev_day} >= ?", (since,)).fetchone()
        (b_runs,) = conn.execute(
            f"SELECT COUNT(*) FROM events"
            f" WHERE kind = 'l3_modeB_result_server' AND {ev_day} >= ?",
            (since,)).fetchone()
        (accepts,) = conn.execute(
            f"SELECT COUNT(*) FROM events"
            f" WHERE kind = 'l3_accept_fix_server' AND {ev_day} >= ?",
            (since,)).fetchone()
        spend_by_day = {}
        calls_by_day = {}
        for d, model, i, o, c in conn.execute(
                "SELECT day, model, COALESCE(SUM(in_tokens),0),"
                " COALESCE(SUM(out_tokens),0), COUNT(*) FROM llm_calls"
                " WHERE day >= ? GROUP BY day, model", (since,)):
            pin, pout = _PRICES.get(model, (5.0, 25.0))
            spend_by_day[d] = spend_by_day.get(d, 0.0) +                 (i * pin + o * pout) / 1e6
            calls_by_day[d] = calls_by_day.get(d, 0) + c
        (calls_total, ok_total, in_tok, out_tok) = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(ok),0),"
            " COALESCE(SUM(in_tokens),0), COALESCE(SUM(out_tokens),0)"
            " FROM llm_calls WHERE day >= ?", (since,)).fetchone()
        return {"since": since, "range_days": range_days,
                "totals": {"active_machines": active,
                           "new_machines": new_machines,
                           "events": ev_total, "llm_calls": calls_total,
                           "ok_calls": ok_total, "in_tokens": in_tok,
                           "out_tokens": out_tok,
                           "est_usd": round(_est_usd_since(conn, since),
                                            2)},
                "active_by_day": active_by_day,
                "by_feature": features,
                "top_kinds": top_kinds,
                "l3": {"modeA_runs": a_runs,
                       "modeA_confirmed": int(a_confirmed or 0),
                       "modeA_cards": int(a_cards or 0),
                       "modeB_runs": b_runs, "fixes_accepted": accepts},
                "spend_by_day": [
                    {"day": d, "est_usd": round(v, 2),
                     "calls": calls_by_day.get(d, 0)}
                    for d, v in sorted(spend_by_day.items())]}
    finally:
        conn.close()


def _est_usd_since(conn, since: str) -> float:
    spend = 0.0
    for model, i, o in conn.execute(
            "SELECT model, COALESCE(SUM(in_tokens),0),"
            " COALESCE(SUM(out_tokens),0) FROM llm_calls WHERE day >= ?"
            " GROUP BY model", (since,)):
        pin, pout = _PRICES.get(model, (5.0, 25.0))
        spend += (i * pin + o * pout) / 1e6
    return spend


@app.get("/admin/export.csv", response_class=PlainTextResponse)
def export_csv(token: str | None = Query(default=None),
               x_dlc_admin_token: str | None = Header(default=None),
               table: str = Query(default="events")) -> str:
    _check_admin(token, x_dlc_admin_token)
    if table not in ("events", "machines", "llm_calls"):
        raise HTTPException(status_code=400, detail="unknown table")
    conn = _db()
    try:
        cur = conn.execute(f"SELECT * FROM {table}")
        cols = [d[0] for d in cur.description]
        lines = [",".join(cols)]
        for row in cur:
            cells = []
            for v in row:
                s = "" if v is None else str(v)
                if any(ch in s for ch in ',"\n'):
                    s = '"' + s.replace('"', '""') + '"'
                cells.append(s)
            lines.append(",".join(cells))
        return "\n".join(lines) + "\n"
    finally:
        conn.close()


_ADMIN_PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DLC course dashboard</title>
<link rel="icon" href="data:,">
<style>
 body{font:14px/1.45 system-ui,sans-serif;margin:0;background:#f6f7f9;color:#111827}
 header{background:#111827;color:#f9fafb;padding:12px 20px;display:flex;
        justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
 header h1{font-size:16px;margin:0}
 main{max-width:1200px;margin:0 auto;padding:18px}
 .tiles{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:14px}
 .tile{background:#fff;border:1px solid #e5e7eb;border-radius:8px;
       padding:10px 16px;min-width:120px}
 .tile b{display:block;font-size:20px}
 .tile span{color:#6b7280;font-size:12px}
 .warn{color:#b45309}
 h2{font-size:15px;margin:18px 0 8px}
 table{border-collapse:collapse;width:100%;background:#fff;
       border:1px solid #e5e7eb;border-radius:8px;overflow:hidden}
 th,td{padding:6px 10px;border-bottom:1px solid #eef0f3;text-align:left;
       font-size:13px;vertical-align:top}
 th{background:#f3f4f6;color:#374151;white-space:nowrap}
 tr:last-child td{border-bottom:none}
 td.nw,th.nw{white-space:nowrap}
 #gate{max-width:420px;margin:80px auto;background:#fff;padding:24px;
       border:1px solid #e5e7eb;border-radius:10px}
 #gate input{width:100%;padding:8px;margin:10px 0;box-sizing:border-box}
 #gate button{padding:8px 16px}
 .err{color:#b91c1c}
 .muted{color:#6b7280}
 button.ghost{background:none;border:1px solid #6b7280;color:#f9fafb;
              border-radius:6px;padding:4px 10px;cursor:pointer}
 .tabs{display:flex;gap:6px;margin:4px 0 12px}
 .tabs button{padding:7px 16px;border:1px solid #d1d5db;background:#fff;
              border-radius:8px;cursor:pointer;font-weight:600;color:#374151}
 .tabs button.on{background:#111827;color:#fff;border-color:#111827}
 .filters{display:flex;gap:10px;flex-wrap:wrap;align-items:center;
          background:#fff;border:1px solid #e5e7eb;border-radius:8px;
          padding:10px 12px;margin:6px 0 10px}
 .filters select{padding:5px 8px;border:1px solid #d1d5db;border-radius:6px}
 .filters label{font-size:12px;color:#6b7280}
 .filters .push{margin-left:auto;display:flex;gap:10px;align-items:center}
 .filters button{padding:5px 12px;border:1px solid #d1d5db;background:#f9fafb;
                 border-radius:6px;cursor:pointer}
 .pager{display:flex;gap:6px;align-items:center;margin:10px 0;flex-wrap:wrap}
 .pager button{min-width:32px;padding:4px 8px;border:1px solid #d1d5db;
               background:#fff;border-radius:6px;cursor:pointer}
 .pager button.cur{background:#111827;color:#fff;border-color:#111827}
 .pager button:disabled{opacity:.4;cursor:default}
 details.props{display:inline}
 details.props summary{cursor:pointer;color:#2563eb;font-size:12px;
                       display:inline;margin-left:6px}
 details.props pre{background:#f3f4f6;border-radius:6px;padding:8px;
                   font-size:11.5px;white-space:pre-wrap;margin:6px 0 0}
 code.mid{background:#f3f4f6;border-radius:4px;padding:1px 5px;font-size:12px}
 .range{display:flex;gap:6px}
 .range button{padding:5px 14px;border:1px solid #d1d5db;background:#fff;
               border-radius:6px;cursor:pointer}
 .range button.on{background:#111827;color:#fff;border-color:#111827}
 .barrow{display:flex;align-items:center;gap:8px;margin:3px 0}
 .barrow .lbl{width:110px;font-size:12px;color:#374151;white-space:nowrap;
              overflow:hidden;text-overflow:ellipsis;text-align:right}
 .barrow .track{flex:1;background:#eef0f3;border-radius:4px;height:14px}
 .barrow .bar{background:#2563eb;height:14px;border-radius:4px;min-width:2px}
 .barrow .val{font-size:12px;color:#374151;white-space:nowrap}
 .cardbox{background:#fff;border:1px solid #e5e7eb;border-radius:8px;
          padding:12px 14px;margin-bottom:14px}
 #dbline{font-size:11.5px;color:#6b7280;margin:-6px 0 10px}
 .aitext{background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px;
         padding:8px 10px;margin-top:6px;white-space:pre-wrap;
         font-size:12.5px;max-height:340px;overflow:auto}
 .aihead{font-size:12px;color:#6b7280;margin-bottom:2px}
 .pill{display:inline-block;background:#eef2ff;color:#3730a3;border-radius:10px;
       padding:1px 8px;font-size:11.5px;margin-right:6px}
</style></head><body>
<header><h1>Digital Lab Coach — course dashboard</h1>
<button class="ghost" id="logout" style="display:none">forget token</button>
</header>
<main>
 <div id="gate">
   <h2 style="margin-top:0">Admin token</h2>
   <p class="muted">Stored only in this browser. Only the instructor
      holding DLC_ADMIN_TOKEN can load data.</p>
   <input id="tok" type="password" placeholder="admin token">
   <button id="go">Open dashboard</button>
   <p id="gate-err" class="err"></p>
 </div>
 <div id="dash" style="display:none">
   <div class="tiles" id="tiles"></div>
   <div class="tabs">
     <button id="tab-overview" class="on">Overview</button>
     <button id="tab-activity">Activity</button>
     <button id="tab-ai">AI outputs</button>
     <button id="tab-stats">Stats</button>
   </div>
   <div id="dbline"></div>
   <div class="filters">
     <label>Day <select id="f-day"><option value="">all</option></select></label>
     <label>Machine <select id="f-mach"><option value="">all</option></select></label>
     <label id="l-kind">Event <select id="f-kind"><option value="">all</option></select></label>
     <label id="l-feat" style="display:none">Feature <select id="f-feat"><option value="">all</option></select></label>
     <label>Rows <select id="f-pp">
       <option>10</option><option selected>25</option>
       <option>50</option><option>100</option></select></label>
     <span class="push">
       <label><input type="checkbox" id="auto"> auto-refresh 60s</label>
       <button id="refresh">Refresh</button>
     </span>
   </div>
   <section id="sec-overview">
     <h2>Per-day LLM usage <span class="muted">(Day / Machine filters apply)</span></h2>
     <div id="llm"></div>
     <h2>Machines</h2><div id="machines"></div>
   </section>
   <section id="sec-activity" style="display:none">
     <h2>Activity <span id="act-count" class="muted"></span></h2>
     <div id="activity"></div>
     <div class="pager" id="act-pager"></div>
   </section>
   <section id="sec-ai" style="display:none">
     <h2>AI outputs <span id="ai-count" class="muted"></span>
       <span class="muted" style="font-weight:normal">— every LLM reply the
       course server relayed (L2 summaries &amp; grades, Mode A, Mode B)</span></h2>
     <div id="ai"></div>
     <div class="pager" id="ai-pager"></div>
   </section>
   <section id="sec-stats" style="display:none">
     <div class="filters"><span class="muted">Window</span>
       <span class="range">
         <button id="rng-7" class="on">Last 7 days</button>
         <button id="rng-30">Last 30 days</button>
       </span>
       <span class="push"><span id="st-since" class="muted"></span></span>
     </div>
     <div class="tiles" id="st-tiles"></div>
     <div class="cardbox"><h2 style="margin-top:0">Daily activity</h2>
       <div id="st-daily"></div></div>
     <div class="cardbox"><h2 style="margin-top:0">Layer 3 outcomes</h2>
       <div class="tiles" id="st-l3" style="margin-bottom:0"></div></div>
     <div class="cardbox"><h2 style="margin-top:0">AI usage by feature</h2>
       <div id="st-feat"></div></div>
     <div class="cardbox"><h2 style="margin-top:0">Most frequent events</h2>
       <div id="st-kinds"></div></div>
     <div class="cardbox"><h2 style="margin-top:0">Spend by day</h2>
       <div id="st-spend"></div></div>
   </section>
 </div>
</main>
<script>
const $=(id)=>document.getElementById(id);
function tok(){try{return localStorage.getItem("dlc_admin_token")||""}catch(e){return ""}}
function setTok(v){try{localStorage.setItem("dlc_admin_token",v)}catch(e){}}
async function api(path){
  const r=await fetch(path,{headers:{"X-DLC-Admin-Token":tok()}});
  if(r.status===401)throw new Error("bad token");
  return r.json();
}
function esc(s){const d=document.createElement("div");
  d.textContent=String(s??"");return d.innerHTML}
function table(rows,cols){
  if(!rows||!rows.length)return '<p class="muted">nothing yet</p>';
  const h=cols.map(c=>`<th>${esc(c)}</th>`).join("");
  const b=rows.map(r=>"<tr>"+cols.map(c=>`<td>${r[c]??""}</td>`)
    .join("")+"</tr>").join("");
  return `<table><tr>${h}</tr>${b}</table>`;
}
const SKIP=new Set(["session_id","install_id","issued","id_source"]);
function kv(props){
  const parts=[];
  for(const [k,v] of Object.entries(props||{})){
    if(SKIP.has(k))continue;
    parts.push(`${k}=${typeof v==="object"?JSON.stringify(v):v}`);
  }
  return parts.join(", ");
}
function describe(kind,p){
  p=p||{};
  const f=p.filename?`<code class="mid">${esc(p.filename)}</code>`:"";
  switch(kind){
    case "app_start": return `app started (v${esc(p.version||"?")})`;
    case "upload": return `uploaded ${p.count??"?"} file(s)`;
    case "tests_run_started": return `running tests on ${f} (${esc(p.mode||"")})`;
    case "tests_run_all_started": return "running ALL files' tests";
    case "tests_run_complete":
    case "tests_run_all_complete": return `tests finished ${kv(p)?"— "+esc(kv(p)):""}`;
    case "l2_llm_started": return "L2 summary requested";
    case "l2_llm_complete": return `L2 summary done ${p.model?"("+esc(p.model)+")":""}`;
    case "l3_modeA_started": return `Mode A started on ${f}`;
    case "l3_modeA_result_server":
      return `Mode A on ${f}: ${p.cards??0} card(s)`+
        `${p.confirmed?", confirmed &#10003;":""}, `+
        `${p.llm_calls??0} LLM call(s)${p.model?", "+esc(p.model):""}`;
    case "l3_modeB_result_server":
      return `Coverage Coach on ${f}: ${kv(p)?esc(kv(p)):"done"}`;
    case "l3_accept_fix_server":
    case "l3_fix_accepted": return `student ACCEPTED a fix ${f?("on "+f):""}`;
    case "l3_fix_animation_played": return "fix walkthrough animation played";
    case "l3_modeA_row_viewed": return "viewed a failing-row analysis";
    case "settings_proxy_saved": return "connected to the course server";
    case "settings_proxy_cleared": return "disconnected from the course server";
    case "settings_official_test_saved": return `official test saved ${f}`;
    default: {
      const t=kv(p);
      return esc(kind)+(t?` — ${esc(t.length>150?t.slice(0,150)+"…":t)}`:"");
    }
  }
}
function renderAiText(feature,text){
  try{
    const o=JSON.parse(text);
    if(o&&o.fix&&o.fix.explanation_for_student){
      const ops=(o.fix.ops||[]).length;
      const why=o.hint&&o.hint.why?o.hint.why:"";
      return `<div class="aihead">Mode A hypothesis — confidence `+
        `${esc(o.confidence??"?")}, ${ops} op(s)</div>`+
        `<div class="aitext"><b>why:</b> ${esc(why)}

`+
        `<b>student explanation:</b> ${esc(o.fix.explanation_for_student)}`+
        `

<b>ops:</b> ${esc(JSON.stringify(o.fix.ops))}</div>`;
    }
    if(o&&Array.isArray(o.proposals)){
      const t=o.proposals.map((pr,i)=>`#${i+1} `+
        esc(JSON.stringify(pr)).slice(0,300)).join(`
`);
      return `<div class="aihead">Mode B — ${o.proposals.length} `+
        `proposal(s)</div><div class="aitext">${t}</div>`;
    }
    if(o&&typeof o==="object")
      return `<div class="aitext">${esc(JSON.stringify(o,null,1))}</div>`;
  }catch(e){}
  return `<div class="aitext">${esc(text)}</div>`;
}
const state={tab:"overview",actPage:1,aiPage:1,range:7};
function filters(){
  return {day:$("f-day").value, mach:$("f-mach").value,
          kind:$("f-kind").value, feat:$("f-feat").value,
          pp:parseInt($("f-pp").value,10)};
}
function fill(sel,values){
  const cur=sel.value;
  sel.innerHTML='<option value="">all</option>'+
    values.map(v=>`<option${v===cur?" selected":""}>${esc(v)}</option>`).join("");
}
function pager(el,total,page,pp,go){
  const n=Math.max(1,Math.ceil(total/pp));
  const btn=(p,label,cur,dis)=>`<button data-p="${p}"
    ${dis?"disabled":""} class="${cur?"cur":""}">${label}</button>`;
  const parts=[btn(page-1,"&laquo;",false,page<=1)];
  const win=new Set([1,2,n-1,n,page-1,page,page+1]);
  let last=0;
  for(let p=1;p<=n;p++){
    if(!win.has(p))continue;
    if(p-last>1)parts.push('<span class="muted">…</span>');
    parts.push(btn(p,p,p===page,false));last=p;
  }
  parts.push(btn(page+1,"&raquo;",false,page>=n));
  el.innerHTML=parts.join("");
  el.querySelectorAll("button[data-p]").forEach(b=>b.onclick=()=>{
    go(parseInt(b.dataset.p,10));});
}
async function loadActivity(){
  const f=filters();
  const q=new URLSearchParams();
  if(f.day)q.set("day",f.day);
  if(f.mach)q.set("install_id",f.mach);
  if(f.kind)q.set("kind",f.kind);
  q.set("page",state.actPage);q.set("per_page",f.pp);
  const d=await api("/admin/events?"+q.toString());
  fill($("f-day"),d.days);fill($("f-mach"),d.machines);
  fill($("f-kind"),d.kinds);
  $("act-count").textContent=`${d.total} event(s)`;
  $("activity").innerHTML=d.rows.length?`<table>
    <tr><th class="nw">time</th><th class="nw">machine</th><th>what happened</th></tr>`+
    d.rows.map(r=>`<tr><td class="nw">${esc(r.time)}</td>
      <td class="nw" title="${esc(r.install_id)}"><code class="mid">${esc(r.install_id.slice(0,8))}</code></td>
      <td>${describe(r.kind,r.props)}
        <details class="props"><summary>raw</summary>
        <pre>${esc(JSON.stringify({kind:r.kind,...r.props},null,1))}</pre>
        </details></td></tr>`).join("")+"</table>"
    :'<p class="muted">no events match these filters</p>';
  pager($("act-pager"),d.total,d.page,d.per_page,
        p=>{state.actPage=p;loadActivity();});
}
async function loadAi(){
  const f=filters();
  const q=new URLSearchParams();
  if(f.day)q.set("day",f.day);
  if(f.mach)q.set("install_id",f.mach);
  if(f.feat)q.set("feature",f.feat);
  q.set("page",state.aiPage);q.set("per_page",f.pp);
  const d=await api("/admin/llm_texts?"+q.toString());
  fill($("f-day"),d.days);fill($("f-mach"),d.machines);
  fill($("f-feat"),d.features);
  $("ai-count").textContent=`${d.total} repl${d.total===1?"y":"ies"}`;
  $("ai").innerHTML=d.rows.length?`<table>
    <tr><th class="nw">time</th><th class="nw">machine</th><th>reply</th></tr>`+
    d.rows.map(r=>`<tr><td class="nw">${esc(r.time)}</td>
      <td class="nw" title="${esc(r.install_id)}"><code class="mid">${esc(r.install_id.slice(0,8))}</code></td>
      <td><span class="pill">${esc(r.feature)}</span>`+
      `<span class="muted">${esc(r.model||"")}, ${r.in_tokens??0}&rarr;${r.out_tokens??0} tok, ${Math.round((r.ms||0)/1000)}s</span>`+
      renderAiText(r.feature,r.response||"")+
      `</td></tr>`).join("")+"</table>"
    :'<p class="muted">no AI replies match these filters</p>';
  pager($("ai-pager"),d.total,d.page,d.per_page,
        p=>{state.aiPage=p;loadAi();});
}
function bars(el,rows,lbl,val,txt){
  const mx=Math.max(1,...rows.map(val));
  el.innerHTML=rows.length?rows.map(r=>`<div class="barrow">
    <span class="lbl" title="${esc(lbl(r))}">${esc(lbl(r))}</span>
    <span class="track"><span class="bar" style="width:${
      Math.round(100*val(r)/mx)}%"></span></span>
    <span class="val">${txt(r)}</span></div>`).join("")
    :'<p class="muted">nothing in this window</p>';
}
async function loadStats(){
  const d=await api("/admin/stats?range_days="+state.range);
  $("st-since").textContent=`since ${d.since}`;
  const t=d.totals;
  const okPct=t.llm_calls?Math.round(100*t.ok_calls/t.llm_calls):100;
  $("st-tiles").innerHTML=[
    [t.active_machines,"active machines"],
    [t.new_machines,"new machines"],
    [t.events,"events"],
    [`${t.llm_calls} (${okPct}% ok)`,"AI calls"],
    [`${t.in_tokens}→${t.out_tokens}`,"tokens in→out"],
    ["$"+t.est_usd,"spend (est)"],
  ].map(([v,l])=>`<div class="tile"><b>${v}</b><span>${l}</span></div>`).join("");
  bars($("st-daily"),d.active_by_day,r=>r.day,r=>r.events,
       r=>`${r.events} events · ${r.machines} machine(s)`);
  const l3=d.l3;
  const confPct=l3.modeA_runs?Math.round(100*l3.modeA_confirmed/l3.modeA_runs):0;
  $("st-l3").innerHTML=[
    [l3.modeA_runs,"Mode A runs"],
    [`${l3.modeA_confirmed} (${confPct}%)`,"confirmed fixes"],
    [l3.modeA_cards,"fix cards shown"],
    [l3.modeB_runs,"Mode B runs"],
    [l3.fixes_accepted,"fixes accepted"],
  ].map(([v,l])=>`<div class="tile"><b>${v}</b><span>${l}</span></div>`).join("");
  $("st-feat").innerHTML=table(d.by_feature.map(f=>({...f,
    est_usd:"$"+f.est_usd})),
    ["feature","calls","ok_calls","in_tokens","out_tokens","est_usd"]);
  bars($("st-kinds"),d.top_kinds,r=>r.kind,r=>r.n,r=>r.n);
  $("st-spend").innerHTML=table(d.spend_by_day.map(r=>({...r,
    est_usd:"$"+r.est_usd})),["day","calls","est_usd"]);
}
async function loadAggregates(){
  const f=filters();
  const health=await fetch("/v1/health").then(r=>r.json());
  if(health.db_path)$("dbline").textContent="serving database: "+health.db_path;
  const s=await api("/admin/summary");
  const d=await api("/admin/daily");
  const cap=health.today_calls>=health.global_daily_calls||
            health.today_est_usd>=health.global_daily_usd;
  $("tiles").innerHTML=[
   [s.machines.length,"machines"],[health.events,"events"],
   [health.today_calls+" / "+health.global_daily_calls,"LLM calls today"],
   ["$"+health.today_est_usd+" / $"+health.global_daily_usd,"spend today (est)"],
   ["$"+s.llm_spend_est_usd,"spend all-time (est)"],
   [cap?"TRIPPED":"ok","capacity breaker",cap],
   [health.key_format_ok?"ok":"CHECK KEY","API key",!health.key_format_ok],
  ].map(([v,l,warn])=>`<div class="tile"><b class="${warn?"warn":""}">${v}</b><span>${l}</span></div>`).join("");
  const spend={};for(const s2 of d.spend_by_day)spend[s2.day]=s2.est_usd;
  const llmRows=d.llm
    .filter(r=>(!f.day||r.day===f.day)&&(!f.mach||r.install_id===f.mach))
    .map(r=>({...r,install_id:`<code class="mid">${esc(r.install_id.slice(0,8))}</code>`,
      est_day_usd:spend[r.day]!==undefined?("$"+spend[r.day]):""}));
  $("llm").innerHTML=table(llmRows,
   ["day","install_id","feature","calls","ok_calls","in_tokens",
    "out_tokens","est_day_usd"]);
  $("machines").innerHTML=table(s.machines,
   ["install_id","first_seen","last_seen","id_source","app_version",
    "events","llm_calls"]);
}
function showTab(t){
  state.tab=t;
  for(const x of ["overview","activity","ai","stats"]){
    $("sec-"+x).style.display=x===t?"":"none";
    $("tab-"+x).classList.toggle("on",x===t);
  }
  $("l-kind").style.display=t==="activity"?"":"none";
  $("l-feat").style.display=t==="ai"?"":"none";
  for(const id of ["f-day","f-mach","f-pp"])
    $(id).parentElement.style.display=t==="stats"?"none":"";
  reloadTab();
}
function reloadTab(){
  if(state.tab==="overview")loadAggregates();
  else if(state.tab==="activity")loadActivity();
  else if(state.tab==="ai")loadAi();
  else loadStats();
}
function setRange(n){
  state.range=n;
  $("rng-7").classList.toggle("on",n===7);
  $("rng-30").classList.toggle("on",n===30);
  loadStats();
}
async function load(){
  await loadAggregates();
  await loadActivity();
  await loadAi();
  $("gate").style.display="none";$("dash").style.display="block";
  $("logout").style.display="inline-block";
  showTab("overview");
}
["f-day","f-mach","f-kind","f-feat","f-pp"].forEach(id=>{
  $(id).addEventListener("change",()=>{state.actPage=1;state.aiPage=1;
    reloadTab();});
});
$("refresh").onclick=()=>reloadTab();
let autoTimer=null;
$("auto").addEventListener("change",()=>{
  if($("auto").checked)autoTimer=setInterval(reloadTab,60000);
  else{clearInterval(autoTimer);autoTimer=null;}
});
$("tab-overview").onclick=()=>showTab("overview");
$("tab-activity").onclick=()=>showTab("activity");
$("tab-ai").onclick=()=>showTab("ai");
$("tab-stats").onclick=()=>showTab("stats");
$("rng-7").onclick=()=>setRange(7);
$("rng-30").onclick=()=>setRange(30);
$("go").onclick=async()=>{setTok($("tok").value.trim());
  try{await load()}catch(e){$("gate-err").textContent=
    "That token was rejected — check it and try again."}};
$("logout").onclick=()=>{setTok("");location.reload()};
if(tok()){load().catch(()=>{$("gate").style.display="block"})}
</script></body></html>"""


@app.get("/admin/view")
def admin_view():
    from fastapi.responses import HTMLResponse
    return HTMLResponse(_ADMIN_PAGE)
