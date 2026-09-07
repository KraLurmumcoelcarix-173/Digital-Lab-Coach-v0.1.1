# Digital Notes

Last updated: 2026/9/6

---

## .dig File Format

### Top-level structure

- Root element: `<circuit>`
- Two main children: `<visualElements>` (components), `<wires>` (connections)
- Wires are geometric (`p1`, `p2` coordinates), not pin-typed — must match endpoints to component pin positions
- Subcircuits referenced as `<elementName>filename.dig</elementName>`
- `<version>2</version>` is the current `.dig` format version
- `<measurementOrdering/>` appears (empty) at the end of every file

### Attribute parsing quirks

- Most `<entry>` values are `<int>`, `<long>`, `<boolean>`, or `<string>`.
- **`<rotation rotation="N"/>`** stores `N` as an XML *attribute*, not text content. Common parser mistake is to read `value.text` (returns `None`) and fall back to the tag name `"rotation"`. Must extract via `element.get("rotation")` and cast to `int`. Values are 0/1/2/3 for 0°/90°/180°/270°.
- Unrecognized `<elementAttributes>` value tags (`<testData>`, `<shape>`, etc.) should be preserved as raw text so nothing is silently lost.

### Element types encountered in 311 labs

| Element name | Purpose | Key attributes |
|---|---|---|
| `And`, `Or`, `XOr`, `NAnd`, `NOr`, `XNOr` | N-input gates | `Inputs` (int, default 2), `wideShape` (bool), `Bits` (default 1) |
| `Not` | Single-input inverter | `Bits` |
| `In`, `Out` | Circuit I/O pins | `Label`, `Bits` (default 1) |
| `Multiplexer` | Mux | `Selector Bits` (default 1 → 2-to-1), `Bits` |
| `Splitter` | Bus split/merge | `Input Splitting`, `Output Splitting`, `splitterSpreading` |
| `Tunnel` | Named net | `NetName`. Tunnels sharing a NetName are electrically connected. Can have `rotation` |
| `ROM` | Read-only memory | `Bits` (data width), `AddrBits`, `Data` (hex bytes), `isProgramMemory`, `bigEndian`. DLC flags an empty `Data` field as `empty_rom` (warning) |
| `Register` | Sequential register | `Bits`, optional `isProgramCounter` |
| `RegisterFile` | Built-in register bank (Memory category): 2 async read ports + 1 clocked write port | `Bits`, `AddrBits`; seen in real student CPUs (2 of 6 in the r28 batch) |
| `Const` | Constant value | `Value` (int), `Bits` |
| `Ground`, `VDD` | Power rails | Single output pin. Can have `rotation`, `Bits` |
| `Comparator` | A vs B (greater/equal/less) | `Bits`, `Signed` |
| `Add` | Adder | `Bits` |
| `BitExtender` | Width conversion | `inputBits`, `outputBits` |
| `BarrelShifter` | Variable shift | `Bits`, `direction`, `barrelShifterMode` |
| `Seven-Seg` | 7-segment LED display (non-hex). Lab 2. | `Color` (awt-color, ignored), `rotation`. **Eight 1-bit input pins**: `a, b, c, d` (top edge) + `e, f, g, dp` (bottom edge). |
| `Decoder` | One-of-N decoder | `Selector Bits` → 2^N outputs |
| `Demultiplexer` | Routes 1 input to one of 2^N outputs (others 0) | `Selector Bits`, `Bits` |
| `PriorityEncoder` | Priority → binary index | `Selector Bits` → 2^N inputs |
| `Clock` | Clock signal | No attributes in basic use |
| `Testcase` | Embedded simulator test cases | `testData/dataString`, default Label `"Testdata"`. **No signal pins.** |
| `Rectangle` | Annotation/grouping box | **No signal pins.** Pure visual |

Elements in scope but with no encountered samples yet: `RAM`, `D-FlipFlop`, `JK-FF`, `T-FF`, `Counter`, `Driver` (tri-state), `Display`, `LED`, `Switch`, `Button`.

### Pin geometry (offsets from anchor, verified empirically)

Digital's coordinate system: x increases rightward, y increases downward. Anchor is the `<pos>` of the visual element. Pin coords = anchor + offset, with rotation applied to the offset before adding the anchor.

| Element | Inputs (left edge) | Outputs (right edge) | Notes |
|---|---|---|---|
| `Not` | `A` (0, 0) | `Y` (40, 0) | Width 40 |
| `And`/`Or`/`XOr` (wideShape=True, even N) | Two halves with **40-unit gap** in the middle | `Y` (80, center_y) | Verified empirically. N=2 → (0,0),(0,40); N=4 → (0,0),(0,20),(0,60),(0,80); N=6 → (0,0),(0,20),(0,40),(0,80),(0,100),(0,120) |
| `And`/`Or`/`XOr` (wideShape=True, odd N) | `in_i` at (0, i*20) — uniform | `Y` (80, center_y) | Tested via three_inputand, five_inputand (tests pass with full I→O); offsets match wire endpoints exactly |
| `And`/`Or`/`XOr` (wideShape=False) | Assumed `in_i` at (0, i*20) | Assumed `Y` (80, center_y) | **Not yet observed in any sample.** Code uses the same uniform-20 path as wideShape+odd. Will verify when we encounter one in the field |
| `NAnd`/`NOr`/`XNOr` (any) | same as positive variants | Output bubble pushes visible pin ~20 right; absorbed by snap tolerance | Only wideShape=True observed (single_nand) |
| `In`/`Out`/`Const`/`Clock`/`Ground`/`VDD` | single pin at anchor (0, 0) | | |
| `Tunnel` | single bidir pin at anchor | | NetName unifies across the circuit |
| `Multiplexer` (sel_bits=1, n=2) | `in0` (0, 0), `in1` (0, 40), `sel` (20, 40) | `out` (40, 20) | **Different spacing for 2-input vs 4+** |
| `Multiplexer` (sel_bits≥2, n≥4) | `in_i` at (0, i*20), `sel` at (20, n*20) | `out` at (40, n*10) | |
| `Splitter` | `in_i` at (0, i*spacing) | `out_i` at (20, i*spacing) | spacing = 20 × `splitterSpreading` (default 1, can be 2+). **`mirror`=true negates the spacing** — pin i at −i*spacing, pin 0 stays on the anchor row (SVG-verified on a real add-sub "32 → 31,1" sign extractor, r34) |
| `Register` | `D` (0, 0), `C` (0, 20), `en` (0, 40) | `Q` (60, 20) | `en` always present even when tied to Const(1) |
| `Comparator` | `A` (0, 0), `B` (0, 20) | `gr` (60, 0), `eq` (60, 20), `le` (60, 40) | Width **60**, not 80 — common mistake |
| `Add` | `a` (0, 0), `b` (0, 20), `c_i` (0, 40) | `s` (60, 0), `c_o` (60, 20) | Width **60**. Input order top-to-bottom matches Digital's UI: a, b, c_i. c_o at y=20 not y=40 — earlier-assumed (80, 40) layout consistently snapped to wire L-bends and produced phantom multi-drivers. |
| `BitExtender` | `in` (0, 0) | `out` (80, 0) | Width varies with outputBits; snap tolerance absorbs ±20 |
| `BarrelShifter` | `in` (0, 0), `sh` (0, 40) | `out` (60, 20) | |
| `Seven-Seg` | `a/b/c/d` at `(0,0)/(20,0)/(40,0)/(60,0)`; `e/f/g/dp` at `(0,140)/(20,140)/(40,140)/(60,140)` | (no outputs — display sink only) | **Corrected r34** via SVG export of a real Lab-2 file: pins sit ON the anchor row and at +140, `dp` on the SAME row as e/f/g. The old −40/180/240 offsets only ever matched because students park tunnels exactly one wire-length past the pins. |
| `ROM` | `A` (0, 0), `sel` (0, 40) | `D` (60, 20) | Box is 60 wide (SVG-verified, r30) — the old (80, 20) survived only via loose endpoint snapping |
| `RegisterFile` | `Din` (0,0), `we` (0,20), `Rw` (0,40), `C` (0,60), `Ra` (0,80), `Rb` (0,100) | `Da` (80,0), `Db` (80,20) | Built-in register bank (Memory category); width **80**; reads combinational, write clocked; measured on a real student CPU (r28, re-landed r31) |
| `Decoder` | `sel` (20, (n_outputs − 1) * 20) | `out_i` at (60, i*20) | **sel sits at the LAST output's height, NOT one row below like the Mux** — measured on a rotation-2 sel_bits=5 Decoder whose sel feed lands exactly at (20, 620); the old n*20 table falsely flagged its sel undriven |
| `Demultiplexer` (sel_bits=1, n=2) | `in` (0, 20), `sel` (20, 40) | `out0` (40, 0), `out1` (40, 40) | mirror of the 2-input Mux |
| `Demultiplexer` (sel_bits≥2, n≥4) | `in` (0, n*10), `sel` (20, n*20) | `out_i` at (40, i*20) | measured on a sel_bits=5 register-file write-enable fan-out; non-selected outputs drive 0 |
| `PriorityEncoder` | `in_i` at (0, i*20) | `num` (80, 0), `f` (80, 20) | `f` = 1-bit "any input set" flag; students wire it as ROM chip select (r30) |

`flipSelPos` (Multiplexer / Demultiplexer / Decoder): Digital's "flip selector position"
attribute moves the `sel` pin to the TOP edge at (20, −20); everything else is unchanged.

### Rotation

- Rotation index N applies a 90°×N counter-clockwise rotation to every pin offset *before* adding the anchor.
- In screen coordinates (y growing down), CCW visual = math CW.
- Formula: `(dx, dy)` → `(dy, -dx)` for N=1, `(-dx, -dy)` for N=2, `(-dy, dx)` for N=3.
- Verified empirically against a rotated Multiplexer (rotation=1, sel_bits=1) in `register-file.dig` and a rotated Splitter (rotation=2) in `cpu.dig`.

### Gates

- Gate multi-input attribute is `Inputs` (`<int>`), absent = 2.
- Gate anchor = TOP input pin, not center.
- For `wideShape=True` with even `N≥4`, the input column has a 40-unit gap in the middle (so the output sits centered between the halves).
- **Negated inputs (`inverterConfig`)**: a gate may carry `<inverterConfig>`
  listing input pin names (`In_1`, `In_2`, …) that are inverted (a bubble on
  that specific input). It changes the gate's logic and its visual state — e.g.
  `add-sub.dig` uses an `And` with `In_1`/`In_2` negated. Parsed and kept
  in `attributes`; the Layer-1 value evaluator (`dlc/sim/simulator.py`)
  applies the per-input negation via the gate's inverter bubbles.

### Wires

- A `<wire>` has exactly two endpoints: `<p1>` and `<p2>`, each with x/y coordinates.
- Wires carry NO pin or signal-type information.
- Connectivity is INFERRED: wires sharing an endpoint coordinate form a net.
- Each `<wire>` is one straight segment between two points (may be horizontal, vertical, or diagonal).
- A visual corner is NOT one bent wire — it's two separate `<wire>` segments sharing an endpoint coordinate. An L-path = 2 wires, a path with 2 turns = 3 wires.
- **Diagonal wires**: Digital allows non-Manhattan wires. They connect their endpoints normally via union-find, but our T-junction detection currently skips them (no observed cases needing it).
- **Mid-wire branch points** (T-junctions): a wire endpoint may land on the *interior* of another wire, not just at its endpoint. Net-building must treat any shared coordinate — not just endpoints — as a potential connection. Implemented via `_midpoint_branches` scanning each horizontal/vertical wire for foreign endpoints landing strictly between p1 and p2.

### Real bug patterns the parser must surface

- **Dangling input** — input pin with no wire endpoint at its predicted coord. Detected as a singleton net containing only sink-direction pins.
- **Multi-driver** — two or more outputs feeding the same net. Detected by `len(net.drivers()) > 1`.
- **Combinational loop** — cycle of purely combinational gates without a clocked register breaking it. Detected via `networkx.simple_cycles(g)` (F8).
- **Bit-width mismatch** — N-bit signal feeding an M-bit pin. Requires splitter bit-range parsing and per-net width inference.
- **Miswire / wrong-pin / wrong-input-position** — connected to wrong pin, surfaces as a failed test vector. Layer 1 sees a valid topology; Layer 3 detects the semantic mismatch.

Digital does NOT flag multi-driver on load. The error only surfaces at simulation time, and only when a signal actually traverses the conflicted net.

### Wire endpoint degree as a pin-vs-routing classifier

A wire endpoint at coord X is **degree N** if N wires terminate there. Used by net builder:
- Degree 1 = a real pin location (exactly one wire ends there). Candidates for snapping or implicit-pin attachment.
- Degree ≥ 2 = L-bend or T-junction routing point. Excluded from implicit-pin assignment to prevent misclaim.

### Pin snap / implicit attachment

The net builder uses two-stage pin attachment:

1. **Predicted-pin snap** (for known-geometry elements): for each (pin, endpoint) pair within `PIN_SNAP_TOLERANCE` (Manhattan distance ≤ 30), build all triples sorted by distance. Walk in sorted order and claim each pair only if neither side already claimed. Multiple pins at the *exact same coord* (distance 0) can share an endpoint.
2. **Implicit-pin attach** (for no-geometry components, mostly subcircuit references): unclaimed degree-1 endpoints get assigned to the nearest no-geometry component within `IMPLICIT_PIN_RADIUS` (= 500). Per-instance cap = `child.inputs() + child.outputs()`; if more endpoints claim the instance than the cap allows, the farthest are dropped.
3. **Co-located output rescue**: if a predicted output-direction pin doesn't snap to a wire endpoint but its exact coord is already part of a known net (most commonly because a Tunnel was placed directly on the pin with no connecting wire), the pin joins that net as a driver. This is how students wire Clock-through-tunnel in pipelined circuits, and applies to any output pin not just Clock.

Dangling **outputs** are dropped from the netlist (they're not errors — just unused). Dangling **inputs** are kept as singleton nets so F5 can detect them as bugs.

## Layer-1 vs Layer-3 detection responsibility

| Bug category | Layer 1 (deterministic) | Layer 3 (LLM) |
|---|:-:|:-:|
| Dangling input pin | ✓ catches | ✓ explains |
| Multi-driver short | ✓ catches | ✓ explains routing intent |
| Combinational loop | ✓ catches | ✓ describes the cycle |
| Width mismatch | ✓ catches (with F6) | ✓ explains |
| Missing subcircuit file | ✓ catches | ✓ suggests fix |
| **Semantic miswire** | ✗ | ✓ (only Layer 3 can know intent) |
| **Wrong input-position** | ✗ | ✓ |
| **Wrong op-encoding**  | ✗ | ✓ |
| **Routing accident through unrelated pin coord** | ✓ catches (multi-driver) but cannot explain | ✓ explains |

The ablation contrast (Layer 1 alone vs Layer 1+3 vs Layer 3 alone) is the project's central evaluation. The 30 bug benchmark is split across all three columns.

## Digital UI Features Relevant to Students

### Debugging tools that exist natively
- Single-step simulation
- Test case runner with pass/fail output

### What students struggle with (from ULA experience)
- Wire routing accidents that look right visually but short signals through an unrelated component's pin coord (mazes).
- Forgetting to wire `en` on a Register.
- More components, more possible bits width mismatch, whereas Digital does not do an ideal job to instantly point the bug
- Multi-driver shorts that don't surface at load time and only become apparent through unexpected test failures.
- Subcircuit reference path issues when sharing labs across machines.

### Features we'd want DLC to add or enhance
- Inline highlighting of dangling pins / multi-driver nets at edit time (before simulation). [done]
- Component-level reachability annotation ("this output is unused", "this input is undriven"). [done]

## Parser scope policy

DLC's parser aims to **semantically understand** elements used in COMP 311 labs so far. Other elements (FPGA-specific blocks, FSM editor outputs, etc.) are parsed structurally but treated as opaque `UnknownComponent` with named pins for now. This lets the analyzer skip unrecognized components and the LLM describe them generically, while keeping the parser future-proof for new labs.

**Known-and-semantically-supported**:
Wire (straight, L, diagonal), And, Or, XOr, NAnd, NOr, XNOr, Not, In, Out, Multiplexer, Demultiplexer, Splitter, Tunnel, ROM, Register, RegisterFile, Const, Comparator (incl. `Signed`), Add, BitExtender, Clock, Ground, VDD, BarrelShifter, Decoder, PriorityEncoder, Testcase, Rectangle, Text, Seven-Seg, and the tier-2.5 switch-level elements NFET, PFET, PullUp, PullDown.

**Annotation-only** (parsed but explicitly carry no signal pins): Testcase, Rectangle, Text. Excluded from implicit-pin candidate set.

**Out of initial scope** (parsed but opaque, may be added later):
FSM elements, FPGA-board-specific blocks, Verilog wrappers, GAL/JEDEC-specific elements, RAM.

## CLI Mode (what the UNC autograder uses)

- Command: `java -cp Digital.jar CLI test -circ FILE.dig [-verbose]`
- Output format: `Test: passed` or `Test: failed (N%)` per test case
- Exit codes:
  - `0` — every testcase passed
  - `1` — at least one testcase failed OR reported a testcase-level
    error (e.g. `name: Test signal Qx not found in the circuit!`)
  - `200` — execution error before testing (e.g. circuit file not found)
- A failing run ends with the line `Tests have failed.`

### `-verbose` value table (the fast per-row source)

With `-verbose`, every FAILED testcase's result line is followed by
Digital's own value table:

```
this_is_a_test: failed (20%)
A B C D load Clock Fa Fb Fc Fd Fe Ff Fg
0 0 0 0 0 0 1 1 1 1 1 1 0
1 1 1 1 0 0 1 0 1 1 0 1 E: 0 / F: 1
...
```

Facts the fast runner (`dlc/testing/runner.py`) relies on, all
verified empirically:

- First table line = the testcase's header names, space-separated.
- One table line per EXECUTED row, in execution order. Digital
  expands `loop(N, K) … end loop` blocks itself — the same expansion
  DLC's TestSpec performs — so table line *i* ↔ `spec.rows[i]`.
- A row with a `C` clock token still yields exactly ONE table line
  (the clock column echoes the post-pulse value, e.g. `0`).
- A failing row renders each mismatched output cell as
  `E: <expected> / F: <found>`; passing rows echo plain values
  (formats vary: `1E`, `0x19`, `FFFFFFE0`...). "Row failed" ==
  "row contains an E:/F: cell".
- Passed testcases print NO table (nothing to print): a passed
  result line means every row passed.
- Testcase labels with spaces print in full (`Register File Test:
  failed (1%)`); a missing label prints as `unnamed`.

## Subcircuit Resolution

- A circuit referencing `alu.dig` means Digital looks for `alu.dig` in the same directory or library path.
- For our parser: recursively load referenced subcircuits to fully analyze a top-level circuit.
- Subcircuit cache is per-parse-session — same `.dig` referenced N times is loaded once. Circular references raise.
- A referenced file with a bare name may live in any subfolder; we search recursively and pick the shallowest match (ambiguity is flagged but doesn't fail the parse).
- **Subcircuit instance pin prediction (r61, mirrors Digital's
  `GenericShape`)**: every RESOLVED child gets declared-pin geometry from
  the child's In/Clock/Out elements in FILE order. Inputs sit at
  `(0, i*20)` and outputs at `(Width*20, i*20)` — EXCEPT when the child
  has exactly ONE output: then the shape is symmetric, the output sits at
  `(Width*20, inputs//2*20)` and an even input count skips the centre
  row (2 inputs → {0,40}, 5 inputs → {0..80} with the output at +40).
  A child with no `Width` attribute is 3 grid units (60 px) wide. Source
  of truth: `java -cp Digital.jar CLI svg -dig f.dig -svg out.svg` (blue
  circles = inputs, red = outputs). The implicit-pin x-midpoint heuristic
  below applies only to UNRESOLVED children (missing files).
- **Subcircuit instance pin direction resolution** (unresolved children only): the instance has no native geometry, so direction is inferred by splitting the instance's implicit pins at the x-midpoint (left = inputs, right = outputs), sorting each side by y, and zipping against the child circuit's `In`/`Out` elements sorted by y. Implicit-pin count is capped to the child's port count to prevent over-claim from neighboring routing.

## L1 regression ground truths

- **PriorityEncoder has TWO outputs**: `num` at (80, 0) and `f` — the
  1-bit "any input set" flag — at (80, 20). Students wire `f` as the
  ROM's chip select (`PriorityEncoder.f -> ROM.sel`).
- **ROM box is 60 wide**: A (0,0), sel (0,40), D (60,20). D at (80,20)
  was wrong and survived only via loose endpoint snapping — and would
  have blessed a wire Digital refuses ("No output connected to a wire").
- **Endpoint snapping**: an OUTPUT pin may claim a nonzero-distance
  endpoint only if that endpoint has wire-degree 1 (a terminating end).
  Degree-2+ coords are routing (L-bends/junctions of other nets); letting
  an unwired `gr`/`le` grab a corner 20 px away fabricated multi-driver
  errors across whole comparator ladders.
- **Multi-driver is a RUNTIME error in Digital**, raised only when tied
  outputs actually disagree ("More than one output is active on a wire").
  A register Q shorted to Ground as an "x0 is always 0" hack passes the
  official register-file test (jar-verified, both v0.30 and v0.31). One
  constant (Ground/VDD/Const) + one real output => WARNING; two real
  outputs => still ERROR.
- **Custom-component pins follow the child's FILE order, not canvas
  order** (re-confirmed: the answer alu declares FlagZ before Result in
  the file but places Result above FlagZ on canvas; Digital renders
  FlagZ on the top row).
- **Multi-driver tolerances**: Digital's short-circuit
  check fires at RUN time on value conflict, so three same-net driver
  mixes run cleanly and are WARNINGS, not errors: (1) one real output +
  agreeing constants; (2) several SAME-valued constants tied by one
  tunnel name; (3) a top-level `In` the file's testcase does not drive —
  the test vector never powers it, and the jar lets the other driver
  win. An In that IS a testcase column, an In in a file with no
  testcase (interactive mode drives every In), two real outputs, or
  constants with different values all stay hard errors.
- **Mode A debugs the injected run**: when the file's testcase is
  missing/modified and an official set exists, /api/llm/debug builds the
  same sibling injected temp the Dashboard runs use and debugs THAT —
  otherwise a header-only testcase yields zero failing rows and the
  board wrongly says "every row passes". Accept-Fix temps built from the
  raw file get the official rows written in place; temps descending
  from Mode B keep their coach-added rows untouched.
- **Gradescope-style injection**: when a filename has an
  official test set registered (data/official_tests_defaults.json or a
  Settings entry) and the file's own testcase does not MATCH it
  (missing, header-only, or modified — normalized-content hash), test
  runs replace the file's testcases with the official rows in a sibling
  temp copy (dlc/testing/inject); the panel says so from upload
  (`official_test_status` in the file summary). ROM contents are NEVER
  injected — a wrong/empty ROM is the student's own work and Layer 3's
  teaching material, and official ROM/program data must never ship in
  the tool. An empty ROM stays a Layer-1 WARNING that blocks nothing;
  Mode B remains the test-expansion teacher on top of always-official
  test runs.

- **Duplicated identical gates tied together demote to WARNING**:
  jar-probed — two And gates with the same inputs driving one tunnel
  net run fine (they always agree), while And+Or on the same inputs
  short-circuit at run time. `_check_multi_drivers` demotes only when
  every driver is a plain commutative gate (or Not) with the same
  element, Bits, input NETS and inverter bubbles
  (`_identical_gate_signature`); anything else stays a hard error.
  Field source: a real Lab-2 SOP decoder rebuilding product terms per
  segment block under one tunnel name.
- **PriorityEncoder drives `f` in the evaluator**: Digital's PE
  has `num` + a 1-bit `f` "any input set" flag. The evaluator only
  produced `num`, so a ROM whose chip-select hangs off `f` never
  evaluated and the whole output stage read undefined — while the jar
  ran it fine (empty ROM words read 0). Both fixed: `f` is emitted and
  empty ROMs read 0, so evaluator mismatch cells now match Digital's.
- **Mode A runaway firewalls**: (1) children failing their
  OFFICIAL tests (injected when missing/modified) gate the parent into
  the free suggestion branch — the s008 cpu routes straight to
  control-unit.dig, 0 model calls; (2) a jar per-row run where EVERY
  row errors is a REFUSAL (unconnected tunnel / renamed test signals)
  — returned as lazy `build_refused`, or `unbound_columns` with rename
  guidance when testcase columns bind to no port, 0 model calls;
  (3) `_MAX_REFUTED_IDEAS = 4` — after 4 verifier-refuted ideas the run
  stops spending (no more retries/escalations/clusters), sets
  `stopped_early`, and ships the best unverified idea (the benchmark's
  best-solution hard trigger); (4) `timings` in the analysis payload
  records per-call and per-verify seconds.
- **Frozen-trunk exception to the lazy bars**: when the failing
  rows are fully explained by "every output frozen at one constant"
  (constant found per column, never-mismatching outputs carry one
  constant expected, passing rows consistent), the scattered flag and
  pass-rate bars stand aside, and all failing rows form ONE cluster so
  a partial fix gets refuted instead of shipping as a per-row card.
  Convicted on s008's empty decode ROM (8/8 rows, stuck at 0). Rows
  failing in differing column sets keep every ratified bar.
- **ROM-data steer only fires on stored words**: the
  "do NOT propose another Data change" escalation steer presumes the
  stored words satisfy the passing rows; on an EMPTY ROM the missing
  words ARE the bug, so the steer is suppressed and suspect attrs
  carry the exact `change_attribute`/`Data` op shape instead
  (`_suspect_attrs`: AddrBits/Bits/splitting ranges/data_words_stored;
  stored words themselves are never listed — injected official
  programs must not leave the backend).
- **Splitter attribute key is `Output Splitting`** (not `Splitting`),
  and Digital rejects a `Splitting` entry silently — the box renders
  with its default 8-bit output. Bit-group syntax `1,1,1,1` verified;
  `1*4` also parses in Digital but our probe used the explicit form.
- **Mode A daily cap is 1** — a booked use requires a delivered
  verified card, and the stop condition bounds one run's spend, so a
  single daily analysis is a full analysis.
- **`no_lazy_gate` files skip the lazy gate**: a file listed under
  `no_lazy_gate` in a lab manifest (name normalized: case, punctuation
  and the `.dig` suffix ignored, injected temps included) bypasses
  gross_check entirely and goes straight to analysis when rows fail
  (`_lazy_exempt_name` / `assemble_evidence(lazy_exempt=True)`; the web
  layer keys on `req.filename` so coach temps qualify too). The shipped
  CPU manifests list `control-unit.dig` and `controlunit.dig`. Refusal
  guards (build_refused / unbound_columns) and the failing-children gate
  still apply. All other filenames keep every ratified lazy bar.

- **Stored data is checked FIRST, not last**:
  the Mode A prompt's "ROM data is a last resort" bias is deleted.
  Mixed rom+logic circuits sort evidence into the data signature
  (frozen outputs, address/select resolving fine) vs the logic
  signature (outputs varying in gate-explainable ways) and fix the
  bucket the rows show. A student's OWN stored table (≤32 words) rides
  the suspect attrs as `stored_words` so a partially-wrong word can be
  convicted; empty ROMs keep the exact-op `data_note`.
- **Rom-injected runs**: Mode A already debugs the rom-filled
  injected temp (prepare_injected_run fills empty ROMs whenever a
  runtime payload is registered, testcase status independent). New:
  the run's prompts carry a [ROM NOTE] ("grader-loaded words are
  correct by definition — never propose Data changes there"), injected
  words are excluded from the payload (`hide_rom_words`), every
  verified card gains a "Check your ROM data" hint (fix.rom_hint + the
  student explanation) because the student's own file still has the
  ROM unprogrammed, and the accept-fix "show the green" rerun now runs
  through the same injection (rom-filled sibling, removed after; the
  registered coach temp never stores official rom words).
- **Premium max tokens 8000**: a live Opus full-decode-
  table derivation still truncated at 6000 and died as
  invalid_response; the single-cluster frozen-trunk shape needs the
  headroom.
- **Data ops are retargeted deterministically**: a
  change_attribute/Data op aimed at a component that cannot store data
  is redirected to the circuit's ONLY storage element before
  verification (`_retarget_data_ops`; ambiguous multi-storage circuits
  untouched). Live conviction: Opus wrote the correct decode table at
  an And gate's index and the verifier refuted a no-op.
- **A complete verified answer ends the run**:
  when a confirmed fix leaves ZERO failing rows
  (verify_ops.remaining_failing empty), remaining clusters and
  escalations are skipped — "as long as an answer is verified, stop
  layer 3 and return result."
- **Frozen-trunk clusters carry the merged localizer report (r41 bug
  fix)**: the r38 single-cluster branch dropped it, stripping
  suspect_wiring from every frozen-trunk payload — the model had no
  component indices at all.
- **Storage suspects carry three machine-traced tables**:
  `address_by_row` (which word each failing row reads),
  `output_bit_map` (which stored bit feeds which named Out — traced
  through splitters, immune to lying tunnel names like an ALUOp bus
  named "opcode"), and `expected_outputs_by_row` (machine-parsed
  expected ints — models kept misreading the whitespace-aligned raw
  rows). Deriving a decode word is now a pure table join; both s008
  and s009 control units land a jar-verified full table in ~25s/2
  calls.

## reasoning-tier models: bounded effort, timeouts, truncation retry

- **claude-opus-5 replies were 100% thinking, 0 bytes of text**: the
  model thinks by default; at the provider-default depth it spent the
  ENTIRE max_tokens budget (8000, then 16000) on thinking blocks —
  stop_reason max_tokens, extracted text empty, surfaced in the UI as
  "not a json response" after minutes per call. The proposed fix was
  likely being derived and then destroyed in transit.
- **Bounded effort is the fix, not bigger caps**: the client now sends
  `output_config={"effort": "low"}` for claude-opus-5 (thinking stays
  on, depth capped server-side) plus a max_tokens floor of 8000 —
  mirroring the existing gpt-5 branch. Mode A pins the same policy
  explicitly (`_effort_for`) with 16000 headroom. Measured swing on
  the same circuit/payload: 200s + 16000 tokens + empty reply →
  11-34s, stop end_turn, complete JSON.
- **Hard per-request wall clock (180s, DLC_LLM_TIMEOUT)**: a model
  that thinks for minutes now fails the call with a clear message
  instead of hanging the student's Analyze click.
- **Truncation-aware format retry**: a reply whose stop_reason is
  max_tokens earns a retry naming the CUT OFF and demanding
  JSON-only output — distinct from the generic invalid-JSON retry.
- **Selector inputs are machine-traced (`address_input_drivers`)**: on
  a 3-gate exam control unit the model twice named the right story
  ("the gate on encoder input 5 asserts on the wrong rows") and twice
  guessed a wrong component_index (59, then a nonexistent 115),
  burning the refuted-ideas budget. Storage suspects now carry the
  selector element plus, per selector input pin, the exact driving
  gate ("Or[112]") and that pin's value on the failing rows; the
  prompt's WRONG-ADDRESS rule makes mis-selection beat data rewrites
  and demands ALL wrongly-asserting gates be fixed in one ops list
  (encoder priority masks the quieter wrong gate until the loudest
  is silenced — the exam circuit needs Or[112]→And AND XOr[113]→And;
  XOr[114] is test-invisible).
- **Refutation retries name partial progress**: a refuted fix that
  turned some target rows green (no regressions) now gets "PARTIALLY
  RIGHT — rows X now pass, keep these ops and ADD what fixes the
  rest" instead of a bare refusal, so multi-fault circuits converge
  across retries instead of restarting from scratch.

## Purple advisory: pins the tests never touch

- **check_test_io_coverage (dlc/analyzer/test_io_coverage.py)**: at
  upload, top-level In/Out components no EFFECTIVE test column binds
  (own rows, or the official rows injection would run) earn ONE purple
  warning card naming them — "either these pins are redundant, or the
  tests are incomplete." Field origin: a student cpu carried extra In
  pins no test drove. Runs from the upload endpoint only (it needs the
  specs; check_all_l1 stays purely structural), severity=warning so no
  gate ever blocks on it, silent when no header matches at all (a
  fully-unbound testcase is the rename story, not a coverage gap).
  Sweep: 0 cards across all 60 sample circuits and the real student
  labs on file — fires only on genuinely untested pins.

## Program-memory guard 

- **Benchmark conviction**: on a cpu whose instruction memory held
  wrong words, the premium model derived a complete WORKING course
  program from the test expectations alone, three rounds out of three,
  machine-verified — a card that does the student's assignment. On
  labs where the course registers a runtime payload, the instruction
  memory IS the deliverable.
- **Deterministic strip**: Data ops aimed at an `isProgramMemory` ROM
  on a payload-registered lab are removed in `norm()` before any
  verification (`_protected_program_memory`; injected-temp filenames
  normalize back to the real lab name). A hypothesis reduced to zero
  ops drops as `program_memory_protected`. The model is told up front
  via a [PROGRAM MEMORY] prompt block; the student gets a note with
  the legitimate move: clear the ROM's Data and re-run — the grader
  then loads the official program, so the datapath can be tested alone.
- **L1 wording**: the empty_rom warning on payload-registered labs now
  says the official program is loaded automatically for runs and that
  the submitted file must still contain the student's own instruction
  memory (green tests were hiding the gap).
- Student-authored ROM content on every other lab (control-unit decode
  tables, own lookup tables) stays fully fixable — the guard keys on
  payload registration + isProgramMemory, nothing else.

## Telemetry + course proxy (machine-keyed, re-download-proof)

- **Machine identity** (`dlc/telemetry/machine.py`): install_id =
  sha256("dlc-v1:" + OS machine id)[:16] — Windows MachineGuid, macOS
  IOPlatformUUID, Linux /etc/machine-id; stable fallback = hash of
  (platform, node, user). The cache file is a convenience only:
  deleting the tool/cache and re-downloading on the same machine
  recomputes the SAME id, so records and limits continue. `issued`
  date stamps locally; the proxy keeps authoritative first_seen.
- **Spool → ship**: events always land in the local SQLite sink first
  (fully offline-capable), then `ship.py` batches everything past a
  high-water mark to the proxy — at-least-once, deduped server-side on
  (install_id, client_row_id), fire-and-forget threads from the app.
- **Proxy** (`proxy/dlc_proxy.py`): key custody (/v1/llm relays via
  the same client wrapper), per-machine per-day CALL budgets per
  feature (v1 backstop: modeA 8, modeB 10 — generous inside client
  limits, wipe-proof against re-downloads; v1.1 path = analysis-
  granular limits with refunds), events ingest, /admin/summary +
  /admin/export.csv. Client relays automatically when `proxy_url` is
  configured, with direct-call fallback only if the proxy is
  unreachable AND a local key exists.
- **Feature tags**: every call_llm site is tagged (modeA/modeB/grade/
  explain) so proxy budgets and usage accounting are per-feature.
- **Server-side result events**: l3_modeA_result_server,
  l3_modeB_result_server, l3_accept_fix_server — authoritative rows
  (mode, cards, tokens, consumed) next to the FE click events.

## CPU-lab

- **Signed comparators**: the evaluator's `Comparator` rule applies
  two's complement at `Bits` when `Signed` is set. Before that, a
  signed branch unit's `blt`/`bge` rows inverted and the Layer 1 overlay
  of a full CPU drifted into a parallel execution from the first signed
  branch on — while the jar verdicts (correct) said "passed".
- **Formula models**: Layer 3 Mode A replaces
  a passing child by the function it computes (ALU, control decode table,
  register file and data memory with state, imm-gen, branch unit, the
  Lab 3 sub-units) — only after the model reproduces every row of the
  child's own testcase, or when the manifest's `subcircuits` block vouches
  for it. Layer 1 never uses models. Evidence stage on the RV32I CPU:
  40 s → 0.3 s.
- **Single-pass replay**: a testcase is replayed once,
  keeping register state between rows; `/api/simulate` caches the replay
  per file content, so a tick costs one row instead of
  a restart from row 0.
- **RV32I program coach**: `program_decode` drives a small
  interpreter that follows branches/jumps and keeps data memory;
  `encode_category_word` knows all formats; a program that parks in a
  `jal x0, 0` halt loop gets its extension spliced in FRONT of the loop
  (`insert_at`), with the halt rows' PC shifted (`observe.pc_port`).
  Manifest attachment picks the manifest covering most uploaded files.
- **Two ALUs, one interface**: `lab5_alu` (shifts B by A[5:0], no
  SLTU) and `rv32i_alu` (shifts A by B[4:0]) are told apart by each
  file's own testcase.

## Known limitations to revisit


## Open Questions under investigation

- Where exactly does Java plugin API expose hooks for adding analysis panels? (Path 3 question, defer investigation)