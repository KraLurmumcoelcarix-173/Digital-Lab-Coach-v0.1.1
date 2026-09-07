# Instructor guide: registering a lab's course program (ROM payload)

> Step 2 of the instructor flow (see the README's Instructor setup).
> Start with `MANIFEST_GUIDE.md` if you have not configured the
> official test set yet; deploy the course proxy last
> (`../proxy/README.md`).

Some labs run a fixed course program from an instruction-memory ROM:
students build the datapath, and the program words are given to them.
Registering that program as the lab's **ROM payload** tells the tool
what the ROM must contain. The payload rides on the lab's official-test
entry, so both live in the same config record.

---

## 1. What the payload does

| Where | Effect |
|---|---|
| Layer 1 test runs (Dashboard, per-row) | An **empty** ROM is filled with the course program for that run only, the way the autograder does, so the datapath can be tested before the student types the program in. The empty-ROM warning says so. A ROM the student programmed, rightly or wrongly, is never touched. |
| Layer 3 Mode A | Before anything else, the file's instruction memory is compared with the payload **word for word**. Empty, different or missing ROM: the board shows one fixed message naming the ROM, how many words differ and the first differing address, and stops. No model call, no daily use consumed. Matching ROM: the analysis runs, the model is told the words are official, and any Data change it proposes on that ROM is stripped. |
| Everywhere | The words never appear in the UI, the Settings page, `list_tests()`, the Mode A message, or the model payload. |

The comparison ignores formatting: bare or upper-case hex, Digital's
`n*word` shorthand and trailing zero words all read the same. The ROM
checked is the one marked *Program Memory*; if none is marked, every ROM
in the file must hold the program.

## 2. Decide whether this lab should have a payload

Ask one question: **is the ROM's content a runtime INPUT to the lab, or
is it the lab's ANSWER?**

| Situation | Configure a payload? | Example in 311 |
|---|---|---|
| The ROM holds a program the circuit executes — students are graded on the datapath around it, not on the words | **Yes** | `cpu.dig` instruction memory |
| The ROM is the deliverable — filling it would hand out the answer, and Mode A would refuse every file whose table differs from yours | **Never** | `control-unit.dig` decode table |

A lab with no payload still gets official-test injection; its empty ROM
stays empty, keeps its Layer 1 warning, and Mode A treats the ROM like
any other component.

## 3. Where the configuration lives

One JSON file, shipped with the tool:

```
data/official_tests_defaults.json
```

One entry per lab **filename** (matching is by exact filename, e.g.
`cpu.dig`):

```json
{
  "romlab.dig": {
    "content": "A D\n0 5\n1 6",
    "sha1": "<normalized fingerprint of content — step 6>",
    "runtime": "<base64 blob — step 5>"
  }
}
```

- `content` — the official testcase rows (Digital test format: first
  line is the signal header, then value rows). Injected into a run-scoped
  copy whenever a student file's own testcase is missing or modified.
- `sha1` — fingerprint used to recognize an unmodified official
  testcase inside a student file (see step 6).
- `runtime` — the course program. **Optional.** Only add it when step 2
  said yes.

The `runtime` key can only be configured here, in the shipped defaults
file.

## 4. Get the ROM words from your answer circuit

Open your answer `.dig` in Digital, double-click the ROM, and read the
data table — or read the `Data` attribute straight out of the XML:

```xml
<entry>
  <string>Data</string>
  <data>5,6</data>
</entry>
```

Format rules:

- comma-separated words, **address 0 first**, one word per address;
- **bare hex** by default (`fe,82,1a` — no `0x` prefixes). A student ROM
  is read with its own `intFormat` attribute, which is `hex` unless the
  student changed it, and the two lists are compared as numbers;
- Digital's run-length shorthand is supported: `7*1f` stores `1f` at 7
  consecutive addresses;
- trailing addresses you omit read as 0 (Digital semantics).

## 5. Build the base64 `runtime` blob

The blob is base64 over a tiny JSON object with a `rom` key:

```bash
.venv/bin/python -c "import base64, json; print(base64.b64encode(json.dumps({'rom': '5,6'}).encode()).decode())"
```

Replace `'5,6'` with your comma-separated words. Paste the printed
string as the entry's `"runtime"` value.

Why base64? It is obfuscation, not encryption. Keep answer `.dig` files
out of it.

## 6. Compute the `sha1` for `content`

The fingerprint is a normalized hash (comments stripped, whitespace
collapsed) so cosmetic edits in a student's copy don't break matching.
Always compute it with the tool's own function:

```bash
.venv/bin/python -c "
from dlc.l3.manifest import normalized_test_hash
print(normalized_test_hash(open('official_rows.txt').read()))"
```

where `official_rows.txt` holds exactly the `content` text.

## 7. Restart and verify

1. Restart the server.
2. Upload a student-style file with the right filename and an **empty**
   ROM, and run its tests. The empty-ROM warning should mention the
   automatic load, and the rows should be judged with the program in
   place.
3. Open Layer 3 and click Analyze on that file. The board must answer
   "ROM check: instruction memory is empty" and the daily-use counter
   must not move.
4. Re-upload the file with the program typed in and one word changed:
   the board must name the first differing address. With the program
   typed in exactly, the analysis runs.

## 8. Quick reference

| Piece | Where |
|---|---|
| The one file to edit | `data/official_tests_defaults.json` (entry per lab filename: `content`, `sha1`, optional `runtime`) |
| Fingerprint command | step 6 above |
| The check itself | `dlc/testing/inject.check_program_rom` (Mode A), `prepare_injected_run` (test runs) |
| Everything else instructors can change | the README's *Where to change what* table |
