# Configuring DLC for your own lab (instructor guide)

Related: ROM payloads — `instructor_rom_config.md`; course proxy —
`../proxy/README.md`.

DLC works on any Digital (`.dig`) circuit with **zero configuration**:
structural checks, signal flow, test coverage by mux arm and boundary,
Mode A debugging. A *manifest* adds course meaning on top: which
instruction or digit each input pattern stands for, what each subcircuit
is for, and how to read a program word. That turns "arm 2 is never
selected" into "the `sub` instruction is never tested" and lets the
coach reason about a CPU's program.

| You maintain | What it is | Where |
|---|---|---|
| **Manifest** | One JSON per lab: categories, subcircuit roles, program decode | `data/manifests/*.json` in your fork, or a folder named by `DLC_MANIFEST_DIR` |
| **Official tests** | The instructor's testcase per file | Settings ⚙ → Official tests; shipped defaults in `data/official_tests_defaults.json` |

A manifest holds only input patterns and names. No expected outputs, no
wiring, no solution content ever goes in it.

---

## Quick start

1. **Register the official tests**: Settings ⚙ → Official tests →
   filename + the testcase rows (header line plus data rows, as in
   Digital's test editor). Comments and spacing do not matter; changed
   rows do.
2. **Ship them as defaults** (forks): generate the entries from the
   `.dig` files and merge them into `data/official_tests_defaults.json`:

       uv run python -m dlc.fingerprint cpu.dig register-file.dig -o defaults.json

3. **Write the manifest**: copy `data/manifests/tier3_latched_display.json`
   (a display lab) or `cpu_new.json` (a RISC-V CPU) and edit the blocks
   below. Any filename works; DLC matches manifests by `applies_to`.
4. **Check it**: upload the lab, open the Layer 3 tab, run the Coverage
   Coach. The notes should say `lab manifest '<name>' applied` and each
   configured file shows a `categories N/M` chip.

The shipped manifests already cover the COMP 311 labs. A lab without
opcodes, digit classes or subcircuit roles needs no manifest at all.

---

## The manifest

```json
{
  "lab": "my-lab",
  "applies_to": ["my-top.dig", "my-sub.dig"],
  "subcircuits": { ... },
  "categories": { ... },
  "program_decode": { ... },
  "official_tests": {},
  "reference_dir": null
}
```

- `lab` — a short name, shown in the notes.
- `applies_to` — the exact filenames of the lab. The manifest that covers
  the most uploaded files wins, so two labs may share subcircuit files.
  A display lab can also attach by element kind with
  `"applies_to_elements": ["Seven-Seg"]`.
- `official_tests` — optional sha1 fingerprints (`dlc.fingerprint
  --hashes-only`); the Settings store normally makes this unnecessary,
  leave `{}`.
- `reference_dir` — leave `null`. To double-check coach proposals against
  solution circuits kept on **your** machine, set the environment variable
  `DLC_REFERENCE_DIR` to that folder when you start the server. Solutions
  never ship.

### `subcircuits` — what each child is for

```json
"subcircuits": {
  "alu.dig":       {"model": "rv32i_alu",
                    "role": "ALU: applies the operation selected by ALUOp to A and B."},
  "my-lookup.dig": {"model": "simulate",
                    "role": "Seven-segment lookup for digits 0-9."}
}
```

- `role` — one line in your words. Layer 3 quotes it when it debugs the
  parent.
- `model` — the formula DLC may use in place of a passing child while it
  debugs the parent circuit (Mode A only starts once every child passes
  its own tests). Without an entry DLC picks a model by the child's
  interface and uses it only after it reproduces every row of the child's
  own testcase. Naming a model here vouches for it, so it is also used
  when the child has no testcase; `"simulate"` keeps gate-level
  simulation for that file. Layer 1 never uses models: students always
  see their own child's signals.

Shipped formulas:

| Model | Interface | Computes |
|---|---|---|
| `rv32i_alu` | A, B, ALUOp → Result, FlagZ | AND, OR, ADD, XOR, SLL, SRL, SUB, SLT, SRA, SLTU (shifts A by B) |
| `lab5_alu` | A, B, ALUOp → Result, FlagZ | the original Lab 5 ALU: same codes without SLTU, shifts B by A |
| `lab5_control` | opcode, funct3, funct7 → the eight Lab 5 signals | Lab 5 decode table |
| `rv32i_control` | opcode, funct3, funct7 → up to 17 signals | all 37 RV32I instructions; unknown word = NOP |
| `rv32i_register_file` | ReadReg1, ReadReg2, WriteReg, WriteData, RegWrite, Clock → ReadData1, ReadData2 | 32 registers, x0 stays 0 |
| `add_sub` | A, B, Sub → Out, Overflow, Sign | add / subtract with flags |
| `boolean_unit` | A, B, Bool → Out | AND, OR, XOR, NOR |
| `bidirectional_shifter` | A, B, Bool → Out | B shifted by A: left, right, arithmetic right |
| `slt_unit` | Sign, Overflow → Result | signed less-than from the flags |
| `rv32i_immgen` | Instr, ImmSrc → Imm | I, S, B, U, J immediates |
| `rv32i_branch_unit` | A, B, funct3, Branch, Jump → Taken | the six branch conditions plus jump |
| `rv32i_data_memory` | Addr, WriteData, MemWrite, funct3, Clock → ReadData | 32 words, byte/half/word access |

The formulas themselves are code, in `dlc/sim/models.py`: one function per
model plus a registration line naming its inputs and outputs. To add a
unit of your own, add a function and a registration there; the manifest
only refers to it by name.

### `categories` — the cases that matter

```json
"categories": {
  "my-display.dig": [
    {"name": "digit_5", "when": {"A": 0, "B": 1, "C": 0, "D": 1, "load": 1}},
    {"name": "hold",    "when": {"load": 0}}
  ]
}
```

- One list per file; a category is a name plus the input cells that
  identify it, written with the testcase's own column names. Values may be
  decimal, `0x…` or `0b…`.
- A file is green when every category is matched by at least one test
  row; the Coverage Coach proposes rows for the missing ones.
- Every column in a `when` must exist in that file's testcase header,
  otherwise the manifest stays silent for that file.

### `program_decode` — RISC-V CPUs

For a CPU that fetches from a program ROM (a ROM with *Program Memory*
checked). The bit fields are the RISC-V standard, so any RV32I lab can
copy the block as is:

```json
"program_decode": {
  "categories_from": "control-unit.dig",
  "fields": {
    "opcode": [0, 7], "funct3": [12, 3], "funct7": [25, 7],
    "rd": [7, 5], "rs1": [15, 5], "rs2": [20, 5]
  },
  "observe": {"rs1_port": "ReadData1", "rs2_port": "ReadData2",
              "pc_port": "PCout"}
}
```

- `categories_from` — the file whose `categories` list names the
  instructions the lab implements (usually the control unit, with
  categories over `opcode` / `funct3` / `funct7`). `cpu.json` lists the
  eight Lab 5 instructions, `cpu_new.json` all 37 of RV32I.
- `observe` — the CPU testcase's columns for the two register-file read
  ports and, if the program parks in a `jal x0, 0` halt loop, the program
  counter. With them the coach reads back every value an extension writes
  and splices new words in front of the halt loop, where they execute.

With this block the coach decodes every program word, runs the program
through a small RV32I interpreter, and can build a correct extension on
its own when the model's proposal fails verification.

---

## Choosing the model

Each Layer 3 board has a model picker: **Sonnet 4.6 (default)** or
**Opus 5**, stronger on hard bugs and costlier per run. The choice applies
to that run only.

What "(default)" means comes from the machine running the app, in this
order: the environment variables `DLC_L3_DEBUG_MODEL` (Mode A) and
`DLC_L3_PROPOSE_MODEL` (Mode B), else the keys `l3_debug_model` and
`l3_propose_model` in `~/.dlc/config.json`, else the built-in default.
There is no model field in Settings. Through the course proxy the same
choice applies and the course key pays.

---

## Official tests: who can change what

- Built-in defaults are view-only. They change only through *Adopt into
  official tests* after a Coverage Coach run ends **all set**; an adopted
  override can be deleted to return to the default.
- Anyone may add official tests for their own labs (filename + testcase
  content, validated as Digital test format); those entries stay editable.
- Manifests are files in the fork: giving a new lab its meaning is the
  instructor's job.

## Troubleshooting

- **No `manifest applied` note** → a filename in the upload must appear
  in `applies_to`; check spelling and case.
- **No `categories` chip** → a `when` column does not match the file's
  testcase header exactly.
- **`official test` chip missing** → no entry for that filename in
  Settings, or the file's rows were modified (the chip then says so).
- **Program coach inactive** → the ROM needs *Program Memory* checked,
  the manifest needs `program_decode`, the testcase needs a clock column.
- **A child is "simulated as drawn" in the Mode A notes** → no model fits
  its interface, or the model disagreed with the child's own testcase (the
  note names the row). Name the model in `subcircuits` to vouch for it, or
  fix the child's test.
