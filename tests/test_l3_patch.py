import glob
import os
from pathlib import Path

import pytest

from dlc.parser.dig_parser import parse_dig_file
from dlc.parser.graph import build_signal_graph
from dlc.parser.netlist import build_netlist
from dlc.testing.runner import find_digital_jar
from dlc.l3.patch import apply_patch, rerun_with_patch

_BUG3 = "data/sample_circuits/30_bug_benchmark/bug3_wrong_cin/Wrong_cin.dig"
_BUG1 = "data/sample_circuits/30_bug_benchmark/bug1_meaningless_mux_in3/tier3_calculator.dig"
_AND = "data/sample_circuits/tier1_minimal/single_and.dig"

_needs_jar = pytest.mark.skipif(
    find_digital_jar() is None, reason="Digital.jar not configured",
)


def _mini_circuit(wires: str, extra_elements: str = "") -> str:
    """A tiny In-A/In-B/Comparator/Out circuit for pin-level op tests.
    Comparator pins: A@(200,0) B@(200,20); gr@(260,0)."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<circuit>
  <version>2</version>
  <attributes/>
  <visualElements>
    <visualElement>
      <elementName>In</elementName>
      <elementAttributes>
        <entry><string>Label</string><string>A</string></entry>
      </elementAttributes>
      <pos x="0" y="0"/>
    </visualElement>
    <visualElement>
      <elementName>In</elementName>
      <elementAttributes>
        <entry><string>Label</string><string>B</string></entry>
      </elementAttributes>
      <pos x="0" y="20"/>
    </visualElement>
    <visualElement>
      <elementName>Comparator</elementName>
      <elementAttributes/>
      <pos x="200" y="0"/>
    </visualElement>
    <visualElement>
      <elementName>Out</elementName>
      <elementAttributes>
        <entry><string>Label</string><string>G</string></entry>
      </elementAttributes>
      <pos x="300" y="0"/>
    </visualElement>{extra_elements}
  </visualElements>
  <wires>
{wires}
  </wires>
  <measurementOrdering/>
</circuit>
"""


_SIMPLE_WIRES = """    <wire><p1 x="0" y="0"/><p2 x="200" y="0"/></wire>
    <wire><p1 x="0" y="20"/><p2 x="200" y="20"/></wire>
    <wire><p1 x="260" y="0"/><p2 x="300" y="0"/></wire>"""


def _edges(path):
    c = parse_dig_file(path)
    nl = build_netlist(c)
    g = build_signal_graph(c, nl)
    out = set()
    for u, v, d in g.edges(data=True):
        cu, cv = c.components[u], c.components[v]
        out.add((cu.label or cu.element_name, d["driver_pin"],
                 cv.label or cv.element_name, d["sink_pin"]))
    return out

def test_swap_pins_exchanges_the_feeding_wires(tmp_path):
    src = tmp_path / "mini.dig"
    src.write_text(_mini_circuit(_SIMPLE_WIRES), encoding="utf-8")
    before = _edges(str(src))
    assert ("A", "out", "Comparator", "A") in before
    assert ("B", "out", "Comparator", "B") in before

    temp, report = apply_patch(str(src), [
        {"op": "swap_pins", "component_index": 2, "pin_a": "A", "pin_b": "B"},
    ])
    assert report.ok, report.warning
    try:
        after = _edges(temp)
        assert ("A", "out", "Comparator", "B") in after
        assert ("B", "out", "Comparator", "A") in after
    finally:
        os.unlink(temp)


def test_rewire_pin_moves_a_sink_to_another_driver(tmp_path):
    src = tmp_path / "mini.dig"
    src.write_text(_mini_circuit(_SIMPLE_WIRES), encoding="utf-8")
    temp, report = apply_patch(str(src), [
        {"op": "rewire_pin", "component_index": 2, "pin": "B",
         "to": {"component_index": 0, "pin": "out"}},
    ])
    assert report.ok, report.warning
    try:
        after = _edges(temp)
        assert ("A", "out", "Comparator", "B") in after
        assert ("B", "out", "Comparator", "B") not in after
    finally:
        os.unlink(temp)


def _tunnel(name, x, y):
    return f"""
    <visualElement>
      <elementName>Tunnel</elementName>
      <elementAttributes>
        <entry><string>NetName</string><string>{name}</string></entry>
      </elementAttributes>
      <pos x="{x}" y="{y}"/>
    </visualElement>"""


_TUNNEL_WIRES = """    <wire><p1 x="0" y="0"/><p2 x="40" y="0"/></wire>
    <wire><p1 x="0" y="20"/><p2 x="40" y="20"/></wire>
    <wire><p1 x="160" y="0"/><p2 x="200" y="0"/></wire>
    <wire><p1 x="160" y="20"/><p2 x="200" y="20"/></wire>
    <wire><p1 x="260" y="0"/><p2 x="300" y="0"/></wire>"""


def test_rewire_pin_through_a_tunnel_stub_renames_the_stub(tmp_path):
    src = tmp_path / "tunnels.dig"
    src.write_text(_mini_circuit(
        _TUNNEL_WIRES,
        _tunnel("a", 40, 0) + _tunnel("b", 40, 20)
        + _tunnel("a", 160, 0) + _tunnel("b", 160, 20)), encoding="utf-8")
    before = parse_dig_file(str(src))
    assert ("B", "out", "Comparator", "B") in _edges(str(src))
    temp, report = apply_patch(str(src), [
        {"op": "rewire_pin", "component_index": 2, "pin": "B",
         "to": {"component_index": 0, "pin": "out"}},
    ])
    assert report.ok, report.warning
    assert "tunnel [7] renamed 'b' -> 'a'" in report.applied[0]
    try:
        after = parse_dig_file(temp)
        assert len(after.components) == len(before.components)
        assert len(after.wires) == len(before.wires)
        assert after.components[7].attributes["NetName"] == "a"
        edges = _edges(temp)
        assert ("A", "out", "Comparator", "B") in edges
        assert ("B", "out", "Comparator", "B") not in edges
    finally:
        os.unlink(temp)


def test_swap_pins_refuses_a_shared_junction(tmp_path):
    wires = _SIMPLE_WIRES + """
    <wire><p1 x="200" y="0"/><p2 x="200" y="-40"/></wire>"""
    src = tmp_path / "mini.dig"
    src.write_text(_mini_circuit(wires), encoding="utf-8")
    temp, report = apply_patch(str(src), [
        {"op": "swap_pins", "component_index": 2, "pin_a": "A", "pin_b": "B"},
    ])
    assert temp is None and not report.ok
    assert "junction" in (report.warning or "")


def test_replace_element_keeps_attributes(tmp_path):
    temp, report = apply_patch(_AND, [
        {"op": "replace_element", "component_index": 2, "new_element": "Or"},
    ]) if parse_dig_file(_AND).components[2].element_name == "And" else (None, None)
    if temp is None:
        c = parse_dig_file(_AND)
        idx = next(i for i, comp in enumerate(c.components)
                   if comp.element_name == "And")
        temp, report = apply_patch(_AND, [
            {"op": "replace_element", "component_index": idx, "new_element": "Or"},
        ])
    assert report.ok, report.warning
    try:
        patched = parse_dig_file(temp)
        assert any(comp.element_name == "Or" for comp in patched.components)
        assert not any(comp.element_name == "And" for comp in patched.components)
    finally:
        os.unlink(temp)


def test_change_attribute_preserves_existing_value_tag(tmp_path):
    extra = """
    <visualElement>
      <elementName>Const</elementName>
      <elementAttributes>
        <entry><string>Value</string><long>0</long></entry>
      </elementAttributes>
      <pos x="0" y="200"/>
    </visualElement>"""
    src = tmp_path / "mini.dig"
    src.write_text(_mini_circuit(_SIMPLE_WIRES, extra), encoding="utf-8")
    temp, report = apply_patch(str(src), [
        {"op": "change_attribute", "component_index": 4,
         "name": "Value", "value": 5},
    ])
    assert report.ok, report.warning
    try:
        text = Path(temp).read_text(encoding="utf-8")
        assert "<long>5</long>" in text
        patched = parse_dig_file(temp)
        assert patched.components[4].attributes["Value"] == 5
    finally:
        os.unlink(temp)


def test_change_attribute_creates_missing_entry_with_typed_tag(tmp_path):
    extra = """
    <visualElement>
      <elementName>Const</elementName>
      <elementAttributes/>
      <pos x="0" y="200"/>
    </visualElement>"""
    src = tmp_path / "mini.dig"
    src.write_text(_mini_circuit(_SIMPLE_WIRES, extra), encoding="utf-8")
    temp, report = apply_patch(str(src), [
        {"op": "change_attribute", "component_index": 4,
         "name": "Value", "value": 0},
    ])
    assert report.ok, report.warning
    try:
        text = Path(temp).read_text(encoding="utf-8")
        assert "<long>0</long>" in text
        assert parse_dig_file(temp).components[4].attributes["Value"] == 0
    finally:
        os.unlink(temp)


def test_change_attribute_normalizes_rom_data_forms(tmp_path):
    extra = """
    <visualElement>
      <elementName>ROM</elementName>
      <elementAttributes>
        <entry><string>AddrBits</string><int>3</int></entry>
        <entry><string>Bits</string><int>8</int></entry>
        <entry><string>Data</string><data>82,86,80</data></entry>
      </elementAttributes>
      <pos x="0" y="200"/>
    </visualElement>"""
    src = tmp_path / "mini.dig"
    src.write_text(_mini_circuit(_SIMPLE_WIRES, extra), encoding="utf-8")
    for value in (["0x82", "0x86", "0xC2"],
                  "0x82, 0x86, 0xC2",
                  "82,86,c2"):
        temp, report = apply_patch(str(src), [
            {"op": "change_attribute", "component_index": 4,
             "name": "Data", "value": value},
        ])
        assert report.ok, report.warning
        try:
            text = Path(temp).read_text(encoding="utf-8")
            assert "<data>82,86,c2</data>" in text
        finally:
            os.unlink(temp)


def test_add_and_delete_wire_roundtrip(tmp_path):
    src = tmp_path / "mini.dig"
    src.write_text(_mini_circuit(_SIMPLE_WIRES), encoding="utf-8")
    temp, report = apply_patch(str(src), [
        {"op": "delete_wire", "p1": [0, 20], "p2": [200, 20]},
        {"op": "add_wire", "p1": [0, 20], "p2": [200, 20]},
    ])
    assert report.ok, report.warning
    try:
        assert _edges(temp) == _edges(str(src))
    finally:
        os.unlink(temp)


def test_unknown_op_and_bad_index_are_rejected(tmp_path):
    src = tmp_path / "mini.dig"
    src.write_text(_mini_circuit(_SIMPLE_WIRES), encoding="utf-8")
    temp, report = apply_patch(str(src), [{"op": "teleport"}])
    assert temp is None and "Unknown patch op" in report.warning
    temp, report = apply_patch(str(src), [
        {"op": "replace_element", "component_index": 99, "new_element": "Or"},
    ])
    assert temp is None and "out of range" in report.warning


def test_delete_missing_wire_is_rejected(tmp_path):
    src = tmp_path / "mini.dig"
    src.write_text(_mini_circuit(_SIMPLE_WIRES), encoding="utf-8")
    temp, report = apply_patch(str(src), [
        {"op": "delete_wire", "p1": [1, 1], "p2": [2, 2]},
    ])
    assert temp is None and "no wire" in report.warning


def test_patch_introducing_l1_errors_is_rejected(tmp_path):
    src = tmp_path / "mini.dig"
    src.write_text(_mini_circuit(_SIMPLE_WIRES), encoding="utf-8")
    temp, report = apply_patch(str(src), [
        {"op": "add_wire", "p1": [0, 20], "p2": [0, 0]},
    ])
    assert temp is None and not report.ok
    assert "new Layer-1" in report.warning
    assert "multi_driver" in report.new_l1_error_kinds
    leftovers = glob.glob(str(tmp_path / "dlc_row_l3fix_*.dig"))
    assert leftovers == []

@_needs_jar
def test_bug3_fix_carry_const_makes_all_rows_pass():
    out = rerun_with_patch(_BUG3, [
        {"op": "change_attribute", "component_index": 16,
         "name": "Value", "value": 0},
    ])
    assert out.ok, out.warning
    assert out.all_passed is True, out.specs
    assert out.temp_path is None
    leftovers = glob.glob(
        "data/sample_circuits/30_bug_benchmark/bug3_wrong_cin/dlc_row_l3fix_*.dig"
    )
    assert leftovers == []


@_needs_jar
def test_bug3_unpatched_baseline_still_fails():
    out = rerun_with_patch(_BUG3, [
        {"op": "change_attribute", "component_index": 16,
         "name": "Value", "value": 1},
    ])
    assert out.ok
    assert out.all_passed is False


@_needs_jar
def test_bug1_rewire_mux_in3_to_bool_unit_makes_all_rows_pass():
    out = rerun_with_patch(_BUG1, [
        {"op": "rewire_pin", "component_index": 14, "pin": "in3",
         "to": {"component_index": 9, "pin": "Result"}},
    ])
    assert out.ok, out.warning
    assert out.all_passed is True, out.specs


def test_add_component_wired_via_add_wire(tmp_path):
    src = tmp_path / "mini.dig"
    src.write_text(_mini_circuit(_SIMPLE_WIRES), encoding="utf-8")
    temp, report = apply_patch(str(src), [
        {"op": "add_component", "element_name": "Not",
         "position": [100, 60], "attributes": {"Bits": 1}},
        {"op": "delete_wire", "p1": [0, 20], "p2": [200, 20]},
        {"op": "add_wire", "p1": [0, 20], "p2": [100, 60]},
        {"op": "add_wire", "p1": [140, 60], "p2": [200, 20]},
    ])
    assert report.ok, report.warning
    try:
        patched = parse_dig_file(temp)
        assert patched.components[-1].element_name == "Not"
        assert patched.components[-1].attributes["Bits"] == 1
        after = _edges(temp)
        assert ("B", "out", "Not", "A") in after
        assert ("Not", "Y", "Comparator", "B") in after
    finally:
        os.unlink(temp)


def test_delete_component_removes_element_and_deadend_wire(tmp_path):
    src = tmp_path / "mini.dig"
    src.write_text(_mini_circuit(_SIMPLE_WIRES), encoding="utf-8")
    temp, report = apply_patch(str(src), [
        {"op": "delete_component", "component_index": 3},
    ])
    assert report.ok, report.warning
    try:
        patched = parse_dig_file(temp)
        assert len(patched.components) == 3
        assert not any(c.element_name == "Out" for c in patched.components)
        assert len(patched.wires) == 2
    finally:
        os.unlink(temp)


def test_delete_component_keeps_junction_wires(tmp_path):
    wires = _SIMPLE_WIRES + """
    <wire><p1 x="300" y="0"/><p2 x="300" y="100"/></wire>"""
    src = tmp_path / "mini.dig"
    src.write_text(_mini_circuit(wires), encoding="utf-8")
    temp, report = apply_patch(str(src), [
        {"op": "delete_component", "component_index": 3},
    ])
    assert report.ok, report.warning
    try:
        patched = parse_dig_file(temp)
        assert len(patched.wires) == 4
        assert not any(c.element_name == "Out" for c in patched.components)
    finally:
        os.unlink(temp)


def test_delete_component_breaking_the_circuit_is_rejected(tmp_path):
    src = tmp_path / "mini.dig"
    src.write_text(_mini_circuit(_SIMPLE_WIRES), encoding="utf-8")
    temp, report = apply_patch(str(src), [
        {"op": "delete_component", "component_index": 0},
    ])
    assert temp is None and not report.ok
    assert "new Layer-1" in report.warning
    assert "dangling_input" in report.new_l1_error_kinds


def test_deletes_apply_last_so_indices_stay_original(tmp_path):
    extra = """
    <visualElement>
      <elementName>Const</elementName>
      <elementAttributes>
        <entry><string>Value</string><long>1</long></entry>
      </elementAttributes>
      <pos x="0" y="200"/>
    </visualElement>
    <visualElement>
      <elementName>Const</elementName>
      <elementAttributes>
        <entry><string>Value</string><long>2</long></entry>
      </elementAttributes>
      <pos x="0" y="240"/>
    </visualElement>"""
    src = tmp_path / "mini.dig"
    src.write_text(_mini_circuit(_SIMPLE_WIRES, extra), encoding="utf-8")
    temp, report = apply_patch(str(src), [
        {"op": "delete_component", "component_index": 4},
        {"op": "change_attribute", "component_index": 5,
         "name": "Value", "value": 7},
    ])
    assert report.ok, report.warning
    try:
        patched = parse_dig_file(temp)
        consts = [c for c in patched.components if c.element_name == "Const"]
        assert len(consts) == 1
        assert consts[0].attributes["Value"] == 7
    finally:
        os.unlink(temp)

def _bubble_fixture(tmp_path, gate: str, rotation: int | None = None,
                    name: str = "bubble.dig") -> tuple[str, int]:
    """Two-pass build: derive every pin coordinate from the parser, so the
    fixture stays valid whatever the geometry tables say."""
    rot = ("<entry><string>rotation</string>"
           f"<rotation rotation=\"{rotation}\"/></entry>") if rotation else ""
    def gate_ve():
        return (f"    <visualElement><elementName>{gate}</elementName>"
                f"<elementAttributes><entry><string>wideShape</string>"
                f"<boolean>true</boolean></entry>{rot}</elementAttributes>"
                f"<pos x=\"240\" y=\"0\"/></visualElement>\n")
    bare = (
        '<?xml version="1.0" encoding="utf-8"?>\n<circuit>\n'
        "  <version>2</version>\n  <attributes/>\n  <visualElements>\n"
        + gate_ve()
        + "  </visualElements>\n  <wires>\n  </wires>\n</circuit>\n")
    probe = tmp_path / ("probe_" + name)
    probe.write_text(bare, encoding="utf-8")
    from dlc.l3.patch import _PinIndex
    pins = {p.pin_name: (p.x, p.y)
            for net in _PinIndex(str(probe)).netlist.nets
            for p in net.pins if p.component_index == 0}
    (ax, ay), (bx, by), (ox, oy) = pins["in0"], pins["in1"], pins["Y"]
    ves = (
        gate_ve()
        + f"    <visualElement><elementName>In</elementName>"
          f"<elementAttributes><entry><string>Label</string><string>a"
          f"</string></entry></elementAttributes>"
          f"<pos x=\"{ax-40}\" y=\"{ay}\"/></visualElement>\n"
        + f"    <visualElement><elementName>In</elementName>"
          f"<elementAttributes><entry><string>Label</string><string>b"
          f"</string></entry></elementAttributes>"
          f"<pos x=\"{bx-40}\" y=\"{by}\"/></visualElement>\n"
        + f"    <visualElement><elementName>Out</elementName>"
          f"<elementAttributes><entry><string>Label</string><string>f"
          f"</string></entry></elementAttributes>"
          f"<pos x=\"{ox+100}\" y=\"{oy}\"/></visualElement>\n"
        + "    <visualElement><elementName>Testcase</elementName>"
          "<elementAttributes><entry><string>Label</string><string>t"
          "</string></entry><entry><string>Testdata</string><testData>"
          "<dataString>a b f\n0 0 0\n1 1 1</dataString></testData></entry>"
          "</elementAttributes><pos x=\"0\" y=\"200\"/></visualElement>\n")
    wires = (
        f"    <wire><p1 x=\"{ax-40}\" y=\"{ay}\"/>"
        f"<p2 x=\"{ax}\" y=\"{ay}\"/></wire>\n"
        f"    <wire><p1 x=\"{bx-40}\" y=\"{by}\"/>"
        f"<p2 x=\"{bx}\" y=\"{by}\"/></wire>\n"
        f"    <wire><p1 x=\"{ox}\" y=\"{oy}\"/>"
        f"<p2 x=\"{ox+100}\" y=\"{oy}\"/></wire>\n")
    xml = ('<?xml version="1.0" encoding="utf-8"?>\n<circuit>\n'
           "  <version>2</version>\n  <attributes/>\n  <visualElements>\n"
           + ves + "  </visualElements>\n  <wires>\n" + wires
           + "  </wires>\n</circuit>\n")
    p = tmp_path / name
    p.write_text(xml, encoding="utf-8")
    return str(p), 0


def _net_connects_gate_to_out(dig_path: str, gate_idx: int) -> bool:
    nl = build_netlist(parse_dig_file(dig_path))
    for net in nl.nets:
        names = {(p.component_index, p.pin_name) for p in net.pins}
        if (gate_idx, "Y") in names and any(
                pn == "in" for ci, pn in names if ci != gate_idx):
            return True
    return False


def test_replace_inverted_gate_with_plain_keeps_output_wired(tmp_path):
    path, gi = _bubble_fixture(tmp_path, "NAnd")
    temp, report = apply_patch(path, [
        {"op": "replace_element", "component_index": gi,
         "new_element": "And"}])
    assert report.ok, report.warning
    assert "stub wire" in " ".join(report.applied)
    try:
        assert _net_connects_gate_to_out(temp, gi)
    finally:
        os.unlink(temp)


def test_replace_plain_gate_with_inverted_keeps_output_wired(tmp_path):
    path, gi = _bubble_fixture(tmp_path, "And", name="plain.dig")
    temp, report = apply_patch(path, [
        {"op": "replace_element", "component_index": gi,
         "new_element": "NAnd"}])
    assert report.ok, report.warning
    try:
        assert _net_connects_gate_to_out(temp, gi)
    finally:
        os.unlink(temp)


def test_replace_within_plain_family_adds_no_stub(tmp_path):
    path, gi = _bubble_fixture(tmp_path, "And", name="plain2.dig")
    n_wires = len(parse_dig_file(path).wires)
    temp, report = apply_patch(path, [
        {"op": "replace_element", "component_index": gi,
         "new_element": "Or"}])
    assert report.ok, report.warning
    try:
        assert len(parse_dig_file(temp).wires) == n_wires
    finally:
        os.unlink(temp)


def test_rotated_inverted_swap_keeps_output_wired(tmp_path):
    path, gi = _bubble_fixture(tmp_path, "NAnd", rotation=2,
                               name="rot.dig")
    temp, report = apply_patch(path, [
        {"op": "replace_element", "component_index": gi,
         "new_element": "And"}])
    assert report.ok, report.warning
    try:
        assert _net_connects_gate_to_out(temp, gi)
    finally:
        os.unlink(temp)


def test_confirmed_fix_after_inverted_swap_end_to_end(tmp_path):
    from dlc.l3.debugger import verify_ops
    path, gi = _bubble_fixture(tmp_path, "NAnd", name="e2e.dig")
    verdict = verify_ops(path, "t", [
        {"op": "replace_element", "component_index": gi,
         "new_element": "And"}],
        cluster_rows=[0, 1], original_failing=[0, 1])
    assert verdict["apply_ok"], verdict["warning"]
    assert verdict["confirmed"] is True
    assert verdict["still_failing"] == []
    assert verdict["regressions"] == []
