# L3 `/api/llm/debug` — Mode A model-call I/O contract (`l3.debug.v1.1`)

The contract string `l3.debug.v1.1` is stamped on every evidence payload
and required in every model reply: `validate_hypothesis` rejects a reply
whose `contract` differs. Any change to the payload shape (§3), the reply
shape (§4) or the ops vocabulary bumps the string. Everything below is the
behavior of the code as it stands:

| Part | Lives in |
|---|---|
| Route, target selection, limits, accept/retest | `dlc/web/l3_routes.py` |
| Coordinator (gates, model calls, verify, cards) | `dlc/l3/debugger.py` |
| Evidence: replay, gross checks, clustering, payload | `dlc/l3/evidence.py` |
| Fault localizer (suspect ranking) | `dlc/l3/localizer.py` |
| Patch applier and re-run | `dlc/l3/patch.py` |
| Formula models for passing subcircuits | `dlc/sim/models.py` |
| Prompt | `prompts/l3_modeA_hypothesis_v1.txt` |
| Daily caps | `dlc/l3/limits.py` |
| Client boards and ladder | `dlc/web/static/l3.js` |

---

## 1. Endpoint + scope

`POST /api/llm/debug` — Mode A coordinator. Explicit trigger only (the
"Analyze failing rows" button).

Request (client → server):

```json
{"session_id": "...", "filename": "cpu.dig", "spec_index": 0, "model": null}
```

`model` is optional (the board's model picker). When absent the server
uses, in order: env `DLC_L3_DEBUG_MODEL`, `l3_debug_model` in
`~/.dlc/config.json`, the built-in default. Calls go through
`dlc/llm/client.call_llm`, so a configured course proxy relays them.

Scope: Mode A debugs the selected file's own testcase (`spec_index`, one
testcase per run). When this session has a coach temp for the file
(`session["l3_temp"].for == filename` — created by Mode B's Accept or by
Accept fix, §7), the run targets that temp instead: original circuit plus
the accepted rows, plus any earlier accepted fix. When the file's testcase
is missing, header-only or modified and an official test set exists for
the filename, the run targets a sibling injected temp carrying the
official rows (`dlc/testing/inject.prepare_injected_run`); ROM contents
are never injected except the course program into an EMPTY program ROM
(`rom_injected`). The circuit cannot be switched inside Layer 3.

## 2. Coordinator pipeline (deterministic, server-side)

Gates before any evidence, in order:

1. **Client lock.** The Layer 3 boards are locked while the file has any
   Layer-1 error; the Analyze button is enabled only after a per-row test
   run on the Dashboard reported at least one failing row (or the coach
   temp has failing accepted rows).
2. **Transistor guard.** A tree containing switch-level elements returns
   `{"ok": false, "unsupported": true, "mode": "unsupported", "cards": []}`.
3. **Daily cap.** `limits.allowed("modeA")` — only enforced when
   `DLC_ENFORCE_LIMITS` is on (§10); otherwise `{"ok": false,
   "limited": true, "warning": ..., "limits": ...}`.
4. **Parse + testcase pick.** Unparsable file, no testcase or a bad
   `spec_index` → `mode: "error"` with a `warning`.
5. **Manifest attachment.** The manifest whose `applies_to` covers the most
   uploaded filenames (top file plus referenced children) wins; ties keep
   file order; an element-hook manifest is the fallback.
6. **Failing children.** Every subcircuit runs its own testcase (its
   official set when one is registered for its filename) through the
   evaluator. Any failing row → `mode: "lazy"` with a
   `subcircuit_failing` / `subcircuit_failing_official` flag: fix the
   child first.
7. **Per-row verdicts.** With a Digital.jar configured, `per_row_run_auto`
   (the fast runner, then the per-row runner) gives the failing rows and
   their mismatched cells; `row_verdict_runner: "digital"`. Every row
   erroring means Digital refused the build → `mode: "lazy"` with
   `build_refused`, or `unbound_columns` when testcase columns match no
   In/Out/Clock label. Without a jar the Python evaluator judges the rows
   (`"evaluator"`).

Evidence stage (`assemble_evidence`):

8. **Formula models.** Each passing subcircuit is replaced by the function
   it computes when a model fits its interface and reproduces every row of
   the child's own testcase, or when the manifest's `subcircuits` block
   vouches for it by name. The notes list the substitutions
   (`subcircuits evaluated as formula models: alu.dig → rv32i_alu, …`) and
   why a child stayed gate-level. Layer 1 never uses models.
9. **One replay of the whole testcase** (`simulate_rows`, register state
   carried between rows). It yields every failing row's net values and,
   across all rows, the components whose output never changes (§3).
10. **Gross checks** (`gross_check`). Skipped entirely for control-unit
    files (`controlunit.dig` / `control-unit.dig` and their injected temps,
    matched case- and punctuation-insensitively); the refusal guards above
    still apply to them. Checked in order:
    - `scattered_failures` — only for trees with more than 30 components
      and no frozen trunk: rows wrong in 4 or more output columns at once
      reach 25% of the testcase's well-formed rows.
    - `unbound_columns`; `missing_clocked_logic` (the testcase steps a
      clock but the tree holds no state element).
    - Pass-rate bars, only for trees with more than 30 components and no
      frozen trunk: 11 or more rows → `too_many_failures` when more than
      20 rows fail AND under 20% pass; 6–10 rows → `low_pass_rate` under
      60% passing; 1–5 rows → under 30%.
    Any flag → `mode: "lazy"`, no model call, no daily use consumed. A
    tree of 30 components or fewer is always analyzable.
11. **Clustering** (`cluster_rows`). Bucket key = (mismatched output
    columns, values of the select columns — inputs that drive a mux
    `sel` or are named like op/opcode/sel/mode/ctrl/aluop/funct —, the
    manifest-decoded instruction category of the word on the program
    ROM's output net). Rows with the same key join a cluster when the
    Jaccard overlap of their top-5 suspects is at least 0.5. Cap 4
    clusters; the smallest overflow cluster folds into the neighbor with
    the highest suspect overlap, never dropped. Frozen trunk — every
    failing row shows the same wrong value per column while the passing
    rows expect one constant — makes a single cluster so a fix must repair
    every row.
11b. **PC divergence** (labs whose manifest names `observe.pc_port`): once
    the program counter is wrong on a row and stays wrong on at least 90%
    of the failing rows after it (3 or more), those later rows are
    consequences of the divergence. They stay out of the evidence and
    the clusters (`consequential_rows`, a note and a diagnosis line say
    so) but every fix is still verified against them.
12. **Per-cluster evidence**: full net values for the first 2 rows of the
    cluster, compact expected-vs-found for the rest, `localize()` per row,
    `merge_reports()` per cluster, one payload per cluster.

## 3. Model INPUT — the evidence payload (one call per cluster)

```json
{
  "contract": "l3.debug.v1.1",
  "circuit": { "inventory": {}, "inputs": [], "outputs": [], "subcircuits": [],
               "has_clock": false, "has_register": false, "has_rom": false,
               "roms": [], "testcases": [], "inverted_inputs": [], "selectors": [] },
  "testcase": { "name": "...", "headers": ["A", "B", "..."] },
  "cluster": {
    "rows": [
      { "index": 6, "raw": "5 10 0 3 15 0 0 1",
        "mismatches": [ {"column": "Result", "expected": "15", "found": "0"} ] }
    ],
    "representative_evidence": [
      { "row_index": 6,
        "net_values": { "10": {"bits": 4, "hex": "0"} },
        "unresolved_nets": [9],
        "outputs": [ {"label": "Result", "expected": "15", "found": "0x0", "ok": false} ] }
    ],
    "net_names": { "10": "isShiftGroup", "12": "ALUOp" }
  },
  "suspects": { "failing_outputs": [], "passing_outputs": [],
                "suspects": [ { "component_index": 159, "element_name": "And",
                                "display_name": "And[159]", "score": 7.1,
                                "reasons": ["..."], "in_failing_cones": [],
                                "in_active_cones": [], "feeds_passing_output": true,
                                "drives_unresolved": false, "is_subcircuit": false } ],
                "notes": ["SELECT-PATH: expected value found on net ShiftOut (arm in2 of Multiplexer[9]) while the row selects arm in0 — ..."] },
  "suspect_wiring": [
    { "component_index": 16, "element": "Const", "label": null,
      "attrs": { "Value": 1, "Bits": 1 },
      "pins": [ { "pin": "out", "direction": "out", "net_id": 7, "net": "cin",
                  "connects_to": [ {"component_index": 5, "element": "Add",
                                    "label": null, "pin": "c_i", "direction": "in"} ],
                  "values": { "6": 1 } } ] }
  ]
}
```

- `circuit` is the compact CircuitFacts view Layer 2 also uses.
- `cluster.net_names` maps net ids to the student's own names (tunnel
  NetName, else the label of an In/Out/Clock on the net) for the nets in
  `representative_evidence`. The same name appears as `net` on every
  `suspect_wiring` pin entry.
- `cluster.state_trace` (only when a failing column is a register-file
  READ, i.e. the output is driven by a subcircuit instance with
  `ReadRegN`/`WriteReg`/`WriteData`/`RegWrite` pins): for each
  representative row, the register it read, the last earlier row that
  wrote it (`written_at_row`), `expected` vs `read_back`, and that write
  row's full `write_row_net_values`. The witness search then runs on the
  write row below the instance, so the write-data multiplexer and its
  select logic are boosted (`… at the row that wrote the register`) and
  `suspects.notes` carries `STATE TRACE: …` plus the select-path sentence
  for that row.
- `suspects` is the merged localizer report. Per row, every component in
  the static cone of a failing output is scored: on the row's ACTIVE path
  (mux arms actually selected) +3.0, plus +1.0 per additional failing
  output it is active for; merely upstream +1.0; also feeding a passing
  output −1.0; driving a net the evaluator left unresolved +1.5;
  Multiplexer / Decoder / Splitter +0.5. Two signals from the replay:
  *select-path* — the failing output's expected value already sits on
  another net of its cone and a multiplexer fed by that net selected a
  different arm: the mux and the logic behind its `sel` (only the
  differing sel bits when `sel` is a bus joined by a Splitter) get
  `SELECT-PATH suspect: on the logic behind sel bit K of Multiplexer[m]`
  (the full finding is written once in `suspects.notes`), +2.5 fading by
  0.1 per hop from the mux; it
  is skipped for 1-bit outputs, values below 8 or all-ones, values seen
  on more than 3 nets, and constants or raw inputs as witnesses.
  *Frozen output* — every output net of a component keeps one value over
  the whole testcase (at least 6 rows) while an input varies: `its output
  never changes over the whole testcase …`, +2.0 (storage elements and
  subcircuit instances excluded). In, Clock, Tunnel, Testcase and
  Rectangle are never suspects. Each row keeps its top 12; the merge
  averages scores, adds the share of rows a suspect appears on (with the
  reason `suspected on all N rows of the cluster`) and keeps 12.
- `suspect_wiring` covers every ranked suspect plus every storage element
  (ROM, RAM, EEPROM, RAMDualPort, LookUpTable) whether suspected or not:
  each pin's net, far ends (up to 6, tunnels resolved; a `label` key only
  when the component has one) and its value on
  the representative rows. `attrs` carries the fix-relevant attributes
  (Bits, Value, Selector Bits, splitting ranges, inputBits/outputBits,
  Signed, …). Storage records add `data_words_stored`, either a
  `data_note` (empty Data, with the exact op to program it) or
  `stored_words` (32 words or fewer, hidden when the run used the
  injected course program), `address_by_row`, `address_input_drivers`,
  `output_bit_map` and `expected_outputs_by_row`.
- Nothing is sent twice: net values carry `hex` and `bits` only (the
  decimal duplicate stays server-side), null-valued suspect fields and
  empty labels are omitted, and a select-path finding is spelled out once
  in `suspects.notes` while each boosted suspect carries the short tag.
  The payload is serialized without indentation.
- A payload over 250,000 characters is slimmed to the nets that appear in
  `suspect_wiring`; the run notes say so.

The prompt is `prompts/l3_modeA_hypothesis_v1.txt` with `<<PAYLOAD_JSON>>`
replaced. Appended blocks: `[ROM NOTE]` when the course program was
injected into an empty ROM; `[PROGRAM MEMORY]` when the file's
program-memory ROM holds the student's own program and the official store
has a runtime program for that filename (Data changes on it are stripped
before verification); `# FORMAT RETRY` after a reply that is not the
strict JSON object (once); `[REFUTED ATTEMPT]` after a refuted fix (once
per cluster) with the re-run's still-failing and regressed rows, a
partial-fix steer when the refuted ops repaired some cluster rows, and a
stored-data steer when a Data rewrite was refuted; `[ESCALATION]` on the
final attempt (§5). Every call is one plain completion: no tools, no
iteration; the model reasons only over the payload and never invents
nets, widths or values.
Output budget by model tier: 3000 tokens, 8000 for premium models, 16000
for reasoning models (called with low effort).

## 4. Model OUTPUT — the hypothesis reply

One call returns both ladder levels: the client shows only `hint` at
level 1 and reveals `fix` at level 2 on the student's "Show me more".
`hint.*` must not state the concrete repair; `fix.explanation_for_student`
teaches.

```json
{
  "contract": "l3.debug.v1.1",
  "confidence": 0.9,
  "hint": {
    "suspect_region": "the adder's carry-in constant",
    "suspect_signals": ["c_i"],
    "why": "every failing row's Sum is exactly one too high"
  },
  "fix": {
    "ops": [
      {"op": "change_attribute", "component_index": 16, "name": "Value", "value": 0}
    ],
    "explanation_for_student": "the Const driving c_i omits Value, which defaults to 1 — every sum gained +1",
    "animation_script": [
      {"act": "diagnose_line", "text": "Rows 1-3 fail: Sum is always 1 too high."},
      {"act": "focus", "component_index": 5, "path": []},
      {"act": "mark_fix", "target": {"component_index": 16, "path": []},
       "label": "fixed: carry-in constant 1 -> 0 (was adding +1 to every sum)"},
      {"act": "retest"}
    ]
  }
}
```

Validation (`validate_hypothesis`; failure triggers the one format
retry, then the cluster is dropped as `invalid_response`): the reply is
the JSON object between the first `{` and the last `}` of the text;
`contract` must match;
`hint.suspect_region` is required; `fix.ops` holds 1 to 6 ops, each from
the vocabulary below with its required fields present; `confidence` is
clamped to 0..1 (default 0.5); `suspect_signals` keeps at most 8 entries;
all text is sanitized.

`fix.ops` vocabulary (`dlc/l3/patch.py`; `component_index` refers to the
ORIGINAL circuit; deletes apply last, highest index first; a component
added in the same patch is wired with `add_wire`, pin-level ops cannot
target it):

| op | required fields |
|---|---|
| `change_attribute` | `component_index`, `name`, `value` |
| `replace_element` | `component_index`, `new_element` |
| `swap_pins` | `component_index`, `pin_a`, `pin_b` |
| `rewire_pin` | `component_index`, `pin`, `to` (`{component_index, pin}`) |
| `add_wire` / `delete_wire` | `p1`, `p2` |
| `add_component` | `element_name`, `position` (+ optional `attributes`) |
| `delete_component` | `component_index` |

### animation_script acts

| act | fields | plays as |
|---|---|---|
| `diagnose_line` | `text` | one line typed onto the red diagnosis board |
| `focus` | `component_index`, `path` | the pointer moves to the component (`path` = component indices from the top circuit down to the enclosing subcircuit instance; `[]` = top level) |
| `drill` | `path` (non-empty) | opens the drill-in overlay at that subcircuit |
| `drill_back` | — | one level up |
| `mark_fix` | `target` (`{component_index, path}` or `{net_id, path}`), `label` | yellow component or wire plus the "what/why fixed" label |
| `retest` | — | draws the green Retest box, clicks it, re-runs the rows on the temp fixed circuit. Always the final act |

Executor validation (`validate_animation`): unknown acts and any `retest`
written by the model are dropped, a single `retest` is appended last,
`focus` / `mark_fix` targets outside the component range are skipped,
`drill` needs a non-empty path, at most 12 acts survive. Playback never
mutates a circuit — the fix was applied to a temp copy and verified before
anything is shown.

## 5. Verify (nothing unverified is ever shown as a fix)

For each reply, in order:

1. **Normalize.** A `Data` rewrite aimed at a component that is not the
   circuit's single storage element is redirected to that element (noted
   in the run). Data changes on a protected program-memory ROM are
   stripped; a reply left with no ops is dropped as
   `program_memory_protected`.
2. **Apply** (`apply_patch`): unknown op → fail; the patched temp is
   written next to the source (so children resolve); it must re-parse and
   must not add Layer-1 errors compared with the original (the L1
   regression guard) or the patch is rejected (`patch_failed`).
3. **Re-run.** With a jar: `rerun_with_patch` runs the whole testcase on
   the temp through the per-row runner. Without a jar: the evaluator
   re-judges the temp with formula models for passing children — the same
   judge that produced the original verdicts. `verified.runner` says
   which (`"digital"` | `"evaluator"`).
4. **Confirmed** iff every row of the cluster now passes and no
   previously passing row regresses. Coach-added rows (the Mode B
   hand-off, `l3_temp.coach_rows`) are judged by strict improvement
   instead of perfection: the fix must repair at least one originally
   flagged column, break no new column, and leave a strict subset of the
   original columns — reported per row in `verified.coach_residuals`
   (`{row: [columns]}`), never as still failing. Official rows keep the
   full bar.
5. **Refuted → one retry** with the `[REFUTED ATTEMPT]` block; the retry's
   verdict replaces the first when it confirms or when the first patch
   did not even apply.
6. **Budget.** Every refutation counts; after 4 refuted ideas the run
   stops, the remaining clusters are skipped and the notes say so. A
   confirmed fix that repairs every failing row also skips the remaining
   clusters.
7. **Escalation.** When a whole run has hypotheses but no confirmed one
   and the budget is not spent, each cluster with a valid reply gets one
   more call with `[ESCALATION]` listing all refuted ops — still verified,
   still droppable.
8. **Rank and dedupe.** Hypotheses are deduplicated by their normalized
   ops (confirmed duplicates merge their row sets), ranked by confirmed
   first, then rows covered, then confidence, then cluster order. The
   top 3 confirmed become cards. Every other hypothesis lands in
   `dropped_ideas` with a reason: `refuted`, `patch_failed`,
   `beyond_top_k`, `invalid_response`, `llm_error`,
   `program_memory_protected`. With no card at all, the best-ranked
   unverified hypothesis is returned as `best_unverified`.

## 6. Response (server → client)

```json
{
  "ok": true, "contract": "l3.debug.v1.1", "model": "...",
  "mode": "analysis",
  "spec_name": "...", "failing_count": 8, "row_verdict_runner": "digital",
  "notes": ["subcircuits evaluated as formula models: ...", "..."],
  "diagnosis_lines": ["Row(s) 21, 22, 23 fail on Result when ALUOp=0b0100."],
  "clusters": [ {"signature": {"columns": ["Result"], "selects": [["ALUOp", "0b0100"]],
                               "category": null}, "rows": [21, 22, 23], "folded_rows": 0} ],
  "cards": [
    { "rank": 1, "confidence": 0.9, "cluster_rows": [21, 22, 23],
      "hint": { "suspect_region": "...", "suspect_signals": ["..."], "why": "..." },
      "verified": { "confirmed": true, "runner": "digital", "regressions": [],
                    "coach_residuals": {} },
      "fix": { "ops": [ "..." ], "ops_pretty": ["replace [159] And with Or"],
               "explanation_for_student": "...",
               "animation_script": [ "...validated, retest last..." ] } }
  ],
  "best_unverified": null,
  "dropped_ideas": [ { "cluster_rows": [], "reason": "refuted", "why": "...",
                       "detail": "...", "ops_pretty": ["..."] } ],
  "stopped_early": false, "refuted_ideas": 0,
  "timings": {"llm_s": [31.2], "verify_s": [1.4], "total_s": 33.1},
  "verify_runner": "digital",
  "usage": {"input_tokens": 0, "output_tokens": 0}, "llm_calls": 1,
  "injected": ["..."], "rom_injected": false,
  "limits": {"date": "...", "caps": {"modeA": 1, "modeB": 2}, "used": {}, "remaining": {}},
  "consumed_use": true, "on_coach_temp": false
}
```

Other modes: `"clear"` (every row passes; `message`), `"lazy"`
(`gross_flags` plus `suggestions[]` — question, hint and Layer-2 library
`terms` per flag; no cards, no ops), `"error"` (`ok: false`, `warning`),
`"unsupported"` (transistor labs). The route adds `injected` (official-row
injection notes), `rom_injected` (also appends the ROM hint to every
card's explanation as `fix.rom_hint`), `limits`, `consumed_use` and
`on_coach_temp`. A run consumes a daily use only when it is an analysis
that delivers at least one card.

Client rendering: the daily-cap chip (with "this run was free" when
nothing was consumed); a note when the coach temp was analyzed; the
diagnosis lines; each card at level 1 (hint only) with "Show me more"
revealing the fix, `ops_pretty`, the animation and "Accept fix → temp
copy"; when there is no card, the amber unverified card for
`best_unverified`; a collapsible list of dropped ideas; the run notes;
the failing-rows table until a fix is revealed. The Analyze button stays
disabled after a run that delivered cards.

## 7. Accept fix and retest

`POST /api/l3/accept_fix` `{session_id, filename, ops, spec_name}` applies
a confirmed card's ops to a temp copy only — never to the student's file.
The temp is registered in the session as `<stem>__coach.dig` and as
`session["l3_temp"]` for the filename, so later Mode A and Mode B runs
target it; earlier coach rows carry over. When the accept starts from the
original file and an official set exists, the official rows are written
into the temp in place. The response re-runs the testcase on the temp
(jar per-row, else the evaluator) and returns per-row `passed`/`failed`
statuses and `all_passed`.

`POST /api/l3/fix_retest` `{session_id, filename, ops, spec_name}` re-runs
a patch on the current target (coach temp when present) without
registering anything; it backs the green Retest box of the animation.

## 8. Card lifetime

Results live in the client's in-memory store keyed by filename
(`l3Store[filename]` → `modeA`, `modeB`, `cards`). They expire when a
file is re-uploaded or the session is cleared (`l3ExpireAll`), and on a
page refresh; switching tabs keeps them. Navigating away during a run
asks for confirmation because it resets the boards. The server keeps no
cards; it keeps only the coach temp registration.

## 9. Telemetry events

Client (`dlc/telemetry/sink.py` via `POST /api/telemetry`):
`l3_modeA_started{filename, model}` · `l3_modeA_run_complete{filename,
mode, cards, llm_calls}` or `{filename, ok: false}` ·
`l3_hint_level{rank, level: 2}` on every "Show me more" ·
`l3_fix_animation_played{rank}` · `l3_fix_accepted{rank, all_passed}` ·
`l3_fix_drillin_opened{depth}` · `l3_modeA_row_viewed{filename, row}` ·
`l3_modeA_rerun_clicked` · `l3_circuit_re_uploaded` (a re-upload wiped a
non-empty store).

Server: `l3_modeA_result_server{filename, mode, cards, confirmed,
llm_calls, in_tokens, out_tokens, model, rom_injected, consumed_use}` ·
`l3_accept_fix_server{filename, n_ops, all_passed, injected}`.

## 10. Limits and model selection

`CAPS = {"modeA": 1, "modeB": 2}` runs per day per machine in
`dlc/l3/limits.py`, stored in `~/.dlc/limits.json` (or `DLC_LIMITS_PATH`),
enforced only when `DLC_ENFORCE_LIMITS` is set (the release launchers set
it; a developer checkout runs uncapped). A Mode A use is consumed only by
an analysis that delivers a card; clear, lazy, error and card-less runs
are free. The course proxy adds its own per-machine budgets and the
whole-class breaker on top.
