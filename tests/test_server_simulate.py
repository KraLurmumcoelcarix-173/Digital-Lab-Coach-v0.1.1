from fastapi.testclient import TestClient

from dlc.web.server import app

client = TestClient(app)

_BASE = "data/sample_circuits"


def _upload(paths: list[str]) -> str:
    files = []
    for p in paths:
        files.append(("files", (p.split("/")[-1], open(p, "rb"), "application/xml")))
    r = client.post("/api/circuit", files=files)
    assert r.status_code == 200
    return r.json()["session_id"]


def test_simulate_combinational_calculator_with_subcircuit():
    sid = _upload([
        f"{_BASE}/tier3_realistic/tier3_calculator.dig",
        f"{_BASE}/tier3_realistic/bool_unit.dig",
    ])
    r = client.post("/api/simulate", json={
        "session_id": sid, "filename": "tier3_calculator.dig",
        "spec_index": 0, "row_index": 0,
    }).json()
    assert r["ok"] is True
    assert r["net_values"], r
    assert r["unresolved_nets"] == []
    result = next(o for o in r["outputs"] if o["label"] == "Result")
    assert result["expected"] == "0x8" and result["found"] == "0x8"
    assert result["ok"] is True
    any_net = next(iter(r["net_values"].values()))
    assert set(any_net) == {"value", "bits", "hex"}


def test_simulate_failed_row_reports_expected_vs_found():
    sid = _upload([f"{_BASE}/30_bug_benchmark/bug3_wrong_cin/Wrong_cin.dig"])
    r = client.post("/api/simulate", json={
        "session_id": sid, "filename": "Wrong_cin.dig",
        "spec_index": 0, "row_index": 2,
    }).json()
    assert r["ok"] is True
    sumo = next(o for o in r["outputs"] if o["label"] == "Sum")
    assert sumo["expected"] == "0x7"
    assert sumo["found"] == "0x8"
    assert sumo["ok"] is False

def test_simulate_signed_output_matches_bit_pattern_not_a_false_mismatch():
    sid = _upload([f"{_BASE}/tier1_minimal/signed_passthrough.dig"])
    r = client.post("/api/simulate", json={
        "session_id": sid, "filename": "signed_passthrough.dig",
        "spec_index": 0, "row_index": 1,
    }).json()
    y = next(o for o in r["outputs"] if o["label"] == "Y")
    assert y["ok"] is True
    assert y["expected"] == "-60"
    assert y["found"] == "-60"


def test_output_ok_and_fmt_helpers():
    from dlc.web.server import _output_ok, _fmt_output
    assert _output_ok(4294967236, -60, 32) is True
    assert _output_ok(4294967236, -61, 32) is False
    assert _output_ok(5, 5, 8) is True
    assert _output_ok(None, 5, 8) is None
    assert _fmt_output(4294967236, 32, True) == "-60"
    assert _fmt_output(0x1F, 8, False) == "0x1F"
    assert _fmt_output(1, 1, False) == "1"


def _official_with_extra_row(path, extra=None):
    """The file's own testcase plus one more row (its last row again by
    default): an official test that is 'modified' relative to the
    upload, one row longer."""
    from dlc.parser.dig_parser import parse_dig_file
    from dlc.testing.spec import extract_test_specs
    spec = extract_test_specs(parse_dig_file(path))[0]
    lines = spec.raw_data_string.rstrip().splitlines()
    return "\n".join(lines + [extra or lines[-1]]) + "\n", len(spec.rows)


def _no_injected_temp_left(sid):
    from pathlib import Path
    from dlc.web import server
    folder = Path(server._SESSIONS[sid]["files"][0]["path"]).parent
    return not list(folder.glob(".dlc_injected__*"))


def test_row_view_follows_the_official_rows_the_run_used(tmp_path, monkeypatch):
    from dlc.l3 import official_store
    monkeypatch.setenv("DLC_OFFICIAL_TESTS_PATH", str(tmp_path / "official.json"))
    path = f"{_BASE}/tier1_minimal/single_and.dig"
    content, n_rows = _official_with_extra_row(path)
    official_store.save_test("single_and.dig", content)
    sid = _upload([path])
    # the extra official row has no counterpart in the upload's testcase
    r = client.post("/api/simulate", json={
        "session_id": sid, "filename": "single_and.dig",
        "spec_index": 0, "row_index": n_rows,
    }).json()
    assert r["ok"] is True and r["row_index"] == n_rows, r
    y = next(o for o in r["outputs"] if o["label"] == "Y")
    assert y["ok"] is True and y["expected"] == y["found"]
    assert _no_injected_temp_left(sid)


def test_subcircuit_view_follows_the_official_rows(tmp_path, monkeypatch):
    from dlc.l3 import official_store
    monkeypatch.setenv("DLC_OFFICIAL_TESTS_PATH", str(tmp_path / "official.json"))
    path = f"{_BASE}/tier3_realistic/tier3_calculator.dig"
    # an extra OR row (Op=3): the drill-in must show live values in the
    # boolean unit for a row the upload's own testcase does not have
    content, n_rows = _official_with_extra_row(path, "5 10 0 3 15 0 0 1")
    official_store.save_test("tier3_calculator.dig", content,
                             allow_default_override=True)
    sid = _upload([path, f"{_BASE}/tier3_realistic/bool_unit.dig"])
    r = client.post("/api/subcircuit", json={
        "session_id": sid, "filename": "tier3_calculator.dig",
        "spec_index": 0, "row_index": n_rows, "path": [9],
    }).json()
    assert r["ok"] is True, r
    assert r["breadcrumb"] and r["depth"] == 1
    assert r["net_values"] and "note" not in r, r.get("note")
    assert _no_injected_temp_left(sid)


def test_simulate_bad_session_is_404():
    r = client.post("/api/simulate", json={
        "session_id": "nope", "filename": "x.dig",
        "spec_index": 0, "row_index": 0,
    })
    assert r.status_code == 404
