"""
Conceptual circuit-summary generator:

Gates BEFORE calling the LLM:
  G1. Many L1 errors -> "fix structural issues first".
  G2. A few L1 issues remain -> precheck text listing top items.
  G3. Tests failed -> guide to Layer 3 debug.

If none fire, builds a prompt from compact CircuitFacts + the
student goal + the COMP 311 syllabus, calls LLM, sanitizes the
response.
"""

import json
from pathlib import Path

from dlc.llm.client import call_llm
from dlc.llm.guard import sanitize_output


_PROMPT_DIR = Path(__file__).parent.parent.parent / "prompts"


def _load_prompt(name: str) -> str:
    return (_PROMPT_DIR / name).read_text(encoding="utf-8")


SYLLABUS_311 = """\
Lecture 4: Transistors, Intro to CMOS Gates
Lecture 5: Logic Gates
Lecture 6-7: Multiplexers, Gate Minimization 
Lecture 8: Karnaugh Maps (K-Maps)
Lecture 9: Circuit Design: A Priority Encoder, Basic Adder Circuits
Lecture 10: Arithmetic Circuits (Adder/Subtractors, Shifters), R-Type and I-Type Instructions
Lecture 11: Introducing the ALU
Lecture 12: Building the ALU, The Register File
Lecture 13: Intro to Timing Analysis
Lecture 14: Computer Performance Metrics, More Timing Analysis
Lecture 15: Intro to Pipelining
Lecture 16: Loads, Stores, Memory
Lecture 17: More RISC-V Programming, Pipelining a CPU
Lecture 18: The 5 Stage RISC-V CPU
Lecture 19: RISC-V Programming with Procedures
Lecture 20: More Stack Review and RISC-V Calling Convention
Lecture 21: Pipeline Hazards
Lecture 22: Source Bypassing/Forwarding
Lecture 23: Intro to Memory
Lecture 24: Caching, RAM
Lecture 25: Caching, Stack Review, Hardware Security Conceptual expanding
"""

_SELECTOR_TYPES = {"Multiplexer", "Demultiplexer", "Decoder", "PriorityEncoder"}


def _selector_facts(facts: dict) -> list[dict]:
    comps = facts.get("components", []) or []
    nets = facts.get("nets", []) or []

    def name(idx):
        if not isinstance(idx, int) or idx < 0 or idx >= len(comps):
            return None
        c = comps[idx]
        return c.get("label") or c.get("element_name")

    net_driver = []
    for net in nets:
        drv = None
        for p in net.get("pins", []):
            if p.get("direction") == "out":
                drv = name(p.get("component_index"))
                break
        net_driver.append(drv)

    out = []
    for i, c in enumerate(comps):
        if c.get("element_name") not in _SELECTOR_TYPES:
            continue
        sel_drv = None
        data = {}
        for ni, net in enumerate(nets):
            for p in net.get("pins", []):
                if p.get("component_index") == i and p.get("direction") == "in":
                    pn = p.get("pin_name") or "?"
                    if pn == "sel":
                        sel_drv = net_driver[ni]
                    else:
                        data[pn] = net_driver[ni]
        if data or sel_drv:
            out.append({
                "selector": c.get("label") or c.get("element_name"),
                "select_driven_by": sel_drv,
                "data_inputs": dict(sorted(data.items())),
            })
    return out



def _io_compact(p: dict) -> dict:
    bits = p.get("bit_width")
    if bits is None:
        bits = p.get("bits")
    return {"label": p.get("label"), "bits": bits}


def _subcircuit_compact(s: dict) -> dict:
    if "resolved_path" in s or "resolution_error" in s:
        resolved = s.get("resolved_path") is not None
    else:
        resolved = s.get("resolved")
    out = {
        "reference": s.get("reference"),
        "resolved": resolved,
        "resolution_error": s.get("resolution_error") or s.get("error"),
    }
    if s.get("child_inputs") is not None:
        out["child_inputs"] = [_io_compact(c) for c in s.get("child_inputs")]
    if s.get("child_outputs") is not None:
        out["child_outputs"] = [_io_compact(c) for c in s.get("child_outputs")]
    if s.get("role"):
        out["role"] = s["role"]
    return out


def attach_subcircuit_roles(facts: dict, circuit, manifest: dict | None) -> list[dict]:
    from dlc.sim.models import role_for
    children = {}
    for ref in getattr(circuit, "subcircuits", []) or []:
        if ref.child_circuit is not None and ref.reference not in children:
            children[ref.reference] = ref.child_circuit
    roles: list[dict] = []
    seen: set[str] = set()
    for s in facts.get("subcircuits", []) or []:
        ref = s.get("reference")
        if not ref or ref in seen:
            continue
        seen.add(ref)
        child = children.get(ref)
        role = role_for(child, manifest, ref) if child is not None else None
        if role:
            s["role"] = role
            roles.append({"reference": ref, "role": role})
    return roles


def format_example_row(example_row: dict | None) -> str:
    if not example_row:
        return "(none)"
    cols = example_row.get("columns") or []
    cells = str(example_row.get("raw", "")).split("#", 1)[0].split()
    pairs = " ".join(f"{c}={v}" for c, v in zip(cols, cells))
    return (f"row {example_row.get('row_index')} of testcase "
            f"'{example_row.get('spec_name', '')}': {pairs}")


_SWITCH_LEVEL_ELEMENTS = ("NFET", "PFET", "PullUp", "PullDown")


def _switch_level_block(inventory: dict) -> dict | None:
    present = {k: inventory[k] for k in _SWITCH_LEVEL_ELEMENTS
               if k in (inventory or {})}
    if not present:
        return None
    return {
        "elements": present,
        "semantics": (
            "Switch-level (transistor) circuit. An NFET conducts while "
            "its gate is 1; a PFET conducts while its gate is 0. "
            "PullUp/PullDown are WEAK drivers setting a node's resting "
            "value; any conducting transistor overrides them. FET "
            "channel pins appear as 'bidir' in the net facts, and the "
            "directed topology graph does not trace through channels — "
            "reason about signal flow from the nets, not the graph."
        ),
    }


def _compact_facts(facts: dict) -> dict:
    inventory = {
        k: v for k, v in (facts.get("inventory", {}) or {}).items()
        if k != "Text"
    }
    switch_level = _switch_level_block(inventory)
    extra = {"switch_level": switch_level} if switch_level else {}
    return {
        **extra,
        "inventory": inventory,
        "inputs": [_io_compact(p) for p in facts.get("inputs", [])],
        "outputs": [_io_compact(p) for p in facts.get("outputs", [])],
        "subcircuits": [
            _subcircuit_compact(s) for s in facts.get("subcircuits", [])
        ],
        "has_clock": "Clock" in (facts.get("inventory", {}) or {}),
        "has_register": "Register" in (facts.get("inventory", {}) or {}),
        "has_rom": "ROM" in (facts.get("inventory", {}) or {}),
        "roms": [
            {"label": r.get("label"),
             "addr_bits": r.get("addr_bits"),
             "data_bits": r.get("data_bits"),
             "int_format": r.get("int_format"),
             "word_count": r.get("word_count"),
             "words_at_addresses": (r.get("words_preview") or [])[:12]}
            for r in facts.get("roms", [])
        ],
        "testcases": [
            {"label": t.get("label"),
             "columns": t.get("columns"),
             "line_count": t.get("line_count"),
             "rows_sample": (t.get("rows_sample") or [])[:20]}
            for t in facts.get("testcases", [])
        ],
        "inverted_inputs": [
            {"component": c.get("display_name") or c.get("element_name"),
             "pins": c.get("inverted_inputs")}
            for c in facts.get("components", [])
            if c.get("inverted_inputs")
        ],
        "selectors": _selector_facts(facts),
    }


_GATE_FIX_FIRST = 3
_GATE_SOFT_LIMIT = 3


def _classify_issues(issues: list[dict]) -> tuple[int, int]:
    n_err = sum(1 for i in issues if i.get("severity") == "error")
    n_warn = sum(1 for i in issues if i.get("severity") == "warning")
    return n_err, n_warn


def _gate_text_for_issues(issues: list[dict]) -> str | None:
    n_err, n_warn = _classify_issues(issues)
    if n_err >= _GATE_FIX_FIRST:
        return (
            f"Your circuit has {n_err} Layer 1 errors and {n_warn} "
            f"warnings. Fix the structural issues on the Dashboard tab "
            f"before asking Layer 2 for a conceptual summary - major "
            f"structural bugs make any functional explanation unreliable."
        )
    if n_err > 0 or n_warn > _GATE_SOFT_LIMIT:
        top = "; ".join(f"{i.get('kind', '?')}" for i in issues[:4])
        return (
            f"Quick precheck before conceptual summary: {n_err} "
            f"error(s), {n_warn} warning(s) remain in Layer 1. "
            f"Top items: {top}. Fix these on the Dashboard tab and "
            f"come back."
        )
    return None


def _gate_text_for_tests(test_summary: str | None) -> str | None:
    if not test_summary:
        return None
    s = test_summary.lower()
    if "failed" in s or "did not pass" in s:
        return (
            f"Your circuit currently has failing tests ({test_summary}). "
            f"For a Layer 2 conceptual summary to be useful the test "
            f"bench should pass first. Head to the L3 Coach tab to "
            f"debug failing rows."
        )
    return None


def explain_circuit(
    facts: dict,
    issues: list[dict],
    test_summary: str | None,
    student_goal: str | None,
    *,
    api_key: str | None = None,
    model: str | None = None,
    example_row: dict | None = None,
) -> dict:
    gate = _gate_text_for_issues(issues)
    if gate is None:
        gate = _gate_text_for_tests(test_summary)
    if gate is not None:
        return {
            "ok": True, "text": None, "gate_message": gate,
            "error": None, "usage": None, "model": None,
        }

    compact = _compact_facts(facts)
    template = _load_prompt("layer2_circuit_summary_v1.txt")
    prompt = template.format(
        circuit_facts_json=json.dumps(compact, indent=2),
        test_results_summary=test_summary or "(tests not yet run)",
        example_row=format_example_row(example_row),
        student_goal_or_none=(student_goal.strip() if student_goal else "(none)"),
        lectures_list=SYLLABUS_311,
    )

    from dlc.llm.client import DEFAULT_MODEL
    result = call_llm(
        prompt,
        api_key=api_key,
        model=model or DEFAULT_MODEL,
        max_tokens=2400,
        feature="explain",
        system=(
            "You are a circuit reasoning assistant for UNC COMP 311. "
            "Use plain text only. No markdown, no bullets, no headers."
        ),
    )
    return {
        "ok": result["ok"],
        "text": sanitize_output(result["text"]) if result["text"] else None,
        "gate_message": None,
        "error": result["error"],
        "usage": result["usage"],
        "model": result["model"],
    }