import pytest
from fastapi.testclient import TestClient

from dlc.l3 import debugger
from dlc.web import server
from dlc.web.server import app

client = TestClient(app)

_BUG3 = "data/sample_circuits/30_bug_benchmark/bug3_wrong_cin/Wrong_cin.dig"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DLC_LIMITS_PATH", str(tmp_path / "limits.json"))
    monkeypatch.setenv("DLC_TELEMETRY_DB", str(tmp_path / "telemetry.db"))
    monkeypatch.delenv("DLC_ENFORCE_LIMITS", raising=False)


def _upload_bug3():
    with open(_BUG3, "rb") as fh:
        r = client.post("/api/circuit",
                        files=[("files", ("Wrong_cin.dig", fh,
                                          "application/xml"))])
    assert r.status_code == 200
    return r.json()["session_id"]


def _debug(sid, **extra):
    return client.post("/api/llm/debug", json={
        "session_id": sid, "filename": "Wrong_cin.dig", **extra,
    }).json()


def _canned(mode, cards):
    def fake(path, **kw):
        fake.calls.append({"path": path, **kw})
        return {"ok": True, "mode": mode, "cards": cards, "notes": []}
    fake.calls = []
    return fake


def test_delivered_cards_consume_one_mode_a_use(monkeypatch):
    sid = _upload_bug3()
    monkeypatch.setattr(debugger, "debug_circuit",
                        _canned("analysis", [{"rank": 1}]))
    body = _debug(sid)
    assert body["ok"] is True
    assert body["consumed_use"] is True
    assert body["limits"]["used"]["modeA"] == 1


def test_empty_analysis_is_free_and_stays_quiet(monkeypatch):
    sid = _upload_bug3()
    monkeypatch.setattr(debugger, "debug_circuit", _canned("analysis", []))
    body = _debug(sid)
    assert body["consumed_use"] is False
    assert body["limits"]["used"]["modeA"] == 0
    assert body["notes"] == []


def test_model_override_reaches_the_coordinator(monkeypatch):
    sid = _upload_bug3()
    fake = _canned("analysis", [{"rank": 1}])
    monkeypatch.setattr(debugger, "debug_circuit", fake)
    _debug(sid)
    assert fake.calls[-1]["model"] is None
    _debug(sid, model="claude-opus-5")
    assert fake.calls[-1]["model"] == "claude-opus-5"


def test_clear_and_lazy_are_free(monkeypatch):
    sid = _upload_bug3()
    for mode in ("clear", "lazy"):
        monkeypatch.setattr(debugger, "debug_circuit", _canned(mode, []))
        body = _debug(sid)
        assert body["consumed_use"] is False
    assert body["limits"]["used"]["modeA"] == 0


def test_enforced_cap_blocks_the_fourth_run(monkeypatch):
    sid = _upload_bug3()
    monkeypatch.setattr(debugger, "debug_circuit",
                        _canned("analysis", [{"rank": 1}]))
    for _ in range(3):
        assert _debug(sid)["consumed_use"] is True
    monkeypatch.setenv("DLC_ENFORCE_LIMITS", "1")
    body = _debug(sid)
    assert body.get("limited") is True
    assert "limit" in body["warning"].lower()


def test_coach_temp_is_targeted_when_registered(monkeypatch):
    sid = _upload_bug3()
    fake = _canned("clear", [])
    monkeypatch.setattr(debugger, "debug_circuit", fake)

    body = _debug(sid)
    assert body["on_coach_temp"] is False
    assert fake.calls[-1]["coach_rows"] is None

    server._SESSIONS[sid]["l3_temp"] = {
        "for": "Wrong_cin.dig",
        "name": "Wrong_cin__coach.dig",
        "path": _BUG3,
        "spec_name": "Testcase_12",
        "coach_rows": [4, 5],
    }
    body = _debug(sid)
    assert body["on_coach_temp"] is True
    assert fake.calls[-1]["path"] == _BUG3
    assert fake.calls[-1]["spec_name"] == "Testcase_12"
    assert fake.calls[-1]["coach_rows"] == [4, 5]


def test_fix_retest_applies_ops_and_reruns():
    sid = _upload_bug3()
    r = client.post("/api/l3/fix_retest", json={
        "session_id": sid, "filename": "Wrong_cin.dig",
        "ops": [{"op": "change_attribute", "component_index": 16,
                 "name": "Value", "value": 0}],
    })
    body = r.json()
    assert body["ok"] is True, body.get("warning")
    assert body["all_passed"] is True
    rows = body["spec"]["rows"]
    assert len(rows) == 4
    assert all(row["status"] == "passed" for row in rows)


def test_fix_retest_rejects_empty_and_broken_ops():
    sid = _upload_bug3()
    r = client.post("/api/l3/fix_retest", json={
        "session_id": sid, "filename": "Wrong_cin.dig", "ops": []})
    assert r.json()["ok"] is False
    r2 = client.post("/api/l3/fix_retest", json={
        "session_id": sid, "filename": "Wrong_cin.dig",
        "ops": [{"op": "change_attribute", "component_index": 9999,
                 "name": "Value", "value": 0}]})
    body2 = r2.json()
    assert body2["ok"] is False and body2["warning"]


def test_accept_fix_registers_fixed_temp_and_retargets_everything(monkeypatch):
    sid = _upload_bug3()
    r = client.post("/api/l3/accept_fix", json={
        "session_id": sid, "filename": "Wrong_cin.dig",
        "ops": [{"op": "change_attribute", "component_index": 16,
                 "name": "Value", "value": 0}],
        "spec_name": "Testcase_12",
    })
    body = r.json()
    assert body["ok"] is True, body.get("warning")
    assert body["temp_filename"] == "Wrong_cin__coach.dig"
    assert body["all_passed"] is True
    lt = server._SESSIONS[sid]["l3_temp"]
    assert lt["for"] == "Wrong_cin.dig" and lt["coach_rows"] == []
    names = [f["name"] for f in server._SESSIONS[sid]["files"]]
    assert "Wrong_cin__coach.dig" in names

    fake = _canned("clear", [])
    monkeypatch.setattr(debugger, "debug_circuit", fake)
    b2 = _debug(sid)
    assert b2["on_coach_temp"] is True
    assert fake.calls[-1]["path"] == lt["path"]

    b3 = client.post("/api/l3/coverage", json={
        "session_id": sid, "filename": "Wrong_cin.dig"}).json()
    assert b3["ok"] is True
    assert b3["on_coach_temp"] is True
    assert b3["total_flags"] == 0

    orig = next(f for f in server._SESSIONS[sid]["files"]
                if f["name"] == "Wrong_cin.dig")
    import dlc.l3.coverage as cov
    assert cov.scan_tree_coverage(orig["path"]).total_flags > 0


def test_accept_fix_refuses_empty_ops():
    sid = _upload_bug3()
    r = client.post("/api/l3/accept_fix", json={
        "session_id": sid, "filename": "Wrong_cin.dig", "ops": []})
    assert r.json()["ok"] is False


def test_empty_testcase_debug_runs_on_injected_temp(monkeypatch, tmp_path):
    import io, os
    empty_cpu = """<?xml version="1.0" encoding="utf-8"?>
<circuit>
  <version>2</version>
  <attributes/>
  <visualElements>
    <visualElement>
      <elementName>In</elementName>
      <elementAttributes>
        <entry><string>Label</string><string>clk</string></entry>
      </elementAttributes>
      <pos x="0" y="0"/>
    </visualElement>
    <visualElement>
      <elementName>ROM</elementName>
      <elementAttributes>
        <entry><string>AddrBits</string><int>10</int></entry>
        <entry><string>Bits</string><int>32</int></entry>
        <entry><string>isProgramMemory</string><boolean>true</boolean></entry>
        <entry><string>Data</string><data>%s</data></entry>
      </elementAttributes>
      <pos x="200" y="0"/>
    </visualElement>
    <visualElement>
      <elementName>Testcase</elementName>
      <elementAttributes>
        <entry><string>Label</string><string>cpu</string></entry>
        <entry><string>Testdata</string><testData><dataString>clk ReadData1 ReadData2
</dataString></testData></entry>
      </elementAttributes>
      <pos x="0" y="200"/>
    </visualElement>
  </visualElements>
  <wires/>
</circuit>
"""
    from dlc.l3 import official_store
    empty_cpu = empty_cpu % official_store.get_runtime_payload("cpu.dig", "rom")
    r = client.post("/api/circuit", files=[
        ("files", ("cpu.dig", io.BytesIO(empty_cpu.encode()),
                   "application/xml"))])
    assert r.status_code == 200
    sid = r.json()["session_id"]

    fake = _canned("analysis", [])
    monkeypatch.setattr(debugger, "debug_circuit", fake)
    res = client.post("/api/llm/debug", json={
        "session_id": sid, "filename": "cpu.dig"}).json()

    assert fake.calls, "debug_circuit was not reached"
    called_path = fake.calls[0]["path"]
    target = server._resolve_target(sid, "cpu.dig")
    assert called_path != target["path"]
    assert ".dlc_injected__" in os.path.basename(called_path)
    assert res.get("injected"), "response should carry the injection note"
    assert not os.path.exists(called_path)


def _upload_romlab(rom_data):
    xml = _ROM_LAB
    if rom_data is not None:
        xml = xml.replace(
            '<entry><string>Bits</string><int>4</int></entry>',
            '<entry><string>Bits</string><int>4</int></entry>'
            f'<entry><string>Data</string><data>{rom_data}</data></entry>')
    r = client.post("/api/circuit", files=[
        ("files", ("romlab.dig", xml.encode(), "application/xml"))])
    assert r.status_code == 200
    return r.json()["session_id"]


def test_rom_gate_refuses_empty_or_wrong_rom_for_free(monkeypatch,
                                                      tmp_path):
    import json as _json
    monkeypatch.setenv("DLC_OFFICIAL_DEFAULTS_PATH",
                       str(_rom_lab_defaults(tmp_path)))
    fake = _canned("analysis", [{"rank": 1}])
    monkeypatch.setattr(debugger, "debug_circuit", fake)
    for data, status in ((None, "empty"), ("5,7", "mismatch")):
        sid = _upload_romlab(data)
        body = client.post("/api/llm/debug", json={
            "session_id": sid, "filename": "romlab.dig"}).json()
        assert body["ok"] is True and body["mode"] == "rom_mismatch"
        assert body["rom_check"]["status"] == status
        assert body["consumed_use"] is False and body["llm_calls"] == 0
        assert body["rom_verified"] is False
        assert (body["limits"]["used"] or {}).get("modeA", 0) == 0
        assert "official contents" in body["message"]
        assert body["rom_check"]["file"] == "romlab.dig"
        assert "5,6" not in _json.dumps(body)
    assert fake.calls == []
    assert body["rom_check"]["first_bad_address"] == 1
    assert "your word there is 7" in body["message"]


def test_rom_gate_lets_matching_contents_through(monkeypatch, tmp_path):
    monkeypatch.setenv("DLC_OFFICIAL_DEFAULTS_PATH",
                       str(_rom_lab_defaults(tmp_path)))
    fake = _canned("analysis", [{"rank": 1}])
    monkeypatch.setattr(debugger, "debug_circuit", fake)
    sid = _upload_romlab("5,6")
    body = client.post("/api/llm/debug", json={
        "session_id": sid, "filename": "romlab.dig"}).json()
    assert fake.calls, "a matching ROM must reach the coordinator"
    assert "rom_injected" not in fake.calls[-1]
    assert body["mode"] == "analysis"
    assert body["rom_verified"] is True
    assert body["consumed_use"] is True

    sid2 = _upload_bug3()
    body2 = _debug(sid2)
    assert body2["rom_verified"] is False


def _rom_lab_defaults(tmp_path):
    import base64
    import json as _json
    defaults = {
        "romlab.dig": {
            "content": "A D\n0 5\n1 6",
            "sha1": "0" * 40,
            "runtime": base64.b64encode(
                _json.dumps({"rom": "5,6"}).encode()).decode(),
        }
    }
    p = tmp_path / "defaults.json"
    p.write_text(_json.dumps(defaults), encoding="utf-8")
    return p


_ROM_LAB = (
    '<?xml version="1.0" encoding="utf-8"?><circuit><version>2</version>'
    '<attributes/><visualElements>'
    '<visualElement><elementName>In</elementName><elementAttributes>'
    '<entry><string>Label</string><string>A</string></entry>'
    '</elementAttributes><pos x="0" y="0"/></visualElement>'
    '<visualElement><elementName>VDD</elementName><elementAttributes/>'
    '<pos x="160" y="40"/></visualElement>'
    '<visualElement><elementName>ROM</elementName><elementAttributes>'
    '<entry><string>AddrBits</string><int>1</int></entry>'
    '<entry><string>Bits</string><int>4</int></entry>'
    '</elementAttributes><pos x="200" y="0"/></visualElement>'
    '<visualElement><elementName>Out</elementName><elementAttributes>'
    '<entry><string>Label</string><string>D</string></entry>'
    '<entry><string>Bits</string><int>4</int></entry>'
    '</elementAttributes><pos x="400" y="20"/></visualElement>'
    '</visualElements><wires>'
    '<wire><p1 x="0" y="0"/><p2 x="200" y="0"/></wire>'
    '<wire><p1 x="160" y="40"/><p2 x="200" y="40"/></wire>'
    '<wire><p1 x="260" y="20"/><p2 x="400" y="20"/></wire>'
    '</wires></circuit>'
)


def test_accept_fix_rerun_never_fills_a_rom(monkeypatch, tmp_path):
    monkeypatch.setenv("DLC_OFFICIAL_DEFAULTS_PATH",
                       str(_rom_lab_defaults(tmp_path)))
    sid = _upload_romlab(None)

    noop = [{"op": "change_attribute", "component_index": 3,
             "name": "Label", "value": "D"}]
    body = client.post("/api/l3/accept_fix", json={
        "session_id": sid, "filename": "romlab.dig", "ops": noop,
    }).json()
    assert body["ok"] is True
    assert body["all_passed"] is False, body
    assert not any("ROM" in n for n in body.get("injected", []))
    lt = server._SESSIONS[sid]["l3_temp"]
    assert "5,6" not in open(lt["path"], encoding="utf-8").read()

    sid2 = _upload_romlab("5,6")
    body2 = client.post("/api/l3/accept_fix", json={
        "session_id": sid2, "filename": "romlab.dig", "ops": noop,
    }).json()
    assert body2["ok"] is True and body2["all_passed"] is True, body2
