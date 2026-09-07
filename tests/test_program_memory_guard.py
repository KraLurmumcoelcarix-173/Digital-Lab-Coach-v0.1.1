"""
Program-memory guard: Mode A must never write course-program words.
"""

import base64
import json
from pathlib import Path

import pytest

from dlc.l3 import debugger
from dlc.l3.debugger import debug_circuit, _protected_program_memory
from dlc.parser.dig_parser import parse_dig_file


def _xml_circuit(elements: str, wires: str = "") -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<circuit>\n'
        "  <version>2</version>\n  <attributes/>\n"
        f"  <visualElements>\n{elements}  </visualElements>\n"
        f"  <wires>\n{wires}  </wires>\n</circuit>\n"
    )


def _ve(name, x, y, entries="") -> str:
    attrs = f"<elementAttributes>{entries}</elementAttributes>" if entries \
        else "<elementAttributes/>"
    return (f"    <visualElement><elementName>{name}</elementName>"
            f"{attrs}<pos x=\"{x}\" y=\"{y}\"/></visualElement>\n")


def _entry(k, v, tag="string") -> str:
    return f"<entry><string>{k}</string><{tag}>{v}</{tag}></entry>"


def _wire(x1, y1, x2, y2) -> str:
    return (f"    <wire><p1 x=\"{x1}\" y=\"{y1}\"/>"
            f"<p2 x=\"{x2}\" y=\"{y2}\"/></wire>\n")

_PROG_LAB = _xml_circuit(
    _ve("In", 0, 0, _entry("Label", "A"))
    + _ve("VDD", 160, 40)
    + _ve("ROM", 200, 0,
          _entry("AddrBits", "1", tag="int")
          + _entry("Bits", "4", tag="int")
          + _entry("isProgramMemory", "true", tag="boolean")
          + _entry("Data", "0,0", tag="data"))
    + _ve("Out", 400, 20,
          _entry("Label", "D") + _entry("Bits", "4", tag="int"))
    + _ve("Testcase", 0, 200,
          _entry("Label", "t")
          + _entry("Testdata", "<dataString>A D\n0 5\n1 6</dataString>",
                   tag="testData")),
    _wire(0, 0, 200, 0) + _wire(160, 40, 200, 40)
    + _wire(260, 20, 400, 20),
)

_ROM_IDX = 2


def _register_payload(monkeypatch, tmp_path, filename):
    defaults = {filename: {
        "content": "A D\n0 5\n1 6", "sha1": "0" * 40,
        "runtime": base64.b64encode(
            json.dumps({"rom": "5,6"}).encode()).decode(),
    }}
    p = tmp_path / "defaults.json"
    p.write_text(json.dumps(defaults), encoding="utf-8")
    monkeypatch.setenv("DLC_OFFICIAL_DEFAULTS_PATH", str(p))


def _write_lab(tmp_path, name="proglab.dig") -> str:
    p = tmp_path / name
    p.write_text(_PROG_LAB, encoding="utf-8")
    return str(p)


def _data_fix_reply(value="5,6"):
    return {
        "contract": "l3.debug.v1.1", "confidence": 0.9,
        "hint": {"suspect_region": "instruction memory",
                 "suspect_signals": ["D"],
                 "why": "the stored words do not match any expected row"},
        "fix": {"ops": [{"op": "change_attribute",
                         "component_index": _ROM_IDX,
                         "name": "Data", "value": value}],
                "explanation_for_student": "rewrite the program words",
                "animation_script": [
                    {"act": "diagnose_line", "text": "words wrong"},
                    {"act": "retest"}]},
    }


@pytest.fixture(autouse=True)
def _no_jar(monkeypatch):
    monkeypatch.setattr(debugger, "find_digital_jar", lambda: None)


def _fake(replies):
    replies = list(replies)
    def call(prompt, **_kw):
        call.log.append(prompt)
        assert replies, "unexpected extra LLM call"
        r = replies.pop(0)
        return {"ok": True,
                "text": json.dumps(r) if isinstance(r, dict) else r,
                "error": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "model": "fake"}
    call.log = []
    return call


def test_protected_indices_need_payload_and_flag(monkeypatch, tmp_path):
    path = _write_lab(tmp_path)
    circuit = parse_dig_file(path)
    monkeypatch.setenv("DLC_OFFICIAL_DEFAULTS_PATH",
                       str(tmp_path / "missing.json"))
    assert _protected_program_memory(circuit, "proglab.dig") == set()
    _register_payload(monkeypatch, tmp_path, "proglab.dig")
    assert _protected_program_memory(circuit, "proglab.dig") == {_ROM_IDX}
    assert _protected_program_memory(
        circuit, ".dlc_injected__proglab.dig") == {_ROM_IDX}
    assert _protected_program_memory(circuit, None) == set()


def test_program_data_op_is_stripped_and_run_says_why(monkeypatch,
                                                      tmp_path):
    _register_payload(monkeypatch, tmp_path, "proglab.dig")
    path = _write_lab(tmp_path)
    call = _fake([_data_fix_reply()])
    res = debug_circuit(path, call=call, use_manifest=False,
                        source_filename="proglab.dig")
    assert res["mode"] == "analysis"
    assert res["cards"] == []
    assert any(d["reason"] == "program_memory_protected"
               for d in res["dropped_ideas"])
    assert any("never edits it" in n for n in res["notes"])
    assert "\n[ROM NOTE]\n" in call.log[0]
    assert "such a change is stripped before verification" in call.log[0]


def test_without_payload_the_same_op_flows_normally(monkeypatch,
                                                    tmp_path):
    monkeypatch.setenv("DLC_OFFICIAL_DEFAULTS_PATH",
                       str(tmp_path / "missing.json"))
    path = _write_lab(tmp_path)
    call = _fake([_data_fix_reply()])
    res = debug_circuit(path, call=call, use_manifest=False,
                        source_filename="proglab.dig")
    assert res["cards"] and res["cards"][0]["verified"]["confirmed"]
    assert res["cards"][0]["fix"]["ops"][0]["name"] == "Data"
    assert "\n[ROM NOTE]\n" not in call.log[0]


def test_unflagged_rom_is_protected_when_a_payload_exists(monkeypatch,
                                                          tmp_path):
    _register_payload(monkeypatch, tmp_path, "proglab.dig")
    p = tmp_path / "proglab.dig"
    p.write_text(_PROG_LAB.replace(
        _entry("isProgramMemory", "true", tag="boolean"), ""),
        encoding="utf-8")
    circuit = parse_dig_file(str(p))
    assert not circuit.components[_ROM_IDX].attributes.get("isProgramMemory")
    assert _protected_program_memory(circuit, "proglab.dig") == {_ROM_IDX}


def test_prompt_teaches_the_rom_rule():
    from dlc.l3.debugger import _load_prompt
    text = _load_prompt()
    assert "[ROM NOTE]" in text
    assert "stripped before verification" in text
    assert "[PROGRAM MEMORY]" not in text


def test_empty_program_rom_warning_names_the_injection(monkeypatch,
                                                       tmp_path):
    from fastapi.testclient import TestClient
    from dlc.web.server import app

    _register_payload(monkeypatch, tmp_path, "proglab.dig")
    empty_lab = _PROG_LAB.replace(
        _entry("Data", "0,0", tag="data"), "")
    client = TestClient(app)
    r = client.post("/api/circuit", files=[
        ("files", ("proglab.dig", empty_lab.encode(), "application/xml"))])
    assert r.status_code == 200
    issues = r.json()["files"][0]["issues"]
    rom_warns = [i for i in issues if i["kind"] == "empty_rom"]
    assert rom_warns, "empty program ROM must still warn"
    assert any("debugger only runs once the ROM holds it" in i["message"]
               for i in rom_warns)
    assert all(i["severity"] == "warning" for i in rom_warns)

    r2 = client.post("/api/circuit", files=[
        ("files", ("other.dig", empty_lab.encode(), "application/xml"))])
    issues2 = r2.json()["files"][0]["issues"]
    plain = [i for i in issues2 if i["kind"] == "empty_rom"]
    assert plain and all(
        "debugger only runs" not in i["message"] for i in plain)


def test_add_component_smuggle_route_is_also_stripped(monkeypatch,
                                                      tmp_path):
    _register_payload(monkeypatch, tmp_path, "proglab.dig")
    path = _write_lab(tmp_path)
    smuggle = {
        "contract": "l3.debug.v1.1", "confidence": 0.9,
        "hint": {"suspect_region": "instruction memory",
                 "suspect_signals": ["D"], "why": "words look wrong"},
        "fix": {"ops": [
            {"op": "add_component", "element_name": "ROM",
             "position": [600, 0],
             "attributes": {"AddrBits": 1, "Bits": 4, "Data": "5,6"}},
            {"op": "add_wire", "p1": [0, 0], "p2": [600, 0]},
        ],
            "explanation_for_student": "use a fresh ROM",
            "animation_script": [
                {"act": "diagnose_line", "text": "swap the memory"},
                {"act": "retest"}]},
    }
    call = _fake([smuggle, _data_fix_reply(), _data_fix_reply()])
    res = debug_circuit(path, call=call, use_manifest=False,
                        source_filename="proglab.dig")
    assert not [c for c in res["cards"]
                if c["verified"]["confirmed"]]
    blob = json.dumps(res)
    assert '"5,6"' not in blob
    assert any("never edits it" in n for n in res["notes"])
