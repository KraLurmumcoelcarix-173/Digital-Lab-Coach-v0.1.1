# Function Plan (F1 – F22)

## Core analyzer (no LLM)

| # | Name | Status |
|---|---|:-:|
| F1 | `.dig` parser | Done |
| F2 | Circuit netlist + signal-flow graph | Done |
| F3 | Structural fact extractor | Done |
| F4 | Test-result parser | Done |

## Layer 1 deterministic checkers

| # | Name | Status |
|---|---|:-:|
| F5 | Wire completeness checker | Done |
| F6 | Bit-width consistency checker | Done |
| F7 | Combinational-loop checker | Done |
| F8 | Interface conformance checker | Done |
| F9 | Timing / sequential checker (register-clock-Q) | Done |
| F9.5 | Value evaluator (combinational + sequential simulator) | Done — `dlc/sim/simulator.py`: gates (inverter bubbles), Const/Ground/VDD, mux, decoder, priority-encoder, splitter (bit-range), adder (carry), comparator, ROM, barrel-shifter, bit-extender, recursive subcircuits; worklist fixpoint; **hierarchical path-keyed sequential register state** so nested registers persist across clock-edge rows; honest unresolved-net reporting. Exposed via `/api/simulate` + `/api/subcircuit`. |
| F10 | K-map / Boolean simplification PRO | Done |
| F10.2 | Gate-minimization practice (Layer 5.2) | TBD — joins the K-map tab in the Practice surface |

## Layer 2 LLM conceptual explanation

| # | Name | Status |
|---|---|:-:|
| F11 | LLM client wrapper (SDK, prompt versioning, cost tracking etc.) | Done |
| F12 | Conceptual explanation generator | Done |
| F13 | Prompt-leakage guard | Done |

## Layer 3 LLM strategic debugging

| # | Name | Status |
|---|---|:-:|
| F14 | Failed-test interpreter | **L3 Mode A** (debug, when tests fail): hypothesis cards + animated wrong-signal-flow. Data side done (per-row runner: failing rows + expected-vs-found; **plus the `dlc/sim` value evaluator + `/api/simulate` now compute and drive the wrong-signal-flow**); LLM side Done (`/api/llm/debug`) | Done |
| F15 | Test-writing coach | **L3 Mode B** (coverage): test-coverage analysis -> non-redundant new tests; gated on L1 clean + all tests pass; ROM/RISC-V -> more program + instruction-memory hints. Case 3.B select-coverage gate: an input-driven mux whose exercised select values fall below a 31/32 share blocks proposing (deterministic, free) — the student writes the op rows first, so the coach never guesses semantics the tests don't define; a 32-way mux missing one address stays coached, not blocked. Done | Done |
| F16 | Signal-flow narrator | The failing-row animation. Its Layer-1 signal-flow-on-click substrate is now **Done** (`/api/simulate` returns per-net values + expected-vs-found outputs + node reactions, which the row-click renderer animates; `/api/subcircuit` drives nested flow). The v3 field names `signal_path_components`/`animation_script` were never built; `animation_script` becomes an L3-agent output. LLM narration layer Done | Done |

## Research infrastructure

| # | Name | Status |
|---|---|:-:|
| F17 | UI design | Ongoing. **Layer 1 signal-flow-on-row-click — Done**: clicking a test row colors every wire by value (1-bit green bright/dark, multi-bit blue + hex label, unresolved gray), real Digital component SVG glyphs, per-component reactions (7-seg lighting, mux/decoder selected-port ring, register value), a user-triggered clock-tick that steps signal flow through the remaining rows, a recursive subcircuit **drill-in** overlay, and a "snow storm" clear-page animation | Done |
| F18 | Ablation condition controller | Done |
| F19 | Telemetry logger & Proxy Server | Done — local SQLite spool + shipper (`dlc/telemetry/`), course proxy with key custody, machine-keyed budgets, breaker and admin dashboard (`proxy/`) |
| F20 | Digital source-code dig (Path-3 plugin viability) | Waived |
| F21 | Evaluation harness | L2 benchmark harness and the L1/L3 bug-benchmark ablation harness done; Layer 3 quality is tracked by hand on the L3 benchmark sheet (live Mode A runs per release) |
| F22 | CLI interface | Done (`dlc/cli/`) |
| F23 | Formula models for passing subcircuits (Layer 3 speed) | Done — `dlc/sim/models.py`, validated per child against its own testcase |
| F24 | RV32I program coach (37 instructions, halt-loop-aware extensions) | Done — `dlc/l3/manifest.py`, `data/manifests/cpu_new.json` |
