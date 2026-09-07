"""
Mode B row proposer (dlc/l3/proposer.py) + POST /api/l3/propose.
"""

import json

import pytest
from fastapi.testclient import TestClient

from dlc.l3 import proposer
from dlc.l3.coverage import scan_tree_coverage
from dlc.web import server
from dlc.web.server import app

client = TestClient(app)

_AND = "data/sample_circuits/tier1_minimal/single_and.dig"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DLC_LIMITS_PATH", str(tmp_path / "limits.json"))
    monkeypatch.setenv("DLC_TELEMETRY_DB", str(tmp_path / "telemetry.db"))
    monkeypatch.delenv("DLC_ENFORCE_LIMITS", raising=False)


def _fake(text):
    def call(prompt, **kw):
        return {"ok": True, "text": text, "error": None,
                "usage": None, "model": kw.get("model")}
    return call


def _fake_two(first, second):
    n = {"v": 0}
    def call(prompt, **kw):
        n["v"] += 1
        return {"ok": True, "text": first if n["v"] == 1 else second,
                "error": None, "usage": None, "model": kw.get("model")}
    return call


def test_targets_carry_headers_io_and_existing_rows():
    report = scan_tree_coverage(_AND)
    targets = proposer.build_targets(report)
    assert len(targets) == 1
    t = targets[0]
    assert t["file"] == "single_and.dig"
    assert t["headers"] == ["A", "B", "Y"]
    assert len(t["existing_rows"]) == 4
    assert {i["label"] for i in t["inputs"]} == {"A", "B"}


def test_prompt_embeds_report_and_targets_not_paths():
    report = scan_tree_coverage(_AND)
    targets = proposer.build_targets(report)
    prompt = proposer.build_prompt(report, targets)
    assert "SPOILER GUARD" in prompt
    assert '"single_and.dig"' in prompt
    assert '"existing_rows"' in prompt
    assert "sample_circuits" not in prompt


def test_parse_tolerates_fences_and_prose():
    text = ("Here you go:\n```json\n"
            + json.dumps({"proposals": [
                {"file": "f.dig", "spec_name": "T", "rows": [" 1 0 0 "],
                 "why": "gap"}]})
            + "\n```\nGood luck!")
    got = proposer.parse_proposals(text)
    assert got == [{"file": "f.dig", "spec_name": "T",
                    "rows": ["1 0 0"], "why": "gap"}]


def test_parse_returns_empty_on_garbage():
    assert proposer.parse_proposals("no json here") == []
    assert proposer.parse_proposals('{"proposals": "nope"}') == []
    assert proposer.parse_proposals("") == []


def test_validate_drops_illegal_duplicate_and_mistargeted_rows():
    report = scan_tree_coverage(_AND)
    targets = proposer.build_targets(report)
    proposals = [
        {"file": "single_and.dig", "spec_name": targets[0]["spec_name"],
         "rows": ["0x1 0x1 0x1", "1 0", "1 0 1"], "why": "w"},
        {"file": "ghost.dig", "spec_name": "T", "rows": ["1 1 1"], "why": "w"},
    ]
    valid, rejected = proposer.validate_and_dedupe(proposals, targets)
    assert len(valid) == 1 and valid[0]["rows"] == ["1 0 1"]
    reasons = " | ".join(r["reason"] for r in rejected)
    assert "duplicate" in reasons
    assert "columns" in reasons
    assert "unknown target" in reasons


def test_total_row_cap_applies_across_groups():
    report = scan_tree_coverage(_AND)
    targets = proposer.build_targets(report)
    sp = targets[0]["spec_name"]
    many = [{"file": "single_and.dig", "spec_name": sp,
             "rows": [f"1 0 {i % 2}" if i else "1 0 1" for i in range(10)],
             "why": "w"}]
    many[0]["rows"] = ["1 0 1", "0 1 1", "1 1 0", "0 0 1",
                       "0x1 0x0 0x0", "0b0 0b1 0b0", "1 1 1",
                       "0 0 0",
                       "1 0 0", "0 1 0"]
    valid, rejected = proposer.validate_and_dedupe(many, targets)
    n_valid = sum(len(v["rows"]) for v in valid)
    assert n_valid <= proposer._MAX_TOTAL_ROWS

def _two_row_and(tmp_path):
    import re as _re
    xml = open(_AND).read()
    m = _re.search(r"<dataString>.*?</dataString>", xml, _re.S)
    xml2 = xml.replace(m.group(0),
                       "<dataString>A B Y\n0 0 0\n1 1 1</dataString>")
    p = tmp_path / "single_and.dig"
    p.write_text(xml2)
    return p


def test_propose_rows_happy_path_with_fake_model(tmp_path):
    student = _two_row_and(tmp_path)
    spec_name = proposer.build_targets(
        scan_tree_coverage(str(student)))[0]["spec_name"]
    text = json.dumps({"proposals": [
        {"file": "single_and.dig", "spec_name": spec_name,
         "rows": ["1 0 0"], "why": "boundary case A=1,B=0"},
    ]})
    selfcheck = json.dumps({"rows": [{"index": 0, "outputs": {"Y": "0"}}]})
    out = proposer.propose_rows(str(student), call=_fake_two(text, selfcheck))
    assert out["ok"] is True
    assert len(out["proposals"]) == 1
    assert out["proposals"][0]["rows"] == ["1 0 0"]
    assert any("self-check confirmed" in n for n in out["notes"])
    assert out["error"] is None


def test_selfcheck_delivers_unconfirmed_rows_as_disputed(tmp_path):
    student = _two_row_and(tmp_path)
    spec_name = proposer.build_targets(
        scan_tree_coverage(str(student)))[0]["spec_name"]
    text = json.dumps({"proposals": [
        {"file": "single_and.dig", "spec_name": spec_name,
         "rows": ["1 0 1"], "why": "gap"},
    ]})
    selfcheck = json.dumps({"rows": [{"index": 0, "outputs": {"Y": "0"}}]})
    out = proposer.propose_rows(str(student), call=_fake_two(text, selfcheck))
    assert out["ok"] is True
    assert len(out["proposals"]) == 1
    assert out["proposals"][0]["rows"] == ["1 0 1"]
    assert out["proposals"][0]["disputed_rows"] == [0]
    assert not any("self-check" in (r.get("reason") or "")
                   for r in out["rejected"])
    assert any("DISPUTED" in n for n in out["notes"])


def test_reference_gate_drops_rows_the_reference_refutes(tmp_path, monkeypatch):
    student = _two_row_and(tmp_path)
    refdir = tmp_path / "refs"
    refdir.mkdir()
    (refdir / "single_and.dig").write_text(open(_AND).read())
    monkeypatch.setenv("DLC_REFERENCE_DIR", str(refdir))
    mdir = tmp_path / "manifests"
    mdir.mkdir()
    (mdir / "t.json").write_text(json.dumps({
        "lab": "t", "applies_to": ["single_and.dig"],
        "categories": {}, "official_tests": {}, "reference_dir": None,
    }))
    monkeypatch.setenv("DLC_MANIFEST_DIR", str(mdir))

    spec_name = proposer.build_targets(scan_tree_coverage(str(student)))[0]["spec_name"]
    text = json.dumps({"proposals": [
        {"file": "single_and.dig", "spec_name": spec_name,
         "rows": ["1 0 1", "0 1 0"], "why": "gaps"},
    ]})
    selfcheck = json.dumps({"rows": [{"index": 0, "outputs": {"Y": "0"}}]})
    out = proposer.propose_rows(str(student), call=_fake_two(text, selfcheck))
    assert out["ok"] is True
    assert len(out["proposals"]) == 1
    assert out["proposals"][0]["rows"] == ["0 1 0"]
    assert any("lab reference" in r["reason"] for r in out["rejected"])


def test_propose_rows_refuses_when_scan_has_flags():
    bug = ("data/sample_circuits/30_bug_benchmark/"
           "bug1_meaningless_mux_in3/tier3_calculator.dig")
    out = proposer.propose_rows(bug, call=_fake("{}"))
    assert out["ok"] is False
    assert "disagree" in out["error"]


def test_propose_rows_refuses_on_select_gate_with_zero_model_calls():
    bug = ("data/sample_circuits/30_bug_benchmark/"
           "bug6_hidden_mux_case3/uncovered_op_calculator.dig")

    def dead(prompt, **_kw):
        raise AssertionError("select-gated propose must not call the model")

    out = proposer.propose_rows(bug, call=dead)
    assert out["ok"] is False
    assert out["select_gate"] and out["select_gate"][0]["missing"] == [3]
    assert "'Op'" in out["error"] and "value 3" in out["error"]


def _and_manifest(tmp_path, monkeypatch, categories):
    mdir = tmp_path / "manifests"
    mdir.mkdir(exist_ok=True)
    (mdir / "and.json").write_text(json.dumps({
        "lab": "and", "applies_to": ["single_and.dig"],
        "categories": {"single_and.dig": categories},
        "official_tests": {}, "reference_dir": None,
    }))
    monkeypatch.setenv("DLC_MANIFEST_DIR", str(mdir))


def test_propose_rows_stops_when_every_category_is_covered(tmp_path, monkeypatch):
    _and_manifest(tmp_path, monkeypatch, [
        {"name": "both_high", "when": {"A": 1, "B": 1}},
        {"name": "a_low", "when": {"A": 0}},
    ])

    def dead(prompt, **_kw):
        raise AssertionError("a complete file must not reach the model")

    out = proposer.propose_rows(_AND, call=dead)
    assert out["ok"] is True and out["proposals"] == []
    assert out["all_categories_covered"] == ["single_and.dig"]
    assert out["model"] is None
    assert out["notes"] == ["single_and.dig: all 2 instruction categories are "
                            "already exercised by your rows — the coach has "
                            "nothing to add."]

    _and_manifest(tmp_path, monkeypatch, [
        {"name": "both_high", "when": {"A": 1, "B": 1}},
        {"name": "never", "when": {"A": 2}},
    ])
    calls = []

    def fake(prompt, **kw):
        calls.append(prompt)
        return {"ok": True, "text": "{}", "error": None, "usage": None,
                "model": kw.get("model")}

    out2 = proposer.propose_rows(_AND, call=fake)
    assert calls and out2["ok"] is True
    assert "all_categories_covered" not in out2


def test_propose_rows_survives_model_failure_and_garbage():
    def dead(prompt, **kw):
        return {"ok": False, "text": None, "error": "no key", "usage": None,
                "model": kw.get("model")}
    out = proposer.propose_rows(_AND, call=dead)
    assert out["ok"] is False and "no key" in out["error"]

    out2 = proposer.propose_rows(_AND, call=_fake("not json at all"))
    assert out2["ok"] is True and out2["proposals"] == []
    assert out2["notes"]

def _upload_and():
    with open(_AND, "rb") as fh:
        r = client.post("/api/circuit",
                        files=[("files", ("single_and.dig", fh, "application/xml"))])
    assert r.status_code == 200
    return r.json()["session_id"]


def test_propose_endpoint_uses_the_proposer(monkeypatch):
    spec_name = proposer.build_targets(scan_tree_coverage(_AND))[0]["spec_name"]
    text = json.dumps({"proposals": [
        {"file": "single_and.dig", "spec_name": spec_name,
         "rows": ["0 1 0"], "why": "boundary"},
    ]})
    monkeypatch.setattr(proposer, "call_llm", _fake(text))
    sid = _upload_and()
    try:
        r = client.post("/api/l3/propose", json={
            "session_id": sid, "filename": "single_and.dig",
        })
        body = r.json()
        assert body["ok"] is True
        assert body["proposals"] == []
        assert body["rejected"] and body["rejected"][0]["kind"] == "duplicate"
    finally:
        server._SESSIONS.pop(sid, None)


def test_propose_endpoint_404s_on_unknown_session():
    r = client.post("/api/l3/propose", json={
        "session_id": "nope", "filename": "x.dig",
    })
    assert r.status_code == 404



from dlc.testing.runner import find_digital_jar

_needs_jar = pytest.mark.skipif(
    find_digital_jar() is None, reason="Digital.jar not configured",
)


def _cpu_like_target():
    return {"file": "cpu.dig", "spec_name": "Test",
            "headers": ["clk", "ReadData1", "ReadData2"],
            "inputs": [],
            "outputs": [{"label": "ReadData1", "bits": 32},
                        {"label": "ReadData2", "bits": 32}],
            "existing_rows": [], "existing_rows_omitted": 0,
            "has_clock": True, "clock_col": "clk",
            "has_program_rom": True, "program_words": ["13"],
            "rom_capacity_left": 100}


def test_program_group_survives_validation_atomically():
    t = _cpu_like_target()
    props = [{"file": "cpu.dig", "spec_name": "Test",
              "rows": ["C 1 2", "C 3 4"], "why": "r-type gap",
              "program_words": ["628e33", "0x40430EB3"]}]
    valid, rejected = proposer.validate_and_dedupe(props, [t])
    assert rejected == []
    assert len(valid) == 1
    assert valid[0]["program_words"] == ["628e33", "40430eb3"]
    assert valid[0]["rows"] == ["C 1 2", "C 3 4"]


def test_program_group_rejections():
    t = _cpu_like_target()
    base = {"file": "cpu.dig", "spec_name": "Test", "why": "w"}

    def reason(p, target=t):
        v, r = proposer.validate_and_dedupe([p], [target])
        assert v == [] and len(r) == 1
        return r[0]["reason"]

    assert "one row per program word" in reason(
        {**base, "rows": ["C 1 2"], "program_words": ["13", "93"]})
    assert "hex" in reason(
        {**base, "rows": ["C 1 2"], "program_words": ["not-hex"]})
    assert "clock" in reason(
        {**base, "rows": ["0 1 2"], "program_words": ["93"]})
    assert "duplicates an instruction" in reason(
        {**base, "rows": ["C 1 2"], "program_words": ["13"]})
    assert "only valid for clocked" in reason(
        {**base, "rows": ["C 1 2"], "program_words": ["13"]},
        target={**_cpu_like_target(), "has_program_rom": False})
    assert "does not fit" in reason(
        {**base, "rows": ["C 1 2"], "program_words": ["13"]},
        target={**_cpu_like_target(), "rom_capacity_left": 0})


def test_parse_proposals_carries_program_words():
    text = json.dumps({"proposals": [
        {"file": "cpu.dig", "spec_name": "Test", "rows": ["C 1 2"],
         "why": "w", "program_words": ["628e33"]}]})
    got = proposer.parse_proposals(text)
    assert got[0]["program_words"] == ["628e33"]
    text2 = json.dumps({"proposals": [
        {"file": "f.dig", "spec_name": "T", "rows": ["1 0 0"], "why": "w"}]})
    assert "program_words" not in proposer.parse_proposals(text2)[0]


@_needs_jar
def test_inject_endpoint_as_second_builds_second_testcase():
    spec_name = proposer.build_targets(scan_tree_coverage(_AND))[0]["spec_name"]
    sid = _upload_and()
    try:
        r = client.post("/api/l3/inject", json={
            "session_id": sid, "filename": "single_and.dig",
            "spec_name": spec_name, "rows": ["1 0 0"], "as_second": True,
        })
        body = r.json()
        assert body["ok"] is True and body["outcome"] == "all_set"
        assert body["spec_name"] == f"{spec_name}_second"
        assert body["spec_index"] == 1
        assert body["base_spec"]["all_passed"] is True
        assert body["temp_filename"] == "single_and__coach.dig"
    finally:
        server._SESSIONS.pop(sid, None)

def _lab5ish_manifest():
    return {
        "lab": "t", "applies_to": ["cpu.dig"],
        "categories": {"control-unit.dig": [
            {"name": "add",  "when": {"opcode": "0b0110011", "funct3": "0b000",
                                      "funct7": "0b0000000"}},
            {"name": "addi", "when": {"opcode": "0b0010011", "funct3": "0b000"}},
        ]},
        "official_tests": {}, "reference_dir": None,
        "program_decode": {"categories_from": "control-unit.dig",
                           "fields": {"opcode": [0, 7], "funct3": [12, 3],
                                      "funct7": [25, 7]}},
    }


def test_program_word_decode_gate_and_word_info():
    m = _lab5ish_manifest()
    t = {**_cpu_like_target(), "program_words": ["fec00213"],
         "program_categories_missing": ["add"]}
    v, r = proposer.validate_and_dedupe(
        [{"file": "cpu.dig", "spec_name": "Test", "rows": ["C 1 2"],
          "why": "w", "program_words": ["ffffffff"]}], [t], manifest=m)
    assert v == [] and "not an instruction this lab defines" in r[0]["reason"]
    v, r = proposer.validate_and_dedupe(
        [{"file": "cpu.dig", "spec_name": "Test", "rows": ["C 1 2"],
          "why": "w", "program_words": ["628e33"]}], [t], manifest=m)
    assert r == [] and v[0]["word_info"] == [
        {"word": "628e33", "category": "add", "closes_gap": True}]
    v, r = proposer.validate_and_dedupe(
        [{"file": "cpu.dig", "spec_name": "Test", "rows": ["C 1 2"],
          "why": "w", "program_words": ["fec00213"]}], [t], manifest=m)
    assert v == [] and "category 'addi'" in r[0]["reason"]


_PIPE = "data/sample_circuits/tier3_realistic/pipelined_adder_correct.dig"


def test_replay_gate_disputes_state_ignorant_rows_without_reference():
    from dlc.parser.dig_parser import parse_dig_file
    from dlc.testing.spec import extract_test_specs
    spec = extract_test_specs(parse_dig_file(_PIPE))[0]
    t = {"file": "pipelined_adder_correct.dig", "spec_name": spec.name,
         "headers": list(spec.headers), "inputs": [], "outputs": [],
         "existing_rows": [], "existing_rows_omitted": 0,
         "has_clock": True, "clock_col": "Clk", "has_program_rom": False}
    paths = {"pipelined_adder_correct.dig": _PIPE}
    valid = [{"file": t["file"], "spec_name": spec.name,
              "rows": ["5 5 C 0", "0 0 C 99", "7 2 C 0"], "why": "w"}]
    kept, rejected, notes = proposer._replay_gate(valid, [], [], [t], paths)
    assert rejected == []
    assert len(kept) == 1
    assert kept[0]["rows"] == ["5 5 C 0", "0 0 C 99", "7 2 C 0"]
    assert kept[0]["disputed_rows"] == [1]
    assert "your circuit computes" in kept[0]["disputed_details"]["1"]
    assert any("DISPUTED" in n for n in notes)


def test_replay_gate_disputes_case3_rows_on_the_buggy_led():
    led = ("data/sample_circuits/30_bug_benchmark/bug8_gapped_led_minterm/"
           "gapped_LED1.dig")
    from dlc.parser.dig_parser import parse_dig_file
    from dlc.testing.spec import extract_test_specs
    spec = extract_test_specs(parse_dig_file(led))[0]
    t = {"file": "gapped_LED1.dig", "spec_name": spec.name,
         "headers": list(spec.headers), "inputs": [], "outputs": [],
         "existing_rows": [], "existing_rows_omitted": 0,
         "has_clock": True, "clock_col": "Clock", "has_program_rom": False}
    good_row = "1 0 1 0 1 C 1 1 1 0 1 1 1"
    valid = [{"file": t["file"], "spec_name": spec.name,
              "rows": [good_row], "why": "w"}]
    kept, rejected, notes = proposer._replay_gate(
        valid, [], [], [t], {"gapped_LED1.dig": led})
    assert rejected == []
    assert len(kept) == 1 and kept[0]["rows"] == [good_row]
    assert kept[0]["disputed_rows"] == [0]
    assert "your circuit computes" in kept[0]["disputed_details"]["0"]


def test_propose_model_default_and_override(monkeypatch):
    monkeypatch.delenv("DLC_L3_PROPOSE_MODEL", raising=False)
    assert proposer._propose_model() == proposer._PROPOSE_MODEL_FALLBACK
    monkeypatch.setenv("DLC_L3_PROPOSE_MODEL", "claude-sonnet-5")
    assert proposer._propose_model() == "claude-sonnet-5"


def test_category_gate_drops_undefined_operations():
    m = {"categories": {"alu-like.dig": [
        {"name": "AND", "when": {"ALUOp": "0b0000"}},
        {"name": "ADD", "when": {"ALUOp": "0b0010"}}]}}
    t = {"file": "alu-like.dig", "spec_name": "T",
         "headers": ["A", "B", "ALUOp", "Out"], "inputs": [], "outputs": [],
         "existing_rows": [], "existing_rows_omitted": 0,
         "has_clock": False, "clock_col": None, "has_program_rom": False}
    valid = [{"file": "alu-like.dig", "spec_name": "T",
              "rows": ["1 1 9 0", "1 1 0 1", "1 1 X 0"], "why": "w"}]
    kept, rejected, _ = proposer._category_gate(valid, [], [], [t], m)
    assert kept[0]["rows"] == ["1 1 0 1", "1 1 X 0"]
    assert len(rejected) == 1
    assert "does not define" in rejected[0]["reason"]
    assert rejected[0]["file"] == "alu-like.dig"


def test_classify_reason_maps_to_student_kinds():
    c = proposer._classify_reason
    assert c("duplicate of an existing or proposed row") == "duplicate"
    assert c("word 13 duplicates an instruction already in the program") == "duplicate"
    assert c("word ff is not an instruction this lab defines") == "undefined_op"
    assert c("tests an operation this lab does not define (x=9)") == "undefined_op"
    assert c("wrong expected value for the state after the existing rows") == "wrong_expectation"
    assert c("disagrees with the lab reference (Out: ...)") == "wrong_expectation"
    assert c("failed the coach's self-check") == "wrong_expectation"
    assert c("row has 2 cells but testcase has 3 columns") == "format"


def test_uninject_endpoint_removes_nothing_gracefully():
    sid = _upload_and()
    try:
        r = client.post("/api/l3/uninject", json={
            "session_id": sid, "filename": "single_and.dig"})
        body = r.json()
        assert body["ok"] is True and body["removed"] is False
    finally:
        server._SESSIONS.pop(sid, None)


@_needs_jar
def test_uninject_endpoint_evicts_registered_temp():
    spec_name = proposer.build_targets(scan_tree_coverage(_AND))[0]["spec_name"]
    sid = _upload_and()
    try:
        r = client.post("/api/l3/inject", json={
            "session_id": sid, "filename": "single_and.dig",
            "spec_name": spec_name, "rows": ["1 0 0"], "as_second": True})
        assert r.json()["ok"] is True
        names = [f["name"] for f in server._SESSIONS[sid]["files"]]
        assert "single_and__coach.dig" in names
        r2 = client.post("/api/l3/uninject", json={
            "session_id": sid, "filename": "single_and.dig"})
        assert r2.json()["removed"] is True
        names = [f["name"] for f in server._SESSIONS[sid]["files"]]
        assert "single_and__coach.dig" not in names
        assert server._SESSIONS[sid].get("l3_temp") is None
    finally:
        server._SESSIONS.pop(sid, None)


def _lab5rich_manifest():
    """_lab5ish + rd/rs1/rs2 fields, so the lazy gate can judge."""
    m = _lab5ish_manifest()
    m["program_decode"]["fields"].update(
        {"rd": [7, 5], "rs1": [15, 5], "rs2": [20, 5]})
    return m


def test_lazy_program_words_are_rejected_with_lazy_kind():
    m = _lab5rich_manifest()
    t = {**_cpu_like_target(), "program_categories_missing": ["add"]}
    base = {"file": "cpu.dig", "spec_name": "Test", "why": "w"}
    # add x5, x0, x0 — both operands zero
    v, r = proposer.validate_and_dedupe(
        [{**base, "rows": ["C 1 2"], "program_words": ["2b3"]}], [t], manifest=m)
    assert v == [] and "lazy test" in r[0]["reason"]
    assert proposer._classify_reason(r[0]["reason"]) == "lazy"
    # addi x0, x0, 7 — discards the result and reads only x0
    v, r = proposer.validate_and_dedupe(
        [{**base, "rows": ["C 1 2"], "program_words": ["700013"]}], [t], manifest=m)
    assert v == [] and "discards its result" in r[0]["reason"]
    assert proposer._classify_reason(r[0]["reason"]) == "lazy"
    # addi x0, x5, 0 — the READ-BACK idiom survives the gate
    v, r = proposer.validate_and_dedupe(
        [{**base, "rows": ["C 1 2"], "program_words": ["28013"]}], [t], manifest=m)
    assert r == [] and v[0]["word_info"][0]["category"] == "addi"
    # addi x4, x0, -20 — the idiomatic loader passes the LAZY gate
    mo = _observing_manifest()
    v, r = proposer.validate_and_dedupe(
        [{**base, "rows": ["C 1 2"], "program_words": ["fec00213"]}],
        [t], manifest=mo)
    assert r == [] and v[0]["word_info"][0]["category"] == "addi"
    assert len(v[0]["program_words"]) == 2
    assert v[0]["rows"][1].startswith("C (-20)")
    v, r = proposer.validate_and_dedupe(
        [{**base, "rows": ["C 1 2"], "program_words": ["628e33"]}],
        [t], manifest=mo)
    assert r == [] and v[0]["word_info"][0]["closes_gap"] is True


def test_inject_endpoint_routes_program_words_to_append_mode(monkeypatch):
    from dlc.l3.oracle import InjectionOutcome
    from dlc.web import l3_routes

    calls = []

    def fake_program(path, spec_name, rows, rom_words, keep_temp=False):
        calls.append(("program", spec_name, [r.raw for r in rows], rom_words))
        return InjectionOutcome(ok=True, spec_name=spec_name, headers=["a"],
                                rows=[], all_passed=True,
                                added_all_passed=True, temp_path=None)

    def fake_second(path, spec_name, rows, rom_words, keep_temp=False):
        calls.append(("second", spec_name, [r.raw for r in rows], rom_words))
        return InjectionOutcome(ok=True, spec_name=f"{spec_name}_second",
                                headers=["a"], rows=[], all_passed=True,
                                added_all_passed=True, temp_path=None)

    monkeypatch.setattr(l3_routes, "rerun_with_program", fake_program)
    monkeypatch.setattr(l3_routes, "rerun_with_second", fake_second)
    spec_name = proposer.build_targets(scan_tree_coverage(_AND))[0]["spec_name"]
    sid = _upload_and()
    try:
        r = client.post("/api/l3/inject", json={
            "session_id": sid, "filename": "single_and.dig",
            "spec_name": spec_name, "rows": ["1 0 0"], "rom_words": ["13"]})
        assert r.json()["outcome"] == "all_set"
        r = client.post("/api/l3/inject", json={
            "session_id": sid, "filename": "single_and.dig",
            "spec_name": spec_name, "rows": ["1 0 0"],
            "rom_words": ["13"], "as_second": True})
        assert r.json()["spec_name"] == f"{spec_name}_second"
        assert [c[0] for c in calls] == ["program", "second"]
    finally:
        server._SESSIONS.pop(sid, None)


def _observing_manifest():
    m = _lab5rich_manifest()
    m["program_decode"]["observe"] = {
        "rs1_port": "ReadData1", "rs2_port": "ReadData2"}
    return m


def test_unobserved_write_gets_auto_readback_with_proven_value():
    m = _observing_manifest()
    t = {**_cpu_like_target(), "program_words": ["fec00213"],
         "program_categories_missing": ["add"]}
    from dlc.l3 import manifest as mf
    w = mf.encode_category_word(m, "add", rd=8, rs1=4, rs2=4)
    v, r = proposer.validate_and_dedupe(
        [{"file": "cpu.dig", "spec_name": "Test", "rows": ["C 1 2"],
          "why": "w", "program_words": [f"{w:x}"]}], [t], manifest=m)
    assert r == [] and len(v) == 1
    g = v[0]
    auto = mf.encode_category_word(m, "addi", rd=0, rs1=8, imm=0)
    assert g["program_words"] == [f"{w:x}", f"{auto:x}"]
    assert g["rows"][1] == "C (-40) 0"
    assert g["word_info"][1]["auto_readback"] is True
    assert g["word_info"][1]["observes"] == "x8"


def test_extension_with_own_readback_is_untouched():
    m = _observing_manifest()
    from dlc.l3 import manifest as mf
    t = {**_cpu_like_target(), "program_words": ["fec00213"]}
    w = mf.encode_category_word(m, "add", rd=8, rs1=4, rs2=4)
    rb = mf.encode_category_word(m, "addi", rd=0, rs1=8, imm=0)
    v, r = proposer.validate_and_dedupe(
        [{"file": "cpu.dig", "spec_name": "Test", "rows": ["C 1 2", "C 3 4"],
          "why": "w", "program_words": [f"{w:x}", f"{rb:x}"]}], [t], manifest=m)
    assert r == [] and v[0]["program_words"] == [f"{w:x}", f"{rb:x}"]
    assert len(v[0]["rows"]) == 2


def test_unobserved_write_without_observe_mapping_is_rejected():
    m = _lab5rich_manifest()
    from dlc.l3 import manifest as mf
    t = {**_cpu_like_target(), "program_words": ["fec00213"]}
    w = mf.encode_category_word(m, "add", rd=8, rs1=4, rs2=4)
    v, r = proposer.validate_and_dedupe(
        [{"file": "cpu.dig", "spec_name": "Test", "rows": ["C 1 2"],
          "why": "w", "program_words": [f"{w:x}"]}], [t], manifest=m)
    assert v == [] and "unobservable" in r[0]["reason"]
    assert proposer._classify_reason(r[0]["reason"]) == "unobserved"


def test_replay_gate_prefers_the_reference_circuit(monkeypatch, tmp_path):
    """With DLC_REFERENCE_DIR set and the file present there, the clocked
    replay judges rows against the REFERENCE (intended truth), not the
    student's circuit."""
    import shutil
    from dlc.l3 import coverage as cov_mod
    ref_dir = tmp_path / "refs"
    ref_dir.mkdir()
    shutil.copy(_PIPE, ref_dir / "pipelined_adder_correct.dig")
    monkeypatch.setenv("DLC_REFERENCE_DIR", str(ref_dir))
    called = {}

    def fake_replay(path, spec_name, rows, rom_words=None):
        called["path"] = path
        return [{"row": r, "verdict": "agrees", "detail": ""} for r in rows]

    monkeypatch.setattr(cov_mod, "replay_appended_rows", fake_replay)
    t = {"file": "pipelined_adder_correct.dig", "spec_name": "T",
         "headers": ["A", "B", "Clk", "Sum"], "inputs": [], "outputs": [],
         "existing_rows": [], "existing_rows_omitted": 0,
         "has_clock": True, "clock_col": "Clk", "has_program_rom": False}
    g = {"file": "pipelined_adder_correct.dig", "spec_name": "T",
         "rows": ["7 8 C 0"], "why": "w"}
    kept, rejected, _ = proposer._replay_gate(
        [g], [], [], [t], {"pipelined_adder_correct.dig": _PIPE})
    assert kept and not rejected
    assert called["path"] == str(ref_dir / "pipelined_adder_correct.dig")

def test_synthesis_fallback_builds_a_gate_clean_extension():
    m = _observing_manifest()
    t = {**_cpu_like_target(), "program_words": ["fec00213"],
         "program_categories_missing": ["add"],
         "headers": ["clk", "ReadData1", "ReadData2"]}
    valid, rejected, notes = proposer._synthesis_fallback(
        [], [], [], [t], m, {})
    assert valid and valid[0]["synthesized"] is True
    g = valid[0]
    from dlc.l3 import manifest as mf
    decoded = [mf.decode_program_word(m, int(w, 16)) for w in g["program_words"]]
    cats = [d["category"] for d in decoded]
    assert "add" in cats
    assert cats.count("addi") >= 3
    assert g["word_info"][-1]["auto_readback"] is True
    add_i = cats.index("add")
    assert g["rows"][add_i] == "C 7 (-3)"
    assert g["rows"][-1] == "C 4 0"
    assert notes == []
    m2 = _observing_manifest()
    t2 = {**_cpu_like_target(), "program_words": ["fec00213"]}
    from dlc.l3 import manifest as mfm
    w_bad = mfm.encode_category_word(m2, "add", rd=8, rs1=4, rs2=4)
    grp = {"file": "cpu.dig", "spec_name": "Test",
           "rows": ["C 9 9"], "why": "w", "program_words": [f"{w_bad:x}"]}

    def fake_replay(path, spec_name, rows, rom_words=None):
        return [{"row": r, "verdict": "disagrees",
                 "detail": f"ReadData1 mismatch on {r}"} for r in rows]

    import dlc.l3.coverage as covm
    old = covm.replay_appended_rows
    covm.replay_appended_rows = fake_replay
    try:
        kept, rej, _ = proposer._replay_gate(
            [grp], [], [], [t2], {"cpu.dig": "/tmp/nope.dig"})
    finally:
        covm.replay_appended_rows = old
    assert rej == [] and len(kept) == 1
    assert kept[0]["disputed_rows"] == [0]
    assert kept[0]["disputed_details"]["0"] == "ReadData1 mismatch on C 9 9"


def test_replay_gate_program_group_still_drops_atomically_on_reference(
        monkeypatch, tmp_path):
    import shutil
    from dlc.l3 import coverage as covm
    ref_dir = tmp_path / "refs"
    ref_dir.mkdir()
    shutil.copy(_PIPE, ref_dir / "cpu.dig")
    monkeypatch.setenv("DLC_REFERENCE_DIR", str(ref_dir))

    def fake_replay(path, spec_name, rows, rom_words=None):
        return [{"row": r, "verdict": "disagrees", "detail": f"d {r}"}
                for r in rows]

    monkeypatch.setattr(covm, "replay_appended_rows", fake_replay)
    t = {**_cpu_like_target(), "program_words": ["fec00213"]}
    grp = {"file": "cpu.dig", "spec_name": "Test",
           "rows": ["C 9 9"], "why": "w", "program_words": ["940533"]}
    kept, rej, _ = proposer._replay_gate(
        [grp], [], [], [t], {"cpu.dig": "/tmp/nope.dig"})
    assert kept == [] and rej
    assert rej[0]["details"] == [{"row": "C 9 9", "detail": "d C 9 9"}]
    assert "—" not in rej[0]["reason"]


def test_synthesis_skipped_when_a_model_extension_survived():
    m = _observing_manifest()
    t = {**_cpu_like_target(), "program_words": ["fec00213"],
         "program_categories_missing": ["add"]}
    survivor = {"file": "cpu.dig", "spec_name": "Test",
                "rows": ["C 1 2"], "why": "w", "program_words": ["940533"]}
    valid, rejected, notes = proposer._synthesis_fallback(
        [survivor], [], [], [t], m, {})
    assert valid == [survivor] and notes == []


def _upload_paths(*paths):
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
    return r.json()["session_id"]


def test_empty_propose_refunds_the_scan_use_once(monkeypatch):
    from dlc.l3 import limits
    monkeypatch.setattr(proposer, "call_llm", _fake("no json at all"))
    sid = _upload_paths(_AND)
    try:
        scan = client.post("/api/l3/coverage", json={
            "session_id": sid, "filename": "single_and.dig"}).json()
        assert scan["consumed_use"] is True
        assert limits.state()["used"]["modeB"] == 1
        body = client.post("/api/l3/propose", json={
            "session_id": sid, "filename": "single_and.dig"}).json()
        assert body["proposals"] == []
        assert body["refunded"] is True
        assert body["limits"]["used"]["modeB"] == 0
        assert any("refunded" in n for n in body["notes"])
        body2 = client.post("/api/l3/propose", json={
            "session_id": sid, "filename": "single_and.dig"}).json()
        assert "refunded" not in body2
        assert limits.state()["used"]["modeB"] == 0
    finally:
        server._SESSIONS.pop(sid, None)


def test_delivering_propose_clears_refundability(monkeypatch, tmp_path):
    from dlc.l3 import limits
    student = _two_row_and(tmp_path)
    sid = _upload_paths(str(student))
    spec_name = proposer.build_targets(
        scan_tree_coverage(str(student)))[0]["spec_name"]
    text = json.dumps({"proposals": [
        {"file": "single_and.dig", "spec_name": spec_name,
         "rows": ["1 0 0"], "why": "boundary"}]})
    selfcheck = json.dumps({"rows": [{"index": 0, "outputs": {"Y": "0"}}]})
    monkeypatch.setattr(proposer, "call_llm", _fake_two(text, selfcheck))
    try:
        scan = client.post("/api/l3/coverage", json={
            "session_id": sid, "filename": "single_and.dig"}).json()
        assert scan["consumed_use"] is True
        body = client.post("/api/l3/propose", json={
            "session_id": sid, "filename": "single_and.dig"}).json()
        assert body["proposals"], "fixture should deliver a row"
        assert "refunded" not in body
        assert limits.state()["used"]["modeB"] == 1
    finally:
        server._SESSIONS.pop(sid, None)
