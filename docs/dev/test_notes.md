# Testing Notes

Last updated: 2026/7/6

---

## Quick reference

From repo root using bash (These are all sample circuit tests, if you want to manually test a new .dig refer to " How to test manually" in each function specified below):

```bash
uv run pytest                              # Run all tests
uv run pytest -v                           # Show each test name
uv run pytest tests/test_netlist.py        # Run the test in one test file
uv run pytest -k subcircuit                # Name-match filter
uv run pytest tests/test_netlist.py::test_bug_multi_driver_flags_one_net_with_two_drivers
                                           # Run one specific test
```
---

## Function 1 — Parser (`test_parser.py`)

### What F1 produces

A `Circuit` object containing:
- `components`: list of `Component` (element_name, position, attributes dict, label)
- `wires`: list of `Wire` (p1, p2 Position pairs)
- `subcircuits`: list of resolved `SubcircuitReference` (recursively loaded child Circuits)
- `format_version`, `source_path`

### How to test manually

Baisc info:

```python
uv run python -c "
from dlc.parser.dig_parser import parse_dig_file
c = parse_dig_file('data/sample_circuits/tier3_realistic/tier3_calculator.dig') # your .dig
print(f'Counts: components={len(c.components)} wires={len(c.wires)} subcircuits={len(c.subcircuits)}')
print()
print('Inputs:')
for inp in c.inputs():
    print(f'  {inp.label} ({inp.bit_width()}-bit)')
print('Outputs:')
for out in c.outputs():
    print(f'  {out.label} ({out.bit_width()}-bit)')
print()
print('All components (use the index for c.components[i] in the pin-geometry test):')
for i, comp in enumerate(c.components):
    attrs = {k: v for k, v in comp.attributes.items() if k != 'Label'}
    print(f'  [{i}] {comp.element_name} @ ({comp.position.x},{comp.position.y}) label={comp.label} attrs={attrs}')
"
```

---

## Function 2 — Netlist + signal-flow graph

Split across three test files: `test_pin_geometry.py`, `test_netlist.py`, `test_graph.py`.

### F2: Pin geometry (`test_pin_geometry.py`)

#### What it produces

For each Component, the absolute pin positions and directions (after applying rotation). This is the table that lets F2 know "this AND gate's `in1` is at coord (X, Y) on the canvas."

#### How to test manually

```python
uv run python -c "
from dlc.parser.dig_parser import parse_dig_file
from dlc.parser.pin_geometry import absolute_pin_positions
c = parse_dig_file('data/sample_circuits/tier3_realistic/tier3_calculator.dig')
for pos, spec in absolute_pin_positions(c.components[7]):
    print(spec.name, (pos.x, pos.y), spec.direction)
"
```

### F2: Netlist construction (`test_netlist.py`)

#### What it produces

A `NetList` where each `Net` has:
- `coords`: every (x, y) point that's electrically the same signal
- `pins`: every pin (component_index, pin_name, direction) attached to this net
- `tunnel_names`: any Tunnel NetNames merging into this net
- `drivers()` / `sinks()` helpers

Plus `summary()`: `"NetList: N nets, M driven, K undriven-with-pins, J multi-driver"`.

#### How to test manually

```python
uv run python -c "
from dlc.parser.dig_parser import parse_dig_file
from dlc.parser.netlist import build_netlist
c = parse_dig_file('data/sample_circuits/tier3_realistic/tier3_calculator.dig')
nl = build_netlist(c)
print(nl.summary())
print()
print('All nets:')
for net in nl.nets:
    flags = []
    if net.pins and not net.drivers():
        flags.append('DANGLING')
    if len(net.drivers()) > 1:
        flags.append('MULTI-DRIVER')
    flag_str = ' [' + ','.join(flags) + ']' if flags else ''
    pins = [(c.components[p.component_index].element_name, p.pin_name, p.direction) for p in net.pins]
    tnames = sorted(net.tunnel_names) if net.tunnel_names else None
    print(f'  net {net.net_id}{flag_str}: coords={len(net.coords)}, pins={pins}, tunnels={tnames}')
"
```

### F2: Signal-flow graph + reachability (`test_graph.py`)

#### What it produces

A `networkx.MultiDiGraph`:
- Nodes = component indices (with `element_name` and `component` attrs).
- Edges = directed driver→sink within each net (with `driver_pin`, `sink_pin`, `net_id` attrs).

Plus helpers: `input_component_indices`, `output_component_indices`, `reachable_outputs_from_inputs`.

#### How to test manually

```python
uv run python -c "
from dlc.parser.dig_parser import parse_dig_file
from dlc.parser.netlist import build_netlist
from dlc.parser.graph import build_signal_graph, reachable_outputs_from_inputs
c = parse_dig_file('data/sample_circuits/tier3_realistic/tier3_calculator.dig')
nl = build_netlist(c)
g = build_signal_graph(c, nl)
reach = reachable_outputs_from_inputs(c, g)
for in_idx, outs in reach.items():
    print(c.components[in_idx].label, '->',
          [c.components[i].label for i in outs])
"
```


Text dumb visulization:

```python
uv run python -c "
from dlc.parser.dig_parser import parse_dig_file
from dlc.parser.netlist import build_netlist
from dlc.parser.graph import build_signal_graph
c = parse_dig_file('data/sample_circuits/tier3_realistic/tier3_calculator.dig')
nl = build_netlist(c)
g = build_signal_graph(c, nl)
print(f'Nodes: {g.number_of_nodes()}, Edges: {g.number_of_edges()}')
print()
print('Edges (driver -> sink, with pin names and net id):')
for u, v, data in g.edges(data=True):
    src = c.components[u]
    dst = c.components[v]
    src_lbl = src.label or src.element_name
    dst_lbl = dst.label or dst.element_name
    driver_pin = data['driver_pin']
    sink_pin = data['sink_pin']
    net_id = data['net_id']
    print(f'  [{u}] {src_lbl}.{driver_pin} -> [{v}] {dst_lbl}.{sink_pin}  (net {net_id})')
"
```


Visulization:

```python
uv run --with matplotlib python -c "
from dlc.parser.dig_parser import parse_dig_file
from dlc.parser.netlist import build_netlist
from dlc.parser.graph import build_signal_graph
import networkx as nx, matplotlib.pyplot as plt

c = parse_dig_file('data/sample_circuits/tier3_realistic/tier3_calculator.dig')
nl = build_netlist(c)
g = build_signal_graph(c, nl)

try:
    topo = list(nx.topological_sort(g))
except nx.NetworkXUnfeasible:
    topo = list(g.nodes())
in_idxs = [i for i, comp in enumerate(c.components) if comp.element_name == 'In']
out_idxs = [i for i, comp in enumerate(c.components) if comp.element_name == 'Out']
layer = {i: 0 for i in in_idxs}
for node in topo:
    if node in layer: continue
    preds = list(g.predecessors(node))
    layer[node] = max((layer.get(p, 0) for p in preds), default=0) + 1
max_l = max(layer.values()) if layer else 0
for i in out_idxs: layer[i] = max_l + 1

keep = [n for n in g.nodes() if g.degree(n) > 0]
gs = g.subgraph(keep).copy()
for n in gs.nodes(): gs.nodes[n]['subset'] = layer.get(n, 0)

def col(comp):
    e = comp.element_name
    if e == 'In': return '#90caf9'
    if e == 'Out': return '#ffcc80'
    if e in ('And', 'Or', 'XOr', 'Not', 'NAnd', 'NOr', 'XNOr'): return '#a5d6a7'
    if e == 'Multiplexer': return '#ce93d8'
    if e == 'Splitter': return '#fff59d'
    if e.endswith('.dig'): return '#ef9a9a'
    if e == 'Comparator': return '#80cbc4'
    if e == 'Add': return '#ffab91'
    if e in ('Tunnel', 'Const', 'Ground', 'VDD', 'Clock'): return '#e0e0e0'
    return '#bdbdbd'

pos = nx.multipartite_layout(gs, subset_key='subset')
colors = [col(c.components[n]) for n in gs.nodes()]
labels = {n: (c.components[n].label or c.components[n].element_name) + f'\n[{n}]' for n in gs.nodes()}

plt.figure(figsize=(20, 12))
nx.draw(gs, pos, labels=labels, node_color=colors, node_size=2200,
        font_size=9, font_weight='bold', arrows=True, arrowsize=18,
        edge_color='#555555', width=1.3,
        connectionstyle='arc3,rad=0.08')
plt.title('tier3_calculator.dig — signal-flow graph', fontsize=15)
plt.axis('off')
plt.tight_layout()
plt.savefig('graph.png', dpi=200, bbox_inches='tight', facecolor='white')
print('Saved to graph.png')
"
```

---

## Function 3 — Structural fact extractor

### How to test manually

Per net width:

```python
uv run python -c "
from dlc.parser.dig_parser import parse_dig_file
from dlc.parser.netlist import build_netlist
from dlc.facts.net_width import infer_net_widths
c = parse_dig_file('data/sample_circuits/tier3_realistic/tier3_calculator.dig')
nl = build_netlist(c)
per_net, conflicts = infer_net_widths(c, nl)
for nid, info in per_net.items():
    print(f'  net {nid}: width={info.width} source={info.source}')
print(f'conflicts: {conflicts}') "
```


Fact extractor:

```python
uv run python -c "
from dlc.parser.dig_parser import parse_dig_file
from dlc.facts.extractor import extract_facts
c = parse_dig_file('data/sample_circuits/tier1_bug/unused_top_output.dig')
f = extract_facts(c)
print(f.header); print()
print('I/O:'); [print(f'  {io.direction.upper()} {io.label} {io.bit_width}-bit @ {io.position}') for io in f.inputs + f.outputs]
print('\nSubcircuits:'); [print(f'  {s.reference}: in={s.child_inputs} out={s.child_outputs}') for s in f.subcircuits]
print('\nNets:'); [print(f'  net {n.net_id}: {n.pin_count}p ({n.driver_count}d/{n.sink_count}s) {n.bit_width}-bit anomalies={n.anomalies}') for n in f.nets]
print('\nComponents (graph view):')
[print(f'  [{c.index}] {c.display_name} ({c.element_name}) preds={c.predecessors} succs={c.successors}') for c in f.components]
print('\nBugs:'); [print(f'  [{b.kind}] {b.description}') for b in f.bugs]
"
```

Json Bundle:

```python
uv run python -c "
from dlc.parser.dig_parser import parse_dig_file
from dlc.facts.extractor import extract_facts
c = parse_dig_file('data/sample_circuits/tier3_realistic/tier3_calculator.dig')
print(extract_facts(c).to_json(indent=2))
" | less
```

## Function 4 — Test result parser

F4 turns each `.dig`'s embedded Testcase data into structured test specs,
parses Digital's CLI text output, and (optionally) re-runs Digital
one row at a time to identify which specific rows fail. Tests across
four files:

- `tests/test_testing_spec.py` — testData → `TestSpec`.
- `tests/test_testing_results.py` — CLI text → `TestRunResults`.
- `tests/test_testing_run.py` — `TestSpec × TestRunResults` join.
- `tests/test_testing_runner.py` — Digital subprocess per-row runner.
- `tests/test_testing_config.py` — jar location config + file picker.

#### How to test manually

Set up the jar path:

```python
uv run python -c "
from dlc.testing.config import (
    set_digital_jar_path, get_configured_jar, prompt_for_jar_path,
)
TARGET_JAR = r'"C:/Users/.../Digital.jar"'
set_digital_jar_path(TARGET_JAR)
print(f'Saved: {get_configured_jar()}')
"
```

Rough overview:

```python
uv run python -c "
from dlc.parser.dig_parser import parse_dig_file
from dlc.testing.spec import extract_test_specs, match_variables_to_io

TARGET_DIG = 'data/sample_circuits/tier3_realistic/tier3_latched_display.dig'

circuit = parse_dig_file(TARGET_DIG)
for spec in extract_test_specs(circuit):
    print(f'Testcase \"{spec.name}\"')
    print(f'  headers: {spec.headers}')
    print(f'  rows: {spec.well_formed_row_count()}/{spec.row_count()} well-formed')
    print(f'  has_unexpanded_loops: {spec.has_unexpanded_loops}')
    bindings = match_variables_to_io(spec.headers, circuit)
    print(f'  bindings:')
    for var, b in bindings.items():
        bw = f'{b.bit_width}-bit' if b.bit_width else 'n/a'
        print(f'    {var:14s} → {b.role:7s} {bw}')
    if spec.rows:
        print(f'  first 3 rows:')
        for row in spec.rows[:3]:
            tokens = [f'{t.raw}={t.value if t.kind == \"int\" else t.kind}' for t in row.values]
            print(f'    [{row.line_index}] {\" \".join(tokens)}')
"
```

### Function 4: per-row vs overall — three manual test modes

DLC offers three ways to check a circuit's testcases. Use whichever
the situation calls for.

#### Mode C — fast per-row (the default; one subprocess total)

One `CLI test -verbose` invocation for the whole file. Digital prints
its own value table under each failed testcase (one line per executed
row, mismatched cells as `E: expected / F: found` — see
`digital_notes.md` § CLI Mode), and the runner maps table line i onto
spec row i. Returns every row's pass/fail PLUS the exact
expected-vs-found cells per failing row (`PerRowResult.mismatches`).
Falls back to Mode B per testcase when the 1:1 mapping can't be
trusted (unexpanded loop expressions, malformed rows, table anomaly).

```python
uv run python -c "
from dlc.parser.dig_parser import parse_dig_file
from dlc.testing.spec import extract_test_specs
from dlc.testing.runner import per_file_run_fast, find_digital_jar

TARGET_DIG = 'data/sample_circuits/30_bug_benchmark/bug4_missing_pipeline/Missing_pipeline.dig'

circuit = parse_dig_file(TARGET_DIG)
specs = [s for s in extract_test_specs(circuit) if s.rows]
results, fallback = per_file_run_fast(specs, TARGET_DIG, jar_path=find_digital_jar())
for name, rows in results.items():
    print(f'Testcase {name!r}:')
    for r in rows:
        line = f'  [{r.status:>6}] row {r.row_index}'
        if r.mismatches:
            line += '  ' + ', '.join(
                f\"{m['column']}: expected {m['expected']} got {m['found']}\"
            for m in r.mismatches)
        print(line)
print('fallback needed for:', [s.name for s in fallback])
"
```

Wallclock: one JVM startup (~1 s) regardless of row count.
`per_row_run_auto(spec, path)` wraps the fast-then-fallback policy for
a single testcase.

#### Mode A — overall pass/fail (fast)

One Digital invocation on the unmodified .dig. Returns the testcase's
overall pass/fail and (on failure) a percentage.

```python
uv run python -c "
from dlc.testing.runner import find_digital_jar, run_digital_cli
from dlc.testing.results import parse_cli_output

TARGET_DIG = 'data/sample_circuits/tier3_realistic/tier3_calculator.dig'

jar = find_digital_jar()
code, out = run_digital_cli(TARGET_DIG, jar)
run = parse_cli_output(out)
for tc in run.testcases:
    pct = f' ({tc.fail_pct}%)' if tc.fail_pct is not None else ''
    print(f'  [{tc.status}] {tc.name}{pct}')
"
```

Wallclock: 1–3 seconds total.

#### Mode B — cumulative per-row (reference + fallback, N subprocesses)

One Digital invocation per row, with each prefix containing rows 0..K
so state accumulates correctly. Returns each row's individual pass/fail
(inferred from the failure-percentage delta between prefixes). Kept as
the reference implementation Mode C is validated against, and as Mode
C's automatic fallback.

```python
uv run python -c "
from dlc.parser.dig_parser import parse_dig_file
from dlc.testing.spec import extract_test_specs
from dlc.testing.runner import per_row_run, find_digital_jar

TARGET_DIG = 'data/sample_circuits/tier3_realistic/tier3_latched_display.dig'

circuit = parse_dig_file(TARGET_DIG)
spec = extract_test_specs(circuit)[0]
jar = find_digital_jar()
print(f'Running {spec.row_count()} rows of testcase \"{spec.name}\" cumulatively...')
per_row = per_row_run(spec, TARGET_DIG, jar_path=jar)
print()
print(f'{\"row\":>3}  {\"status\":<6}  row text')
print('-' * 60)
for r in per_row:
    raw = spec.rows[r.row_index].raw
    marker = {'passed':'PASS', 'failed':'FAIL', 'no_run':'SKIP', 'error':'ERR'}[r.status]
    print(f'{r.row_index:>3}  {marker:<6}  {raw}')
    if r.error_message:
        print(f'      ↳ {r.error_message}')
"
```

Wallclock: roughly N × JVM-startup, roughly N seconds. Takes minutes for files with more than 100 tests.

#### When to use which

| Need | Use | Wallclock |
|---|---|:-:|
| Per-row results + expected-vs-found cells (any circuit, incl. stateful) | Mode C | ~1 s/file |
| Overall pass/fail only | Mode A | ~1 s/file |
| Verifying Mode C's mapping, or specs Mode C can't trust (unexpanded loops / malformed rows) | Mode B | ~1 s/row |

The web UI's per-row runner and the "Test all" toolbar button both use
Mode C (per-spec fallback to Mode B happens automatically).
---


## Key tests — what each guarantees

#### Function 1 — Parser

| Test | If it fails… | File |
|---|---|---|
| `test_subfolder_reference_resolves` | Recursive subcircuit search broke; references in `subs/` folders won't load. | `tests/test_parser.py` |
| `test_ambiguous_subcircuit_flagged_but_resolved` | Two files with the same name no longer flagged;parent parse may silently pick the wrong one. | `tests/test_parser.py` |
| `test_missing_subcircuit_recorded_not_crashed` | A missing subcircuit file now crashes the parser instead of attaching a `resolution_error`. | `tests/test_parser.py` |
| `test_testcase_dataString_extracted_into_attribute` | F4 will break — we're back to capturing whitespace instead of the real test rows. | `tests/test_parser.py` |
| `test_testcase_with_clock_token_captured` | Clock pulse `C` token is being eaten somewhere on the way from XML to attribute. | `tests/test_parser.py` |


#### Function 4 — Test extractor + runner

| Test | If it fails… | File |
|---|---|---|
| `test_tokenize_*` (decimal / hex / binary / parens-negative / clock / highZ / dontcare / loop_expr) |One of Digital's testdata cell forms no longer parses. | `tests/test_testing_spec.py` |
| `test_parse_data_string_expands_loop_blocks` | `loop(N, 30) … end loop` no longer expands;register-file's 93 rows collapse. | `tests/test_testing_spec.py` |
| `test_loop_expansion_wraps_negative_results_in_parens` | Negative loop-expansion outputs back to bare `-60`; Digital rejects them and per-row hits "saw: \<none\>". | `tests/test_testing_spec.py` |
| `test_parse_handles_java_log_noise_around_result` | Autograder Java logger noise around `cpu: passed` is no longer tolerated. | `tests/test_testing_results.py` |
| `test_lines_with_colons_but_not_results_are_ignored` | `INFO: Created user preferences directory.`gets misread as a testcase result. | `tests/test_testing_results.py` |
| `test_join_with_matching_cli_result` | TestSpec × TestRunResults join broken — L3 can't get its single-shape input. | `tests/test_testing_run.py` |
| `test_failing_rows_returns_rows_marked_failed_by_per_row_runner` | `TestRun.failing_rows()` can't surface the actual failing rows. | `tests/test_testing_run.py` |
| `test_per_row_run_cumulative_all_pass` | Cumulative runner regressed on the all-pass path. | `tests/test_testing_runner.py` |
| `test_per_row_run_cumulative_isolates_failing_row` | Middle-row failure no longer pinpointed via the percentage-delta inference. | `tests/test_testing_runner.py` |
| `test_per_row_run_cumulative_detects_multiple_failures` | Multi-failure case (bug1's two FAIL rows) no longer attributed correctly. | `tests/test_testing_runner.py` |
| `test_verbose_parse_*` (failed table / passed / two testcases / spaced name / tc error / log noise) | `CLI test -verbose` value-table parsing broke; the fast per-row path loses its source. | `tests/test_testing_fast_runner.py` |
| `test_map_section_*` | Table-line → spec-row 1:1 mapping (or its refuse-and-fallback guard)regressed. | `tests/test_testing_fast_runner.py` |
| `test_fast_equals_cumulative_on_30_bug_circuit` | Fast results no longer match the cumulative reference (issue #11 acceptance; needs `DIGITAL_JAR`). | `tests/test_testing_fast_runner.py` |
| `test_prompt_for_jar_path_saves_when_user_picks_file` | First-run tkinter dialog → config-save flow broken; students would re-prompt every run. | `tests/test_testing_config.py` |
| `test_set_digital_jar_path_writes_config` | The "set once via Python" path doesn't persist. | `tests/test_testing_config.py` |

#### Layer 3 — oracle (temp-inject + rerun)

| Test | If it fails… | File |
|---|---|---|
| `test_injected_temp_differs_only_inside_datastring` | Row injection is no longer byte-preserving; a temp circuit may differ structurally from the student's original. | `tests/test_l3_oracle.py` |
| `test_injection_targets_named_testcase_only` | Multi-testcase targeting broke; injecting into one testcase corrupts another. | `tests/test_l3_oracle.py` |
| `test_validate_rejects_*` | Bad coach/student rows reach the temp file instead of being rejected up front. | `tests/test_l3_oracle.py` |
| `test_rerun_flags_wrong_added_row_and_keeps_originals_green` | The Mode-B accept-flow's core signal (added-row pass/fail + expected-vs-found) regressed. | `tests/test_l3_oracle.py` |
| `test_rerun_all_added_pass_sets_mode_b_lock_signal` | `added_all_passed` (Mode B's "you're all set" /daily-lock signal) or temp-file cleanup regressed. | `tests/test_l3_oracle.py` |

#### Telemetry sink + session GC

| Test | If it fails… | File |
|---|---|---|
| `test_log_events_stores_kind_ts_and_details` | The SQLite sink drops or mangles events; the IRB telemetry pairs stop recording. | `tests/test_telemetry_sink.py` |
| `test_log_events_skips_malformed_entries_without_failing_the_batch` | One bad frontend event can now kill a whole telemetry batch. | `tests/test_telemetry_sink.py` |
| `test_telemetry_endpoint_stores_batch` | `/api/telemetry` broke; `window.dlcEventLog` flushes go nowhere. | `tests/test_telemetry_sink.py` |
| `test_gc_sessions_removes_idle_sessions_and_their_tmp_dirs` | Idle sessions / upload temp dirs accumulate forever (pre-cohort leak). | `tests/test_telemetry_sink.py` |
| `test_activity_defers_the_session_ttl` | Active students can lose their session mid-lab to the GC. |`tests/test_telemetry_sink.py` |
| `test_upload_runs_the_gc_sweep` | The GC no longer rides on uploads; nothing ever gets collected. |`tests/test_telemetry_sink.py` |

## When you add a new test

1. One test file per source module (`tests/test_<package>_<module>.py`);
   keep fixtures inline when they're small captured texts (see
   `test_testing_fast_runner.py`) and in `data/sample_circuits/` when
   they're circuits.
2. Tests that shell out to Digital must skip cleanly when the jar is
   absent: gate on `DIGITAL_JAR`/`find_digital_jar()` with
   `pytest.mark.skipif` so CI and fresh clones stay green.
3. Add a row to the "Key tests" table above saying what a failure of
   the new test means.
