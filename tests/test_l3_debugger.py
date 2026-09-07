import json
import shutil
from pathlib import Path

import pytest

from dlc.l3 import debugger
from dlc.l3.debugger import (
    debug_circuit,
    dedupe_hypotheses,
    validate_animation,
    validate_hypothesis,
    verify_ops,
)
from dlc.testing.runner import find_digital_jar

_BENCH = "data/sample_circuits/30_bug_benchmark"
_BUG3 = f"{_BENCH}/bug3_wrong_cin/Wrong_cin.dig"
_BUG4 = f"{_BENCH}/bug4_missing_pipeline/Missing_pipeline.dig"
_CLEAN = "data/sample_circuits/tier3_realistic/pipelined_adder_correct.dig"

_needs_jar = pytest.mark.skipif(
    find_digital_jar() is None, reason="Digital.jar not configured",
)

GOOD_OPS = [{"op": "change_attribute", "component_index": 16,
             "name": "Value", "value": 0}]
BAD_OPS = [{"op": "change_attribute", "component_index": 16,
            "name": "Value", "value": 1}]


def _reply(ops, why="Sum is one too high on every failing row."):
    return {
        "contract": "l3.debug.v1.1",
        "confidence": 0.9,
        "hint": {"suspect_region": "the adder's carry-in constant",
                 "suspect_signals": ["c_i"], "why": why},
        "fix": {"ops": ops,
                "explanation_for_student": ("the constant driving c_i "
                                            "defaults to 1; set it to 0."),
                "animation_script": [
                    {"act": "diagnose_line", "text": "Sum runs one high."},
                    {"act": "mark_fix",
                     "target": {"component_index": 16, "path": []},
                     "label": "carry-in 1 -> 0"},
                    {"act": "retest"},
                ]},
    }


def _fake(replies):
    replies = list(replies)
    def call(prompt, **_kw):
        call.log.append(prompt)
        assert replies, "unexpected extra LLM call"
        r = replies.pop(0)
        return {"ok": True,
                "text": json.dumps(r) if isinstance(r, dict) else r,
                "error": None,
                "usage": {"input_tokens": 10, "output_tokens": 20},
                "model": "fake"}
    call.log = []
    return call


def _never(prompt, **_kw):
    raise AssertionError("the model must not be called on this path")


@pytest.fixture(autouse=True)
def _no_jar(monkeypatch):
    """Pin the evaluator path; the jar-gated test below re-enables it."""
    monkeypatch.setattr(debugger, "find_digital_jar", lambda: None)

def test_bug3_end_to_end_confirmed_card():
    call = _fake([_reply(GOOD_OPS)])
    res = debug_circuit(_BUG3, call=call, use_manifest=False,
                        failing_indices=[0, 1])
    assert res["mode"] == "analysis"
    assert res["llm_calls"] == 1
    assert len(res["cards"]) == 1
    card = res["cards"][0]
    assert card["rank"] == 1
    assert card["cluster_rows"] == [0, 1]
    assert card["verified"] == {"confirmed": True, "runner": "evaluator",
                                "regressions": [], "coach_residuals": {}}
    assert card["fix"]["ops"] == GOOD_OPS
    assert card["fix"]["animation_script"][-1] == {"act": "retest"}
    assert card["hint"]["suspect_region"]
    assert res["dropped_ideas"] == []
    assert res["diagnosis_lines"] and "Sum" in res["diagnosis_lines"][0]
    assert res["usage"]["output_tokens"] == 20


def test_clear_circuit_makes_no_model_calls():
    res = debug_circuit(_CLEAN, call=_never, use_manifest=False)
    assert res["mode"] == "clear"
    assert res["llm_calls"] == 0


def test_lazy_circuit_makes_no_model_calls_and_suggests():
    res = debug_circuit(_BUG4, call=_never, use_manifest=False)
    assert res["mode"] == "lazy"
    assert res["llm_calls"] == 0
    assert [s["kind"] for s in res["suggestions"]] == ["missing_clocked_logic"]
    sug = res["suggestions"][0]
    assert sug["question"] and sug["hint"]
    assert "register" in sug["terms"]


def test_invalid_json_earns_one_format_reprompt():
    call = _fake(["sorry, I cannot produce JSON today", _reply(GOOD_OPS)])
    res = debug_circuit(_BUG3, call=call, use_manifest=False,
                        failing_indices=[0, 1])
    assert res["llm_calls"] == 2
    assert "FORMAT RETRY" in call.log[1]
    assert len(res["cards"]) == 1 and res["cards"][0]["verified"]["confirmed"]


def test_double_garbage_becomes_dropped_idea():
    call = _fake(["nope", "still nope"])
    res = debug_circuit(_BUG3, call=call, use_manifest=False,
                        failing_indices=[0, 1])
    assert res["llm_calls"] == 2
    assert res["cards"] == []
    assert [d["reason"] for d in res["dropped_ideas"]] == ["invalid_response"]


def test_refuted_fix_earns_one_retry_with_evidence():
    call = _fake([_reply(BAD_OPS), _reply(GOOD_OPS)])
    res = debug_circuit(_BUG3, call=call, use_manifest=False,
                        failing_indices=[0, 1])
    assert res["llm_calls"] == 2
    assert '"refuted_ops"' in call.log[1]
    assert '"still_failing"' in call.log[1]
    assert len(res["cards"]) == 1
    assert res["cards"][0]["fix"]["ops"] == GOOD_OPS


def test_unknown_op_is_a_format_error_then_dropped():
    bad = _reply(GOOD_OPS)
    bad["fix"]["ops"] = [{"op": "explode_everything"}]
    call = _fake([bad, "garbage"])
    res = debug_circuit(_BUG3, call=call, use_manifest=False,
                        failing_indices=[0, 1])
    assert res["llm_calls"] == 2
    assert "unknown op" in call.log[1]
    assert res["cards"] == []
    assert res["dropped_ideas"][0]["reason"] == "invalid_response"


@_needs_jar
def test_bug3_fix_verifies_through_real_digital(monkeypatch):
    monkeypatch.setattr(debugger, "find_digital_jar",
                        lambda: find_digital_jar())
    call = _fake([_reply(GOOD_OPS)])
    res = debug_circuit(_BUG3, call=call, use_manifest=False,
                        failing_indices=[0, 1])
    assert res["mode"] == "analysis"
    assert len(res["cards"]) == 1
    assert res["cards"][0]["verified"]["confirmed"] is True
    assert res["cards"][0]["verified"]["runner"] == "digital"


def test_verify_ops_confirms_the_correct_fix_offline():
    v = verify_ops(_BUG3, "Testcase_12", GOOD_OPS,
                   cluster_rows=[0, 1, 2, 3],
                   original_failing=[0, 1, 2, 3])
    assert v["confirmed"] is True and v["apply_ok"] is True
    assert v["runner"] == "evaluator"
    assert v["still_failing"] == [] and v["regressions"] == []


def test_verify_ops_refutes_a_no_op_fix():
    v = verify_ops(_BUG3, "Testcase_12", BAD_OPS,
                   cluster_rows=[0, 1, 2, 3],
                   original_failing=[0, 1, 2, 3])
    assert v["confirmed"] is False and v["apply_ok"] is True
    assert v["still_failing"] == [0, 1, 2, 3]
    assert v["details"][0], "refutation evidence must carry the cells"


def test_verify_ops_reports_apply_failure():
    v = verify_ops(_BUG3, "Testcase_12",
                   [{"op": "delete_wire", "p1": [1, 1], "p2": [2, 2]}],
                   cluster_rows=[0], original_failing=[0])
    assert v["confirmed"] is False and v["apply_ok"] is False
    assert v["warning"]


def _hyp(ops, rows, confirmed, confidence=0.5, ci=0):
    return {"cluster_index": ci, "cluster_rows": rows,
            "confidence": confidence, "hint": {"why": "w"},
            "ops": ops, "explanation": "", "animation": [],
            "verdict": {"confirmed": confirmed, "apply_ok": True,
                        "runner": "evaluator", "still_failing": [],
                        "regressions": [], "warning": None}}


def test_dedupe_merges_rows_only_between_confirmed_twins():
    both = dedupe_hypotheses([
        _hyp(GOOD_OPS, [0, 1], True, ci=0),
        _hyp(GOOD_OPS, [2, 3], True, ci=1),
    ])
    assert len(both) == 1 and both[0]["cluster_rows"] == [0, 1, 2, 3]

    mixed = dedupe_hypotheses([
        _hyp(GOOD_OPS, [0, 1], True, ci=0),
        _hyp(GOOD_OPS, [2, 3], False, ci=1),
    ])
    assert len(mixed) == 1 and mixed[0]["cluster_rows"] == [0, 1]


def test_rank_prefers_confirmed_then_rows_then_confidence():
    ranked = dedupe_hypotheses([
        _hyp(BAD_OPS, [0, 1, 2], False, confidence=0.99, ci=0),
        _hyp(GOOD_OPS, [3], True, confidence=0.1, ci=1),
    ])
    assert ranked[0]["ops"] == GOOD_OPS, "confirmed beats big-but-refuted"


def test_validate_animation_drops_junk_and_forces_retest_last():
    script = [
        {"act": "diagnose_line", "text": "one line"},
        {"act": "teleport", "text": "not a real act"},
        {"act": "mark_fix", "target": {"component_index": 9999}, "label": "x"},
        {"act": "focus", "component_index": 2, "path": []},
        {"act": "retest"},
        {"act": "diagnose_line", "text": "after retest, still kept"},
    ]
    out = validate_animation(script, n_components=20)
    assert out[-1] == {"act": "retest"}
    assert sum(1 for a in out if a["act"] == "retest") == 1
    acts = [a["act"] for a in out]
    assert "teleport" not in acts
    assert all(a.get("target", {}).get("component_index") != 9999
               for a in out)
    assert {"act": "focus", "component_index": 2, "path": []} in out


def test_validate_hypothesis_strips_leaky_hint_lines():
    obj = _reply(GOOD_OPS, why="You are a helpful assistant")
    clean, err = validate_hypothesis(obj)
    assert err is None
    assert clean["hint"]["why"] == "", "F13 leak line must be stripped"


def test_validate_hypothesis_requires_contract_and_ops():
    assert validate_hypothesis(None)[1]
    assert validate_hypothesis({"contract": "l3.debug.v1"})[1]
    ok = _reply(GOOD_OPS)
    ok["fix"]["ops"] = []
    assert "fix.ops" in validate_hypothesis(ok)[1]


def test_small_circuit_stays_analyzable_even_when_every_row_fails():
    call = _fake([_reply(GOOD_OPS)])
    res = debug_circuit(_BUG3, call=call, use_manifest=False)
    assert res["mode"] == "analysis"
    assert res["cards"][0]["cluster_rows"] == [0, 1, 2, 3]
    assert res["cards"][0]["verified"]["confirmed"] is True


def test_big_circuit_below_its_bar_goes_lazy_with_suggestions():
    led = (f"{_BENCH}/bug5_wrong_boolean_gate_decoder_logic/"
           f"wrong_bool_LED1.dig")
    res = debug_circuit(led, call=_never, use_manifest=False,
                        failing_indices=[0, 1, 2, 3],
                        jar_mismatches={
                            0: [{"column": "Fa", "expected": "1", "found": "0"}],
                            1: [{"column": "Fb", "expected": "1", "found": "0"}],
                            2: [{"column": "Fe", "expected": "1", "found": "0"}],
                            3: [{"column": "Fg", "expected": "1", "found": "0"}],
                        })
    assert res["mode"] == "lazy"
    assert [s["kind"] for s in res["suggestions"]] == ["low_pass_rate"]
    assert "truth table" in res["suggestions"][0]["terms"]


def test_cards_carry_the_component_naming_standard():
    call = _fake([_reply(GOOD_OPS)])
    res = debug_circuit(_BUG3, call=call, use_manifest=False,
                        failing_indices=[0, 1])
    pretty = res["cards"][0]["fix"]["ops_pretty"]
    assert pretty == ["set [16] Const attribute Value = 0"]


def test_describe_ops_arrows_follow_signal_direction():
    from dlc.l3.debugger import describe_ops
    from dlc.parser.dig_parser import parse_dig_file
    calc = parse_dig_file(f"{_BENCH}/bug1_meaningless_mux_in3/"
                          f"tier3_calculator.dig")
    lines = describe_ops(calc, [
        {"op": "rewire_pin", "component_index": 14, "pin": "in3",
         "to": {"component_index": 9, "pin": "Result"}},
        {"op": "delete_component", "component_index": 23},
    ])
    assert lines[0] == "rewire [14] Multiplexer.in3 ← [9] bool_unit.dig.Result"
    assert lines[1] == "delete [23] Ground"


def test_zero_card_run_earns_one_escalation_per_cluster():
    call = _fake([_reply(BAD_OPS), _reply(BAD_OPS), _reply(GOOD_OPS)])
    res = debug_circuit(_BUG3, call=call, use_manifest=False,
                        failing_indices=[0, 1])
    assert res["llm_calls"] == 3
    assert "[ESCALATION]" in call.log[2] and '"refuted_ops"' in call.log[2]
    assert len(res["cards"]) == 1
    assert res["cards"][0]["fix"]["ops"] == GOOD_OPS
    assert not any("escalation" in n for n in res["notes"])


def test_led5_gate_swap_fix_confirms_offline():
    led = (f"{_BENCH}/bug5_wrong_boolean_gate_decoder_logic/"
           f"wrong_bool_LED5.dig")
    reply = _reply([{"op": "replace_element", "component_index": 164,
                     "new_element": "Or"}],
                   why="Ff is 0 whenever either minterm group fires.")
    call = _fake([reply])
    res = debug_circuit(led, call=call, use_manifest=False,
                        failing_indices=[1, 2])
    assert res["mode"] == "analysis"
    card = res["cards"][0]
    assert card["verified"]["confirmed"] is True
    assert card["fix"]["ops_pretty"] == ["replace [164] And with Or"]


def test_best_unverified_survivor_when_everything_is_refuted():
    call = _fake([_reply(BAD_OPS), _reply(BAD_OPS), _reply(BAD_OPS)])
    res = debug_circuit(_BUG3, call=call, use_manifest=False,
                        failing_indices=[0, 1])
    assert res["llm_calls"] == 3
    assert res["cards"] == []
    b = res["best_unverified"]
    assert b is not None
    assert b["fix"]["ops"] == BAD_OPS
    assert b["fix"]["ops_pretty"] == ["set [16] Const attribute Value = 1"]
    assert b["verdict"]["still_failing"] == [0, 1]
    assert b["hint"]["suspect_region"]
    assert not any("unverified" in n.lower() for n in res["notes"])
    assert all((d.get("ops_pretty") or []) for d in res["dropped_ideas"]), \
        "dropped ideas must show what they tried"


def test_no_best_unverified_without_any_valid_hypothesis():
    call = _fake(["nope", "still nope"])
    res = debug_circuit(_BUG3, call=call, use_manifest=False,
                        failing_indices=[0, 1])
    assert res["cards"] == []
    assert res["best_unverified"] is None


def test_failing_subcircuit_gates_the_parent_into_suggestions():
    parent = f"{_BENCH}/bug7_broken_child/tier3_calculator.dig"
    res = debug_circuit(parent, call=_never, use_manifest=False)
    assert res["mode"] == "lazy"
    assert res["llm_calls"] == 0
    assert [f["kind"] for f in res["gross_flags"]] == ["subcircuit_failing"]
    assert "bool_unit.dig" in res["gross_flags"][0]["detail"]
    assert [s["kind"] for s in res["suggestions"]] == ["subcircuit_failing"]
    assert "bottom-up" in res["suggestions"][0]["hint"]


_BUG6 = f"{_BENCH}/bug6_hidden_mux_case3/uncovered_op_calculator.dig"
MUX_FIX = [{"op": "rewire_pin", "component_index": 14, "pin": "in3",
            "to": {"component_index": 9, "pin": "Result"}}]


def _coach_temp(tmp_path):
    src = Path(_BUG6)
    shutil.copy(src.parent / "bool_unit.dig", tmp_path / "bool_unit.dig")
    text = src.read_text(encoding="utf-8")
    anchor = "0 0 0 2 0 0 1 0</dataString>"
    assert text.count(anchor) == 1
    out = tmp_path / "uncovered_op_calculator.dig"
    out.write_text(
        text.replace(anchor,
                     "0 0 0 2 0 0 1 0\n5 10 0 3 15 1 0 1</dataString>"),
        encoding="utf-8")
    return str(out)


def test_verify_ops_refutes_partial_fix_without_coach_targets(tmp_path):
    path = _coach_temp(tmp_path)
    v = verify_ops(path, "Testcase_25", MUX_FIX, [10], [10])
    assert v["confirmed"] is False
    assert v["still_failing"] == [10]


def test_coach_row_strict_improvement_confirms_with_residual(tmp_path):
    path = _coach_temp(tmp_path)
    v = verify_ops(path, "Testcase_25", MUX_FIX, [10], [10],
                   coach_targets={10: {"Result", "Carry", "Zero", "Bit0"}})
    assert v["confirmed"] is True
    assert v["still_failing"] == [] and v["regressions"] == []
    assert v["coach_residuals"] == {10: ["Carry"]}


def test_coach_row_must_improve_and_break_nothing(tmp_path):
    path = _coach_temp(tmp_path)
    noop = [{"op": "change_attribute", "component_index": 23,
             "name": "Bits", "value": 4}]
    v = verify_ops(path, "Testcase_25", noop, [10], [10],
                   coach_targets={10: {"Result", "Carry", "Zero", "Bit0"}})
    assert v["confirmed"] is False and v["still_failing"] == [10]
    v2 = verify_ops(path, "Testcase_25", MUX_FIX, [10], [10],
                    coach_targets={10: {"Result", "Zero", "Bit0"}})
    assert v2["confirmed"] is False and v2["still_failing"] == [10]


def test_debug_circuit_judges_coach_rows_by_improvement(tmp_path):
    path = _coach_temp(tmp_path)
    call = _fake([_reply(MUX_FIX)])
    res = debug_circuit(path, call=call, use_manifest=False,
                        coach_rows=[10])
    assert res["mode"] == "analysis"
    assert res["llm_calls"] == 1
    card = res["cards"][0]
    assert card["verified"]["confirmed"] is True
    assert card["verified"]["coach_residuals"] == {10: ["Carry"]}
    assert card["fix"]["ops"] == MUX_FIX


def test_all_rows_error_returns_build_refused_lazy(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(debugger, "find_digital_jar", lambda: "fake.jar")
    monkeypatch.setattr(
        debugger, "per_row_run_auto",
        lambda spec, path, jar_path=None: [
            SimpleNamespace(status="error", row_index=i,
                            error_message="A tunnel rd2 is not connected!",
                            mismatches=[])
            for i in range(4)
        ])
    res = debug_circuit(_BUG3, call=_never, use_manifest=False)
    assert res["mode"] == "lazy"
    assert res["llm_calls"] == 0
    assert [f["kind"] for f in res["gross_flags"]] == ["build_refused"]
    assert "rd2" in res["gross_flags"][0]["detail"]
    assert [s["kind"] for s in res["suggestions"]] == ["build_refused"]
    assert "Layer 1" in res["suggestions"][0]["hint"]


def test_child_failing_official_tests_gates_the_parent(tmp_path):
    child_parts = []
    for i, (label, bits) in enumerate(
            [("opcode", 7), ("funct3", 3), ("funct7", 7)]):
        child_parts.append(
            f'<visualElement><elementName>In</elementName><elementAttributes>'
            f'<entry><string>Label</string><string>{label}</string></entry>'
            f'<entry><string>Bits</string><int>{bits}</int></entry>'
            f'</elementAttributes><pos x="0" y="{i * 60}"/></visualElement>')
    outs = ["RegWrite", "ALUSrc", "ImmSrc1", "ImmSrc0",
            "ALUOp3", "ALUOp2", "ALUOp1", "ALUOp0"]
    wires = []
    for i, label in enumerate(outs):
        y = 300 + i * 40
        child_parts.append(
            f'<visualElement><elementName>Ground</elementName>'
            f'<elementAttributes/><pos x="160" y="{y}"/></visualElement>')
        child_parts.append(
            f'<visualElement><elementName>Out</elementName><elementAttributes>'
            f'<entry><string>Label</string><string>{label}</string></entry>'
            f'</elementAttributes><pos x="200" y="{y}"/></visualElement>')
        wires.append(f'<wire><p1 x="160" y="{y}"/><p2 x="200" y="{y}"/></wire>')
    child = ('<?xml version="1.0" encoding="utf-8"?><circuit><version>2'
             '</version><attributes/><visualElements>'
             + "".join(child_parts)
             + '</visualElements><wires>' + "".join(wires)
             + '</wires></circuit>')
    (tmp_path / "control-unit.dig").write_text(child, encoding="utf-8")

    parent = ('<?xml version="1.0" encoding="utf-8"?><circuit><version>2'
              '</version><attributes/><visualElements>'
              '<visualElement><elementName>In</elementName>'
              '<elementAttributes><entry><string>Label</string>'
              '<string>A</string></entry></elementAttributes>'
              '<pos x="0" y="0"/></visualElement>'
              '<visualElement><elementName>Out</elementName>'
              '<elementAttributes><entry><string>Label</string>'
              '<string>X</string></entry></elementAttributes>'
              '<pos x="200" y="0"/></visualElement>'
              '<visualElement><elementName>control-unit.dig</elementName>'
              '<elementAttributes/><pos x="0" y="200"/></visualElement>'
              '<visualElement><elementName>Testcase</elementName>'
              '<elementAttributes><entry><string>Testdata</string>'
              '<testData><dataString>A X\n0 0</dataString></testData>'
              '</entry></elementAttributes><pos x="0" y="400"/>'
              '</visualElement>'
              '</visualElements><wires><wire><p1 x="0" y="0"/>'
              '<p2 x="200" y="0"/></wire></wires></circuit>')
    p = tmp_path / "top.dig"
    p.write_text(parent, encoding="utf-8")

    res = debug_circuit(str(p), call=_never, use_manifest=False)
    assert res["mode"] == "lazy"
    assert res["llm_calls"] == 0
    assert [f["kind"] for f in res["gross_flags"]] == [
        "subcircuit_failing_official"]
    assert "control-unit.dig" in res["gross_flags"][0]["detail"]
    assert "official" in res["gross_flags"][0]["detail"]
    assert not list(tmp_path.glob(".dlc_injected__*"))


def test_stop_condition_returns_best_solution_early(monkeypatch):
    monkeypatch.setattr(debugger, "_MAX_REFUTED_IDEAS", 1)
    call = _fake([_reply(BAD_OPS)])
    res = debug_circuit(_BUG3, call=call, use_manifest=False,
                        failing_indices=[0, 1])
    assert res["mode"] == "analysis"
    assert res["llm_calls"] == 1
    assert res["stopped_early"] is True
    assert res["refuted_ideas"] == 1
    assert res["cards"] == []
    assert res["best_unverified"] is not None
    assert res["best_unverified"]["fix"]["ops"] == BAD_OPS
    assert any("stopped after 1 refuted" in n for n in res["notes"])
    assert len(res["timings"]["llm_s"]) == 1
    assert len(res["timings"]["verify_s"]) == 1
    assert res["timings"]["total_s"] >= 0


def test_unstopped_run_reports_flag_false():
    call = _fake([_reply(GOOD_OPS)])
    res = debug_circuit(_BUG3, call=call, use_manifest=False,
                        failing_indices=[0, 1])
    assert res["stopped_early"] is False
    assert res["refuted_ideas"] == 0
    assert res["cards"] and res["cards"][0]["verified"]["confirmed"]


def test_all_rows_error_with_unbound_columns_gets_rename_guidance(
        monkeypatch, tmp_path):
    from types import SimpleNamespace
    p = tmp_path / "renamed.dig"
    p.write_text(
        '<?xml version="1.0" encoding="utf-8"?><circuit><version>2'
        '</version><attributes/><visualElements>'
        '<visualElement><elementName>In</elementName><elementAttributes>'
        '<entry><string>Label</string><string>A</string></entry>'
        '</elementAttributes><pos x="0" y="0"/></visualElement>'
        '<visualElement><elementName>Out</elementName><elementAttributes>'
        '<entry><string>Label</string><string>X</string></entry>'
        '</elementAttributes><pos x="200" y="0"/></visualElement>'
        '<visualElement><elementName>Testcase</elementName>'
        '<elementAttributes><entry><string>Testdata</string><testData>'
        '<dataString>A Q\n0 0</dataString></testData></entry>'
        '</elementAttributes><pos x="0" y="200"/></visualElement>'
        '</visualElements><wires><wire><p1 x="0" y="0"/>'
        '<p2 x="200" y="0"/></wire></wires></circuit>',
        encoding="utf-8")
    monkeypatch.setattr(debugger, "find_digital_jar", lambda: "fake.jar")
    monkeypatch.setattr(
        debugger, "per_row_run_auto",
        lambda spec, path, jar_path=None: [
            SimpleNamespace(status="error", row_index=0,
                            error_message="Test signal Q not found in "
                                          "the circuit!",
                            mismatches=[])
        ])
    res = debug_circuit(str(p), call=_never, use_manifest=False)
    assert res["mode"] == "lazy"
    assert res["llm_calls"] == 0
    assert [f["kind"] for f in res["gross_flags"]] == ["unbound_columns"]
    assert "'Q'" in res["gross_flags"][0]["detail"]
    assert [s["kind"] for s in res["suggestions"]] == ["unbound_columns"]
    assert "Rename" in res["suggestions"][0]["hint"]


def test_control_unit_files_skip_the_lazy_gate(tmp_path):
    from dlc.l3.debugger import _lazy_exempt_name
    assert _lazy_exempt_name("control-unit.dig") is True
    assert _lazy_exempt_name("ControlUnit.dig") is True
    assert _lazy_exempt_name("/x/y/.dlc_injected__control-unit.dig") is True
    assert _lazy_exempt_name("cpu.dig") is False
    assert _lazy_exempt_name("register-file.dig") is False
    assert _lazy_exempt_name(None) is False

    led5 = Path(f"{_BENCH}/bug5_wrong_boolean_gate_decoder_logic/"
                f"wrong_bool_LED5.dig")
    cells = {0: [{"column": "Fa", "expected": "1", "found": "0"}],
             1: [{"column": "Fb", "expected": "1", "found": "0"}],
             2: [{"column": "Fe", "expected": "1", "found": "0"}],
             3: [{"column": "Fg", "expected": "1", "found": "0"}]}

    other = tmp_path / "led.dig"
    other.write_text(led5.read_text(encoding="utf-8"), encoding="utf-8")
    res = debug_circuit(str(other), call=_never, use_manifest=False,
                        failing_indices=[0, 1, 2, 3], jar_mismatches=cells)
    assert res["mode"] == "lazy"

    cu = tmp_path / "control-unit.dig"
    cu.write_text(led5.read_text(encoding="utf-8"), encoding="utf-8")
    call = _fake(["nope", "still nope"])
    res2 = debug_circuit(str(cu), call=call, use_manifest=False,
                         failing_indices=[0, 1, 2, 3], jar_mismatches=cells)
    assert res2["mode"] == "analysis"
    assert any("lazy-gate checks skipped" in n for n in res2["notes"])


def test_prompt_treats_stored_data_as_fixed():
    from dlc.l3.debugger import _load_prompt, _ROM_NOTE
    text = _load_prompt()
    assert "CHECK IT FIRST" not in text
    assert "derive the FULL table" not in text
    assert "IS NEVER THE FIX" in text
    assert "[ROM NOTE]" in text
    assert "never the fix" in _ROM_NOTE


def test_verify_ops_exposes_remaining_failing():
    good = verify_ops(_BUG3, "Testcase_12", GOOD_OPS,
                      cluster_rows=[0, 1, 2, 3],
                      original_failing=[0, 1, 2, 3])
    assert good["remaining_failing"] == []
    bad = verify_ops(_BUG3, "Testcase_12", BAD_OPS,
                     cluster_rows=[0, 1, 2, 3],
                     original_failing=[0, 1, 2, 3])
    assert bad["remaining_failing"]


_ROM_DECOY = (
    '<?xml version="1.0" encoding="utf-8"?><circuit><version>2</version>'
    '<attributes/><visualElements>'
    '<visualElement><elementName>In</elementName><elementAttributes>'
    '<entry><string>Label</string><string>sel</string></entry>'
    '</elementAttributes><pos x="0" y="0"/></visualElement>'
    '<visualElement><elementName>ROM</elementName><elementAttributes>'
    '<entry><string>AddrBits</string><int>1</int></entry>'
    '<entry><string>Bits</string><int>4</int></entry>'
    '</elementAttributes><pos x="200" y="0"/></visualElement>'
    '<visualElement><elementName>Out</elementName><elementAttributes>'
    '<entry><string>Label</string><string>D</string></entry>'
    '<entry><string>Bits</string><int>4</int></entry>'
    '</elementAttributes><pos x="400" y="20"/></visualElement>'
    '<visualElement><elementName>In</elementName><elementAttributes>'
    '<entry><string>Label</string><string>X</string></entry>'
    '</elementAttributes><pos x="0" y="200"/></visualElement>'
    '<visualElement><elementName>In</elementName><elementAttributes>'
    '<entry><string>Label</string><string>Y</string></entry>'
    '</elementAttributes><pos x="0" y="280"/></visualElement>'
    '<visualElement><elementName>And</elementName><elementAttributes>'
    '<entry><string>wideShape</string><boolean>true</boolean></entry>'
    '</elementAttributes><pos x="200" y="200"/></visualElement>'
    '<visualElement><elementName>Out</elementName><elementAttributes>'
    '<entry><string>Label</string><string>W</string></entry>'
    '</elementAttributes><pos x="400" y="220"/></visualElement>'
    '<visualElement><elementName>Testcase</elementName>'
    '<elementAttributes><entry><string>Testdata</string><testData>'
    '<dataString>sel X Y D W\n0 1 1 5 1\n1 1 0 6 0</dataString>'
    '</testData></entry></elementAttributes><pos x="0" y="400"/>'
    '</visualElement>'
    '<visualElement><elementName>VDD</elementName><elementAttributes/>'
    '<pos x="160" y="40"/></visualElement>'
    '</visualElements><wires>'
    '<wire><p1 x="160" y="40"/><p2 x="200" y="40"/></wire>'
    '<wire><p1 x="0" y="0"/><p2 x="200" y="0"/></wire>'
    '<wire><p1 x="260" y="20"/><p2 x="400" y="20"/></wire>'
    '<wire><p1 x="0" y="200"/><p2 x="200" y="200"/></wire>'
    '<wire><p1 x="0" y="280"/><p2 x="100" y="280"/></wire>'
    '<wire><p1 x="100" y="240"/><p2 x="100" y="280"/></wire>'
    '<wire><p1 x="100" y="240"/><p2 x="200" y="240"/></wire>'
    '<wire><p1 x="280" y="220"/><p2 x="400" y="220"/></wire>'
    '</wires></circuit>'
)


def test_data_op_on_any_rom_is_stripped(tmp_path):
    p = tmp_path / "romdecoy.dig"
    p.write_text(_ROM_DECOY, encoding="utf-8")
    reply = _reply([{"op": "change_attribute", "component_index": 1,
                     "name": "Data", "value": "5,6"}],
                   why="the lookup stage reads 0 on every failing row")
    call = _fake([reply, reply])
    res = debug_circuit(str(p), call=call, use_manifest=False)
    assert res["mode"] == "analysis"
    assert res["cards"] == []
    assert res["dropped_ideas"] and all(
        d["reason"] == "rom_protected" for d in res["dropped_ideas"])
    assert any("never edits a ROM" in n for n in res["notes"])
    assert "\n[ROM NOTE]\n" in call.log[0]
    assert '"5,6"' not in json.dumps(res["cards"])


def test_truncated_reply_earns_a_json_only_retry():
    replies = [
        {"ok": True, "text": '{"contract": "l3.debug.v1.1", "hint": ',
         "error": None, "usage": {"input_tokens": 1, "output_tokens": 1},
         "stop_reason": "max_tokens", "model": "fake"},
        {"ok": True, "text": json.dumps(_reply(GOOD_OPS)), "error": None,
         "usage": {"input_tokens": 1, "output_tokens": 1},
         "stop_reason": "end_turn", "model": "fake"},
    ]
    log = []
    def call(prompt, **_kw):
        log.append(prompt)
        return replies[len(log) - 1]
    res = debug_circuit(_BUG3, call=call, use_manifest=False,
                        failing_indices=[0, 1])
    assert res["llm_calls"] == 2
    assert "CUT OFF at the output token limit" in log[1]
    assert res["cards"] and res["cards"][0]["verified"]["confirmed"]


def test_opus5_gets_reasoning_headroom():
    from dlc.l3.debugger import _max_tokens_for
    assert _max_tokens_for("claude-opus-5") == 16000
    assert _max_tokens_for("claude-opus-4-8") == 8000
    assert _max_tokens_for("claude-haiku-4-5-20251001") == 3000


def test_refutation_block_names_partial_progress():
    from dlc.l3.debugger import _refutation_block
    ops = [{"op": "replace_element", "component_index": 5,
            "new_element": "And"}]
    verdict = {"apply_ok": True, "still_failing": [1], "regressions": [],
               "details": {}, "warning": None}
    txt = _refutation_block(ops, verdict, target_rows=[0, 1])
    assert "PARTIALLY RIGHT" in txt
    assert "KEEP the refuted ops" in txt
    assert "rows_these_ops_did_fix" in txt

    verdict = {"apply_ok": True, "still_failing": [0, 1],
               "regressions": [], "details": {}, "warning": None}
    txt = _refutation_block(ops, verdict, target_rows=[0, 1])
    assert "PARTIALLY RIGHT" not in txt

    verdict = {"apply_ok": True, "still_failing": [1], "regressions": [4],
               "details": {}, "warning": None}
    txt = _refutation_block(ops, verdict, target_rows=[0, 1])
    assert "PARTIALLY RIGHT" not in txt


def test_prompt_teaches_the_wrong_address_rule():
    from dlc.l3.debugger import _load_prompt
    text = _load_prompt()
    assert "the ADDRESS PATH is the bug" in text
    assert "address_input_drivers" in text
    assert "address_by_row" in text


def test_opus5_thinking_depth_is_bounded():
    from dlc.l3.debugger import _effort_for
    assert _effort_for("claude-opus-5") == "low"
    assert _effort_for("claude-opus-4-8") is None
    assert _effort_for("claude-haiku-4-5-20251001") is None
    assert _effort_for(None) is None

    seen = {}
    def call(prompt, **kw):
        seen.update(kw)
        return {"ok": True, "text": json.dumps(_reply(GOOD_OPS)),
                "error": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "model": "claude-opus-5"}
    debug_circuit(_BUG3, call=call, use_manifest=False,
                  failing_indices=[0, 1], model="claude-opus-5")
    assert seen.get("effort") == "low"
    assert seen.get("max_tokens") == 16000


def test_bug9_swapped_select_gate_is_fixed_by_replace_element():
    ops = [{"op": "replace_element", "component_index": 12, "new_element": "Or"}]
    call = _fake([_reply(ops, why="ADD and XOR rows return the AND/OR arm.")])
    res = debugger.debug_circuit(
        f"{_BENCH}/bug9_swapped_select_gate/mini_alu_swapped_gate.dig",
        call=call, use_manifest=False)
    assert res["mode"] == "analysis" and res["llm_calls"] == 1
    card = res["cards"][0]
    assert card["verified"]["confirmed"] is True
    assert card["fix"]["ops_pretty"] == ["replace [12] And with Or"]
    prompt = call.log[0]
    assert "SELECT-PATH suspect" in prompt and '"net_names"' in prompt
    assert "never changes over the whole testcase" in prompt


def test_bug10_swapped_write_back_arms_are_fixed_by_swap_pins():
    from dlc.parser.dig_parser import parse_dig_file
    path = f"{_BENCH}/bug10_writeback_select_swapped/writeback_swapped.dig"
    circ = parse_dig_file(path)
    mux = next(i for i, c in enumerate(circ.components)
               if c.element_name == "Multiplexer")
    ops = [{"op": "swap_pins", "component_index": mux,
            "pin_a": "in0", "pin_b": "in1"}]
    call = _fake([_reply(ops, why="registers read back the other write-back arm")])
    res = debugger.debug_circuit(path, call=call, use_manifest=False)
    assert res["mode"] == "analysis" and res["llm_calls"] == 1
    assert res["cards"][0]["verified"]["confirmed"] is True
    prompt = call.log[0]
    assert '"state_trace"' in prompt and "STATE TRACE" in prompt
