import pytest

from dlc.l3.evidence import (
    Cluster,
    RowEvidence,
    assemble_evidence,
    assemble_evidence_for_file,
    build_payload,
    cluster_rows,
    gross_check,
    row_category,
    select_columns,
    _program_rom_out_net,
)
from dlc.l3.localizer import Suspect, SuspectReport
from dlc.parser.dig_parser import parse_dig_file
from dlc.parser.graph import build_signal_graph
from dlc.parser.netlist import build_netlist
from dlc.sim.simulator import simulate_sequential
from dlc.testing.spec import TestSpec, extract_test_specs

_BENCH = "data/sample_circuits/30_bug_benchmark"
_BUG1 = f"{_BENCH}/bug1_meaningless_mux_in3/tier3_calculator.dig"
_BUG3 = f"{_BENCH}/bug3_wrong_cin/Wrong_cin.dig"
_BUG4 = f"{_BENCH}/bug4_missing_pipeline/Missing_pipeline.dig"
_BUG5 = (f"{_BENCH}/bug5_wrong_boolean_gate_decoder_logic/"
         f"wrong_bool_LED1.dig")
_CLEAN = "data/sample_circuits/tier3_realistic/pipelined_adder_correct.dig"
_ROM = "data/sample_circuits/tier1_minimal/rom_lookup.dig"


def _parsed(path):
    c = parse_dig_file(path)
    nl = build_netlist(c)
    g = build_signal_graph(c, nl)
    return c, nl, g


def test_bug3_is_analysis_with_one_cluster_and_the_const_suspect():
    res = assemble_evidence_for_file(
        _BUG3, use_manifest=False, failing_indices=[0, 1],
    )
    assert res.mode == "analysis"
    assert res.failing_count == 2
    assert len(res.clusters) == 1
    cluster = res.clusters[0]
    assert [r.row_index for r in cluster.rows] == [0, 1]
    assert cluster.signature["columns"] == ["Sum"]
    idxs = cluster.merged.suspect_indices()
    assert 5 in idxs, "the Add itself must survive the merge"
    assert 16 in idxs, "the Const driving c_i (the seeded bug) must survive"


def test_small_circuit_is_exempt_from_the_rate_bars():
    res = assemble_evidence_for_file(_BUG3, use_manifest=False)
    assert res.mode == "analysis"
    assert res.failing_count == 4
    assert [r.row_index for r in res.clusters[0].rows] == [0, 1, 2, 3]
    idxs = res.clusters[0].merged.suspect_indices()
    assert 5 in idxs and 16 in idxs


def test_bug1_cluster_signature_carries_op_and_pins_the_mux():
    res = assemble_evidence_for_file(_BUG1, use_manifest=False)
    assert res.mode == "analysis"
    assert res.failing_count == 2
    assert len(res.clusters) == 1
    cluster = res.clusters[0]
    assert [r.row_index for r in cluster.rows] == [6, 11]
    assert sorted(cluster.signature["columns"]) == ["Bit0", "Result", "Zero"]
    assert ["Op", "3"] in cluster.signature["selects"]
    top3 = cluster.merged.suspect_indices()[:3]
    assert 14 in top3 and 23 in top3, (
        "dynamic slicing must keep the mux and its Ground on top of the "
        f"merged report; got {top3}"
    )


def test_clean_circuit_is_clear():
    res = assemble_evidence_for_file(_CLEAN, use_manifest=False)
    assert res.mode == "clear"
    assert res.failing_count == 0
    assert res.clusters == [] and res.payloads == []
    assert res.gross_flags == []


def test_bug4_missing_pipeline_goes_lazy():
    res = assemble_evidence_for_file(_BUG4, use_manifest=False)
    assert res.mode == "lazy"
    assert res.failing_count == 2
    kinds = [f["kind"] for f in res.gross_flags]
    assert "missing_clocked_logic" in kinds
    assert res.clusters == [] and res.payloads == []


def test_big_circuit_below_its_bar_goes_lazy_before_any_evidence():
    res = assemble_evidence_for_file(
        _BUG5, use_manifest=False, failing_indices=[0, 1, 2, 3],
        jar_mismatches={
            0: [{"column": "Fa", "expected": "1", "found": "0"}],
            1: [{"column": "Fb", "expected": "1", "found": "0"}],
            2: [{"column": "Fe", "expected": "1", "found": "0"}],
            3: [{"column": "Fg", "expected": "1", "found": "0"}],
        },
    )
    assert res.mode == "lazy"
    kinds = [f["kind"] for f in res.gross_flags]
    assert "low_pass_rate" in kinds
    assert res.clusters == [] and res.payloads == []


def test_big_circuit_on_or_above_its_bar_is_analyzable():
    res = assemble_evidence_for_file(
        _BUG5, use_manifest=False, failing_indices=[1, 2],
    )
    assert res.mode == "analysis"
    assert res.failing_count == 2


def _rows(n):
    from dlc.testing.spec import TestRow
    return [TestRow(raw="0 0", values=[], line_index=i) for i in range(n)]


def _spec_of(n):
    return TestSpec(name="t", component_index=0, headers=["A", "Sum"],
                    rows=_rows(n), raw_data_string="",
                    has_unexpanded_loops=False)


def test_tiered_pass_rate_bars():
    c, _nl, _g = _parsed(_BUG3)
    on = {"rate_gate_min_components": 0}
    assert gross_check(c, _spec_of(200), failing_count=15, **on) == []
    assert gross_check(c, _spec_of(100), failing_count=15, **on) == []
    assert gross_check(c, _spec_of(20), failing_count=11, **on) == []
    assert gross_check(c, _spec_of(23), failing_count=17, **on) == []
    assert gross_check(c, _spec_of(23), failing_count=19, **on) == []
    kinds = [f["kind"] for f in gross_check(c, _spec_of(30), 25, **on)]
    assert kinds == ["too_many_failures"]
    assert gross_check(c, _spec_of(12), failing_count=2, **on) == []
    assert gross_check(c, _spec_of(8), failing_count=3, **on) == []
    assert [f["kind"] for f in gross_check(c, _spec_of(8), 4, **on)] == [
        "low_pass_rate"]
    assert gross_check(c, _spec_of(4), failing_count=2, **on) == []
    assert [f["kind"] for f in gross_check(c, _spec_of(4), 3, **on)] == [
        "low_pass_rate"]
    assert gross_check(c, _spec_of(4), failing_count=4) == []

def test_gross_check_flags_unbound_columns():
    c, _nl, _g = _parsed(_BUG3)
    fake = TestSpec(name="t", component_index=0, headers=["Ghost", "Sum"],
                    rows=[], raw_data_string="", has_unexpanded_loops=False)
    flags = gross_check(c, fake, failing_count=1)
    unbound = [f for f in flags if f["kind"] == "unbound_columns"]
    assert unbound and "'Ghost'" in unbound[0]["detail"]
    assert all(f["kind"] != "missing_clocked_logic" for f in flags)


def test_gross_check_quiet_on_registered_clocked_circuit():
    c, _nl, _g = _parsed(_BUG3)
    spec = extract_test_specs(c)[0]
    assert gross_check(c, spec, failing_count=2) == []


def test_select_columns_probe_and_hints():
    c, nl, _g = _parsed(_BUG1)
    spec = extract_test_specs(c)[0]
    assert select_columns(c, nl, spec) == ["Op"]
    c3, nl3, _g3 = _parsed(_BUG3)
    spec3 = extract_test_specs(c3)[0]
    assert select_columns(c3, nl3, spec3) == []


def _fake_row(idx, columns, selects=(), category=None, suspects=()):
    report = SuspectReport(
        failing_outputs=list(columns),
        suspects=[
            Suspect(component_index=i, element_name="And",
                    display_name=f"And #{i}", score=1.0)
            for i in suspects
        ],
    )
    return RowEvidence(
        row_index=idx,
        raw=f"row {idx}",
        mismatches=[{"column": col, "expected": "1", "found": "0"}
                    for col in columns],
        selects=[list(s) for s in selects],
        category=category,
        suspect_report=report,
    )


def test_cluster_rows_splits_on_disjoint_suspects():
    rows = [
        _fake_row(0, ["Sum"], suspects=(1, 2)),
        _fake_row(1, ["Sum"], suspects=(1, 2, 3)),
        _fake_row(2, ["Sum"], suspects=(8, 9)),
    ]
    clusters, _notes = cluster_rows(rows)
    assert [[r.row_index for r in c.rows] for c in clusters] == [[0, 1], [2]]


def test_cluster_rows_keeps_empty_suspect_rows_together():
    rows = [_fake_row(0, ["Sum"]), _fake_row(1, ["Sum"])]
    clusters, _notes = cluster_rows(rows)
    assert len(clusters) == 1 and len(clusters[0].rows) == 2


def test_cluster_rows_separates_by_selects_and_category():
    rows = [
        _fake_row(0, ["R"], selects=[("Op", "1")], suspects=(1,)),
        _fake_row(1, ["R"], selects=[("Op", "2")], suspects=(1,)),
        _fake_row(2, ["R"], selects=[("Op", "2")], category="addi",
                  suspects=(1,)),
    ]
    clusters, _notes = cluster_rows(rows)
    assert len(clusters) == 3


def test_cluster_cap_folds_overflow_instead_of_dropping():
    rows = [_fake_row(i, [f"O{i}"], suspects=(i,)) for i in range(6)]
    clusters, notes = cluster_rows(rows, cap=4)
    assert len(clusters) == 4
    assert sum(len(c.rows) for c in clusters) == 6, "no row may be dropped"
    assert sum(c.folded_rows for c in clusters) == 2
    assert notes and "folded" in notes[0]

def test_payload_matches_frozen_contract_shape():
    res = assemble_evidence_for_file(
        _BUG3, use_manifest=False, failing_indices=[0, 1],
    )
    payload = res.payloads[0]
    assert set(payload) == {"contract", "circuit", "testcase", "cluster",
                            "suspects", "suspect_wiring"}
    assert payload["contract"] == "l3.debug.v1.1"
    assert payload["testcase"] == {"name": "Testcase_12",
                                   "headers": ["A", "B", "Clk", "Sum"]}
    assert {"inventory", "selectors", "inputs", "outputs"} <= set(
        payload["circuit"])
    cluster = payload["cluster"]
    assert set(cluster) == {"rows", "representative_evidence", "net_names"}
    assert all(isinstance(k, str) and isinstance(v, str)
               for k, v in cluster["net_names"].items())
    assert [set(r) for r in cluster["rows"]] == [
        {"index", "raw", "mismatches"}] * 2
    reps = cluster["representative_evidence"]
    assert len(reps) == 2, "full per-net evidence for at most 2 rows"
    for rep in reps:
        assert set(rep) == {"row_index", "net_values", "unresolved_nets",
                            "outputs"}
        some_net = next(iter(rep["net_values"].values()))
        assert set(some_net) == {"bits", "hex"}
        for out in rep["outputs"]:
            assert set(out) == {"label", "expected", "found", "ok"}
    assert payload["suspects"]["failing_outputs"] == ["Sum"]
    assert payload["suspects"]["suspects"], "merged suspects must be present"


def test_suspect_wiring_names_the_true_driver():
    res = assemble_evidence_for_file(
        _BUG3, use_manifest=False, failing_indices=[0, 1],
    )
    wiring = res.payloads[0]["suspect_wiring"]
    assert any(w["component_index"] == 16 for w in wiring)
    w16 = next(w for w in wiring if w["component_index"] == 16)
    ends = [(e["component_index"], e["pin"])
            for p in w16["pins"] for e in p["connects_to"]]
    assert (5, "c_i") in ends, "Const #16 must be shown driving Add.c_i"


def test_jar_verdict_is_authoritative_when_evaluator_disagrees():
    cells = [{"column": "Sum", "expected": "9", "found": "1"}]
    res = assemble_evidence_for_file(
        _CLEAN, use_manifest=False,
        failing_indices=[0], jar_mismatches={0: cells},
    )
    assert res.mode == "analysis"
    assert res.failing_count == 1
    assert res.clusters[0].rows[0].mismatches == cells
    assert any("cannot reproduce" in n for n in res.notes)


def test_row_category_is_none_without_a_program_rom():
    c, nl, g = _parsed(_BUG3)
    assert _program_rom_out_net(c, nl) is None
    spec = extract_test_specs(c)[0]
    sim = simulate_sequential(c, nl, g, spec, 0)
    manifest = {"program_decode": {"fields": {"opcode": [0, 7]}}}
    assert row_category(c, nl, sim, manifest) is None


def test_row_category_decodes_through_a_program_rom():
    c = parse_dig_file(_ROM)
    rom_idx = next(i for i, comp in enumerate(c.components)
                   if comp.element_name == "ROM")
    c.components[rom_idx].attributes["isProgramMemory"] = "true"
    nl = build_netlist(c)
    g = build_signal_graph(c, nl)
    nid = _program_rom_out_net(c, nl)
    assert nid is not None
    spec = extract_test_specs(c)[0]
    sim = simulate_sequential(c, nl, g, spec, 0)
    word = sim.net_values.get(nid)
    assert word is not None, "probe row must resolve the ROM output"
    manifest = {
        "program_decode": {"fields": {"opcode": [0, 4]},
                           "categories_from": "ops"},
        "categories": {"ops": [{"name": "demo",
                                "when": {"opcode": word & 0xF}}]},
    }
    got = row_category(c, nl, sim, manifest)
    assert got is not None
    assert got["category"] == "demo"
    assert got["word"] == f"{word:x}"
    assert got["fields"]["opcode"] == word & 0xF


def test_suspect_wiring_carries_pin_values_for_representative_rows():
    res = assemble_evidence_for_file(
        _BUG3, use_manifest=False, failing_indices=[0, 1],
    )
    w16 = next(w for w in res.payloads[0]["suspect_wiring"]
               if w["component_index"] == 16)
    out_pin = next(p for p in w16["pins"] if p["direction"] == "out")
    assert out_pin["values"] == {"0": 1, "1": 1}, \
        "the carry-in Const must show value 1 on both representative rows"


def test_case3_fixture_is_clean_but_hides_the_mux_bug():
    path = (f"{_BENCH}/bug6_hidden_mux_case3/uncovered_op_calculator.dig")
    res = assemble_evidence_for_file(path, use_manifest=False)
    assert res.mode == "clear"
    from dlc.l3.coverage import scan_tree_coverage
    rep = scan_tree_coverage(path)
    root = rep.circuits[0]
    assert root.flags == []
    assert rep.select_gate and rep.select_gate[0]["missing"] == [3]
    assert not any("never selected" in n for n in (root.notes or []))


def test_focus_is_a_requisite_not_an_amnesty():
    led5 = (f"{_BENCH}/bug5_wrong_boolean_gate_decoder_logic/"
            f"wrong_bool_LED5.dig")
    cells = {i: [{"column": "Ff", "expected": "1", "found": "0"}]
             for i in range(5)}
    res = assemble_evidence_for_file(
        led5, use_manifest=False,
        failing_indices=[0, 1, 2, 3, 4], jar_mismatches=cells,
    )
    assert res.mode == "lazy"
    kinds = [f["kind"] for f in res.gross_flags]
    assert "low_pass_rate" in kinds
    assert "scattered_failures" not in kinds
    res2 = assemble_evidence_for_file(
        led5, use_manifest=False, failing_indices=[0, 1, 2, 3, 4],
    )
    assert res2.mode == "lazy"


def test_scattered_rows_are_lazy_regardless_of_pass_rate():
    led5 = (f"{_BENCH}/bug5_wrong_boolean_gate_decoder_logic/"
            f"wrong_bool_LED5.dig")
    four = [{"column": c, "expected": "1", "found": "0"}
            for c in ("Fa", "Fb", "Fd", "Ff")]
    res = assemble_evidence_for_file(
        led5, use_manifest=False, failing_indices=[1, 2],
        jar_mismatches={1: four, 2: four},
    )
    assert res.mode == "lazy"
    assert [f["kind"] for f in res.gross_flags] == ["scattered_failures"]


def test_rare_scattered_row_in_a_long_suite_passes():
    c, _nl, _g = _parsed(_BUG3)
    on = {"rate_gate_min_components": 0}
    rows_cols = [{"Fa", "Fb", "Fd", "Ff"}] + [{"Ff"}] * 4
    assert gross_check(c, _spec_of(20), failing_count=5,
                       row_mismatch_columns=rows_cols, **on) == []
    flags = gross_check(c, _spec_of(4), failing_count=4,
                        row_mismatch_columns=rows_cols[:4], **on)
    assert "scattered_failures" in [f["kind"] for f in flags]


def test_dead_trunk_full_output_surface_is_analyzable():
    led5 = (f"{_BENCH}/bug5_wrong_boolean_gate_decoder_logic/"
            f"wrong_bool_LED5.dig")
    all_cols = [{"column": c, "expected": "1", "found": "0"}
                for c in ("Fa", "Fb", "Fc", "Fd", "Fe", "Ff", "Fg")]
    res = assemble_evidence_for_file(
        led5, use_manifest=False, failing_indices=[0, 1, 2, 3, 4],
        jar_mismatches={i: list(all_cols) for i in range(5)},
    )
    assert res.mode == "analysis"
    assert res.failing_count == 5
    assert res.clusters and res.clusters[0].merged.suspect_indices()
    assert res.payloads[0].get("suspect_wiring")


def test_suspect_attrs_never_carry_stored_words():
    from types import SimpleNamespace
    from dlc.l3.evidence import _suspect_attrs

    rom = SimpleNamespace(element_name="ROM",
                          attributes={"AddrBits": 3, "Bits": 8,
                                      "Data": "82,86,80"})
    shown = _suspect_attrs(rom)
    assert "stored_words" not in shown
    assert "82,86,80" not in str(shown)
    assert shown["data_words_stored"] == 3
    assert "data_note" not in shown

    empty = SimpleNamespace(element_name="ROM",
                            attributes={"AddrBits": 3, "Bits": 8})
    note = _suspect_attrs(empty)["data_note"]
    assert "EMPTY" in note and "change_attribute" not in note


def test_address_input_drivers_traces_selector_gates():
    from types import SimpleNamespace as NS
    from dlc.l3.evidence import _address_input_drivers

    comps = [NS(element_name="ROM"), NS(element_name="PriorityEncoder"),
             NS(element_name="And"), NS(element_name="Or")]
    circuit = NS(components=comps)

    def pin(ci, name, d):
        return NS(component_index=ci, pin_name=name, direction=d)

    nets = [
        NS(net_id=10, pins=[pin(0, "A", "in"), pin(1, "num", "out")]),
        NS(net_id=11, pins=[pin(1, "in_0", "in"), pin(2, "out", "out")]),
        NS(net_id=12, pins=[pin(1, "in_1", "in"), pin(3, "out", "out")]),
    ]
    netlist = NS(nets=nets)
    rows = [NS(row_index=0,
               net_values={"11": {"value": 0}, "12": {"value": 1}})]

    out = _address_input_drivers(circuit, netlist, 10, 0, rows)
    assert out["selector"] == "PriorityEncoder[1]"
    assert out["inputs"]["in_0"] == {"driven_by": "And[2]",
                                     "values": {"0": 0}}
    assert out["inputs"]["in_1"] == {"driven_by": "Or[3]",
                                     "values": {"0": 1}}

    nets[0].pins.append(pin(2, "out", "out"))
    assert _address_input_drivers(circuit, netlist, 10, 0, rows) is None


_BUG9 = ("data/sample_circuits/30_bug_benchmark/bug9_swapped_select_gate/"
         "mini_alu_swapped_gate.dig")


def test_bug9_payload_names_nets_and_boosts_the_select_path():
    res = assemble_evidence_for_file(_BUG9, use_manifest=False)
    assert res.mode == "analysis" and res.failing_count == 7
    assert any("frozen over the whole testcase" in n and "And[12]" in n
               for n in res.notes)
    payload = res.payloads[0]
    names = payload["cluster"]["net_names"]
    assert {"S1", "isADD", "isXOR"} <= set(names.values())
    top = payload["suspects"]["suspects"][0]
    assert top["component_index"] == 12 and top["element_name"] == "And"
    wiring = next(w for w in payload["suspect_wiring"]
                  if w["component_index"] == 12)
    assert {p.get("net") for p in wiring["pins"]} == {"isADD", "isXOR", "S1"}


def test_payload_sends_nothing_twice():
    res = assemble_evidence_for_file(_BUG9, use_manifest=False)
    payload = res.payloads[0]
    for rep in payload["cluster"]["representative_evidence"]:
        assert all(set(v) == {"bits", "hex"} for v in rep["net_values"].values())
    for w in payload["suspect_wiring"]:
        assert w.get("label") is not None or "label" not in w
        for p in w["pins"]:
            for e in p["connects_to"]:
                assert e.get("label") is not None or "label" not in e
    for s in payload["suspects"]["suspects"]:
        assert None not in s.values()
    tags = [r for s in payload["suspects"]["suspects"] for r in s["reasons"]
            if r.startswith("SELECT-PATH suspect")]
    assert tags and all(len(t) < 90 for t in tags), tags
    assert any(n.startswith("SELECT-PATH: expected value found on net")
               for n in payload["suspects"]["notes"])


_BUG10 = ("data/sample_circuits/30_bug_benchmark/bug10_writeback_select_swapped/"
          "writeback_swapped.dig")


def test_pc_divergence_marks_the_rows_after_the_first_pc_mismatch():
    from dlc.l3.evidence import pc_divergence
    cells = {41: [{"column": "ReadData1"}], 42: [{"column": "ReadData1"}],
             43: [{"column": "PCout"}, {"column": "ReadData1"}],
             44: [{"column": "PCout"}], 45: [{"column": "PCout"}],
             46: [{"column": "PCout"}, {"column": "ReadData2"}]}
    assert pc_divergence([41, 42, 43, 44, 45, 46], cells, "PCout") == (43, [44, 45, 46])
    sporadic = {1: [{"column": "PCout"}], 2: [{"column": "R"}],
                3: [{"column": "R"}], 4: [{"column": "R"}]}
    assert pc_divergence([1, 2, 3, 4], sporadic, "PCout") == (1, [])
    assert pc_divergence([41, 42], cells, "PCout") == (None, [])


def test_bug10_state_trace_points_at_the_write_row_and_its_mux():
    res = assemble_evidence_for_file(_BUG10, use_manifest=False)
    assert res.mode == "analysis" and res.failing_count == 9
    assert any("regfile.dig → rv32i_register_file" in n for n in res.notes)
    payload = res.payloads[0]
    traces = payload["cluster"]["state_trace"]
    assert traces and all(t["written_at_row"] < t["failing_row"] for t in traces)
    first = traces[0]
    assert first["column"] == "ReadData1" and first["register"] == 1
    assert first["written_at_row"] == 0 and "write_row_net_values" in first
    top = payload["suspects"]["suspects"][0]
    assert top["element_name"] == "Multiplexer"
    assert any(r.startswith("SELECT-PATH suspect") and "wrote the register" in r
               for r in top["reasons"])
    notes = payload["suspects"]["notes"]
    assert any(n.startswith("STATE TRACE: ReadData1 on row 2 reads register 1")
               for n in notes)
    assert any(n.startswith("SELECT-PATH at row 0, when register 1 was written")
               for n in notes)


_BUG12 = f"{_BENCH}/bug12_encoder_line"


def _detector(path, label):
    circ = parse_dig_file(path)
    return next(i for i, c in enumerate(circ.components) if c.label == label)


def test_bug12_loud_detector_is_named_by_the_line_witness():
    path = f"{_BUG12}/loud_detector.dig"
    culprit = _detector(path, "is_a")
    res = assemble_evidence_for_file(path, use_manifest=False)
    assert res.mode == "analysis" and res.failing_count == 3
    assert any(n.startswith("line witness:") and f"Or[{culprit}]" in n
               for n in res.notes)
    for cluster, payload in zip(res.clusters, res.payloads):
        top = payload["suspects"]["suspects"][0]
        assert top["component_index"] == culprit and top["element_name"] == "Or"
        row = cluster.rows[0].row_index
        assert any(r.startswith(f"LINE WITNESS: PriorityEncoder[9] input in_0 "
                                f"asserts on row {row}") for r in top["reasons"])
        notes = payload["suspects"]["notes"]
        assert any(n.startswith(f"LINE WITNESS row {row}:")
                   and f"driven by Or[{culprit}]" in n
                   and "idle output" in n for n in notes)
    import json
    assert "9,6,a,5" not in json.dumps(res.payloads)


def test_bug12_silent_detector_is_named_on_both_of_its_rows():
    path = f"{_BUG12}/silent_detector.dig"
    culprit = _detector(path, "is_c")
    res = assemble_evidence_for_file(path, use_manifest=False)
    assert res.mode == "analysis" and res.failing_count == 2
    by_row = {c.rows[0].row_index: p for c, p in zip(res.clusters, res.payloads)}
    assert set(by_row) == {3, 7}
    for row, payload in by_row.items():
        top = payload["suspects"]["suspects"][0]
        assert top["component_index"] == culprit and top["element_name"] == "And"
    silent = by_row[3]["suspects"]
    assert any(r.startswith("LINE WITNESS: PriorityEncoder[9] input in_2 stays "
                            "silent on row 3 although the expected word lives "
                            "at address 2") for r in silent["suspects"][0]["reasons"])
    assert any("the word at address 2, which PriorityEncoder[9] selects when "
               f"in_2 (driven by And[{culprit}]) is 1" in n for n in silent["notes"])
    loud = by_row[7]["suspects"]
    assert any(r.startswith("LINE WITNESS: PriorityEncoder[9] input in_2 asserts "
                            "on row 7") for r in loud["suspects"][0]["reasons"])


def test_line_witness_stays_silent_without_an_encoder_fed_rom():
    from dlc.l3.evidence import selector_line_witness
    from dlc.testing.spec import match_variables_to_io
    from dlc.sim.simulator import simulate_rows

    c, nl, g = _parsed(_BUG3)
    spec = extract_test_specs(c)[0]
    bindings = match_variables_to_io(spec.headers, c)
    sims = simulate_rows(c, nl, g, spec)
    failing = [i for i, s in sims.items()
               if _outputs_report_mismatch(spec, bindings, c, i, s)]
    assert selector_line_witness(c, nl, g, spec, bindings, sims, failing) == {}


def _outputs_report_mismatch(spec, bindings, circuit, idx, sim):
    from dlc.l3.evidence import _outputs_report
    row = next(r for r in spec.rows if r.line_index == idx)
    return bool(_outputs_report(spec, bindings, row, sim)[1])
