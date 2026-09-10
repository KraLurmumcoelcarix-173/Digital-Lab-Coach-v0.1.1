from fastapi.testclient import TestClient

from dlc.llm import explain as explain_mod
from dlc.web import server
from dlc.web.server import app

client = TestClient(app)

_BASE = "data/sample_circuits"
_CALC = f"{_BASE}/tier3_realistic/tier3_calculator.dig"
_BOOL = f"{_BASE}/tier3_realistic/bool_unit.dig"


def _upload(paths):
    files = [("files", (p.split("/")[-1], open(p, "rb"), "application/xml"))
             for p in paths]
    r = client.post("/api/circuit", files=files)
    assert r.status_code == 200
    return r.json()["session_id"]


def test_walkthrough_endpoint_returns_steps_values_and_expressions():
    sid = _upload([_CALC, _BOOL])
    try:
        r = client.post("/api/l2/walkthrough", json={
            "session_id": sid, "filename": "tier3_calculator.dig",
            "spec_index": 0, "row_index": 0}).json()
        assert r["ok"] is True and r["row_index"] == 0
        assert r["spec_name"] and r["steps"]
        assert r["net_values"] and set(next(iter(r["net_values"].values()))) == {"value", "bits", "hex"}
        out = next(o for o in r["outputs"] if o["label"] == "Result")
        assert out["ok"] is True and out["expression"].startswith("Result = ")
        # Op=3 (row 6) runs through the boolean unit: the child is a step
        r2 = client.post("/api/l2/walkthrough", json={
            "session_id": sid, "filename": "tier3_calculator.dig",
            "spec_index": 0, "row_index": 6}).json()
        assert r2["ok"] is True
        assert any(s["element"] == "bool_unit.dig" for s in r2["steps"])
        bad = client.post("/api/l2/walkthrough", json={
            "session_id": sid, "filename": "tier3_calculator.dig",
            "spec_index": 0, "row_index": 99}).json()
        assert bad["ok"] is False and "row" in bad["warning"]
    finally:
        server._SESSIONS.pop(sid, None)


def test_explain_reply_carries_the_example_row_and_subcircuit_roles(tmp_path, monkeypatch):
    import json
    seen = {}

    def fake_call(prompt, **kw):
        seen["prompt"] = prompt
        return {"ok": True, "text": "one\n\ntwo\n\nthree\n\nfour\n\nfive\n\nsix",
                "error": None, "usage": None, "model": kw.get("model")}

    monkeypatch.setattr(explain_mod, "call_llm", fake_call)
    mdir = tmp_path / "manifests"
    mdir.mkdir()
    (mdir / "calc.json").write_text(json.dumps({
        "lab": "calc", "applies_to": ["tier3_calculator.dig", "bool_unit.dig"],
        "subcircuits": {"bool_unit.dig": {"role": "Boolean unit: AND, OR, XOR or NOR of A and B."}},
        "categories": {}, "official_tests": {}, "reference_dir": None}))
    monkeypatch.setenv("DLC_MANIFEST_DIR", str(mdir))
    sid = _upload([_CALC, _BOOL])
    try:
        r = client.post("/api/llm/explain", json={
            "session_id": sid, "filename": "tier3_calculator.dig",
            "test_summary": "All rows passed."}).json()
        assert r["ok"] is True and r["gate_message"] is None
        ex = r["example_row"]
        assert ex["spec_index"] == 0 and ex["row_index"] == 0
        assert ex["columns"][:2] == ["A", "B"] and ex["raw"].startswith("5 3")
        assert "[EXAMPLE ROW]" in seen["prompt"]
        assert "row 0 of testcase" in seen["prompt"] and "A=5 B=3" in seen["prompt"]
        roles = {x["reference"]: x["role"] for x in r["subcircuit_roles"]}
        assert roles == {"bool_unit.dig": "Boolean unit: AND, OR, XOR or NOR of A and B."}
        assert '"role": "Boolean unit' in seen["prompt"]
    finally:
        server._SESSIONS.pop(sid, None)


def test_example_row_absent_without_a_testcase(tmp_path, monkeypatch):
    monkeypatch.setattr(explain_mod, "call_llm", lambda prompt, **kw: {
        "ok": True, "text": "a\n\nb\n\nc\n\nd\n\ne\n\nf", "error": None,
        "usage": None, "model": "fake"})
    xml = open(_CALC, encoding="utf-8").read()
    import re
    stripped = re.sub(r"<visualElement>\s*<elementName>Testcase</elementName>.*?</visualElement>",
                      "", xml, flags=re.S)
    p = tmp_path / "tier3_calculator.dig"
    p.write_text(stripped, encoding="utf-8")
    sid = _upload([str(p), _BOOL])
    try:
        r = client.post("/api/llm/explain", json={
            "session_id": sid, "filename": "tier3_calculator.dig"}).json()
        assert r["ok"] is True and r["example_row"] is None
        w = client.post("/api/l2/walkthrough", json={
            "session_id": sid, "filename": "tier3_calculator.dig"}).json()
        assert w["ok"] is False
    finally:
        server._SESSIONS.pop(sid, None)
