from __future__ import annotations

import networkx as nx

from dlc.l3.localizer import _active_cone
from dlc.sim.simulator import SimResult, _mask

_SKIP = frozenset({"Tunnel", "Testcase", "Rectangle", "Text", "Probe",
                   "PullUp", "PullDown"})
_SOURCES = frozenset({"In", "Const", "Ground", "VDD", "Clock"})
_STATE = frozenset({"Register", "RAM", "RAMDualPort", "EEPROM", "Counter",
                    "D_FF", "JK_FF", "T_FF", "RS_FF"})
_GATE_WORD = {"And": "AND", "Or": "OR", "XOr": "XOR", "NAnd": "NAND",
              "NOr": "NOR", "XNOr": "XNOR"}
_MAX_STEPS = 40
_MAX_DEPTH = 6


def fmt_value(v, bits: int | None) -> str:
    if v is None:
        return "?"
    bits = bits or 1
    v = int(v) & _mask(bits) if bits > 0 else int(v)
    if bits <= 8:
        return str(v)
    if bits <= 16:
        return f"0x{v:X} ({v})"
    return f"0x{v:X}"


def pick_example_row(spec, bindings):
    rows = [r for r in spec.rows if not r.is_malformed]
    col = {h: i for i, h in enumerate(spec.headers)}
    first = None
    for row in rows:
        outs = []
        ok = True
        for h, b in bindings.items():
            if b is None or b.role != "output" or h not in col:
                continue
            i = col[h]
            if i >= len(row.values):
                ok = False
                break
            tok = row.values[i]
            if tok.kind != "int" or tok.value is None:
                ok = False
                break
            outs.append(tok.value)
        if not ok:
            continue
        if first is None:
            first = row
        if any(outs):
            return row
    return first


class _Pins:
    def __init__(self, circuit, netlist, res: SimResult):
        self.circuit = circuit
        self.res = res
        self.by_comp: dict[int, list[tuple[str, str, int]]] = {}
        self.driver_of_net: dict[int, int] = {}
        for net in netlist.nets:
            for p in net.pins:
                self.by_comp.setdefault(p.component_index, []).append(
                    (p.pin_name, p.direction, net.net_id))
                if p.direction == "out" and net.net_id not in self.driver_of_net:
                    self.driver_of_net[net.net_id] = p.component_index

    def value(self, nid: int):
        return self.res.net_values.get(nid)

    def bits(self, nid: int) -> int:
        return self.res.net_bits.get(nid, 1)

    def ins(self, idx: int) -> list[tuple[str, int]]:
        return [(pn, nid) for pn, d, nid in self.by_comp.get(idx, []) if d == "in"]

    def outs(self, idx: int) -> list[tuple[str, int]]:
        return [(pn, nid) for pn, d, nid in self.by_comp.get(idx, []) if d == "out"]

    def pin_net(self, idx: int, pin: str):
        for pn, _d, nid in self.by_comp.get(idx, []):
            if pn == pin:
                return nid
        return None


def _name(circuit, idx: int) -> str:
    comp = circuit.components[idx]
    if comp.element_name.endswith(".dig"):
        return f"{comp.element_name}[{idx}]"
    if comp.label and comp.element_name not in ("In", "Out"):
        return f"{comp.label} ({comp.element_name}[{idx}])"
    return f"{comp.element_name}[{idx}]"


def _pin_key(pin: str):
    if pin == "sel":
        return (0, "", 0)
    head = pin.rstrip("0123456789")
    tail = pin[len(head):]
    return (1, head, int(tail) if tail else -1)


def _pin_entries(pins: _Pins, pairs) -> list[dict]:
    out = []
    for pn, nid in sorted(pairs, key=lambda x: _pin_key(x[0])):
        v = pins.value(nid)
        b = pins.bits(nid)
        out.append({"pin": pn, "net_id": nid, "bits": b,
                    "value": v, "text": fmt_value(v, b)})
    return out


def _pv(entries: list[dict], pin: str) -> str:
    for e in entries:
        if e["pin"] == pin:
            return e["text"]
    return "?"


def _step_text(circuit, idx: int, ins: list[dict], outs: list[dict],
               pins: _Pins, role: str | None) -> str:
    comp = circuit.components[idx]
    kind = comp.element_name
    name = _name(circuit, idx)
    if kind in _GATE_WORD:
        args = [e["text"] for e in ins]
        return f"{name}: {f' {_GATE_WORD[kind]} '.join(args)} = {_pv(outs, 'Y')}"
    if kind == "Not":
        return f"{name}: NOT {_pv(ins, 'A') if ins else '?'} = {_pv(outs, 'Y')}"
    if kind == "Add":
        c = _pv(ins, "c_i")
        carry = f" + carry-in {c}" if c not in ("?", "0") else ""
        return (f"{name}: {_pv(ins, 'a')} + {_pv(ins, 'b')}{carry} = "
                f"{_pv(outs, 's')}"
                + (f" (carry-out {_pv(outs, 'c_o')})" if _pv(outs, 'c_o') == '1' else ""))
    if kind == "Sub":
        return f"{name}: {_pv(ins, 'a')} - {_pv(ins, 'b')} = {_pv(outs, 's')}"
    if kind == "Multiplexer":
        sel = _pv(ins, "sel")
        arm = f"in{sel}" if sel != "?" else "?"
        return (f"{name}: sel = {sel}, so it passes {arm} = {_pv(ins, arm)} "
                f"to out")
    if kind == "Demultiplexer":
        sel = _pv(ins, "sel")
        return f"{name}: sel = {sel} routes in = {_pv(ins, 'in')} to out{sel}"
    if kind == "Decoder":
        sel = _pv(ins, "sel")
        return f"{name}: sel = {sel}, so only out{sel} is 1"
    if kind == "PriorityEncoder":
        active = [e["pin"] for e in ins if e["value"]]
        if active:
            top = f"in_{_pv(outs, 'num')}"
            return (f"{name}: {', '.join(active)} active; the highest, "
                    f"{top}, gives num = {_pv(outs, 'num')}")
        return f"{name}: no input active, num = {_pv(outs, 'num')}, f = 0"
    if kind == "Register":
        q = _pv(outs, "Q")
        d = _pv(ins, "D")
        return (f"{name}: holds Q = {q} (loaded on the last clock edge); "
                f"D = {d} waits for the next edge")
    if kind == "ROM":
        return f"{name}: address {_pv(ins, 'A')} reads the word {_pv(outs, 'D')}"
    if kind == "Splitter":
        parts = ", ".join(f"{e['pin']} = {e['text']}" for e in outs)
        return f"{name}: splits {', '.join(e['text'] for e in ins)} into {parts}"
    if kind == "BarrelShifter":
        direction = str(comp.attributes.get("direction", "left") or "left")
        mode = str(comp.attributes.get("barrelShifterMode", "logical") or "logical")
        return (f"{name}: shifts {_pv(ins, 'in')} {direction} by {_pv(ins, 'sh')} "
                f"({mode}) = {_pv(outs, 'out')}")
    if kind == "Comparator":
        return (f"{name}: compares A = {_pv(ins, 'A')} with B = {_pv(ins, 'B')}: "
                f"gr = {_pv(outs, 'gr')}, eq = {_pv(outs, 'eq')}, le = {_pv(outs, 'le')}")
    if kind == "BitExtender":
        return f"{name}: extends {_pv(ins, 'in')} to {_pv(outs, 'out')}"
    if kind == "Seven-Seg":
        lit = [e["pin"] for e in ins if e["value"]]
        return f"{name}: segments {', '.join(lit) if lit else 'none'} lit"
    if kind.endswith(".dig"):
        a = ", ".join(f"{e['pin']} = {e['text']}" for e in ins)
        b = ", ".join(f"{e['pin']} = {e['text']}" for e in outs)
        lead = f"{role} " if role else ""
        return f"{name}: {lead}{a} → {b}"
    if kind in ("Const", "Ground", "VDD"):
        return f"{name}: constant {_pv(outs, 'out')}"
    a = ", ".join(f"{e['pin']} = {e['text']}" for e in ins)
    b = ", ".join(f"{e['pin']} = {e['text']}" for e in outs)
    return f"{name}: {a} → {b}"


def _active_inputs(circuit, idx: int, ins: list[dict]) -> list[dict]:
    comp = circuit.components[idx]
    if comp.element_name == "Multiplexer":
        sel = next((e for e in ins if e["pin"] == "sel"), None)
        if sel is not None and sel["value"] is not None:
            arm = f"in{sel['value']}"
            return [e for e in ins if e["pin"] in ("sel", arm)]
    return ins


def _order(graph, cone: set[int], circuit) -> list[int]:
    sub = nx.DiGraph()
    sub.add_nodes_from(cone)
    for u, v, data in graph.edges(data=True):
        if u in cone and v in cone and u != v:
            if circuit.components[v].element_name in _STATE:
                continue
            sub.add_edge(u, v)
    try:
        order = list(nx.topological_sort(sub))
    except nx.NetworkXUnfeasible:
        while True:
            try:
                cycle = nx.find_cycle(sub)
            except nx.NetworkXNoCycle:
                break
            sub.remove_edge(*cycle[0][:2])
        order = list(nx.topological_sort(sub))
    pos = {n: i for i, n in enumerate(order)}
    for v in list(order):
        if circuit.components[v].element_name not in _STATE:
            continue
        feeders = [u for u, w in graph.in_edges(v) if u in cone and u != v]
        if not feeders:
            continue
        last = max(pos[u] for u in feeders)
        if last > pos[v]:
            order.remove(v)
            order.insert(last, v)
            pos = {n: i for i, n in enumerate(order)}
    return order


def _out_pin_of(pins: _Pins, drv: int, nid: int) -> str | None:
    for pn, d, n in pins.by_comp.get(drv, []):
        if d == "out" and n == nid:
            return pn
    return None


def _expression(circuit, pins: _Pins, idx: int, cone: set[int],
                depth: int, seen: set[int], want: str | None = None) -> str:
    comp = circuit.components[idx]
    kind = comp.element_name
    outs = _pin_entries(pins, pins.outs(idx))
    value = _pv(outs, want) if want else (outs[0]["text"] if outs else "?")
    if kind == "In":
        return f"{comp.label or 'In'}={value}"
    if kind in ("Const", "Ground", "VDD"):
        return value
    if kind == "Clock":
        return "Clock"
    label = comp.label or kind.replace(".dig", "")
    tag = f"{label}[{idx}]"
    pin_tag = f".{want}" if want and len(outs) > 1 else ""
    if kind in _STATE:
        return f"{tag}.{want or 'Q'}={value}"
    if depth >= _MAX_DEPTH or idx in seen:
        return f"{tag}{pin_tag}=…={value}"
    seen = seen | {idx}
    ins = _active_inputs(circuit, idx, _pin_entries(pins, pins.ins(idx)))
    args = []
    for e in ins:
        drv = pins.driver_of_net.get(e["net_id"])
        if drv is None or drv not in cone:
            args.append(f"{e['pin']}={e['text']}")
            continue
        dkind = circuit.components[drv].element_name
        inner = _expression(circuit, pins, drv, cone, depth + 1, seen,
                            _out_pin_of(pins, drv, e["net_id"]))
        if dkind == "In" and not e["pin"].startswith(("sel", "in")):
            args.append(inner)              # "A=5" reads better than "a=A=5"
        else:
            args.append(f"{e['pin']}={inner}")
    return f"{tag}({', '.join(args)}){pin_tag}={value}"


def build_walkthrough(circuit, netlist, graph, spec, row, res: SimResult,
                      bindings, *, roles: dict[str, str] | None = None) -> dict:
    pins = _Pins(circuit, netlist, res)
    col = {h: i for i, h in enumerate(spec.headers)}
    roles = roles or {}

    inputs = []
    for h, b in bindings.items():
        if b is None or b.role not in ("input", "clock") or h not in col:
            continue
        i = col[h]
        tok = row.values[i] if i < len(row.values) else None
        text = tok.raw if tok is not None else "?"
        if tok is not None and tok.kind == "int" and tok.value is not None:
            text = fmt_value(tok.value, b.bit_width)
        inputs.append({"label": h, "text": text, "bits": b.bit_width,
                       "role": b.role})

    outs_by_label = {}
    for idx, comp in enumerate(circuit.components):
        if comp.is_output() and comp.label:
            outs_by_label[comp.label] = idx

    cone: set[int] = set()
    outputs = []
    for h, b in bindings.items():
        if b is None or b.role != "output" or h not in col:
            continue
        out_idx = outs_by_label.get(h)
        if out_idx is None:
            continue
        i = col[h]
        tok = row.values[i] if i < len(row.values) else None
        expected = None
        if tok is not None and tok.kind == "int" and tok.value is not None:
            expected = tok.value
        found = res.output_values.get(h)
        width = b.bit_width or 1
        ok = None
        if expected is not None and found is not None:
            ok = (found & _mask(width)) == (expected & _mask(width))
        c = _active_cone(circuit, netlist, graph, res, out_idx)
        cone |= c
        in_net = pins.pin_net(out_idx, "in")
        drv = pins.driver_of_net.get(in_net) if in_net is not None else None
        expr = (_expression(circuit, pins, drv, c, 0, set(),
                            _out_pin_of(pins, drv, in_net))
                if drv is not None else fmt_value(found, width))
        outputs.append({
            "label": h, "component_index": out_idx,
            "expected": fmt_value(expected, width) if expected is not None else None,
            "found": fmt_value(found, width) if found is not None else None,
            "ok": ok,
            "expression": f"{h} = {expr}",
        })

    steps = []
    wave_of: dict[int, int] = {}
    for idx in _order(graph, cone, circuit):
        comp = circuit.components[idx]
        kind = comp.element_name
        if kind in _SKIP or kind in _SOURCES or comp.is_output():
            continue
        ins = _active_inputs(circuit, idx, _pin_entries(pins, pins.ins(idx)))
        outs = _pin_entries(pins, pins.outs(idx))
        role = roles.get(kind) if kind.endswith(".dig") else None
        wave = 1
        for e in ins:
            drv = pins.driver_of_net.get(e["net_id"])
            if drv in wave_of and circuit.components[drv].element_name not in _STATE:
                wave = max(wave, wave_of[drv] + 1)
        wave_of[idx] = wave
        steps.append({
            "component_index": idx,
            "element": kind,
            "label": comp.label or "",
            "wave": wave,
            "inputs": ins,
            "outputs": outs,
            "text": _step_text(circuit, idx, ins, outs, pins, role),
        })
    steps.sort(key=lambda s: s["wave"])
    notes = []
    if len(steps) > _MAX_STEPS:
        notes.append(f"{len(steps) - _MAX_STEPS} later step(s) folded — the "
                     f"expression still names the whole path.")
        steps = steps[:_MAX_STEPS]
    if not steps:
        notes.append("no component lies between the inputs and the outputs "
                     "on this row.")
    return {
        "row_index": row.line_index,
        "raw": row.raw,
        "inputs": inputs,
        "outputs": outputs,
        "valid": all(o["ok"] is not False for o in outputs),
        "sources": [i for i, c in enumerate(circuit.components)
                    if c.element_name in _SOURCES or c.element_name in _STATE],
        "steps": steps,
        "waves": max((s["wave"] for s in steps), default=0),
        "notes": notes,
    }
