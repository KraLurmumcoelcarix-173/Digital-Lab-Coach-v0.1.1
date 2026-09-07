"""
Daily use-limit stub (dlc/l3/limits.py) + its /api/l3/coverage wiring:
counters always tick, blocking only under DLC_ENFORCE_LIMITS, and the
ratified rule that a disagreement scan is a free redirect.
"""

import json

import pytest
from fastapi.testclient import TestClient

from dlc.l3 import limits
from dlc.web import server
from dlc.web.server import app

client = TestClient(app)

_CALC_DIR = "data/sample_circuits/30_bug_benchmark/bug1_meaningless_mux_in3"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DLC_LIMITS_PATH", str(tmp_path / "limits.json"))
    monkeypatch.setenv("DLC_TELEMETRY_DB", str(tmp_path / "telemetry.db"))
    monkeypatch.delenv("DLC_ENFORCE_LIMITS", raising=False)

def test_counters_tick_even_when_unenforced():
    assert limits.state()["used"]["modeB"] == 0
    limits.consume("modeB")
    st = limits.consume("modeB")
    assert st["used"]["modeB"] == 2
    assert st["remaining"]["modeB"] == 0
    assert st["enforced"] is False
    assert limits.allowed("modeB") is True


def test_enforced_blocks_at_cap(monkeypatch):
    monkeypatch.setenv("DLC_ENFORCE_LIMITS", "1")
    assert limits.allowed("modeA") is True
    for _ in range(3):
        limits.consume("modeA")
    assert limits.allowed("modeA") is False
    assert limits.allowed("modeB") is True


def test_counters_reset_on_a_new_day(tmp_path, monkeypatch):
    p = tmp_path / "limits.json"
    monkeypatch.setenv("DLC_LIMITS_PATH", str(p))
    p.write_text(json.dumps(
        {"date": "1999-01-01", "used": {"modeA": 1, "modeB": 2}},
    ))
    st = limits.state()
    assert st["used"] == {"modeA": 0, "modeB": 0}


def test_corrupt_file_resets_instead_of_crashing(tmp_path, monkeypatch):
    p = tmp_path / "limits.json"
    monkeypatch.setenv("DLC_LIMITS_PATH", str(p))
    p.write_text("{not json")
    assert limits.state()["used"]["modeB"] == 0
    assert limits.allowed("modeB") is True


def test_unknown_mode_is_a_noop():
    assert limits.allowed("modeZ") is True
    before = limits.state()["used"]
    limits.consume("modeZ")
    assert limits.state()["used"] == before

def _upload(*paths):
    files, handles = [], []
    for p in paths:
        fh = open(p, "rb")
        handles.append(fh)
        files.append(("files", (p.split("/")[-1], fh, "application/xml")))
    try:
        r = client.post("/api/circuit", files=files)
    finally:
        for fh in handles:
            fh.close()
    assert r.status_code == 200
    return r.json()["session_id"]


def _scan(sid, filename):
    return client.post("/api/l3/coverage", json={
        "session_id": sid, "filename": filename,
    }).json()


def test_clean_scan_consumes_a_use_and_reports_limits():
    sid = _upload("data/sample_circuits/tier1_minimal/single_and.dig")
    try:
        body = _scan(sid, "single_and.dig")
        assert body["ok"] is True and body["consumed_use"] is True
        assert body["limits"]["used"]["modeB"] == 1
    finally:
        server._SESSIONS.pop(sid, None)


def test_disagreement_scan_is_a_free_redirect():
    sid = _upload(f"{_CALC_DIR}/tier3_calculator.dig", f"{_CALC_DIR}/bool_unit.dig")
    try:
        body = _scan(sid, "tier3_calculator.dig")
        assert body["ok"] is True and body["total_flags"] > 0
        assert body["consumed_use"] is False
        assert body["limits"]["used"]["modeB"] == 0
    finally:
        server._SESSIONS.pop(sid, None)


def test_enforced_cap_blocks_the_third_clean_scan(monkeypatch):
    monkeypatch.setenv("DLC_ENFORCE_LIMITS", "1")
    sid = _upload("data/sample_circuits/tier1_minimal/single_and.dig")
    try:
        assert _scan(sid, "single_and.dig")["consumed_use"] is True
        assert _scan(sid, "single_and.dig")["consumed_use"] is True
        third = _scan(sid, "single_and.dig")
        assert third["ok"] is False and third["limited"] is True
        assert "limit" in third["warning"].lower()
    finally:
        server._SESSIONS.pop(sid, None)


def test_refund_gives_a_use_back_with_floor_zero():
    limits.consume("modeB")
    assert limits.state()["used"]["modeB"] == 1
    st = limits.refund("modeB")
    assert st["used"]["modeB"] == 0
    st = limits.refund("modeB")
    assert st["used"]["modeB"] == 0
    limits.refund("modeZ")
