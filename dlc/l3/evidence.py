from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import networkx as nx

from dlc.analyzer.sequential import _CLOCKED_ELEMENTS as _STATE_ELEMENTS
from dlc.facts.extractor import extract_facts
from dlc.l3.localizer import (_W_LINE, SuspectReport, _net_of_pin,
                              _output_component_index, _witness_from_root,
                              localize, merge_reports)
from dlc.l3.manifest import decode_program_word, find_manifest
from dlc.llm.explain import _compact_facts
from dlc.parser.dig_parser import parse_dig_file
from dlc.parser.graph import build_signal_graph
from dlc.parser.netlist import build_netlist
from dlc.sim import models as formula_models
from dlc.sim.simulator import (SimResult, _gate_bits, _rom_words,
                               simulate_rows, simulate_sequential)
from dlc.testing.spec import TestSpec, extract_test_specs, match_variables_to_io

CONTRACT = "l3.debug.v1.1"

GROSS_MAX_FAILING = 20

RATE_GATE_MIN_COMPONENTS = 30

SCATTERED_ROW_MAX_SHARE = 0.25

_MAX_CLUSTERS = 4
_MAX_REPRESENTATIVES = 2
_TOP_SUSPECTS = 5
_MIN_OVERLAP = 0.5

_SELECT_NAME_HINTS = frozenset({
    "op", "opcode", "sel", "select", "mode", "ctrl", "control",
    "aluop", "func", "funct", "operation",
})


@dataclass
class RowEvidence:
    """Everything the pipeline knows about ONE failing row."""

    row_index: int
    raw: str
    mismatches: list[dict] = field(default_factory=list)
    outputs: list[dict] = field(default_factory=list)
    net_values: dict[str, dict] = field(default_factory=dict)
    unresolved_nets: list[int] = field(default_factory=list)
    selects: list[list[str]] = field(default_factory=list)
    category: str | None = None
    program_word: str | None = None
    suspect_report: SuspectReport = field(default_factory=SuspectReport)
    state_trace: dict | None = None


@dataclass
class Cluster:
    signature: dict = field(default_factory=dict)
    rows: list[RowEvidence] = field(default_factory=list)
    merged: SuspectReport = field(default_factory=SuspectReport)
    folded_rows: int = 0


@dataclass
class EvidenceResult:
    mode: str = "clear"
    gross_flags: list[dict] = field(default_factory=list)
    failing_count: int = 0
    spec_name: str | None = None
    headers: list[str] = field(default_factory=list)
    clusters: list[Cluster] = field(default_factory=list)
    payloads: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    consequential_rows: list[int] = field(default_factory=list)
    divergence: dict | None = None

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "gross_flags": self.gross_flags,
            "failing_count": self.failing_count,
            "spec_name": self.spec_name,
            "headers": self.headers,
            "clusters": [
                {
                    "signature": c.signature,
                    "rows": [r.row_index for r in c.rows],
                    "folded_rows": c.folded_rows,
                }
                for c in self.clusters
            ],
            "payloads": self.payloads,
            "notes": self.notes,
            "consequential_rows": self.consequential_rows,
        }

def _mask(bits: int | None) -> int:
    return (1 << bits) - 1 if bits and bits > 0 else 0


def _output_ok(found, exp_val, width) -> bool | None:
    if found is None:
        return None
    if width:
        return (found & _mask(width)) == (exp_val & _mask(width))
    return found == exp_val


def _fmt_value(v, width, signed_hint) -> str | None:
    if v is None:
        return None
    if not width or width <= 1:
        return str(v)
    u = v & _mask(width)
    if signed_hint and (u >> (width - 1)) & 1:
        return str(u - (1 << width))
    return f"0x{u:X}"


def _outputs_report(spec: TestSpec, bindings, row, sim: SimResult):
    col = {h: i for i, h in enumerate(spec.headers)}
    outputs: list[dict] = []
    mismatches: list[dict] = []
    for h in spec.headers:
        b = bindings.get(h)
        if b is None or b.role != "output":
            continue
        tok = row.values[col[h]]
        if tok.kind != "int" or tok.value is None:
            continue
        found = sim.output_values.get(h)
        signed = tok.value < 0
        ok = _output_ok(found, tok.value, b.bit_width)
        entry = {
            "label": h,
            "expected": _fmt_value(tok.value, b.bit_width, signed),
            "found": _fmt_value(found, b.bit_width, signed),
            "ok": ok,
        }
        outputs.append(entry)
        if ok is not True:
            mismatches.append({
                "column": h,
                "expected": entry["expected"],
                "found": entry["found"],
            })
    return outputs, mismatches


def _expected_ints(spec: TestSpec, bindings, row) -> dict[str, tuple[int, int | None]]:
    col = {h: i for i, h in enumerate(spec.headers)}
    out: dict[str, tuple[int, int | None]] = {}
    for h in spec.headers:
        b = bindings.get(h)
        if b is None or b.role != "output" or col[h] >= len(row.values):
            continue
        tok = row.values[col[h]]
        if tok.kind == "int" and tok.value is not None:
            out[h] = (tok.value, b.bit_width)
    return out


def net_names_map(circuit, netlist) -> dict[int, str]:
    names: dict[int, str] = {}
    for net in netlist.nets:
        if net.tunnel_names:
            names[net.net_id] = "/".join(sorted(net.tunnel_names))
            continue
        for p in net.pins:
            comp = circuit.components[p.component_index]
            if comp.element_name in ("In", "Out", "Clock") and comp.label:
                names[net.net_id] = comp.label
                break
    return names


_STUCK_MIN_ROWS = 6
_STUCK_SKIP = frozenset({
    "In", "Out", "Const", "Ground", "VDD", "Clock", "Tunnel", "Testcase",
    "Rectangle", "Text", "Probe", "PullUp", "PullDown",
})


def stuck_components(circuit, netlist, sims: dict[int, SimResult]) -> dict[int, str]:
    if len(sims) < _STUCK_MIN_ROWS:
        return {}
    ins: dict[int, set[int]] = {}
    outs: dict[int, set[int]] = {}
    for net in netlist.nets:
        for p in net.pins:
            if p.direction == "in":
                ins.setdefault(p.component_index, set()).add(net.net_id)
            elif p.direction == "out":
                outs.setdefault(p.component_index, set()).add(net.net_id)
    rows = list(sims.values())
    result: dict[int, str] = {}
    for idx, comp in enumerate(circuit.components):
        if (comp.element_name in _STUCK_SKIP or comp.element_name in _DATA_ELEMENTS
                or comp.element_name.endswith(".dig")
                or idx not in outs or idx not in ins):
            continue
        frozen: list[int] = []
        for nid in outs[idx]:
            vals = [s.net_values.get(nid) for s in rows]
            if any(v is None for v in vals) or len(set(vals)) != 1:
                frozen = []
                break
            frozen.append(vals[0])
        if not frozen:
            continue
        varies = any(
            len({s.net_values.get(nid) for s in rows} - {None}) > 1
            for nid in ins[idx])
        if not varies:
            continue
        shown = ", ".join(f"0x{v:X}" for v in frozen[:2])
        result[idx] = (
            f"its output never changes over the whole testcase (always "
            f"{shown}) although its inputs do — dead or wrong-kind logic")
    return result


_LINE_MAX_INPUTS = 64
_LINE_MAX_ADDRESSES = 3
_LINE_SELECTORS = frozenset({"PriorityEncoder", "Splitter"})
_LINE_SKIP_DRIVERS = frozenset({"Ground", "VDD", "Const", "In", "Clock"})


def _rom_addr_selector(circuit, netlist, rom_idx: int):
    """(address net id, selector index, [(pin, net id, driver index)]) when
    one component alone drives the ROM's address; else None."""
    a_net = _net_of_pin(netlist, rom_idx, "A", "in")
    if a_net is None:
        return None
    drivers = [p.component_index for p in a_net.pins
               if p.direction == "out" and p.component_index != rom_idx]
    if len(drivers) != 1:
        return None
    sel_idx = drivers[0]
    lines: list[tuple[str, int, int | None]] = []
    for net in netlist.nets:
        mine = [p for p in net.pins
                if p.component_index == sel_idx and p.direction == "in"]
        if not mine:
            continue
        drv = next((q.component_index for q in net.pins
                    if q.direction == "out" and q.component_index != sel_idx),
                   None)
        for p in mine:
            lines.append((p.pin_name, net.net_id, drv))
    if not lines or len(lines) > _LINE_MAX_INPUTS:
        return None
    return a_net.net_id, sel_idx, lines


def selector_line_witness(circuit, netlist, graph, spec, bindings,
                          all_sims: dict, failing, *,
                          net_names=None) -> dict[int, tuple[dict, list[str]]]:
    """Stored words are trusted, so when a ROM's address comes from a
    selector fed by 1-bit lines (a priority encoder behind instruction
    detectors), the passing rows teach which word each output column
    reads; for a failing row the address holding its expected word is
    then known, and the line that asserts although the word lives
    elsewhere — or stays silent on the row that needs it — names the
    broken gate. {failing row: ({driver index: (weight, reason)}, notes)}."""
    failing_set = set(failing)
    rows_by_index = {r.line_index: r for r in spec.rows if not r.is_malformed}
    out: dict[int, tuple[dict, list[str]]] = {}
    sample = next(iter(all_sims.values()), None)
    if sample is None:
        return out
    for rom_idx, comp in enumerate(circuit.components):
        if comp.element_name != "ROM":
            continue
        found = _rom_addr_selector(circuit, netlist, rom_idx)
        if found is None:
            continue
        addr_nid, sel_idx, lines = found
        sel_kind = circuit.components[sel_idx].element_name
        if sel_kind not in _LINE_SELECTORS:
            continue
        if any(sample.net_bits.get(nid, 1) != 1 for _p, nid, _d in lines):
            continue
        words = _rom_words(comp)
        if not words:
            continue
        bits = _gate_bits(comp)
        downstream = set(nx.descendants(graph, rom_idx)) if rom_idx in graph else set()
        outs = {h: _output_component_index(circuit, h) for h in spec.headers
                if bindings.get(h) is not None and bindings[h].role == "output"}
        fed = {h for h, i in outs.items() if i is not None and i in downstream}
        if not fed:
            continue

        cand: dict[str, set[int]] = {}
        widths: dict[str, int] = {}
        lines_at: dict[int, set[str]] = {}
        idle: dict | None = None
        idle_ok = True
        learned = 0
        for ridx, sim in all_sims.items():
            row = rows_by_index.get(ridx)
            if ridx in failing_set or row is None:
                continue
            _outs, mism = _outputs_report(spec, bindings, row, sim)
            exp = {h: v for h, v in _expected_ints(spec, bindings, row).items()
                   if h in fed}
            if mism or not exp:
                continue
            asserted = {p for p, nid, _d in lines if sim.net_values.get(nid) == 1}
            if not asserted:
                vec = {h: v[0] for h, v in exp.items()}
                if idle is None:
                    idle = vec
                elif idle != vec:
                    idle_ok = False
                continue
            addr = sim.net_values.get(addr_nid)
            if addr is None:
                continue
            word = words[addr] if 0 <= addr < len(words) else 0
            learned += 1
            lines_at[addr] = (lines_at[addr] & asserted if addr in lines_at
                              else set(asserted))
            for h, (val, width) in exp.items():
                w = width or 1
                m = (1 << w) - 1
                ok = {b for b in range(0, bits - w + 1)
                      if ((word >> b) & m) == (val & m)}
                cand[h] = cand[h] & ok if h in cand else ok
                widths[h] = w
        mapped = {h: bs for h, bs in cand.items() if bs}
        if not learned or not mapped:
            continue
        if not idle_ok:
            idle = None
        idle_seen = idle is not None
        if idle is None:
            # no passing idle row to learn from: when the selector's "any
            # line set" flag is wired in (chip select or output mask), no
            # asserted line means every output reads 0
            f_net = _net_of_pin(netlist, sel_idx, "f", "out")
            if f_net is not None and any(p.component_index != sel_idx
                                         for p in f_net.pins):
                idle = {h: 0 for h in mapped}

        pin_names = {p for p, _n, _d in lines}
        drv_of = {p: d for p, _n, d in lines}
        is_penc = sel_kind == "PriorityEncoder"
        # a Splitter joining single bits: input i is address bit i
        bit_join = (sel_kind == "Splitter" and all(
            t.strip() == "1" for t in str(circuit.components[sel_idx]
                                          .attributes.get("Input Splitting", ""))
            .split(",")))

        def lines_for(a: int) -> set[str]:
            if is_penc and f"in_{a}" in pin_names:
                return {f"in_{a}"}
            if bit_join:
                return {f"in{i}" for i in range(a.bit_length())
                        if (a >> i) & 1 and f"in{i}" in pin_names}
            return set(lines_at.get(a, set()))

        def drv_name(p: str) -> str:
            d = drv_of.get(p)
            if d is None:
                return "nothing"
            return f"{circuit.components[d].element_name}[{d}]"

        def boostable(p: str) -> bool:
            d = drv_of.get(p)
            return (d is not None and circuit.components[d].element_name
                    not in _LINE_SKIP_DRIVERS)

        sel_name = f"{circuit.components[sel_idx].element_name}[{sel_idx}]"
        rom_name = f"ROM[{rom_idx}]"
        for ridx in failing:
            sim = all_sims.get(ridx)
            row = rows_by_index.get(ridx)
            if sim is None or row is None:
                continue
            exp = _expected_ints(spec, bindings, row)
            vec = {h: exp[h][0] for h in mapped if h in exp}
            if not vec:
                continue
            asserted = {p for p, nid, _d in lines if sim.net_values.get(nid) == 1}
            hits: list[int] = []
            for a, word in enumerate(words):
                if all(any(((word >> b) & ((1 << widths[h]) - 1))
                           == (v & ((1 << widths[h]) - 1)) for b in mapped[h])
                       for h, v in vec.items()):
                    hits.append(a)
            expects_idle = (idle is not None
                            and all(idle.get(h) == v for h, v in vec.items()))
            if not hits and not expects_idle:
                continue
            legit: set[str] = set()
            for a in hits:
                legit |= lines_for(a)
            boosts, notes = out.setdefault(ridx, ({}, []))
            addr_r = sim.net_values.get(addr_nid)
            if hits:
                where = (f"address {hits[0]}" if len(hits) == 1
                         else "address " + " or ".join(str(a) for a in hits[:_LINE_MAX_ADDRESSES]))
                by = sorted(legit)
                word_where = (f"the word at {where}"
                              + (f", which {sel_name} selects when "
                                 f"{', '.join(by)} is 1" if by else ""))
            else:
                where = "no address (the row expects the idle output)"
                word_where = ("the idle output seen on passing rows where "
                              "no input line asserts" if idle_seen else
                              "the all-zero idle output the circuit produces "
                              "when no selector input line is 1")
            for p in sorted(asserted - legit):
                if not boostable(p):
                    continue
                d = drv_of[p]
                tag = (f"LINE WITNESS: {sel_name} input {p} asserts on row "
                       f"{ridx} although the expected word lives at {where} "
                       f"(see notes)")
                if d not in boosts or _W_LINE > boosts[d][0]:
                    boosts[d] = (_W_LINE, tag)
                notes.append(
                    f"LINE WITNESS row {ridx}: {sel_name} input {p} (driven by "
                    f"{drv_name(p)}) is 1, so {rom_name} reads address {addr_r}; "
                    f"the expected outputs are {word_where} — the gate driving "
                    f"{p} must be silent on this row: check its kind against "
                    f"its input values.")
            if hits and len(hits) <= _LINE_MAX_ADDRESSES and not (asserted & legit):
                for a in hits:
                    for p in sorted(lines_for(a)):
                        if not boostable(p):
                            continue
                        d = drv_of[p]
                        tag = (f"LINE WITNESS: {sel_name} input {p} stays silent "
                               f"on row {ridx} although the expected word lives "
                               f"at address {a} (see notes)")
                        if d not in boosts or _W_LINE > boosts[d][0]:
                            boosts[d] = (_W_LINE, tag)
                        notes.append(
                            f"LINE WITNESS row {ridx}: the expected outputs are "
                            f"the word at address {a}, which {sel_name} selects "
                            f"when {p} (driven by {drv_name(p)}) is 1 — {p} is 0 "
                            f"on this row, so the gate driving it must assert "
                            f"here: check its kind and inputs.")
            if not boosts and not notes:
                out.pop(ridx, None)
    return out


def _pc_column(manifest, bindings) -> str | None:
    pc = (((manifest or {}).get("program_decode") or {}).get("observe")
          or {}).get("pc_port")
    b = bindings.get(pc) if pc else None
    return pc if b is not None and b.role == "output" else None


_DIVERGENCE_MIN_TAIL = 3
_DIVERGENCE_MIN_SHARE = 0.9


def pc_divergence(failing: list[int], cells_by_row: dict[int, list[dict]],
                  pc_col: str) -> tuple[int | None, list[int]]:
    order = sorted(failing)
    first = next((i for i in order
                  if any(c.get("column") == pc_col
                         for c in cells_by_row.get(i) or [])), None)
    if first is None:
        return None, []
    tail = [i for i in order if i > first]
    if len(tail) < _DIVERGENCE_MIN_TAIL:
        return first, []
    wrong = sum(1 for i in tail
                if any(c.get("column") == pc_col
                       for c in cells_by_row.get(i) or []))
    if wrong / len(tail) < _DIVERGENCE_MIN_SHARE:
        return first, []
    return first, tail


_REGFILE_WRITE_PINS = ("WriteReg", "WriteData", "RegWrite")


def _instance_pin_nets(netlist, inst: int) -> dict[str, int]:
    out: dict[str, int] = {}
    for net in netlist.nets:
        for p in net.pins:
            if p.component_index == inst:
                out[p.pin_name] = net.net_id
    return out


def register_read_trace(circuit, netlist, graph, all_sims: dict, row_order: list[int],
                        row_index: int, column: str, expected: int,
                        width: int | None, net_names: dict | None):
    out_idx = next((i for i, c in enumerate(circuit.components)
                    if c.is_output() and (c.label or f"out_{i}") == column), None)
    if out_idx is None:
        return None
    out_net = _net_of_pin(netlist, out_idx, "in", "in")
    if out_net is None:
        return None
    inst = pin = None
    for p in out_net.pins:
        if (p.direction == "out"
                and circuit.components[p.component_index].element_name.endswith(".dig")):
            inst, pin = p.component_index, p.pin_name
    if inst is None or not pin.startswith("ReadData"):
        return None
    pins = _instance_pin_nets(netlist, inst)
    sel_pin = pin.replace("ReadData", "ReadReg")
    if sel_pin not in pins or any(k not in pins for k in _REGFILE_WRITE_PINS):
        return None
    sim = all_sims.get(row_index)
    if sim is None:
        return None
    reg = sim.net_values.get(pins[sel_pin])
    if not reg:
        return None
    if row_index not in row_order:
        return None
    pos = row_order.index(row_index)
    for w in reversed(row_order[:pos]):
        s = all_sims.get(w)
        if s is None:
            continue
        if (s.net_values.get(pins["RegWrite"]) == 1
                and s.net_values.get(pins["WriteReg"]) == reg):
            found = sim.net_values.get(out_net.net_id)
            mask = (1 << width) - 1 if width else None
            trace = {
                "failing_row": row_index, "column": column, "register": reg,
                "written_at_row": w,
                "expected": f"0x{(expected & mask) if mask else expected:X}",
                "read_back": None if found is None else f"0x{found:X}",
                "write_row_net_values": {
                    str(n): {"bits": s.net_bits.get(n, 1), "hex": format(v, "X")}
                    for n, v in s.net_values.items()},
            }
            tag = f" at row {w}, when register {reg} was written"
            boosts, wnotes = _witness_from_root(
                circuit, netlist, graph, s, inst, pins["WriteData"],
                expected, width, net_names=net_names, row_tag=tag)
            note = (f"STATE TRACE: {column} on row {row_index} reads register "
                    f"{reg}, which row {w} wrote as {trace['read_back']} while "
                    f"{trace['expected']} was expected — judge row {w}.")
            return trace, boosts, [note] + wnotes
    return None


def select_columns(circuit, netlist, spec: TestSpec, bindings=None) -> list[str]:
    if bindings is None:
        bindings = match_variables_to_io(spec.headers, circuit)
    sel_fed: set[int] = set()
    for net in netlist.nets:
        if any(p.pin_name == "sel" and p.direction == "in" for p in net.pins):
            for p in net.pins:
                if p.direction == "out":
                    sel_fed.add(p.component_index)
    out: list[str] = []
    for h in spec.headers:
        b = bindings.get(h)
        if b is None or b.role != "input":
            continue
        if b.component_index in sel_fed or h.lower() in _SELECT_NAME_HINTS:
            out.append(h)
    return out


def _program_rom_out_net(circuit, netlist) -> int | None:
    roms = [
        i for i, c in enumerate(circuit.components)
        if c.element_name == "ROM"
        and str(c.attributes.get("isProgramMemory", "")).lower() == "true"
    ]
    if len(roms) != 1:
        return None
    idx = roms[0]
    for net in netlist.nets:
        if any(p.component_index == idx and p.direction == "out"
               for p in net.pins):
            return net.net_id
    return None


def row_category(circuit, netlist, sim: SimResult, manifest) -> dict | None:
    if not manifest:
        return None
    nid = _program_rom_out_net(circuit, netlist)
    if nid is None:
        return None
    word = sim.net_values.get(nid)
    if word is None:
        return None
    d = decode_program_word(manifest, word)
    if d is None:
        return None
    return {"word": f"{word:x}", "category": d.get("category"),
            "fields": d.get("fields")}

def _holds_state(circuit) -> bool:
    for comp in circuit.components:
        if comp.element_name in _STATE_ELEMENTS:
            return True
    for sub in circuit.subcircuits:
        if sub.child_circuit is not None and _holds_state(sub.child_circuit):
            return True
    return False


def _cell_int(raw) -> int | None:
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, int):
        return raw
    s = str(raw).strip()
    try:
        return int(s, 0)
    except (TypeError, ValueError):
        return None


def _frozen_trunk(spec: TestSpec, bindings,
                  row_mismatch_cells: dict[int, list[dict]] | None) -> bool:
    if not row_mismatch_cells or len(row_mismatch_cells) < 2:
        return False
    hdr_idx = {h: i for i, h in enumerate(spec.headers)}
    output_cols = {h for h, b in bindings.items() if b.role == "output"}
    if not output_cols:
        return False
    frozen: dict[str, int] = {}
    mismatched_rows: dict[str, set[int]] = {}
    for idx, cells in row_mismatch_cells.items():
        for c in cells or []:
            col = c.get("column")
            found = _cell_int(c.get("found"))
            if not col or col not in output_cols or found is None:
                return False
            if frozen.setdefault(col, found) != found:
                return False
            mismatched_rows.setdefault(col, set()).add(idx)
    if not frozen:
        return False
    rows_by_index = {r.line_index: r for r in spec.rows if not r.is_malformed}

    def _expected(idx: int, col: str) -> int | None:
        row = rows_by_index.get(idx)
        i = hdr_idx.get(col)
        if row is None or i is None or i >= len(row.values):
            return None
        tok = row.values[i]
        return tok.value if tok.kind == "int" else None
    all_rows = [r.line_index for r in spec.rows if not r.is_malformed]
    seen_const: dict[str, set[int]] = {}
    for idx in all_rows:
        for col in output_cols:
            if col in frozen and idx in mismatched_rows.get(col, ()):
                continue
            exp = _expected(idx, col)
            if exp is None:
                continue
            if col in frozen:
                if exp != frozen[col]:
                    return False
            else:
                seen_const.setdefault(col, set()).add(exp)
                if len(seen_const[col]) > 1:
                    return False
    return True


def gross_check(circuit, spec: TestSpec, failing_count: int, *,
                max_failing: int = GROSS_MAX_FAILING,
                rate_gate_min_components: int = RATE_GATE_MIN_COMPONENTS,
                row_mismatch_columns: list[set] | None = None,
                row_mismatch_cells: dict[int, list[dict]] | None = None,
                ) -> list[dict]:
    flags: list[dict] = []
    bars_on = len(circuit.components) > rate_gate_min_components
    bindings = match_variables_to_io(spec.headers, circuit)
    frozen_trunk = _frozen_trunk(spec, bindings, row_mismatch_cells)

    if bars_on and row_mismatch_columns and not frozen_trunk:
        total_rows = spec.well_formed_row_count()
        scattered = [cols for cols in row_mismatch_columns
                     if len(cols) >= 4]
        if scattered and total_rows and (len(scattered) / total_rows
                                         >= SCATTERED_ROW_MAX_SHARE):
            flags.append({
                "kind": "scattered_failures",
                "detail": (
                    f"{len(scattered)} of the testcase's {total_rows} rows "
                    f"are wrong in 4 or more output columns AT ONCE. "
                    f"That spread points at the design plan, not one "
                    f"localized bug — whatever the pass rate says."
                ),
            })

    unbound = [h for h in spec.headers
               if bindings[h].role == "unbound"]
    if unbound:
        flags.append({
            "kind": "unbound_columns",
            "detail": (
                "testcase column(s) " + ", ".join(repr(h) for h in unbound)
                + " match no input, output, or clock label in this circuit "
                "— ports are missing or renamed, so the tests cannot drive "
                "or observe what they were written for."
            ),
        })
    has_clock_col = any(b.role == "clock" for b in bindings.values()) or any(
        tok.kind == "clock"
        for row in spec.rows if not row.is_malformed
        for tok in row.values
    )
    if has_clock_col and not _holds_state(circuit):
        flags.append({
            "kind": "missing_clocked_logic",
            "detail": (
                "the testcase drives a clock, but the circuit contains no "
                "register or other clocked element — nothing can hold "
                "state between rows (is the pipeline stage missing?)."
            ),
        })
    if not bars_on or frozen_trunk:
        n_rows = 0
    else:
        n_rows = spec.well_formed_row_count()
    passing = max(0, n_rows - failing_count)
    rate = passing / n_rows if n_rows else 0.0
    if n_rows >= 11:
        if failing_count > max_failing and rate < 0.20:
            flags.append({
                "kind": "too_many_failures",
                "detail": (
                    f"{failing_count} of {n_rows} rows fail — more than "
                    f"{max_failing}, with under 20% passing. That is "
                    f"usually a structural problem (wrong wiring plan, "
                    f"missing block), not one localized bug; revisit the "
                    f"design before chasing single rows."
                ),
            })
    elif n_rows >= 6:
        if rate < 0.60:
            flags.append({
                "kind": "low_pass_rate",
                "detail": (
                    f"only {passing} of {n_rows} rows pass — below the 60% "
                    f"bar for a 6-10 row testcase. Rebuild the basics "
                    f"before hunting a single bug."
                ),
            })
    elif n_rows >= 1:
        if rate < 0.30:
            flags.append({
                "kind": "low_pass_rate",
                "detail": (
                    f"only {passing} of {n_rows} rows pass — below the 30% "
                    f"bar for a 1-5 row testcase. Rebuild the basics "
                    f"before hunting a single bug."
                ),
            })
    return flags

def _row_evidence(circuit, netlist, graph, spec, bindings, row, *,
                  sel_cols, manifest, sim=None, jar_cells=None,
                  notes=None, stuck=None, net_names=None,
                  all_sims=None, row_order=None,
                  line_witness=None) -> RowEvidence:
    if sim is None:
        sim = simulate_sequential(circuit, netlist, graph, spec,
                                  row.line_index)
    outputs, mismatches = _outputs_report(spec, bindings, row, sim)
    if jar_cells and not mismatches:
        mismatches = [dict(c) for c in jar_cells]
        if notes is not None:
            notes.append(
                f"row {row.line_index}: Digital reports a failure the "
                f"evaluator cannot reproduce; using Digital's cells."
            )
    col = {h: i for i, h in enumerate(spec.headers)}
    selects = [[h, row.values[col[h]].raw] for h in sel_cols]
    cat = row_category(circuit, netlist, sim, manifest)
    expected = _expected_ints(spec, bindings, row)
    steer_extra: dict = {}
    trace = None
    trace_notes: list[str] = []
    if all_sims and row_order:
        for m in mismatches:
            colname = m.get("column")
            if colname not in expected:
                continue
            try:
                found = register_read_trace(
                    circuit, netlist, graph, all_sims, row_order,
                    row.line_index, colname, expected[colname][0],
                    expected[colname][1], net_names)
            except Exception:
                found = None
            if found is None:
                continue
            t, boosts, tnotes = found
            if trace is None:
                trace = t
            for idx, hit in boosts.items():
                if idx not in steer_extra or hit[0] > steer_extra[idx][0]:
                    steer_extra[idx] = hit
            trace_notes.extend(n for n in tnotes if n not in trace_notes)
    if line_witness:
        boosts, lnotes = line_witness
        for idx, hit in boosts.items():
            if idx not in steer_extra or hit[0] > steer_extra[idx][0]:
                steer_extra[idx] = hit
        trace_notes.extend(n for n in lnotes if n not in trace_notes)
    report = localize(circuit, netlist, graph, sim, outputs,
                      expected_values=expected, stuck=stuck,
                      net_names=net_names, steer_extra=steer_extra or None)
    for n in trace_notes:
        if n not in report.notes:
            report.notes.append(n)
    net_values = {
        str(nid): {
            "value": val,
            "bits": sim.net_bits.get(nid, 1),
            "hex": format(val, "X"),
        }
        for nid, val in sim.net_values.items()
    }
    return RowEvidence(
        row_index=row.line_index,
        raw=row.raw,
        mismatches=mismatches,
        outputs=outputs,
        net_values=net_values,
        unresolved_nets=sorted(sim.unresolved_nets),
        selects=selects,
        category=cat["category"] if cat else None,
        program_word=cat["word"] if cat else None,
        suspect_report=report,
        state_trace=trace,
    )

def _bucket_key(r: RowEvidence):
    return (
        frozenset(m.get("column", "?") for m in r.mismatches),
        tuple(tuple(s) for s in r.selects),
        r.category,
    )


def _top_set(r: RowEvidence) -> set[int]:
    return {s.component_index
            for s in r.suspect_report.suspects[:_TOP_SUSPECTS]}


def _overlap(a: set[int], b: set[int]) -> float:
    if not a or not b:
        return 1.0
    union = a | b
    return len(a & b) / len(union)


def _signature_dict(r: RowEvidence) -> dict:
    return {
        "columns": sorted({m.get("column", "?") for m in r.mismatches}),
        "selects": [list(s) for s in r.selects],
        "category": r.category,
    }


def cluster_rows(rows: list[RowEvidence], *, cap: int = _MAX_CLUSTERS,
                 min_overlap: float = _MIN_OVERLAP):
    notes: list[str] = []
    clusters: list[Cluster] = []
    meta: list[dict] = []
    for r in sorted(rows, key=lambda x: x.row_index):
        key = _bucket_key(r)
        tops = _top_set(r)
        placed = False
        for c, m in zip(clusters, meta):
            if m["key"] == key and _overlap(tops, m["tops"]) >= min_overlap:
                c.rows.append(r)
                m["tops"] |= tops
                placed = True
                break
        if not placed:
            clusters.append(Cluster(signature=_signature_dict(r), rows=[r]))
            meta.append({"key": key, "tops": set(tops)})

    while len(clusters) > cap:
        i = min(range(len(clusters)),
                key=lambda k: (len(clusters[k].rows), -k))
        small, small_meta = clusters.pop(i), meta.pop(i)
        j = max(range(len(clusters)),
                key=lambda k: (_overlap(small_meta["tops"], meta[k]["tops"]),
                               len(clusters[k].rows), -k))
        clusters[j].rows.extend(small.rows)
        clusters[j].rows.sort(key=lambda x: x.row_index)
        clusters[j].folded_rows += len(small.rows)
        meta[j]["tops"] |= small_meta["tops"]
        notes.append(
            f"cluster cap {cap}: folded {len(small.rows)} row(s) with "
            f"signature {small.signature} into a neighboring cluster."
        )

    for c in clusters:
        c.merged = merge_reports([r.suspect_report for r in c.rows])
    return clusters, notes

def compact_circuit_facts(circuit, netlist=None, graph=None) -> dict:
    """The §3 `circuit` field: the same compact CircuitFacts view the L2
    explainer sends (inventory, io, subcircuits, selectors, ...)."""
    return _compact_facts(extract_facts(circuit, netlist, graph).to_dict())

_SUSPECT_ATTR_KEYS = (
    "AddrBits", "Bits", "Inputs", "Selector Bits",
    "Input Splitting", "Output Splitting", "Value", "intFormat",
    "inverterConfig", "flipSelPos", "Signed", "inputBits", "outputBits",
    "splitterSpreading", "isProgramCounter",
)

_DATA_ELEMENTS = ("ROM", "RAM", "EEPROM", "RAMDualPort", "LookUpTable")


def _suspect_attrs(comp) -> dict:
    out: dict = {}
    for k in _SUSPECT_ATTR_KEYS:
        v = comp.attributes.get(k)
        if v not in (None, "", []):
            out[k] = v
    if comp.element_name in ("ROM", "RAM", "EEPROM", "RAMDualPort"):
        raw = comp.attributes.get("Data", "")
        tokens = [t for t in str(raw or "").replace(",", " ").split() if t]
        out["data_words_stored"] = len(tokens)
        if not tokens:
            out["data_note"] = "Data is EMPTY - every address reads 0."
    return out


def _address_input_drivers(circuit, netlist, addr_net_id,
                           storage_idx: int, rows) -> dict | None:
    net = next((n for n in netlist.nets if n.net_id == addr_net_id), None)
    if net is None:
        return None
    drivers = [p for p in net.pins
               if p.direction == "out" and p.component_index != storage_idx]
    if len(drivers) != 1:
        return None
    sel_idx = drivers[0].component_index
    sel = circuit.components[sel_idx]
    inputs: dict[str, dict] = {}
    for n in netlist.nets:
        mine = [p for p in n.pins
                if p.component_index == sel_idx and p.direction == "in"]
        if not mine:
            continue
        feeder = next((q for q in n.pins if q.direction == "out"
                       and q.component_index != sel_idx), None)
        values = {}
        for r in rows:
            nv = r.net_values.get(str(n.net_id))
            if nv is not None:
                values[str(r.row_index)] = nv.get("value")
        for p in mine:
            entry: dict = {}
            if feeder is not None:
                fc = circuit.components[feeder.component_index]
                entry["driven_by"] = (
                    f"{fc.element_name}[{feeder.component_index}]")
            if values:
                entry["values"] = values
            if entry:
                inputs[p.pin_name] = entry
    if not inputs or len(inputs) > 16:
        return None
    return {"selector": f"{sel.element_name}[{sel_idx}]", "inputs": inputs}


def suspect_wiring(circuit, netlist, indices: list[int],
                   rep_rows: list["RowEvidence"] | None = None) -> list[dict]:
    out: list[dict] = []
    names = net_names_map(circuit, netlist)
    for idx in indices:
        if not (0 <= idx < len(circuit.components)):
            continue
        comp = circuit.components[idx]
        attrs = _suspect_attrs(comp)
        pins: list[dict] = []
        for net in netlist.nets:
            mine = [p for p in net.pins if p.component_index == idx]
            if not mine:
                continue
            others = []
            for q in net.pins:
                if q.component_index == idx:
                    continue
                qc = circuit.components[q.component_index]
                other = {"component_index": q.component_index,
                         "element": qc.element_name,
                         "pin": q.pin_name, "direction": q.direction}
                if qc.label:
                    other["label"] = qc.label
                others.append(other)
            values = {}
            for r in rep_rows or []:
                nv = r.net_values.get(str(net.net_id))
                if nv is not None:
                    values[str(r.row_index)] = nv.get("value")
            for p in mine:
                entry = {"pin": p.pin_name, "direction": p.direction,
                         "net_id": net.net_id,
                         "connects_to": others[:6]}
                if net.net_id in names:
                    entry["net"] = names[net.net_id]
                if values:
                    entry["values"] = values
                pins.append(entry)
        rec = {"component_index": idx, "element": comp.element_name,
               "pins": pins}
        if comp.label:
            rec["label"] = comp.label
        if attrs:
            rec["attrs"] = attrs
        out.append(rec)
    return out


def _compact_net_values(net_values: dict) -> dict:
    return {nid: {"bits": nv.get("bits"), "hex": nv.get("hex")}
            for nid, nv in net_values.items()}


def _compact_suspects(report: dict) -> dict:
    report["suspects"] = [{k: v for k, v in s.items() if v is not None}
                          for s in report.get("suspects") or []]
    return report


def build_payload(compact_circuit: dict, spec: TestSpec, cluster: Cluster, *,
                  circuit=None, netlist=None,
                  max_representatives: int = _MAX_REPRESENTATIVES) -> dict:
    reps = cluster.rows[:max_representatives]
    payload = {
        "contract": CONTRACT,
        "circuit": compact_circuit,
        "testcase": {"name": spec.name, "headers": list(spec.headers)},
        "cluster": {
            "rows": [
                {"index": r.row_index, "raw": r.raw,
                 "mismatches": r.mismatches}
                for r in cluster.rows
            ],
            "representative_evidence": [
                {"row_index": r.row_index,
                 "net_values": _compact_net_values(r.net_values),
                 "unresolved_nets": r.unresolved_nets,
                 "outputs": r.outputs}
                for r in reps
            ],
        },
        "suspects": _compact_suspects(cluster.merged.to_dict()),
    }
    if circuit is not None and netlist is not None:
        names = net_names_map(circuit, netlist)
        seen_nets = {nid for r in reps for nid in r.net_values}
        payload["cluster"]["net_names"] = {
            str(nid): name for nid, name in sorted(names.items())
            if str(nid) in seen_nets}
        traces = [r.state_trace for r in reps if r.state_trace]
        if traces:
            payload["cluster"]["state_trace"] = traces
        indices = list(cluster.merged.suspect_indices())
        for i, comp in enumerate(circuit.components):
            if comp.element_name in _DATA_ELEMENTS and i not in indices:
                indices.append(i)
        payload["suspect_wiring"] = suspect_wiring(
            circuit, netlist, indices, rep_rows=reps)
        for rec in payload["suspect_wiring"]:
            comp = circuit.components[rec["component_index"]]
            if comp.element_name not in _DATA_ELEMENTS:
                continue
            a_nets = [p["net_id"] for p in rec["pins"]
                      if p["pin"] == "A" and p["direction"] == "in"]
            if not a_nets:
                continue
            key = str(a_nets[0])
            by_row = {}
            for r in cluster.rows:
                nv = r.net_values.get(key)
                if nv is not None:
                    by_row[str(r.row_index)] = nv.get("value")
            if by_row:
                rec["address_by_row"] = by_row
            aid = _address_input_drivers(
                circuit, netlist, a_nets[0],
                rec["component_index"], cluster.rows)
            if aid:
                rec["address_input_drivers"] = aid
    return payload


def assemble_evidence(circuit, netlist, graph, spec: TestSpec, *,
                      manifest: dict | None = None,
                      failing_indices: list[int] | None = None,
                      jar_mismatches: dict[int, list[dict]] | None = None,
                      compact_circuit: dict | None = None,
                      max_clusters: int = _MAX_CLUSTERS,
                      max_representatives: int = _MAX_REPRESENTATIVES,
                      max_failing: int = GROSS_MAX_FAILING,
                      lazy_exempt: bool = False) -> EvidenceResult:
    res = EvidenceResult(spec_name=spec.name, headers=list(spec.headers))
    bindings = match_variables_to_io(spec.headers, circuit)
    rows_by_index = {r.line_index: r for r in spec.rows if not r.is_malformed}

    resolver = formula_models.resolver_for(circuit, manifest)
    if resolver.decided:
        res.notes.append(
            "subcircuits evaluated as formula models: "
            + ", ".join(f"{f} → {m}" for f, m in sorted(resolver.decided.items())))

    sims: dict[int, SimResult] = {}
    row_mismatch_columns: list[set] | None = None
    row_mismatch_cells: dict[int, list[dict]] | None = None
    try:
        all_sims = simulate_rows(circuit, netlist, graph, spec,
                                 model_resolver=resolver)
    except Exception as exc:
        res.notes.append(
            f"evaluator error {type(exc).__name__}: {exc}"
            + ("" if failing_indices is None
               else "; rows are re-evaluated one by one."))
        all_sims = {}
    if failing_indices is None:
        failing: list[int] = []
        row_mismatch_columns = []
        row_mismatch_cells = {}
        for row in spec.rows:
            if row.is_malformed:
                res.notes.append(
                    f"row {row.line_index} is malformed and was skipped.")
                continue
            sim = all_sims.get(row.line_index)
            if sim is None:
                res.notes.append(
                    f"row {row.line_index}: evaluator produced no result.")
                continue
            _outs, mism = _outputs_report(spec, bindings, row, sim)
            if mism:
                failing.append(row.line_index)
                sims[row.line_index] = sim
                row_mismatch_columns.append(
                    {m.get("column") for m in mism if m.get("column")})
                row_mismatch_cells[row.line_index] = list(mism)
    else:
        failing = list(failing_indices)
        sims = {i: all_sims[i] for i in failing if i in all_sims}
        if jar_mismatches:
            sets = [
                {c.get("column") for c in (jar_mismatches.get(i) or [])
                 if isinstance(c, dict) and c.get("column")}
                for i in failing]
            row_mismatch_columns = sets if any(sets) else None
            if row_mismatch_columns is not None:
                row_mismatch_cells = {
                    i: [c for c in (jar_mismatches.get(i) or [])
                        if isinstance(c, dict)]
                    for i in failing}
    res.failing_count = len(failing)

    if not failing:
        res.mode = "clear"
        return res

    if lazy_exempt:
        res.notes.append(
            "lazy-gate checks skipped for this file (control-unit rule).")
    else:
        flags = gross_check(circuit, spec, len(failing),
                            max_failing=max_failing,
                            row_mismatch_columns=row_mismatch_columns,
                            row_mismatch_cells=row_mismatch_cells)
        if flags:
            res.mode = "lazy"
            res.gross_flags = flags
            return res

    res.mode = "analysis"
    sel_cols = select_columns(circuit, netlist, spec, bindings)
    net_names = net_names_map(circuit, netlist)
    try:
        stuck = stuck_components(circuit, netlist, all_sims)
    except Exception:
        stuck = {}
    if stuck:
        res.notes.append(
            "output frozen over the whole testcase although inputs vary: "
            + ", ".join(f"{circuit.components[i].element_name}[{i}]"
                        for i in sorted(stuck)))
    row_order = [r.line_index for r in spec.rows if not r.is_malformed]
    try:
        line_hits = selector_line_witness(circuit, netlist, graph, spec,
                                          bindings, all_sims, failing,
                                          net_names=net_names)
    except Exception:
        line_hits = {}
    if line_hits:
        named = sorted({f"{circuit.components[i].element_name}[{i}]"
                        for b, _n in line_hits.values() for i in b})
        res.notes.append(
            "line witness: judged against the trusted ROM words, the selector "
            "lines that assert or stay silent on the failing rows point at "
            + ", ".join(named) + ".")
    evidence_rows = list(failing)
    pc_col = _pc_column(manifest, bindings)
    if pc_col and row_mismatch_cells and _holds_state(circuit):
        first, tail = pc_divergence(failing, row_mismatch_cells, pc_col)
        if tail:
            res.consequential_rows = tail
            res.divergence = {"column": pc_col, "first_row": first, "rows": tail}
            res.notes.append(
                f"{pc_col} leaves the expected path at row {first}; the "
                f"{len(tail)} failing row(s) after it ({tail[0]}–{tail[-1]}) "
                f"are its consequences and stay out of the evidence — a fix "
                f"must still repair them.")
            evidence_rows = [i for i in failing if i not in set(tail)]
    evidence: list[RowEvidence] = []
    for idx in evidence_rows:
        row = rows_by_index.get(idx)
        if row is None:
            res.notes.append(
                f"failing row {idx} is missing or malformed in the spec; "
                f"skipped.")
            continue
        jar_cells = (jar_mismatches or {}).get(idx)
        try:
            evidence.append(_row_evidence(
                circuit, netlist, graph, spec, bindings, row,
                sel_cols=sel_cols, manifest=manifest,
                sim=sims.get(idx), jar_cells=jar_cells, notes=res.notes,
                stuck=stuck, net_names=net_names,
                all_sims=all_sims, row_order=row_order,
                line_witness=line_hits.get(idx),
            ))
        except Exception as exc:
            res.notes.append(
                f"row {idx}: evaluator error {type(exc).__name__}: {exc} — "
                f"evidence limited to Digital's cells.")
            evidence.append(RowEvidence(
                row_index=idx, raw=row.raw,
                mismatches=[dict(c) for c in jar_cells or []],
            ))

    if _frozen_trunk(spec, bindings, row_mismatch_cells) and evidence:
        first = evidence[0]
        clusters = [Cluster(
            signature=_signature_dict(first),
            rows=list(evidence),
            merged=merge_reports([r.suspect_report for r in evidence]),
        )]
        res.notes.append(
            "all failing rows show one frozen output stage — analyzed "
            "as a single cluster so a fix must repair every row.")
    else:
        clusters, cnotes = cluster_rows(evidence, cap=max_clusters)
        res.notes.extend(cnotes)
    res.clusters = clusters
    if compact_circuit is None:
        compact_circuit = compact_circuit_facts(circuit, netlist, graph)
    res.payloads = [
        build_payload(compact_circuit, spec, c, circuit=circuit,
                      netlist=netlist,
                      max_representatives=max_representatives)
        for c in clusters
    ]
    return res


def assemble_evidence_for_file(dig_path, *, spec_name: str | None = None,
                               spec_index: int = 0,
                               manifest: dict | None = None,
                               use_manifest: bool = True,
                               **kwargs) -> EvidenceResult:
    """
    Parse + build + assemble for one file. 
    """
    circuit = parse_dig_file(str(dig_path))
    netlist = build_netlist(circuit)
    graph = build_signal_graph(circuit, netlist)
    specs = extract_test_specs(circuit)
    if not specs:
        raise ValueError(f"{Path(dig_path).name} has no testcase.")
    spec = None
    if spec_name is not None:
        spec = next((s for s in specs if s.name == spec_name), None)
        if spec is None:
            names = ", ".join(repr(s.name) for s in specs)
            raise ValueError(
                f"No testcase named {spec_name!r}; saw: {names}")
    else:
        if spec_index < 0 or spec_index >= len(specs):
            raise ValueError(
                f"spec_index {spec_index} out of range "
                f"({len(specs)} testcase(s)).")
        spec = specs[spec_index]
    if manifest is None and use_manifest:
        names = {Path(dig_path).name}
        for sub in circuit.subcircuits:
            ref = getattr(sub, "reference", None)
            if ref:
                names.add(ref)
        from dlc.l3.manifest import tree_element_names
        manifest = find_manifest(names,
                                 element_names=tree_element_names(circuit))
    return assemble_evidence(circuit, netlist, graph, spec,
                             manifest=manifest, **kwargs)
