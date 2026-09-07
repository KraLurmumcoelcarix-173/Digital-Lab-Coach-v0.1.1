from __future__ import annotations

from dataclasses import dataclass, field

import networkx as nx

from dlc.parser.models import Circuit
from dlc.parser.netlist import NetList
from dlc.sim.simulator import SimResult

_NEVER_SUSPECT = frozenset({"In", "Clock", "Tunnel", "Testcase", "Rectangle"})

_HOT_KINDS = frozenset({"Multiplexer", "Decoder", "Splitter"})


@dataclass
class Suspect:
    component_index: int
    element_name: str
    display_name: str
    score: float
    reasons: list[str] = field(default_factory=list)
    in_failing_cones: list[str] = field(default_factory=list)
    in_active_cones: list[str] = field(default_factory=list)
    feeds_passing_output: bool = False
    drives_unresolved: bool = False
    is_subcircuit: bool = False
    child_reference: str | None = None
    child_self_test: str | None = None


@dataclass
class SuspectReport:
    failing_outputs: list[str] = field(default_factory=list)
    passing_outputs: list[str] = field(default_factory=list)
    suspects: list[Suspect] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)

    def suspect_indices(self) -> list[int]:
        return [s.component_index for s in self.suspects]


def _output_component_index(circuit: Circuit, label: str) -> int | None:
    for idx, comp in enumerate(circuit.components):
        if comp.is_output() and (comp.label or f"out_{idx}") == label:
            return idx
    return None


def _static_cone(graph: nx.MultiDiGraph, out_idx: int) -> set[int]:
    if out_idx not in graph:
        return {out_idx}
    return set(nx.ancestors(graph, out_idx)) | {out_idx}


def _mux_sel_value(circuit, netlist, sim: SimResult, idx: int) -> int | None:
    for net in netlist.nets:
        for p in net.pins:
            if (p.component_index == idx and p.pin_name == "sel"
                    and p.direction == "in"):
                return sim.net_values.get(net.net_id)
    return None


def _active_cone(
    circuit: Circuit,
    netlist: NetList,
    graph: nx.MultiDiGraph,
    sim: SimResult,
    out_idx: int,
) -> set[int]:
    if out_idx not in graph:
        return {out_idx}
    seen: set[int] = {out_idx}
    frontier = [out_idx]
    while frontier:
        node = frontier.pop()
        comp = circuit.components[node]
        sel_val = None
        if comp.element_name == "Multiplexer":
            sel_val = _mux_sel_value(circuit, netlist, sim, node)
        for pred, _node, edata in graph.in_edges(node, keys=False, data=True):
            if sel_val is not None:
                pin = edata.get("sink_pin") or ""
                if pin != "sel" and pin != f"in{sel_val}":
                    continue
            if pred not in seen:
                seen.add(pred)
                frontier.append(pred)
    return seen

def _unresolved_drivers(netlist: NetList, sim: SimResult) -> set[int]:
    out: set[int] = set()
    for nid in sim.unresolved_nets:
        for p in netlist.nets[nid].pins:
            if p.direction == "out":
                out.add(p.component_index)
    return out


def _child_of_instance(circuit: Circuit, idx: int):
    comp = circuit.components[idx]
    for sub in circuit.subcircuits:
        if sub.parent_component is comp:
            return sub
    return None


def _run_child_self_test(sub_ref, jar_path, timeout) -> str:
    if sub_ref is None or sub_ref.resolved_path is None:
        return "no_tests"
    from dlc.parser.dig_parser import parse_dig_file
    from dlc.testing.spec import extract_test_specs
    from dlc.testing.runner import per_file_run_fast

    try:
        child = parse_dig_file(sub_ref.resolved_path)
        specs = [s for s in extract_test_specs(child) if s.rows]
        if not specs:
            return "no_tests"
        results, fallback = per_file_run_fast(
            specs, sub_ref.resolved_path, jar_path=jar_path, timeout=timeout,
        )
        for rows in results.values():
            if any(r.status in ("failed", "error") for r in rows):
                return "failed"
        if fallback:
            return "no_tests"
        return "passed"
    except Exception:
        return "no_tests"


def _display_name(circuit: Circuit, idx: int) -> str:
    from dlc.facts.extractor import _component_display_name
    return _component_display_name(circuit.components[idx], idx)

_W_ACTIVE = 3.0
_W_SHARED = 1.0
_W_STATIC = 1.0
_W_EXONERATED = -1.0
_W_MUTED = 1.5
_W_HOT = 0.5
_W_CHILD_FAILED = 2.0
_W_CHILD_PASSED = -1.0
_W_WITNESS = 2.5
_W_STUCK = 2.0

_MAX_WITNESSES = 3
_MIN_WITNESS_BITS = 2


def _net_of_pin(netlist: NetList, comp_idx: int, pin_name: str,
                direction: str):
    for net in netlist.nets:
        for p in net.pins:
            if (p.component_index == comp_idx and p.pin_name == pin_name
                    and p.direction == direction):
                return net
    return None


def _upstream_hops(graph: nx.MultiDiGraph, start: int,
                   limit: set[int]) -> dict[int, int]:
    hops = {start: 0}
    frontier = [start]
    while frontier:
        nxt = []
        for n in frontier:
            for pred in graph.predecessors(n):
                if pred in limit and pred not in hops:
                    hops[pred] = hops[n] + 1
                    nxt.append(pred)
        frontier = nxt
    return hops


def _wrong_bit_cone(circuit: Circuit, netlist: NetList, sel_net,
                    diff_bits: list[int], ancestors) -> set[int] | None:
    from dlc.facts.splitter import parse_splitting
    drivers = [p for p in sel_net.pins if p.direction == "out"]
    if len(drivers) != 1:
        return None
    d = drivers[0].component_index
    comp = circuit.components[d]
    if (comp.element_name != "Splitter"
            or not drivers[0].pin_name.startswith("out")):
        return None
    try:
        in_groups = parse_splitting(
            str(comp.attributes.get("Input Splitting", "")))
        out_groups = parse_splitting(
            str(comp.attributes.get("Output Splitting", "")))
        grp = out_groups[int(drivers[0].pin_name[3:])]
    except (ValueError, IndexError):
        return None
    cone = {d}
    for bit in diff_bits:
        k = grp.bit_lo + bit
        src = next((i for i, g in enumerate(in_groups)
                    if g.bit_lo <= k <= g.bit_hi), None)
        if src is None:
            return None
        in_net = _net_of_pin(netlist, d, f"in{src}", "in")
        if in_net is None:
            return None
        for p in in_net.pins:
            if p.direction == "out":
                cone.add(p.component_index)
                cone |= ancestors(p.component_index)
    return cone


def witness_steer(circuit: Circuit, netlist: NetList, graph: nx.MultiDiGraph,
                  sim: SimResult, out_idx: int, exp_val: int,
                  width: int | None, *, net_names: dict | None = None,
                  ) -> tuple[dict[int, tuple[float, str]], list[str]]:
    if (width is None or width < _MIN_WITNESS_BITS or exp_val is None
            or out_idx not in graph):
        return {}, []
    mask = (1 << width) - 1
    want = exp_val & mask
    if want < 8 or want == mask:
        return {}, []
    out_net = _net_of_pin(netlist, out_idx, "in", "in")
    out_nid = out_net.net_id if out_net is not None else None
    cone = _static_cone(graph, out_idx)
    witnesses: list[tuple[int, int]] = []
    for nid, val in sim.net_values.items():
        if nid == out_nid or val is None:
            continue
        if sim.net_bits.get(nid) != width or (val & mask) != want:
            continue
        net = netlist.nets[nid]
        drivers = [p.component_index for p in net.pins if p.direction == "out"]
        if not drivers or drivers[0] not in cone:
            continue
        if circuit.components[drivers[0]].element_name in (
                "Const", "Ground", "VDD", "In"):
            continue
        witnesses.append((nid, drivers[0]))
    if not witnesses or len(witnesses) > _MAX_WITNESSES:
        return {}, []

    anc_cache: dict[int, set[int]] = {}

    def ancestors(n: int) -> set[int]:
        if n not in anc_cache:
            anc_cache[n] = set(nx.ancestors(graph, n)) if n in graph else set()
        return anc_cache[n]

    boosted: dict[int, tuple[float, str]] = {}
    notes: list[str] = []
    for nid, driver in witnesses:
        name = (net_names or {}).get(nid) or f"net {nid}"
        for m in sorted(cone):
            comp = circuit.components[m]
            if comp.element_name != "Multiplexer":
                continue
            sel_val = _mux_sel_value(circuit, netlist, sim, m)
            if sel_val is None:
                continue
            arms: set[int] = set()
            sel_preds: set[int] = set()
            for pred, _n, edata in graph.in_edges(m, data=True):
                pin = edata.get("sink_pin") or ""
                if pin == "sel":
                    sel_preds.add(pred)
                elif pin.startswith("in") and (
                        pred == driver or driver in ancestors(pred)):
                    try:
                        arms.add(int(pin[2:]))
                    except ValueError:
                        continue
            if not arms or sel_val in arms:
                continue
            arm = min(arms, key=lambda a: (bin(a ^ sel_val).count("1"), a))
            xor = arm ^ sel_val
            diff_bits = [b for b in range(xor.bit_length()) if (xor >> b) & 1]
            sel_net = _net_of_pin(netlist, m, "sel", "in")
            steer = None
            if sel_net is not None:
                try:
                    steer = _wrong_bit_cone(circuit, netlist, sel_net,
                                            diff_bits, ancestors)
                except Exception:
                    steer = None
            if steer is None:
                steer = set(sel_preds)
                for sp in sel_preds:
                    steer |= ancestors(sp)
            steer.add(m)
            bits_txt = ", ".join(str(b) for b in diff_bits) or "?"
            reason = (f"SELECT-PATH suspect: on the logic behind sel bit "
                      f"{bits_txt} of {comp.element_name}[{m}] (see notes)")
            hops = _upstream_hops(graph, m, steer)
            for idx in steer:
                w = round(_W_WITNESS - 0.1 * min(hops.get(idx, 8), 8), 2)
                prev = boosted.get(idx)
                if prev is None or w > prev[0]:
                    boosted[idx] = (w, reason)
            notes.append(
                f"SELECT-PATH: expected value found on net {name} (arm in{arm} "
                f"of {comp.element_name}[{m}]) while the row selects arm "
                f"in{sel_val} — the data is computed right; the logic behind "
                f"sel bit {bits_txt} chooses the wrong arm.")
    return boosted, notes

def localize(
    circuit: Circuit,
    netlist: NetList,
    graph: nx.MultiDiGraph,
    sim: SimResult,
    outputs_report: list[dict],
    *,
    max_suspects: int = 12,
    run_child_self_tests: bool = False,
    jar_path: str | None = None,
    child_test_timeout: float = 60.0,
    expected_values: dict[str, tuple[int, int | None]] | None = None,
    stuck: dict[int, str] | None = None,
    net_names: dict[int, str] | None = None,
) -> SuspectReport:
    report = SuspectReport()
    failing = [o["label"] for o in outputs_report if o.get("ok") is not True]
    passing = [o["label"] for o in outputs_report if o.get("ok") is True]
    report.failing_outputs = failing
    report.passing_outputs = passing
    if not failing:
        report.notes.append("No failing outputs on this row; nothing to localize.")
        return report

    static_cones: dict[str, set[int]] = {}
    active_cones: dict[str, set[int]] = {}
    steer: dict[int, tuple[float, str]] = {}
    for label in failing:
        out_idx = _output_component_index(circuit, label)
        if out_idx is None:
            report.notes.append(f"Output column {label!r} has no Out component.")
            continue
        static_cones[label] = _static_cone(graph, out_idx)
        active_cones[label] = _active_cone(circuit, netlist, graph, sim, out_idx)
        expected = (expected_values or {}).get(label)
        if expected is not None:
            try:
                boosted, w_notes = witness_steer(
                    circuit, netlist, graph, sim, out_idx, expected[0],
                    expected[1], net_names=net_names)
            except Exception:
                boosted, w_notes = {}, []
            for idx, hit in boosted.items():
                if idx not in steer or hit[0] > steer[idx][0]:
                    steer[idx] = hit
            report.notes.extend(w_notes)

    passing_union: set[int] = set()
    for label in passing:
        out_idx = _output_component_index(circuit, label)
        if out_idx is not None:
            passing_union |= _static_cone(graph, out_idx)

    muted = _unresolved_drivers(netlist, sim)

    candidates: set[int] = set()
    for cone in static_cones.values():
        candidates |= cone

    suspects: list[Suspect] = []
    for idx in sorted(candidates):
        comp = circuit.components[idx]
        if comp.element_name in _NEVER_SUSPECT:
            continue
        in_static = [lb for lb, cone in static_cones.items() if idx in cone]
        in_active = [lb for lb, cone in active_cones.items() if idx in cone]
        if not in_static:
            continue

        is_sub = comp.element_name.endswith(".dig")
        score = 0.0
        reasons: list[str] = []
        if in_active:
            score += _W_ACTIVE + _W_SHARED * (len(in_active) - 1)
            reasons.append(
                "on the ACTIVE signal path of failing output(s) "
                + ", ".join(in_active)
            )
            if len(in_active) > 1:
                reasons.append(
                    f"common cause candidate: active in {len(in_active)} "
                    f"failing outputs' paths"
                )
        else:
            score += _W_STATIC
            reasons.append(
                "upstream of failing output(s) " + ", ".join(in_static)
                + " (not on the row's active mux path)"
            )
        exonerated = idx in passing_union
        if exonerated:
            score += _W_EXONERATED
            reasons.append("also feeds a passing output (partly exonerated)")
        drives_unres = idx in muted
        if drives_unres:
            score += _W_MUTED
            reasons.append("its output net stayed unresolved this row (muted)")
        if comp.element_name in _HOT_KINDS:
            score += _W_HOT
            reasons.append("selector/splitter — semantic-miswire hot spot")
        if idx in steer:
            score += steer[idx][0]
            reasons.append(steer[idx][1])
        if stuck and idx in stuck:
            score += _W_STUCK
            reasons.append(stuck[idx])

        child_ref = None
        child_verdict = None
        if is_sub:
            sub = _child_of_instance(circuit, idx)
            child_ref = sub.reference if sub else comp.element_name
            reasons.append("subcircuit instance — expandable via drill-in")
            if run_child_self_tests:
                child_verdict = _run_child_self_test(
                    sub, jar_path, child_test_timeout,
                )
                if child_verdict == "failed":
                    score += _W_CHILD_FAILED
                    reasons.append("its OWN embedded tests fail")
                elif child_verdict == "passed":
                    score += _W_CHILD_PASSED
                    reasons.append(
                        "its OWN embedded tests pass (parent wiring likelier)"
                    )

        suspects.append(Suspect(
            component_index=idx,
            element_name=comp.element_name,
            display_name=_display_name(circuit, idx),
            score=round(score, 2),
            reasons=reasons,
            in_failing_cones=sorted(in_static),
            in_active_cones=sorted(in_active),
            feeds_passing_output=exonerated,
            drives_unresolved=drives_unres,
            is_subcircuit=is_sub,
            child_reference=child_ref,
            child_self_test=child_verdict,
        ))

    suspects.sort(key=lambda s: (-s.score, s.component_index))
    if len(suspects) > max_suspects:
        report.notes.append(
            f"{len(suspects) - max_suspects} low-ranked suspect(s) beyond "
            f"max_suspects={max_suspects} were dropped."
        )
        suspects = suspects[:max_suspects]
    report.suspects = suspects
    return report


def merge_reports(reports: list[SuspectReport], *, max_suspects: int = 12) -> SuspectReport:
    merged = SuspectReport()
    if not reports:
        return merged
    for r in reports:
        for lb in r.failing_outputs:
            if lb not in merged.failing_outputs:
                merged.failing_outputs.append(lb)
        for lb in r.passing_outputs:
            if lb not in merged.passing_outputs:
                merged.passing_outputs.append(lb)
        for note in r.notes:
            if "max_suspects" not in note and note not in merged.notes:
                merged.notes.append(note)

    by_idx: dict[int, Suspect] = {}
    hits: dict[int, int] = {}
    for r in reports:
        for s in r.suspects:
            hits[s.component_index] = hits.get(s.component_index, 0) + 1
            prev = by_idx.get(s.component_index)
            if prev is None:
                by_idx[s.component_index] = Suspect(**{
                    **s.__dict__, "reasons": list(s.reasons),
                    "in_failing_cones": list(s.in_failing_cones),
                    "in_active_cones": list(s.in_active_cones),
                })
            else:
                prev.score += s.score
                for reason in s.reasons:
                    if reason not in prev.reasons:
                        prev.reasons.append(reason)
                for lb in s.in_failing_cones:
                    if lb not in prev.in_failing_cones:
                        prev.in_failing_cones.append(lb)
                for lb in s.in_active_cones:
                    if lb not in prev.in_active_cones:
                        prev.in_active_cones.append(lb)

    n_rows = len(reports)
    out: list[Suspect] = []
    for idx, s in by_idx.items():
        s.score = round(s.score / n_rows + (hits[idx] / n_rows), 2)
        if hits[idx] == n_rows and n_rows > 1:
            s.reasons.append(f"suspected on all {n_rows} rows of the cluster")
        out.append(s)
    out.sort(key=lambda s: (-s.score, s.component_index))
    merged.suspects = out[:max_suspects]
    return merged