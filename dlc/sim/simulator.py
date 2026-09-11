"""
Combinational value evaluator.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from dlc.parser.models import Circuit
from dlc.parser.netlist import NetList, build_netlist
from dlc.facts.net_width import infer_net_widths
from dlc.facts.splitter import parse_splitting
from dlc.parser.pin_geometry import inverted_input_names

# never given an assumed value: they carry no logic of their own
_NEVER_ASSUMED = frozenset({
    "Tunnel", "Testcase", "Text", "Rectangle", "Probe", "PullUp", "PullDown",
    "Seven-Seg", "LED", "Out", "In", "Clock", "Const", "Ground", "VDD",
})

_UNMODELED = frozenset({
    "Register", "Counter", "Memory",
    "RAMDualPort", "RAMSinglePort", "D_FF", "T_FF", "JK_FF", "FlipflopD",
})

@dataclass
class SimResult:
    net_values: dict[int, int] = field(default_factory=dict)
    net_bits: dict[int, int] = field(default_factory=dict)
    unresolved_nets: set[int] = field(default_factory=set)
    output_values: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    reg_next: dict[tuple[int, ...], int] = field(default_factory=dict)
    # net -> why its value was assumed rather than computed (only when the
    # caller passed an `assume` policy; Layer 1 never does)
    assumed: dict[int, str] = field(default_factory=dict)


def _mask(bits: int) -> int:
    return (1 << bits) - 1 if bits and bits > 0 else 0


def _as_int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def inputs_for_row(circuit: Circuit, headers: list[str], row) -> dict[str, int]:
    from dlc.testing.spec import match_variables_to_io

    bindings = match_variables_to_io(headers, circuit)
    assignment: dict[str, int] = {}
    values = getattr(row, "values", None) or []
    for col, header in enumerate(headers):
        if col >= len(values):
            break
        binding = bindings.get(header)
        if binding is None:
            continue
        tok = values[col]
        if getattr(tok, "kind", None) != "int" or tok.value is None:
            continue
        if binding.role in ("input", "clock"):
            width = binding.bit_width or 1
            assignment[header] = tok.value & _mask(width)
    return assignment


def _gate_bits(comp) -> int:
    return max(1, _as_int(comp.attributes.get("Bits", 1), 1))


def _eval_gate(comp, in_vals: dict[str, int]) -> dict[str, int] | None:
    bits = _gate_bits(comp)
    mask = _mask(bits)
    n = _as_int(comp.attributes.get("Inputs", 2), 2)
    inverted = set(inverted_input_names(comp))

    operands: list[int] = []
    for i in range(n):
        name = f"in{i}"
        if name not in in_vals:
            return None
        v = in_vals[name] & mask
        if name in inverted:
            v = (~v) & mask
        operands.append(v)
    if not operands:
        return None

    base = comp.element_name
    acc = operands[0]
    if base in ("And", "NAnd"):
        for v in operands[1:]:
            acc &= v
    elif base in ("Or", "NOr"):
        for v in operands[1:]:
            acc |= v
    elif base in ("XOr", "XNOr"):
        for v in operands[1:]:
            acc ^= v
    else:
        return None
    if base in ("NAnd", "NOr", "XNOr"):
        acc = (~acc) & mask
    return {"Y": acc & mask}


def _eval_not(comp, in_vals):
    if "A" not in in_vals:
        return None
    bits = _gate_bits(comp)
    return {"Y": (~in_vals["A"]) & _mask(bits)}


def _eval_const(comp, _in_vals):
    bits = _gate_bits(comp)
    return {"out": _as_int(comp.attributes.get("Value", 1), 1) & _mask(bits)}


def _eval_ground(comp, _in_vals):
    return {"out": 0}


def _eval_vdd(comp, _in_vals):
    return {"out": _mask(_gate_bits(comp))}


def _eval_mux(comp, in_vals):
    sel_bits = _as_int(comp.attributes.get("Selector Bits", 1), 1)
    if "sel" not in in_vals:
        return None
    sel = in_vals["sel"] & _mask(sel_bits)
    chosen = f"in{sel}"
    if chosen not in in_vals:
        return None
    return {"out": in_vals[chosen]}


def _eval_decoder(comp, in_vals):
    sel_bits = _as_int(comp.attributes.get("Selector Bits", 1), 1)
    n_out = 2 ** sel_bits
    if "sel" not in in_vals:
        return None
    sel = in_vals["sel"] & _mask(sel_bits)
    return {f"out_{i}": (1 if i == sel else 0) for i in range(n_out)}


def _eval_demux(comp, in_vals):
    sel_bits = _as_int(comp.attributes.get("Selector Bits", 1), 1)
    n_out = 2 ** sel_bits
    if "sel" not in in_vals or "in" not in in_vals:
        return None
    sel = in_vals["sel"] & _mask(sel_bits)
    return {f"out_{i}": (in_vals["in"] if i == sel else 0)
            for i in range(n_out)}


def _eval_priority_encoder(comp, in_vals):
    sel_bits = _as_int(comp.attributes.get("Selector Bits", 1), 1)
    n_in = 2 ** sel_bits
    highest = None
    for i in range(n_in):
        name = f"in_{i}"
        if name in in_vals and in_vals[name]:
            highest = i
    return {"num": highest if highest is not None else 0,
            "f": 0 if highest is None else 1}


def _eval_splitter(comp, in_vals):
    in_split = str(comp.attributes.get("Input Splitting", "1"))
    out_split = str(comp.attributes.get("Output Splitting", "1"))
    try:
        in_groups = parse_splitting(in_split)
        out_groups = parse_splitting(out_split)
    except ValueError:
        return None
    bus = 0
    for i, grp in enumerate(in_groups):
        name = f"in{i}"
        if name not in in_vals:
            return None
        v = in_vals[name] & _mask(grp.width)
        bus |= v << grp.bit_lo
    out: dict[str, int] = {}
    for i, grp in enumerate(out_groups):
        out[f"out{i}"] = (bus >> grp.bit_lo) & _mask(grp.width)
    return out


def _eval_add(comp, in_vals):
    bits = _gate_bits(comp)
    mask = _mask(bits)
    a = in_vals.get("a", 0) & mask
    b = in_vals.get("b", 0) & mask
    c_i = in_vals.get("c_i", 0) & 1
    total = a + b + c_i
    return {"s": total & mask, "c_o": (total >> bits) & 1}


def _eval_comparator(comp, in_vals):
    if "A" not in in_vals or "B" not in in_vals:
        return None
    a, b = in_vals["A"], in_vals["B"]
    if comp.attributes.get("Signed"):
        # Digital's Signed comparator reads A/B as two's complement at Bits
        bits = int(comp.attributes.get("Bits", 1) or 1)
        mask = (1 << bits) - 1
        top = 1 << (bits - 1)
        a &= mask
        b &= mask
        if a & top:
            a -= 1 << bits
        if b & top:
            b -= 1 << bits
    return {
        "gr": 1 if a > b else 0,
        "eq": 1 if a == b else 0,
        "le": 1 if a < b else 0,
    }

def _rom_words(comp) -> list[int]:
    """
    Parse a ROM's Data field into the word stored at each address.
    """
    raw = comp.attributes.get("Data", "")
    if not isinstance(raw, str):
        raw = "" if raw is None else str(raw)
    tokens = [t for t in raw.replace(",", " ").split() if t]
    fmt = str(comp.attributes.get("intFormat", "hex") or "hex").lower()
    base = {"hex": 16, "bin": 2, "oct": 8, "dec": 10, "def": 10}.get(fmt, 16)

    def _parse_one(t: str) -> int:
        try:
            return int(t, base)
        except ValueError:
            try:
                return int(t, 16)
            except ValueError:
                return 0

    words: list[int] = []
    for t in tokens:
        if "*" in t:
            cnt_s, _, val_s = t.partition("*")
            try:
                cnt = int(cnt_s, 10)
            except ValueError:
                cnt = 1
            words.extend([_parse_one(val_s)] * max(cnt, 1))
        else:
            words.append(_parse_one(t))
    return words


def _eval_rom(comp, in_vals):
    if "A" not in in_vals:
        return None
    if in_vals.get("sel", 1) == 0:
        return {"D": 0}
    words = _rom_words(comp)
    addr = in_vals["A"]
    val = words[addr] if 0 <= addr < len(words) else 0
    return {"D": val & _mask(_gate_bits(comp))}

def _eval_barrel_shifter(comp, in_vals):
    if "in" not in in_vals or "sh" not in in_vals:
        return None
    bits = _gate_bits(comp)
    mask = _mask(bits)
    x = in_vals["in"] & mask
    sh = in_vals["sh"]
    direction = str(comp.attributes.get("direction", "left") or "left").lower()
    mode = str(comp.attributes.get("barrelShifterMode", "logical") or "logical").lower()
    if mode == "rotate" and bits:
        s = sh % bits
        if direction == "right":
            out = ((x >> s) | (x << (bits - s))) & mask
        else:
            out = ((x << s) | (x >> (bits - s))) & mask
    elif direction == "right":
        if mode == "arithmetic":
            sx = x - (1 << bits) if (x >> (bits - 1)) & 1 else x
            out = (sx >> min(sh, bits)) & mask
        else:
            out = (x >> sh) & mask if sh < bits else 0
    else:  # left, logical
        out = (x << sh) & mask if sh < bits else 0
    return {"out": out}


def _eval_bitextender(comp, in_vals):
    if "in" not in in_vals:
        return None
    in_bits = max(1, _as_int(comp.attributes.get("inputBits", 1), 1))
    out_bits = max(in_bits, _as_int(comp.attributes.get("outputBits", in_bits), in_bits))
    x = in_vals["in"] & _mask(in_bits)
    if (x >> (in_bits - 1)) & 1:  # sign bit set -> extend ones
        x |= (~_mask(in_bits)) & _mask(out_bits)
    return {"out": x & _mask(out_bits)}



_RULES = {
    "And": _eval_gate, "Or": _eval_gate, "XOr": _eval_gate,
    "NAnd": _eval_gate, "NOr": _eval_gate, "XNOr": _eval_gate,
    "Not": _eval_not,
    "Const": _eval_const, "Ground": _eval_ground, "VDD": _eval_vdd,
    "Multiplexer": _eval_mux,
    "Demultiplexer": _eval_demux,
    "Decoder": _eval_decoder,
    "PriorityEncoder": _eval_priority_encoder,
    "Splitter": _eval_splitter,
    "Add": _eval_add,
    "Comparator": _eval_comparator,
    "ROM": _eval_rom,
    "BarrelShifter": _eval_barrel_shifter,
    "BitExtender": _eval_bitextender,
}


def _build_child_by_index(circuit: Circuit) -> dict[int, Circuit]:
    out: dict[int, Circuit] = {}
    for sub_ref in circuit.subcircuits:
        if sub_ref.child_circuit is None:
            continue
        for idx, comp in enumerate(circuit.components):
            if comp is sub_ref.parent_component:
                out[idx] = sub_ref.child_circuit
                break
    return out


def simulate(
    circuit: Circuit,
    netlist: NetList,
    graph,
    inputs: dict[str, int],
    *,
    state: dict[int, int] | None = None,
    state_store: dict[tuple[int, ...], int] | None = None,
    path: tuple[int, ...] = (),
    capture_path: tuple[int, ...] | None = None,
    capture_box: dict | None = None,
    model_resolver=None,
    assume=None,
    _depth: int = 0,
    _max_depth: int = 16,
) -> SimResult:
    result = SimResult()
    if state_store is None:
        state_store = {}
    if state:
        for k, v in state.items():
            key = k if isinstance(k, tuple) else path + (k,)
            state_store.setdefault(key, v)

    def reg_state(idx: int) -> int:
        return state_store.get(path + (idx,), 0)

    def regfile_state(idx: int) -> dict:
        v = state_store.get(path + (idx,))
        return v if isinstance(v, dict) else {}

    per_net, _conflicts = infer_net_widths(circuit, netlist)
    for nid, info in per_net.items():
        if info.width is not None:
            result.net_bits[nid] = info.width

    comp_pins: dict[int, list[tuple[str, str, int]]] = defaultdict(list)
    for net in netlist.nets:
        for p in net.pins:
            comp_pins[p.component_index].append((p.pin_name, p.direction, net.net_id))

    net_values = result.net_values
    child_by_index = _build_child_by_index(circuit)
    have_unmodeled = set()

    net_strength: dict[int, str] = {}

    def set_net(nid: int, val: int, strength: str = "strong") -> bool:
        cur = net_strength.get(nid)
        if nid in net_values:
            if not (strength == "strong" and cur == "weak"):
                return False
        bits = result.net_bits.get(nid)
        v = (val & _mask(bits)) if bits else val
        if nid in net_values and net_values[nid] == v and cur == strength:
            return False
        net_values[nid] = v
        net_strength[nid] = strength
        return True

    def pin_net(idx: int, pin_name: str) -> int | None:
        for name, _d, nid in comp_pins.get(idx, []):
            if name == pin_name:
                return nid
        return None

    for idx, comp in enumerate(circuit.components):
        pins = comp_pins.get(idx, [])
        if comp.is_input():
            label = comp.label
            if label in inputs:
                for name, direction, nid in pins:
                    if direction == "out":
                        set_net(nid, inputs[label])
        elif comp.element_name in ("Const", "Ground", "VDD"):
            rule = _RULES[comp.element_name]
            outs = rule(comp, {})
            for name, direction, nid in pins:
                if direction == "out" and outs and name in outs:
                    set_net(nid, outs[name])
        elif comp.element_name == "Register":
            q_net = pin_net(idx, "Q")
            if q_net is not None:
                set_net(q_net, reg_state(idx))

    fet_idxs = [i for i, c in enumerate(circuit.components)
                if c.element_name in ("NFET", "PFET")]
    pull_idxs = [i for i, c in enumerate(circuit.components)
                 if c.element_name in ("PullUp", "PullDown")]

    def _switch_pass() -> bool:
        if not fet_idxs and not pull_idxs:
            return False
        changed = False
        parent: dict[int, int] = {}

        def find(x: int) -> int:
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        member_nets: set[int] = set()
        for i in fet_idxs:
            comp = circuit.components[i]
            nets = {name: nid for name, _d, nid in comp_pins.get(i, [])}
            a, b = nets.get("d"), nets.get("s")
            if a is not None:
                member_nets.add(a)
            if b is not None:
                member_nets.add(b)
            g = nets.get("g")
            if g is None or g not in net_values or a is None or b is None:
                continue
            gv = net_values[g]
            on = (gv != 0) if comp.element_name == "NFET" else (gv == 0)
            if on:
                union(a, b)

        weak_by_net: dict[int, int] = {}
        for i in pull_idxs:
            comp = circuit.components[i]
            bits = max(1, _as_int(comp.attributes.get("Bits", 1), 1))
            weak_val = _mask(bits) if comp.element_name == "PullUp" else 0
            for _name, _d, nid in comp_pins.get(i, []):
                member_nets.add(nid)
                weak_by_net[nid] = weak_val

        groups: dict[int, list[int]] = defaultdict(list)
        for nid in member_nets:
            groups[find(nid)].append(nid)

        for members in groups.values():
            strong_vals = {
                net_values[n] for n in members
                if n in net_values and net_strength.get(n) == "strong"
            }
            if len(strong_vals) == 1:
                v = strong_vals.pop()
                for n in members:
                    if set_net(n, v):
                        changed = True
                continue
            if strong_vals:
                continue
            weak_vals = {weak_by_net[n] for n in members if n in weak_by_net}
            weak_vals |= {
                net_values[n] for n in members
                if n in net_values and net_strength.get(n) == "weak"
            }
            if len(weak_vals) == 1:
                v = weak_vals.pop()
                for n in members:
                    if n not in net_values:
                        if set_net(n, v, strength="weak"):
                            changed = True
        return changed

    max_iters = len(circuit.components) + 4
    sub_cache: dict[int, tuple] = {}

    stuck_pins: dict[int, list[tuple[str, int]]] = {}

    def _propagate() -> None:
      for _ in range(max_iters):
        changed = False
        stuck_pins.clear()
        for idx, comp in enumerate(circuit.components):
            pins = comp_pins.get(idx, [])
            if not pins:
                continue
            out_nets = [nid for name, d, nid in pins if d == "out"]
            if (out_nets and all(nid in net_values for nid in out_nets)
                    and not comp.element_name.endswith(".dig")):
                continue

            in_vals: dict[str, int] = {}
            unresolved_input = False
            for name, direction, nid in pins:
                if direction == "in":
                    if nid in net_values:
                        in_vals[name] = net_values[nid]
                    else:
                        unresolved_input = True
            is_subcircuit = comp.element_name.endswith(".dig")
            is_regfile = comp.element_name == "RegisterFile"
            if unresolved_input and not is_subcircuit and not is_regfile:
                continue

            if is_regfile:
                bits = _as_int(comp.attributes.get("Bits", 1), 1)
                abits = _as_int(comp.attributes.get("AddrBits", 3), 3)
                mem = regfile_state(idx)
                outs = {}
                if "Ra" in in_vals:
                    outs["Da"] = mem.get(in_vals["Ra"] & _mask(abits), 0) & _mask(bits)
                if "Rb" in in_vals:
                    outs["Db"] = mem.get(in_vals["Rb"] & _mask(abits), 0) & _mask(bits)
                for name, direction, nid in pins:
                    if direction == "out" and name in outs:
                        if set_net(nid, outs[name]):
                            changed = True
                continue

            if is_subcircuit:
                sig = frozenset(in_vals.items())
                cached = sub_cache.get(idx)
                child_path = path + (idx,)
                on_capture_route = (
                    capture_path is not None
                    and capture_path[:len(child_path)] == child_path)
                model = None
                if model_resolver is not None and not on_capture_route:
                    child_c = child_by_index.get(idx)
                    if child_c is not None:
                        model = model_resolver(child_c)
                if (cached is not None and cached[0] == sig
                        and not on_capture_route):
                    outs, child_rn = cached[1], cached[2]
                elif model is not None:
                    outs, child_rn = _eval_model(
                        model, in_vals, state_store.get(child_path), child_path)
                    sub_cache[idx] = (sig, outs, child_rn)
                else:
                    outs, child_rn = _eval_subcircuit(
                        comp, idx, in_vals, child_by_index, _depth, _max_depth,
                        state_store, path, capture_path, capture_box,
                        model_resolver=model_resolver, assume=assume)
                    sub_cache[idx] = (sig, outs, child_rn)
                if child_rn:
                    result.reg_next.update(child_rn)
            else:
                outs = _eval_node(comp, idx, in_vals, child_by_index,
                                  _depth, _max_depth)
            if outs is None:
                if comp.element_name in _UNMODELED or comp.element_name.endswith(".dig"):
                    have_unmodeled.add(comp.element_name)
                stuck_pins[idx] = [(name, nid) for name, d, nid in pins
                                   if d == "out" and nid not in net_values]
                continue
            for name, direction, nid in pins:
                if direction == "out" and name in outs:
                    if set_net(nid, outs[name]):
                        changed = True
            missing = [(name, nid) for name, d, nid in pins
                       if d == "out" and name not in outs and nid not in net_values]
            if missing:
                stuck_pins[idx] = missing
        if _switch_pass():
            changed = True
        if not changed:
            break

    def _assumption_pass() -> bool:
        made = False
        for net in netlist.nets:
            nid = net.net_id
            if nid in net_values or not net.sinks() or net.drivers():
                continue
            stuck = [p for p in net.pins if p.direction not in ("in", "out")
                     and circuit.components[p.component_index].element_name
                     not in _NEVER_ASSUMED]
            comp_idx = stuck[0].component_index if stuck else None
            comp = circuit.components[comp_idx] if comp_idx is not None else None
            got = assume({"comp": comp, "idx": comp_idx,
                          "pin": stuck[0].pin_name if stuck else None,
                          "nid": nid, "bits": result.net_bits.get(nid, 1),
                          "path": path, "in_vals": {}, "floating": True,
                          "values": net_values})
            if got is not None:
                val, why = got
                if set_net(nid, val):
                    result.assumed[nid] = why
                    made = True
        if made:
            return True
        for idx, outs_unknown in list(stuck_pins.items()):
            comp = circuit.components[idx]
            if comp.element_name in _NEVER_ASSUMED or comp.is_output():
                continue
            pins = comp_pins.get(idx, [])
            in_vals = {name: net_values[nid] for name, d, nid in pins
                       if d == "in" and nid in net_values}
            for name, nid in outs_unknown:
                if nid in net_values:
                    continue
                got = assume({"comp": comp, "idx": idx, "pin": name, "nid": nid,
                              "bits": result.net_bits.get(nid, 1), "path": path,
                              "in_vals": in_vals, "floating": False,
                              "values": net_values})
                if got is None:
                    continue
                val, why = got
                if set_net(nid, val):
                    result.assumed[nid] = why
                    made = True
        return made

    _propagate()
    if assume is not None:
        for _ in range(max_iters):
            if not _assumption_pass():
                break
            _propagate()
    has_register = False
    for idx, comp in enumerate(circuit.components):
        if comp.element_name == "RegisterFile":
            has_register = True
            cur = regfile_state(idx)
            din_net = pin_net(idx, "Din")
            rw_net = pin_net(idx, "Rw")
            we_net = pin_net(idx, "we")
            if (din_net is not None and din_net in net_values
                    and rw_net is not None and rw_net in net_values):
                enabled = True
                if we_net is not None and we_net in net_values:
                    enabled = bool(net_values[we_net])
                if enabled:
                    bits = _as_int(comp.attributes.get("Bits", 1), 1)
                    abits = _as_int(comp.attributes.get("AddrBits", 3), 3)
                    nxt = dict(cur)
                    nxt[net_values[rw_net] & _mask(abits)] = (
                        net_values[din_net] & _mask(bits))
                    result.reg_next[path + (idx,)] = nxt
                else:
                    result.reg_next[path + (idx,)] = cur
            continue
        if comp.element_name != "Register":
            continue
        has_register = True
        d_net = pin_net(idx, "D")
        en_net = pin_net(idx, "en")
        if d_net is not None and d_net in net_values:
            enabled = True
            if en_net is not None and en_net in net_values:
                enabled = bool(net_values[en_net])
            result.reg_next[path + (idx,)] = (
                net_values[d_net] if enabled else reg_state(idx))

    for idx, comp in enumerate(circuit.components):
        if comp.is_output():
            for name, direction, nid in comp_pins.get(idx, []):
                if direction == "in" and nid in net_values:
                    result.output_values[comp.label or f"out_{idx}"] = net_values[nid]

    for net in netlist.nets:
        if net.net_id in net_values:
            continue
        if net.drivers() and net.sinks():
            result.unresolved_nets.add(net.net_id)

    if has_register and result.unresolved_nets and not state:
        result.notes.append(
            "Registers/clocked state not evaluated (combinational-only); "
            "click through rows in order for register values to fill in."
        )
    return result


def simulate_sequential(
    circuit: Circuit,
    netlist: NetList,
    graph,
    spec,
    row_index: int,
    *,
    capture_path: tuple[int, ...] | None = None,
    capture_box: dict | None = None,
    model_resolver=None,
) -> SimResult:
    rows = [r for r in spec.rows if not r.is_malformed]
    target = None
    for r in rows:
        if r.line_index == row_index:
            target = r
            break
    if target is None:
        return SimResult()

    reg_state: dict[tuple[int, ...], int] = {}

    def apply_row(row, cap_path=None, cap_box=None) -> SimResult:
        nonlocal reg_state
        inp = inputs_for_row(circuit, spec.headers, row)
        clocked = _row_has_clock_edge(circuit, spec.headers, row)
        res = simulate(circuit, netlist, graph, inp, state_store=dict(reg_state),
                       capture_path=None if clocked else cap_path,
                       capture_box=None if clocked else cap_box,
                       model_resolver=model_resolver)
        if clocked:
            new_state = dict(reg_state)
            new_state.update(res.reg_next)
            reg_state = new_state
            res = simulate(circuit, netlist, graph, inp,
                           state_store=dict(reg_state),
                           capture_path=cap_path, capture_box=cap_box,
                           model_resolver=model_resolver)
        return res

    result = SimResult()
    for row in rows:
        is_target = row.line_index == row_index
        result = apply_row(row,
                           capture_path if is_target else None,
                           capture_box if is_target else None)
        if is_target:
            break
    return result


class RowReplay:

    def __init__(self, circuit, netlist, graph, spec, *, model_resolver=None,
                 assume=None):
        self.circuit = circuit
        self.netlist = netlist
        self.graph = graph
        self.spec = spec
        self.model_resolver = model_resolver
        self.assume = assume
        self.rows = [r for r in spec.rows if not r.is_malformed]
        self.results: dict[int, SimResult] = {}
        self._pos = 0
        self._reg_state: dict[tuple[int, ...], int] = {}

    def _step(self) -> None:
        row = self.rows[self._pos]
        inp = inputs_for_row(self.circuit, self.spec.headers, row)
        clocked = _row_has_clock_edge(self.circuit, self.spec.headers, row)
        res = simulate(self.circuit, self.netlist, self.graph, inp,
                       state_store=dict(self._reg_state),
                       model_resolver=self.model_resolver, assume=self.assume)
        if clocked:
            new_state = dict(self._reg_state)
            new_state.update(res.reg_next)
            self._reg_state = new_state
            res = simulate(self.circuit, self.netlist, self.graph, inp,
                           state_store=dict(self._reg_state),
                           model_resolver=self.model_resolver, assume=self.assume)
        self.results[row.line_index] = res
        self._pos += 1

    def upto(self, row_index: int) -> SimResult:
        if row_index not in self.results:
            wanted = any(r.line_index == row_index for r in self.rows)
            if not wanted:
                return SimResult()
            while self._pos < len(self.rows) and row_index not in self.results:
                self._step()
        return self.results.get(row_index, SimResult())


def simulate_rows(circuit, netlist, graph, spec, row_indices=None, *,
                  model_resolver=None) -> dict[int, SimResult]:
    """Post-edge results for `row_indices` (default: every row), one replay."""
    replay = RowReplay(circuit, netlist, graph, spec,
                       model_resolver=model_resolver)
    if row_indices is None:
        row_indices = [r.line_index for r in replay.rows]
    if not row_indices:
        return {}
    replay.upto(max(row_indices))
    return {i: replay.results[i] for i in row_indices if i in replay.results}


def _row_has_clock_edge(circuit, headers, row) -> bool:
    from dlc.testing.spec import match_variables_to_io

    bindings = match_variables_to_io(headers, circuit)
    values = getattr(row, "values", None) or []
    for col, header in enumerate(headers):
        if col >= len(values):
            break
        binding = bindings.get(header)
        if binding is not None and binding.role == "clock":
            if getattr(values[col], "kind", None) == "clock":
                return True
    return False

def _eval_node(comp, idx, in_vals, child_by_index, depth, max_depth):
    rule = _RULES.get(comp.element_name)
    if rule is not None:
        return rule(comp, in_vals)
    return None


def _eval_model(model, in_vals, state, child_path):
    ev = model.evaluate(in_vals, state)
    if ev is None:
        return None, {}
    outs, nxt = ev
    return outs, ({child_path: nxt} if model.stateful else {})


def _eval_subcircuit(comp, idx, in_vals, child_by_index, depth, max_depth,
                     state_store, path, capture_path=None, capture_box=None,
                     model_resolver=None, assume=None):
    child = child_by_index.get(idx)
    if child is None or depth >= max_depth:
        return None, {}
    child_path = path + (idx,)
    want = (capture_path is not None
            and capture_path[:len(child_path)] == child_path)
    try:
        child_nl = getattr(child, "_sim_nl", None)
        if child_nl is None:
            from dlc.parser.graph import build_signal_graph
            child_nl = build_netlist(child)
            child._sim_nl = child_nl
            child._sim_g = build_signal_graph(child, child_nl)
        child_g = child._sim_g
        sub = simulate(
            child, child_nl, child_g, dict(in_vals),
            state_store=state_store, path=child_path,
            capture_path=capture_path if want else None,
            capture_box=capture_box if want else None,
            model_resolver=model_resolver, assume=assume,
            _depth=depth + 1, _max_depth=max_depth,
        )
    except Exception:
        return None, {}
    if want and capture_box is not None and child_path == capture_path:
        capture_box["result"] = sub
    outs = dict(sub.output_values) if sub.output_values else None
    return outs, dict(sub.reg_next)
