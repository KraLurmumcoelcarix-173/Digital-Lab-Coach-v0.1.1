# Configuring DLC for your own lab (instructor guide)

Related Doc: ROM payloads — `instructor_rom_config.md`; course proxy
deployment — `../proxy/README.md`.

DLC works on any Digital (`.dig`) circuit out of the box: structural
coverage (mux arms, boundaries, constant outputs and every structural
health check) needs **zero configuration**. What this guide adds is the
*intent* layer — the small amount of course knowledge that turns "arm 2 is
never selected" into "the `sub` instruction is never tested", protects your
official tests, tells the debugger what each subcircuit is for, and unlocks
the RISC-V program coach.

Two local artifacts:

| Artifact | What it is | Where it lives |
|---|---|---|
| **Manifest** (a json) | Names your lab's test categories, the role and formula model of each subcircuit, and how to decode program words | `data/manifests/<your-lab>.json` in your fork |
| **Official tests** | The instructor-issued testcase per file | Settings ⚙ → Official tests (stored in `~/.dlc/official_tests.json`), or shipped defaults in `data/official_tests_defaults.json` |

A manifest holds only **input patterns and names** (which opcode values
exist, which digit each ABCD pattern means, which known function a
subcircuit computes). It has no expected outputs, no wiring, no solution
content. Set `DLC_MANIFEST_DIR` to keep your manifests in a folder
outside the repo checkout.

---

## Quickstart: a new lab in five steps

DLC's "server" is the tool itself, running on each user's own machine —
there is no shared server to configure. A manifest is only needed when the
lab needs semantic interpretation (opcode / RISC-V decode, display-digit
classes, subcircuit roles); the shipped defaults already cover the 311
course labs.

1. **Fork the repo** (or work in your local clone).
2. **Register the official tests**: Settings ⚙ → Official tests → filename
   + paste the testcase rows (the header line + data rows exactly as in
   the `.dig`'s test editor; content must be valid Digital test format —
   the tool rejects anything else). Comments and spacing are ignored by
   the match, changed rows are not.
3. **(Forks) Ship the official tests as built-in defaults** — generate
   ready-made entries straight from your `.dig` files with the
   fingerprint helper:

       uv run python -m dlc.fingerprint cpu.dig register-file.dig -o defaults.json

   It reads each file's first testcase and emits
   `{"<file>.dig": {"content": "...", "sha1": "..."}}` — merge those
   entries into `data/official_tests_defaults.json` in your fork, done.
   The sha1 is the same normalized fingerprint the Settings list shows;
   `--hashes-only` prints the `{filename: sha1}` shape used by a
   manifest's `official_tests` block instead.
4. **Write the manifest** — copy `data/manifests/tier3_latched_display.json`
   as a template and edit (details below). Drop it in `data/manifests/`,
   **named after the lab's top-level `.dig`**.
5. **Validate**: upload the lab, open L3 Coach, run the Coverage Coach.
   You should see `lab manifest '<name>' applied` in the whole-tree notes
   and a `categories N/M` chip on the file. If not, see Troubleshooting.

---

## The manifest, block by block

```json
{
  "lab": "my-lab",
  "applies_to": ["my-top.dig", "my-sub.dig"],
  "subcircuits": { ... },
  "categories": { ... },
  "official_tests": {},
  "reference_dir": null
}
```

- `lab` — any short name; shown in the scan notes.
- `applies_to` — the EXACT filenames of your lab (matching is by
  filename; if students rename files, the manifest will not attach).
- `official_tests` — optional sha1 fingerprints (the Settings store
  usually replaces this; leave `{}`).
- `reference_dir` — leave `null`. If you keep solution circuits on YOUR
  machine, point the `DLC_REFERENCE_DIR` environment variable at that
  folder when starting the server: proposed rows are then also checked
  against the solutions before students see them. Never ship solutions.

### `subcircuits` — role and formula model of each child

Layer 3 Mode A only starts once every subcircuit passes its own tests, so
while it debugs the top circuit it can treat a passing child as the
function it is supposed to compute instead of simulating it gate by gate.
DLC ships these **formula models**:

| Model | Interface it expects | Computes |
|---|---|---|
| `rv32i_alu` | A, B, ALUOp → Result, FlagZ | AND, OR, ADD, XOR, SLL, SRL, SUB, SLT, SRA, SLTU (shifts A by B) |
| `lab5_alu` | A, B, ALUOp → Result, FlagZ | the original Lab 5 ALU: same codes without SLTU, shifts B by A |
| `lab5_control` | opcode, funct3, funct7 → the eight Lab 5 signals | decode table, unknown word decodes as `add` |
| `rv32i_control` | opcode, funct3, funct7 → up to 17 signals | decode table for all 37 RV32I instructions, unknown word is a NOP |
| `rv32i_register_file` | ReadReg1, ReadReg2, WriteReg, WriteData, RegWrite, Clock → ReadData1, ReadData2 | 32 registers, edge-triggered write, x0 stays 0 |
| `add_sub` | A, B, Sub → Out, Overflow, Sign | add / subtract with flags |
| `boolean_unit` | A, B, Bool → Out | AND, OR, XOR, NOR |
| `bidirectional_shifter` | A, B, Bool → Out | B shifted by A: left, right logical, right arithmetic |
| `slt_unit` | Sign, Overflow → Result | signed less-than from the flags |
| `rv32i_immgen` | Instr, ImmSrc → Imm | I, S, B, U, J immediates |
| `rv32i_branch_unit` | A, B, funct3, Branch, Jump → Taken | the six branch conditions plus jump |
| `rv32i_data_memory` | Addr, WriteData, MemWrite, funct3, Clock → ReadData | 32 words, byte/half/word loads and stores |

Without any manifest entry DLC picks a model by the child's interface
(labels and widths) and uses it **only after it reproduces every row of
the child's own testcase**; a child without a testcase is simulated as
drawn. The block lets you decide per file:

```json
"subcircuits": {
  "alu.dig":  {"model": "rv32i_alu",
               "role": "ALU: applies the operation selected by ALUOp to A and B."},
  "my-lookup.dig": {"model": "simulate",
                    "role": "Seven-segment lookup for digits 0-9."}
}
```

- `model` — a name from the table, used as vouched for (its interface
  must still fit) even when the child has no testcase; `"simulate"`
  forces gate-level simulation for that file.
- `role` — one line, in your words: what this block is for. Layer 2 shows
  it on the subcircuit's card and Layer 3 quotes it; leave it out to fall
  back to the model's own description.

Every Mode A run lists what it did in its notes (`subcircuits evaluated
as formula models: alu.dig → rv32i_alu, ...`), and Layer 1's signal flow
never uses a model — students always see what their own child does.

### `categories` — name the cases that matter

One list per file. Each category = a name + the input cells that
identify it, using the **testcase's own column names**:

```json
"categories": {
  "my-display.dig": [
    {"name": "digit_5", "when": {"A": 0, "B": 1, "C": 0, "D": 1, "load": 1}},
    {"name": "hold",    "when": {"load": 0}}
  ]
}
```

A circuit is category-GREEN when every named category is matched by at
least one test row. The Coverage Coach proposes rows for the missing
ones, and its category claims are checked deterministically — the model
cannot mislabel a row.

Values may be written as decimal, `0x...`, or `0b...`. Every column
named in a `when` must exist in that file's testcase header, or the
manifest stays silent for that file (by design — it never guesses).

### `program_decode` — RISC-V CPUs (copy-paste block)

For a CPU whose instructions come from a program ROM (a ROM component
with *Program Memory* checked), add this block. For any RV32I lab you
can copy it **verbatim** — the bit fields are the RISC-V standard:

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

Adjust only two things:

- `categories_from`: the file whose `categories` list enumerates the
  instructions your lab implements (typically your control/decode unit —
  categories written over `opcode`/`funct3`/`funct7` columns). Two
  shipped examples: `data/manifests/cpu.json` (the eight-instruction
  Lab 5 subset) and `data/manifests/cpu_new.json` (all 37 RV32I
  instructions — copy its `controlunit.dig` list for any full RV32I lab).
- `observe`: the CPU testcase's column names that show the register-file
  read ports, and (optional) the program counter. The read ports let the
  coach add machine-derived read-back rows (`addi x0, xN, 0`) so every
  value an extension writes is actually observed; `pc_port` lets it
  place an extension correctly when the program **parks in a halt loop**
  (`jal x0, 0`): the new words are spliced in front of the loop, where
  they actually execute, and the rows that expect the loop's PC move
  down accordingly. Without `pc_port` a halted program can only be
  extended by appending, which never executes — leave it out only for
  programs that run straight off the end.

With this block the tool decodes every program word deterministically,
rejects lazy or undefined instructions, derives register and memory
values by running the program through a small RV32I interpreter that
follows branches and jumps, and — if the model's proposal fails its
gates — machine-builds a correct extension on its own (this part even
works offline).

---

## What anyone can and cannot do in Settings

- **Built-in defaults are view-only for everyone** — the only way to
  raise a default's standard is *Adopt into official tests* after a
  Coverage Coach run ends **all set** (server-side, from the verified
  temp circuit — no free-form editing). An Adopt override can always be
  deleted to revert to the shipped default.
- **Anyone may add official tests for their own labs as test standards**
  (filename + testcase content); content is validated as Digital test
  format and rejected otherwise. These entries stay fully editable and
  deletable.
- Manifests are repo/fork files — configuring a NEW lab's semantics
  (categories, subcircuit roles and models, program decode) is the
  instructor's (fork owner's) job, per the quickstart above.

## Troubleshooting

- **No `manifest applied` note** → a filename in the uploaded tree must
  appear in `applies_to`; check exact spelling and case.
- **No `categories` chip** → a `when` column name doesn't match the
  file's testcase header exactly; the manifest stays silent rather than
  guess.
- **`official test` chip missing** → the file has no entry in Settings →
  Official tests (or the content was modified — the chip then says
  `modified`, which is the point).
- **Program coach inactive** → the ROM component must have *Program
  Memory* checked in Digital; `program_decode` must be present; the
  testcase needs a clock column.
- **A child is still "simulated as drawn" in the Mode A notes** → its
  interface matches no model, or the model disagreed with the child's own
  testcase (the note says which row). Name the model in `subcircuits` to
  vouch for it, or fix the child's test.
