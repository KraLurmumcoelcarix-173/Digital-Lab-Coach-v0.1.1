from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from dlc.analyzer.sequential import _CLOCKED_ELEMENTS as _STATE_ELEMENTS
from dlc.facts.extractor import extract_facts
from dlc.l3.localizer import SuspectReport, localize, merge_reports
from dlc.l3.manifest import decode_program_word, find_manifest
from dlc.llm.explain import _compact_facts
from dlc.parser.dig_parser import parse_dig_file
from dlc.parser.graph import build_signal_graph
from dlc.parser.netlist import build_netlist
from dlc.sim import models as formula_models
from dlc.sim.simulator import SimResult, simulate_rows, simulate_sequential
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
                  notes=None, stuck=None, net_names=None) -> RowEvidence:
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
    report = localize(circuit, netlist, graph, sim, outputs,
                      expected_values=_expected_ints(spec, bindings, row),
                      stuck=stuck, net_names=net_names)
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


def _suspect_attrs(comp, *, hide_rom_words: bool = False,
                   comp_index: int | None = None) -> dict:
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
            idx_txt = ("<this component>" if comp_index is None
                       else str(comp_index))
            out["data_note"] = (
                "Data is EMPTY - every address reads 0. To program it, "
                "use exactly: {\"op\": \"change_attribute\", "
                f"\"component_index\": {idx_txt}, \"name\": "
                "\"Data\", \"value\": \"w0,w1,...\"} - comma-separated "
                "hex words, address 0 first, one word PER ADDRESS the "
                "circuit uses. The attribute name is Data, never Value, "
                f"and the component_index MUST be {idx_txt} (this "
                "storage element itself, never a gate).")
        elif not hide_rom_words and len(tokens) <= 32:
            out["stored_words"] = ",".join(tokens)
    return out


def _data_output_bit_map(circuit, netlist, data_idx: int) -> dict:
    from dlc.facts.splitter import parse_splitting

    def net_of(comp_idx, pin_name):
        for net in netlist.nets:
            for p in net.pins:
                if p.component_index == comp_idx and p.pin_name == pin_name:
                    return net
        return None

    start = net_of(data_idx, "D")
    if start is None:
        return {}
    bits = int(circuit.components[data_idx].attributes.get("Bits", 1) or 1)
    out: dict[str, list[int]] = {}
    queue = [(start, {i: i for i in range(bits)})]
    seen: set[int] = set()
    while queue:
        net, pos_map = queue.pop()
        if net.net_id in seen:
            continue
        seen.add(net.net_id)
        for p in net.pins:
            comp = circuit.components[p.component_index]
            if comp.element_name == "Out" and p.direction == "in":
                label = comp.label or f"Out[{p.component_index}]"
                got = sorted(pos_map.values())
                if got:
                    out[label] = got
            elif (comp.element_name == "Splitter"
                  and p.direction == "in"):
                try:
                    groups = parse_splitting(str(
                        comp.attributes.get("Output Splitting", "")))
                except ValueError:
                    continue
                for gi, grp in enumerate(groups):
                    sub = {k - grp.bit_lo: v for k, v in pos_map.items()
                           if grp.bit_lo <= k <= grp.bit_hi}
                    if not sub:
                        continue
                    nxt = net_of(p.component_index, f"out{gi}")
                    if nxt is not None:
                        queue.append((nxt, sub))
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
                   rep_rows: list["RowEvidence"] | None = None,
                   hide_rom_words: bool = False) -> list[dict]:
    out: list[dict] = []
    names = net_names_map(circuit, netlist)
    for idx in indices:
        if not (0 <= idx < len(circuit.components)):
            continue
        comp = circuit.components[idx]
        attrs = _suspect_attrs(comp, hide_rom_words=hide_rom_words,
                               comp_index=idx)
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
                  max_representatives: int = _MAX_REPRESENTATIVES,
                  hide_rom_words: bool = False) -> dict:
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
        indices = list(cluster.merged.suspect_indices())
        for i, comp in enumerate(circuit.components):
            if comp.element_name in _DATA_ELEMENTS and i not in indices:
                indices.append(i)
        payload["suspect_wiring"] = suspect_wiring(
            circuit, netlist, indices,
            rep_rows=reps, hide_rom_words=hide_rom_words)
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
            bit_map = _data_output_bit_map(
                circuit, netlist, rec["component_index"])
            if bit_map:
                rec["output_bit_map"] = {
                    label: (f"bit {bits[0]}" if len(bits) == 1
                            else f"bits {bits[0]}-{bits[-1]}")
                    for label, bits in bit_map.items()}
            bindings = match_variables_to_io(spec.headers, circuit)
            hdr_idx = {h: i for i, h in enumerate(spec.headers)}
            spec_rows = {r.line_index: r for r in spec.rows
                         if not r.is_malformed}
            exp: dict[str, dict] = {}
            for r in cluster.rows:
                row = spec_rows.get(r.row_index)
                if row is None:
                    continue
                vals = {}
                for h, b in bindings.items():
                    if b.role != "output":
                        continue
                    i = hdr_idx[h]
                    if i < len(row.values):
                        tok = row.values[i]
                        if tok.kind == "int" and tok.value is not None:
                            vals[h] = tok.value
                if vals:
                    exp[str(r.row_index)] = vals
            if exp:
                rec["expected_outputs_by_row"] = exp
    return payload


def assemble_evidence(circuit, netlist, graph, spec: TestSpec, *,
                      manifest: dict | None = None,
                      failing_indices: list[int] | None = None,
                      jar_mismatches: dict[int, list[dict]] | None = None,
                      compact_circuit: dict | None = None,
                      max_clusters: int = _MAX_CLUSTERS,
                      max_representatives: int = _MAX_REPRESENTATIVES,
                      max_failing: int = GROSS_MAX_FAILING,
                      lazy_exempt: bool = False,
                      hide_rom_words: bool = False) -> EvidenceResult:
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
    evidence: list[RowEvidence] = []
    for idx in failing:
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
                      max_representatives=max_representatives,
                      hide_rom_words=hide_rom_words)
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
