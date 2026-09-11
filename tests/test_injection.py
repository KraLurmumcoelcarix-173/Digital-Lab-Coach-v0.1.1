import json
import os
import xml.etree.ElementTree as ET

import pytest

from dlc.l3 import official_store
from dlc.testing.inject import (
    prepare_injected_run, cleanup_injected, file_test_status,
)


_EMPTY_CPU = """<?xml version="1.0" encoding="utf-8"?>
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
        <entry><string>Label</string><string>Instruction Memory</string></entry>
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


def _with_testcase(rows: str) -> str:
    return _EMPTY_CPU.replace(
        "<dataString>clk ReadData1 ReadData2\n</dataString>",
        f"<dataString>{rows}</dataString>",
    )


@pytest.fixture(autouse=True)
def _isolated_user_store(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "DLC_OFFICIAL_TESTS_PATH", str(tmp_path / "official_tests.json")
    )


def test_defaults_expose_cpu_testcase_and_hidden_runtime():
    assert official_store.get_content("cpu.dig")
    assert not hasattr(official_store, "get_rom_program")
    defaults_path = os.path.join(
        os.path.dirname(__file__), "..", "data",
        "official_tests_defaults.json",
    )
    assert "fec00213" not in open(defaults_path, encoding="utf-8").read()
    rom = official_store.get_runtime_payload("cpu.dig", "rom")
    assert rom and rom.startswith("fec00213")
    assert official_store.get_runtime_payload("mystery.dig", "rom") is None
    assert "fec00213" not in json.dumps(official_store.list_tests())
    assert "runtime" not in json.dumps(official_store.list_tests())


def test_status_missing_modified_official(tmp_path):
    from dlc.parser.dig_parser import parse_dig_file

    p = tmp_path / "cpu.dig"
    p.write_text(_EMPTY_CPU, encoding="utf-8")
    assert file_test_status(parse_dig_file(str(p)), "cpu.dig") == "missing"

    p.write_text(_with_testcase("clk ReadData1 ReadData2\n0 0 0\n"),
                 encoding="utf-8")
    assert file_test_status(parse_dig_file(str(p)), "cpu.dig") == "modified"

    p.write_text(_with_testcase(official_store.get_content("cpu.dig")),
                 encoding="utf-8")
    assert file_test_status(parse_dig_file(str(p)), "cpu.dig") == "official"

    q = tmp_path / "mystery.dig"
    q.write_text(_EMPTY_CPU, encoding="utf-8")
    assert file_test_status(parse_dig_file(str(q)), "mystery.dig") is None


def test_injects_official_testcase_when_missing(tmp_path):
    p = tmp_path / "cpu.dig"
    p.write_text(_EMPTY_CPU, encoding="utf-8")
    before = p.read_text(encoding="utf-8")

    temp, notes = prepare_injected_run(str(p), "cpu.dig")
    try:
        assert temp and os.path.exists(temp)
        assert os.path.dirname(temp) == str(tmp_path)
        assert any("no test rows" in n for n in notes)

        root = ET.parse(temp).getroot()
        ds = [el.text or "" for el in root.iter("dataString")]
        assert any("ReadData1" in t and len(t.splitlines()) > 5 for t in ds)
        datas = [
            kids[1].text
            for ve in root.iter("visualElement")
            for e in ve.iter("entry")
            if len(kids := list(e)) == 2 and kids[0].text == "Data"
        ]
        assert not any((d or "").strip() for d in datas), \
            "a ROM must never be filled"
        assert len(notes) == 1
        assert p.read_text(encoding="utf-8") == before
    finally:
        cleanup_injected(temp)
    assert temp and not os.path.exists(temp)


def test_injects_official_testcase_when_modified(tmp_path):
    p = tmp_path / "cpu.dig"
    p.write_text(_with_testcase("clk ReadData1 ReadData2\n0 0 0\nC 1 1\n"),
                 encoding="utf-8")
    temp, notes = prepare_injected_run(str(p), "cpu.dig")
    try:
        assert temp and notes and "modified" in notes[0]
        root = ET.parse(temp).getroot()
        tcs = [ve for ve in root.iter("visualElement")
               if ve.findtext("elementName") == "Testcase"]
        assert len(tcs) == 1
        ds = [el.text or "" for el in root.iter("dataString")]
        assert any("ReadData1" in t and len(t.splitlines()) > 5 for t in ds)
        assert not any("C 1 1" in t for t in ds)
    finally:
        cleanup_injected(temp)


def test_no_temp_when_tests_official_even_with_an_empty_rom(tmp_path):
    p = tmp_path / "cpu.dig"
    p.write_text(_with_testcase(official_store.get_content("cpu.dig")),
                 encoding="utf-8")
    temp, notes = prepare_injected_run(str(p), "cpu.dig")
    assert temp is None and notes == []


def test_no_injection_when_tests_official_and_rom_programmed(tmp_path):
    src2 = _with_testcase(official_store.get_content("cpu.dig")).replace(
        "<entry><string>Label</string><string>Instruction Memory</string></entry>",
        "<entry><string>Label</string><string>Instruction Memory</string></entry>"
        "<entry><string>Data</string><data>1,2,3</data></entry>",
    )
    p = tmp_path / "cpu.dig"
    p.write_text(src2, encoding="utf-8")
    temp, notes = prepare_injected_run(str(p), "cpu.dig")
    assert temp is None and notes == []


def test_no_injection_for_unregistered_filename(tmp_path):
    p = tmp_path / "mystery.dig"
    p.write_text(_EMPTY_CPU, encoding="utf-8")
    temp, notes = prepare_injected_run(str(p), "mystery.dig")
    assert temp is None and notes == []


def test_empty_rom_is_warning_and_never_blocks(tmp_path):
    from dlc.parser.dig_parser import parse_dig_file
    from dlc.analyzer import check_all_l1_deep
    from dlc.web.server import _l1_error_block

    p = tmp_path / "cpu.dig"
    p.write_text(_EMPTY_CPU, encoding="utf-8")
    c = parse_dig_file(str(p))
    issues = check_all_l1_deep(c)
    kinds = {(i.kind, i.severity.value) for i in issues.issues}
    assert ("empty_rom", "warning") in kinds
    assert not [i for i in issues.issues if i.severity.value == "error"]
    assert _l1_error_block(c) is None


def test_data_words_parses_digital_formats():
    from dlc.testing.inject import _data_words
    assert _data_words("5,6") == [5, 6]
    assert _data_words("5 6 0 0") == [5, 6]
    assert _data_words("2*1f,3") == [31, 31, 3]
    assert _data_words("10,11", "dec") == [10, 11]
    assert _data_words("FEC00213") == [0xFEC00213]
    assert _data_words("") == []
    assert _data_words("0,0,0") == []


def _cpu_with_rom(data):
    if data is None:
        return _EMPTY_CPU
    return _EMPTY_CPU.replace(
        "<entry><string>Label</string><string>Instruction Memory</string></entry>",
        "<entry><string>Label</string><string>Instruction Memory</string></entry>"
        f"<entry><string>Data</string><data>{data}</data></entry>",
    )


def test_rom_gate_verdicts(tmp_path):
    import re
    from dlc.testing.inject import check_rom_contents

    official = official_store.get_runtime_payload("cpu.dig", "rom")
    n_official = len(official.split(","))
    p = tmp_path / "cpu.dig"

    p.write_text(_cpu_with_rom(None), encoding="utf-8")
    v = check_rom_contents(str(p), "cpu.dig")
    assert v["status"] == "empty" and v["rom"] == "Instruction Memory"
    assert v["file"] == "cpu.dig"
    assert v["words_expected"] == n_official and v["words_found"] == 0
    assert "in cpu.dig is empty" in v["message"]

    p.write_text(_cpu_with_rom("1,2,3"), encoding="utf-8")
    v = check_rom_contents(str(p), "cpu.dig")
    assert v["status"] == "mismatch" and v["first_bad_address"] == 0
    assert v["words_found"] == 3 and v["differing"] == n_official
    assert "your word there is 1" in v["message"]
    assert official.split(",")[0] not in v["message"]

    p.write_text(_cpu_with_rom(official), encoding="utf-8")
    assert check_rom_contents(str(p), "cpu.dig") is None
    assert check_rom_contents(str(p), ".dlc_injected__cpu.dig") is None
    assert check_rom_contents(str(p), "mystery.dig") is None

    p.write_text(_cpu_with_rom(official.upper() + ",0,0"), encoding="utf-8")
    assert check_rom_contents(str(p), "cpu.dig") is None

    q = tmp_path / "norom.dig"
    q.write_text(re.sub(r"<visualElement>\s*<elementName>ROM</elementName>"
                        r".*?</visualElement>\s*", "", _EMPTY_CPU,
                        flags=re.S), encoding="utf-8")
    v = check_rom_contents(str(q), "cpu.dig")
    assert v["status"] == "missing" and "has no ROM" in v["message"]
    assert v["file"] == "cpu.dig"
    assert check_rom_contents(str(tmp_path / "absent.dig"), "cpu.dig") is None


_CHILD_ROM_LAB = (
    '<?xml version="1.0" encoding="utf-8"?><circuit><version>2</version>'
    '<attributes/><visualElements>'
    '<visualElement><elementName>In</elementName><elementAttributes>'
    '<entry><string>Label</string><string>A</string></entry>'
    '</elementAttributes><pos x="0" y="0"/></visualElement>'
    '<visualElement><elementName>ROM</elementName><elementAttributes>'
    '<entry><string>AddrBits</string><int>1</int></entry>'
    '<entry><string>Bits</string><int>4</int></entry>%s'
    '</elementAttributes><pos x="200" y="0"/></visualElement>'
    '<visualElement><elementName>Out</elementName><elementAttributes>'
    '<entry><string>Label</string><string>D</string></entry>'
    '<entry><string>Bits</string><int>4</int></entry>'
    '</elementAttributes><pos x="400" y="20"/></visualElement>'
    '</visualElements><wires>'
    '<wire><p1 x="0" y="0"/><p2 x="200" y="0"/></wire>'
    '<wire><p1 x="260" y="20"/><p2 x="400" y="20"/></wire>'
    '</wires></circuit>'
)

_PARENT_OF_ROM_LAB = (
    '<?xml version="1.0" encoding="utf-8"?><circuit><version>2</version>'
    '<attributes/><visualElements>'
    '<visualElement><elementName>In</elementName><elementAttributes>'
    '<entry><string>Label</string><string>A</string></entry>'
    '</elementAttributes><pos x="0" y="0"/></visualElement>'
    '<visualElement><elementName>romlab.dig</elementName>'
    '<elementAttributes/><pos x="200" y="0"/></visualElement>'
    '<visualElement><elementName>Out</elementName><elementAttributes>'
    '<entry><string>Label</string><string>D</string></entry>'
    '<entry><string>Bits</string><int>4</int></entry>'
    '</elementAttributes><pos x="400" y="0"/></visualElement>'
    '<visualElement><elementName>Testcase</elementName><elementAttributes>'
    '<entry><string>Label</string><string>t</string></entry>'
    '<entry><string>Testdata</string><testData><dataString>A D\n0 5'
    '</dataString></testData></entry>'
    '</elementAttributes><pos x="0" y="200"/></visualElement>'
    '</visualElements><wires/></circuit>'
)


def test_rom_gate_walks_the_subcircuit_tree(tmp_path, monkeypatch):
    import base64
    from dlc.testing.inject import check_rom_contents

    defaults = {"romlab.dig": {
        "content": "A D\n0 5\n1 6", "sha1": "0" * 40,
        "runtime": base64.b64encode(
            json.dumps({"rom": "5,6"}).encode()).decode()}}
    (tmp_path / "defaults.json").write_text(json.dumps(defaults),
                                            encoding="utf-8")
    monkeypatch.setenv("DLC_OFFICIAL_DEFAULTS_PATH",
                       str(tmp_path / "defaults.json"))
    parent = tmp_path / "top.dig"
    parent.write_text(_PARENT_OF_ROM_LAB, encoding="utf-8")
    child = tmp_path / "romlab.dig"

    child.write_text(_CHILD_ROM_LAB % "", encoding="utf-8")
    v = check_rom_contents(str(parent), "top.dig")
    assert v["status"] == "empty" and v["file"] == "romlab.dig"
    assert "in romlab.dig is empty" in v["message"]

    child.write_text(_CHILD_ROM_LAB % (
        '<entry><string>Data</string><data>5,7</data></entry>'),
        encoding="utf-8")
    v = check_rom_contents(str(parent), "top.dig")
    assert v["status"] == "mismatch" and v["file"] == "romlab.dig"
    assert v["first_bad_address"] == 1

    child.write_text(_CHILD_ROM_LAB % (
        '<entry><string>Data</string><data>5,6</data></entry>'),
        encoding="utf-8")
    assert check_rom_contents(str(parent), "top.dig") is None
    assert check_rom_contents(str(child), "romlab.dig") is None


def test_rom_gate_checks_only_the_program_memory_when_flagged(tmp_path):
    from dlc.testing.inject import check_rom_contents

    official = official_store.get_runtime_payload("cpu.dig", "rom")
    flagged = _cpu_with_rom(official).replace(
        "<entry><string>Label</string><string>Instruction Memory</string></entry>",
        "<entry><string>Label</string><string>Instruction Memory</string></entry>"
        "<entry><string>isProgramMemory</string><boolean>true</boolean></entry>",
    ).replace(
        "    <visualElement>\n      <elementName>Testcase</elementName>",
        "    <visualElement>\n      <elementName>ROM</elementName>\n"
        "      <elementAttributes>\n"
        "        <entry><string>Label</string><string>lookup</string></entry>\n"
        "      </elementAttributes>\n      <pos x=\"400\" y=\"0\"/>\n"
        "    </visualElement>\n"
        "    <visualElement>\n      <elementName>Testcase</elementName>",
    )
    p = tmp_path / "cpu.dig"
    p.write_text(flagged, encoding="utf-8")
    assert check_rom_contents(str(p), "cpu.dig") is None

    p.write_text(flagged.replace(
        "<entry><string>isProgramMemory</string><boolean>true</boolean></entry>",
        ""), encoding="utf-8")
    v = check_rom_contents(str(p), "cpu.dig")
    assert v["status"] == "empty" and v["rom"] == "lookup"


def test_injected_testcase_is_labeled(tmp_path):
    from dlc.testing.inject import INJECTED_TEST_LABEL
    from dlc.parser.dig_parser import parse_dig_file
    from dlc.testing.spec import extract_test_specs

    p = tmp_path / "cpu.dig"
    p.write_text(_EMPTY_CPU, encoding="utf-8")
    temp, notes = prepare_injected_run(str(p), "cpu.dig")
    try:
        assert temp and notes
        specs = extract_test_specs(parse_dig_file(temp))
        assert [s.name for s in specs] == [INJECTED_TEST_LABEL]
        assert specs[0].rows
    finally:
        cleanup_injected(temp)
