"""
L3 circuit-patch vocabulary + applier (fix representation).
"""

from __future__ import annotations

import os
import re as _re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

from dlc.analyzer import check_all_l1_deep
from dlc.parser.dig_parser import parse_dig_file
from dlc.parser.netlist import build_netlist
from dlc.testing.runner import find_digital_jar, per_row_run_auto
from dlc.testing.spec import extract_test_specs

_TEMP_PREFIX = "dlc_row_l3fix_"

KNOWN_OPS = frozenset({
    "change_attribute", "replace_element", "swap_pins", "rewire_pin",
    "add_wire", "delete_wire", "add_component", "delete_component",
})

_NEW_ENTRY_TAG = {
    "Value": "long",
    "Bits": "int", "Inputs": "int", "Selector Bits": "int",
    "AddrBits": "int", "splitterSpreading": "int",
    "inputBits": "int", "outputBits": "int",
    "Label": "string", "NetName": "string",
    "Input Splitting": "string", "Output Splitting": "string",
    "Data": "data", "intFormat": "intFormat",
    "direction": "direction", "barrelShifterMode": "barrelShifterMode",
    "wideShape": "boolean", "isProgramCounter": "boolean",
    "isProgramMemory": "boolean", "bigEndian": "boolean",
    "rotation": "rotation",
}


@dataclass
class PatchReport:
    ok: bool
    warning: str | None = None
    applied: list[str] = field(default_factory=list)
    l1_errors_before: int | None = None
    l1_errors_after: int | None = None
    new_l1_error_kinds: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


@dataclass
class PatchOutcome:
    ok: bool
    warning: str | None = None
    report: PatchReport | None = None
    temp_path: str | None = None
    specs: list[dict] = field(default_factory=list)
    all_passed: bool | None = None

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


def _visual_elements(root) -> list:
    block = root.find("visualElements")
    return [] if block is None else block.findall("visualElement")


def _ve_for_index(root, component_index: int):
    ves = _visual_elements(root)
    if component_index < 0 or component_index >= len(ves):
        raise ValueError(
            f"component_index {component_index} out of range "
            f"(0..{len(ves) - 1})."
        )
    return ves[component_index]


def _normalize_data_words(value) -> str:
    if isinstance(value, (list, tuple)):
        items = [str(v) for v in value]
    else:
        items = _re.split(r"[,\s]+", str(value).strip().strip("[]"))
    out = []
    for it in items:
        s = it.strip().strip("'\"")
        if not s:
            continue
        if _re.fullmatch(r"(?:0[xX])[0-9a-fA-F]+", s):
            s = s[2:].lower()
        out.append(s)
    return ",".join(out)


def _format_value(tag: str, value) -> tuple[str, str | None, dict]:
    if tag == "boolean":
        return tag, ("true" if value else "false"), {}
    if tag == "rotation":
        return tag, None, {"rotation": str(int(value))}
    if tag == "data":
        return tag, _normalize_data_words(value), {}
    return tag, str(value), {}

def _append_entry(attrs_el, name: str, value) -> None:
    if name in _NEW_ENTRY_TAG:
        tag = _NEW_ENTRY_TAG[name]
    elif isinstance(value, bool):
        tag = "boolean"
    elif isinstance(value, int):
        tag = "int"
    else:
        tag = "string"
    entry = etree.SubElement(attrs_el, "entry")
    key_el = etree.SubElement(entry, "string")
    key_el.text = name
    tag, text, xattr = _format_value(tag, value)
    val_el = etree.SubElement(entry, tag)
    val_el.text = text
    for k, v in xattr.items():
        val_el.set(k, v)



def _apply_change_attribute(root, op) -> str:
    ve = _ve_for_index(root, op["component_index"])
    name = op["name"]
    value = op["value"]
    attrs = ve.find("elementAttributes")
    if attrs is None:
        attrs = etree.Element("elementAttributes")
        ve.insert(list(ve).index(ve.find("elementName")) + 1, attrs)

    for entry in attrs.findall("entry"):
        children = list(entry)
        if len(children) >= 2 and children[0].text == name:
            old_tag = children[1].tag
            tag, text, xattr = _format_value(old_tag, value)
            new_val = etree.Element(tag)
            new_val.text = text
            for k, v in xattr.items():
                new_val.set(k, v)
            entry.replace(children[1], new_val)
            return f"change_attribute[{op['component_index']}].{name} -> {value!r}"

    _append_entry(attrs, name, value)
    return f"change_attribute[{op['component_index']}].{name} -> {value!r} (new entry)"

_OUTPUT_BUBBLE_GATES = {"NAnd", "NOr", "XNOr"}


def _element_rotation(ve) -> int:
    attrs = ve.find("elementAttributes")
    if attrs is not None:
        for entry in attrs.findall("entry"):
            children = list(entry)
            if len(children) >= 2 and children[0].text == "rotation":
                try:
                    return int(children[1].get("rotation") or 0)
                except (TypeError, ValueError):
                    return 0
    return 0


def _facing_vector(rotation: int, dist: int) -> tuple[int, int]:
    return [(dist, 0), (0, -dist), (-dist, 0), (0, dist)][rotation % 4]


def _apply_replace_element(root, op, pins: "_PinIndex | None" = None) -> str:
    ve = _ve_for_index(root, op["component_index"])
    name_el = ve.find("elementName")
    old = name_el.text
    new = str(op["new_element"])
    name_el.text = new
    note = ""
    delta = ((20 if new in _OUTPUT_BUBBLE_GATES else 0)
             - (20 if old in _OUTPUT_BUBBLE_GATES else 0))
    if pins is not None and delta:
        try:
            old_out = pins.pin_coord(op["component_index"], "Y")
            dx, dy = _facing_vector(_element_rotation(ve), delta)
            new_out = (old_out[0] + dx, old_out[1] + dy)
            wires = _wires_block(root)
            wire = etree.SubElement(wires, "wire")
            for tag, (x, y) in (("p1", new_out), ("p2", old_out)):
                p = etree.SubElement(wire, tag)
                p.set("x", str(x))
                p.set("y", str(y))
            note = f" (+stub wire {new_out}-{old_out} for output bubble)"
        except (ValueError, KeyError, AttributeError):
            note = " (output-bubble stub skipped: pin not found)"
    return (f"replace_element[{op['component_index']}]: {old} -> "
            f"{op['new_element']}{note}")


def _wires_block(root):
    wires = root.find("wires")
    if wires is None:
        raise ValueError("Circuit has no <wires> block.")
    return wires


def _wire_endpoints(wire) -> tuple[tuple[int, int], tuple[int, int]]:
    p1, p2 = wire.find("p1"), wire.find("p2")
    return ((int(p1.get("x")), int(p1.get("y"))),
            (int(p2.get("x")), int(p2.get("y"))))


def _apply_add_wire(root, op) -> str:
    wires = _wires_block(root)
    (x1, y1), (x2, y2) = tuple(op["p1"]), tuple(op["p2"])
    wire = etree.SubElement(wires, "wire")
    etree.SubElement(wire, "p1", x=str(int(x1)), y=str(int(y1)))
    etree.SubElement(wire, "p2", x=str(int(x2)), y=str(int(y2)))
    return f"add_wire ({x1},{y1}) -> ({x2},{y2})"


def _apply_delete_wire(root, op) -> str:
    wires = _wires_block(root)
    want = {tuple(op["p1"]), tuple(op["p2"])}
    removed = 0
    for wire in list(wires.findall("wire")):
        a, b = _wire_endpoints(wire)
        if {a, b} == want:
            wires.remove(wire)
            removed += 1
    if removed == 0:
        raise ValueError(
            f"delete_wire: no wire between {op['p1']} and {op['p2']}."
        )
    return f"delete_wire ({op['p1']}) -> ({op['p2']}) x{removed}"


class _PinIndex:
    """Claimed pin coordinates + wire-endpoint degrees for one circuit."""

    def __init__(self, dig_path: str):
        self.circuit = parse_dig_file(dig_path)
        self.netlist = build_netlist(self.circuit)
        self.degree: dict[tuple[int, int], int] = {}
        for w in self.circuit.wires:
            for ep in (w.p1.as_tuple(), w.p2.as_tuple()):
                self.degree[ep] = self.degree.get(ep, 0) + 1

    def pin_coord(self, component_index: int, pin_name: str) -> tuple[int, int]:
        for net in self.netlist.nets:
            for p in net.pins:
                if p.component_index == component_index and p.pin_name == pin_name:
                    return (p.x, p.y)
        comp = self.circuit.components[component_index]
        raise ValueError(
            f"Pin {pin_name!r} not found on component "
            f"[{component_index}] {comp.element_name}."
        )

    def require_simple(self, coord: tuple[int, int], what: str) -> None:
        if self.degree.get(coord, 0) > 1:
            raise ValueError(
                f"{what}: pin coordinate {coord} is a shared junction "
                f"({self.degree[coord]} wire endpoints); use "
                f"delete_wire/add_wire explicitly instead."
            )

    def net_of_pin(self, component_index: int, pin_name: str):
        for net in self.netlist.nets:
            for p in net.pins:
                if p.component_index == component_index and p.pin_name == pin_name:
                    return net
        return None

    def stub_tunnel(self, coord: tuple[int, int]):
        """(tunnel index, tunnel coord) when exactly one wire leaves `coord`
        and its far end is a Tunnel element nothing else touches."""
        segs = [w for w in self.circuit.wires
                if coord in (w.p1.as_tuple(), w.p2.as_tuple())]
        if len(segs) != 1:
            return None
        w = segs[0]
        far = w.p2.as_tuple() if w.p1.as_tuple() == coord else w.p1.as_tuple()
        if self.degree.get(far, 0) != 1:
            return None
        for i, comp in enumerate(self.circuit.components):
            if (comp.element_name == "Tunnel"
                    and (comp.position.x, comp.position.y) == far):
                return i, far
        return None

    def attach_coord(self, component_index: int, pin_name: str) -> tuple[int, int]:
        target_net = None
        pin_coord = None
        for net in self.netlist.nets:
            for p in net.pins:
                if p.component_index == component_index and p.pin_name == pin_name:
                    target_net, pin_coord = net, (p.x, p.y)
                    break
            if target_net is not None:
                break
        if target_net is None:
            comp = self.circuit.components[component_index]
            raise ValueError(
                f"Pin {pin_name!r} not found on component "
                f"[{component_index}] {comp.element_name}."
            )
        junctions = sorted(
            c for c in target_net.coords if self.degree.get(c, 0) >= 2
        )
        return junctions[0] if junctions else pin_coord


def _apply_swap_pins(root, op, pins: _PinIndex) -> str:
    idx = op["component_index"]
    ca = pins.pin_coord(idx, op["pin_a"])
    cb = pins.pin_coord(idx, op["pin_b"])
    pins.require_simple(ca, "swap_pins")
    pins.require_simple(cb, "swap_pins")
    if pins.degree.get(ca, 0) == 0 and pins.degree.get(cb, 0) == 0:
        raise ValueError("swap_pins: neither pin has a wire to swap.")
    swapped = 0
    for wire in _wires_block(root).findall("wire"):
        for pt in (wire.find("p1"), wire.find("p2")):
            coord = (int(pt.get("x")), int(pt.get("y")))
            if coord == ca:
                pt.set("x", str(cb[0])); pt.set("y", str(cb[1]))
                swapped += 1
            elif coord == cb:
                pt.set("x", str(ca[0])); pt.set("y", str(ca[1]))
                swapped += 1
    return (f"swap_pins[{idx}] {op['pin_a']}@{ca} <-> {op['pin_b']}@{cb} "
            f"({swapped} endpoint(s))")


def _apply_rewire_pin(root, op, pins: _PinIndex) -> str:
    idx = op["component_index"]
    src = pins.pin_coord(idx, op["pin"])
    to = op["to"]
    dst = pins.attach_coord(to["component_index"], to["pin"])
    pins.require_simple(src, "rewire_pin")
    # a pin wired through its own tunnel stub (the tunnel-per-pin habit)
    # is moved the way a student would: the stub keeps its wire and takes
    # the destination net's tunnel name, so no Tunnel is left dangling
    stub = pins.stub_tunnel(src)
    dst_net = pins.net_of_pin(to["component_index"], to["pin"])
    names = sorted(dst_net.tunnel_names) if dst_net is not None else []
    if stub is not None and names:
        t_idx, _far = stub
        old = pins.circuit.components[t_idx].attributes.get("NetName")
        _apply_change_attribute(root, {"component_index": t_idx,
                                       "name": "NetName", "value": names[0]})
        return (f"rewire_pin[{idx}].{op['pin']}@{src} -> "
                f"[{to['component_index']}].{to['pin']} "
                f"(tunnel [{t_idx}] renamed {old!r} -> {names[0]!r})")
    wires = _wires_block(root)
    removed = 0
    for wire in list(wires.findall("wire")):
        a, b = _wire_endpoints(wire)
        if src in (a, b):
            wires.remove(wire)
            removed += 1
    wire = etree.SubElement(wires, "wire")
    etree.SubElement(wire, "p1", x=str(src[0]), y=str(src[1]))
    etree.SubElement(wire, "p2", x=str(dst[0]), y=str(dst[1]))
    return (f"rewire_pin[{idx}].{op['pin']}@{src} -> "
            f"[{to['component_index']}].{to['pin']}@{dst} "
            f"(detached {removed} segment(s))")

def _apply_add_component(root, op) -> str:
    block = root.find("visualElements")
    if block is None:
        raise ValueError("Circuit has no <visualElements> block.")
    element_name = str(op["element_name"])
    x, y = tuple(op["position"])
    ve = etree.SubElement(block, "visualElement")
    name_el = etree.SubElement(ve, "elementName")
    name_el.text = element_name
    attrs_el = etree.SubElement(ve, "elementAttributes")
    for name, value in (op.get("attributes") or {}).items():
        _append_entry(attrs_el, name, value)
    etree.SubElement(ve, "pos", x=str(int(x)), y=str(int(y)))
    return f"add_component {element_name} @ ({x},{y})"


def _apply_delete_component(root, op, pins: _PinIndex) -> str:
    idx = op["component_index"]
    ve = _ve_for_index(root, idx)
    pin_coords = {
        (p.x, p.y)
        for net in pins.netlist.nets
        for p in net.pins
        if p.component_index == idx
    }
    removed_wires = 0
    wires = root.find("wires")
    if wires is not None:
        for wire in list(wires.findall("wire")):
            a, b = _wire_endpoints(wire)
            if ((a in pin_coords and pins.degree.get(a, 0) == 1)
                    or (b in pin_coords and pins.degree.get(b, 0) == 1)):
                wires.remove(wire)
                removed_wires += 1
    name = pins.circuit.components[idx].element_name
    ve.getparent().remove(ve)
    return (f"delete_component[{idx}] {name} "
            f"(removed {removed_wires} dead-end wire segment(s))")


# Public API

def apply_patch(dig_path: str, ops: list[dict]) -> tuple[str | None, PatchReport]:
    report = PatchReport(ok=False)
    if not ops:
        report.warning = "No patch ops given."
        return None, report
    for op in ops:
        if op.get("op") not in KNOWN_OPS:
            report.warning = f"Unknown patch op: {op.get('op')!r}."
            return None, report

    src_path = Path(dig_path)
    try:
        original_errors = len(check_all_l1_deep(parse_dig_file(str(src_path))).errors())
        original_kinds = {
            i.kind for i in check_all_l1_deep(parse_dig_file(str(src_path))).errors()
        }
    except Exception as exc:
        report.warning = f"Could not parse original circuit: {exc}"
        return None, report
    report.l1_errors_before = original_errors

    parser = etree.XMLParser(remove_blank_text=False)
    tree = etree.parse(str(src_path), parser)
    root = tree.getroot()
    pins = _PinIndex(str(src_path))

    deletes = [op for op in ops if op["op"] == "delete_component"]
    deletes.sort(key=lambda op: -op.get("component_index", 0))
    ordered = [op for op in ops if op["op"] != "delete_component"] + deletes


    try:
        for op in ordered:
            kind = op["op"]
            if kind == "change_attribute":
                report.applied.append(_apply_change_attribute(root, op))
            elif kind == "replace_element":
                report.applied.append(_apply_replace_element(root, op, pins))
            elif kind == "swap_pins":
                report.applied.append(_apply_swap_pins(root, op, pins))
            elif kind == "rewire_pin":
                report.applied.append(_apply_rewire_pin(root, op, pins))
            elif kind == "add_wire":
                report.applied.append(_apply_add_wire(root, op))
            elif kind == "delete_wire":
                report.applied.append(_apply_delete_wire(root, op))
            elif kind == "add_component":
                report.applied.append(_apply_add_component(root, op))
            elif kind == "delete_component":
                report.applied.append(_apply_delete_component(root, op, pins))
    except (ValueError, KeyError, TypeError) as exc:
        report.warning = f"Patch failed: {exc}"
        return None, report

    fd, temp_path = tempfile.mkstemp(
        suffix=".dig", prefix=_TEMP_PREFIX, dir=str(src_path.parent),
    )
    os.close(fd)
    tree.write(temp_path, xml_declaration=True, encoding="utf-8")

    try:
        patched = parse_dig_file(temp_path)
        issues = check_all_l1_deep(patched)
        n_after = len(issues.errors())
        report.l1_errors_after = n_after
        report.new_l1_error_kinds = sorted(
            {i.kind for i in issues.errors()} - original_kinds
        )
        if n_after > original_errors:
            report.warning = (
                f"Patch introduces {n_after - original_errors} new Layer-1 "
                f"error(s) ({', '.join(report.new_l1_error_kinds) or 'same kinds'}); "
                f"rejected."
            )
            os.unlink(temp_path)
            return None, report
    except Exception as exc:
        report.warning = f"Patched circuit failed to reparse: {exc}"
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        return None, report

    report.ok = True
    return temp_path, report


def rerun_with_patch(
    dig_path: str,
    ops: list[dict],
    *,
    spec_name: str | None = None,
    jar_path: str | None = None,
    timeout: float = 60.0,
    keep_temp: bool = False,
) -> PatchOutcome:
    jar = jar_path or find_digital_jar()
    if jar is None:
        return PatchOutcome(ok=False, warning=(
            "Digital.jar not configured. Open the jar picker from the "
            "toolbar to select it."
        ))

    temp_path, report = apply_patch(dig_path, ops)
    if temp_path is None:
        return PatchOutcome(ok=False, warning=report.warning, report=report)

    try:
        circuit = parse_dig_file(temp_path)
        specs = [s for s in extract_test_specs(circuit) if s.rows]
        if spec_name is not None:
            specs = [s for s in specs if s.name == spec_name]
            if not specs:
                return PatchOutcome(
                    ok=False, report=report,
                    warning=f"No testcase named {spec_name!r} in this circuit.",
                )

        spec_payloads: list[dict] = []
        overall_ok = True
        for spec in specs:
            rows_by_idx = {r.line_index: r for r in spec.rows}
            results = per_row_run_auto(spec, temp_path, jar_path=jar,
                                       timeout=timeout)
            rows = []
            spec_ok = True
            for rr in results:
                src_row = rows_by_idx.get(rr.row_index)
                rows.append({
                    "index": rr.row_index,
                    "raw": src_row.raw if src_row else "",
                    "status": rr.status,
                    "error_message": rr.error_message,
                    "mismatches": rr.mismatches,
                })
                if rr.status in ("failed", "error"):
                    spec_ok = False
            spec_payloads.append({
                "name": spec.name, "headers": list(spec.headers),
                "rows": rows, "all_passed": spec_ok,
            })
            overall_ok &= spec_ok

        return PatchOutcome(
            ok=True,
            report=report,
            temp_path=temp_path if keep_temp else None,
            specs=spec_payloads,
            all_passed=overall_ok,
        )
    finally:
        if not keep_temp:
            try:
                os.unlink(temp_path)
            except OSError:
                pass