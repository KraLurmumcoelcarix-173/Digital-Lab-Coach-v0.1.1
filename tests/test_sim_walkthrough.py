"""
Deterministic Layer 2 signal-flow walkthrough (dlc/sim/walkthrough.py).
"""

from dlc.parser.dig_parser import parse_dig_file
from dlc.parser.graph import build_signal_graph
from dlc.parser.netlist import build_netlist
from dlc.sim.simulator import RowReplay
from dlc.sim.walkthrough import build_walkthrough, fmt_value, pick_example_row
from dlc.testing.spec import extract_test_specs, match_variables_to_io

_BASE = "data/sample_circuits"
_CALC = f"{_BASE}/tier3_realistic/tier3_calculator.dig"
_PIPE = f"{_BASE}/tier3_realistic/pipelined_adder_correct.dig"
_ROM = f"{_BASE}/tier1_minimal/rom_lookup.dig"


def _walk(path, row_index=None):
    c = parse_dig_file(path)
    nl = build_netlist(c)
    g = build_signal_graph(c, nl)
    spec = extract_test_specs(c)[0]
    bindings = match_variables_to_io(spec.headers, c)
    row = (pick_example_row(spec, bindings) if row_index is None
           else next(r for r in spec.rows if r.line_index == row_index))
    res = RowReplay(c, nl, g, spec).upto(row.line_index)
    return build_walkthrough(c, nl, g, spec, row, res, bindings), c


def test_fmt_value_follows_the_layer2_notation():
    assert fmt_value(1, 1) == "1"
    assert fmt_value(8, 4) == "8"
    assert fmt_value(0x1F, 5) == "31"
    assert fmt_value(0xABC, 16) == "0xABC (2748)"
    assert fmt_value(0xDEADBEEF, 32) == "0xDEADBEEF"
    assert fmt_value(None, 8) == "?"


def test_example_row_prefers_a_row_with_a_nonzero_output():
    c = parse_dig_file(_PIPE)
    spec = extract_test_specs(c)[0]
    row = pick_example_row(spec, match_variables_to_io(spec.headers, c))
    assert row.line_index == 2 and row.raw.split()[-1] == "7"


def test_calculator_row_is_told_along_the_active_path():
    walk, circ = _walk(_CALC)
    assert walk["row_index"] == 0
    assert [i["label"] for i in walk["inputs"]] == ["A", "B", "Ci", "Op"]
    result = next(o for o in walk["outputs"] if o["label"] == "Result")
    assert result["ok"] is True and result["found"] == "8"
    assert result["expression"] == (
        "Result = Multiplexer[14](sel=Op=0, in0=Add[7](A=5, B=3, Ci=0).s=8)=8")
    texts = [s["text"] for s in walk["steps"]]
    assert "Add[7]: 5 + 3 = 8" in texts
    assert "Multiplexer[14]: sel = 0, so it passes in0 = 8 to out" in texts
    # the unselected boolean unit (Op=0 picks the adder arm) is not a step
    assert not any(s["element"].endswith(".dig") for s in walk["steps"])
    # signal order: the adder is told before the multiplexer that reads it
    assert texts.index("Add[7]: 5 + 3 = 8") < texts.index(
        "Multiplexer[14]: sel = 0, so it passes in0 = 8 to out")
    # every step names real pins with values and the index the graph uses
    for s in walk["steps"]:
        assert 0 <= s["component_index"] < len(circ.components)
        assert all("net_id" in e and "text" in e for e in s["inputs"] + s["outputs"])


def test_clocked_row_stops_the_expression_at_the_register():
    walk, _ = _walk(_PIPE, 2)
    out = walk["outputs"][0]
    assert out["expression"] == "Sum = RegSum2[6].Q=7"
    texts = [s["text"] for s in walk["steps"]]
    assert any(t.startswith("RegSum2 (Register[6]): holds Q = 7") for t in texts)
    # the register that latches the sum is told after the adder feeding it
    add = next(i for i, t in enumerate(texts) if t.startswith("Add["))
    reg = next(i for i, t in enumerate(texts) if t.startswith("RegSum2"))
    assert add < reg


def test_rom_row_reads_the_word_at_the_address():
    walk, _ = _walk(_ROM)
    assert walk["steps"][0]["text"] == "ROM[2]: address 0 reads the word 3"
    assert walk["outputs"][0]["expression"] == "D = ROM[2](sel=1, A=0)=3"
    assert walk["valid"] is True and walk["waves"] == 1


def test_waves_follow_the_signal_and_wait_for_every_input():
    walk, _ = _walk(_CALC)
    wave = {s["text"].split(":")[0]: s["wave"] for s in walk["steps"]}
    assert wave["Add[7]"] == 1
    assert wave["Multiplexer[14]"] == 2          # reads the adder's sum
    assert wave["Comparator[16]"] == 3           # reads the multiplexer
    assert walk["waves"] == max(wave.values())
    # steps are handed out wave by wave
    assert [s["wave"] for s in walk["steps"]] == sorted(s["wave"] for s in walk["steps"])
    assert 0 in walk["sources"] or any(
        i in walk["sources"] for i in range(4))   # the In components
