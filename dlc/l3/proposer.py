from __future__ import annotations

import json
import re
from pathlib import Path

from dlc.l3.coverage import TreeCoverageReport, scan_tree_coverage
from dlc.l3.oracle import InjectedRow, validate_rows
from dlc.llm.client import DEFAULT_MODEL, call_llm
from dlc.parser.dig_parser import parse_dig_file
from dlc.testing.spec import _tokenize, extract_test_specs, match_variables_to_io

_PROMPT_DIR = Path(__file__).parent.parent.parent / "prompts"
_PROMPT_NAME = "l3_coverage_proposer_v1.txt"

_MAX_EXISTING_ROWS_SHOWN = 40
_MAX_TOTAL_ROWS = 50

_MAX_PROGRAM_WORDS = 12

_PROPOSE_MODEL_FALLBACK = "claude-sonnet-4-6"


def _propose_model() -> str:
    import os
    env = os.environ.get("DLC_L3_PROPOSE_MODEL", "").strip()
    if env:
        return env
    try:
        from dlc.llm.client import _load_config
        cfg = _load_config().get("l3_propose_model")
        if isinstance(cfg, str) and cfg.strip():
            return cfg.strip()
    except Exception:
        pass
    return _PROPOSE_MODEL_FALLBACK


def _load_prompt() -> str:
    return (_PROMPT_DIR / _PROMPT_NAME).read_text(encoding="utf-8")

def build_targets(report: TreeCoverageReport) -> list[dict]:
    targets: list[dict] = []
    for cov in report.circuits:
        if not cov.has_testcases or not cov.path:
            continue
        try:
            circuit = parse_dig_file(cov.path)
            specs = extract_test_specs(circuit)
        except Exception:
            continue
        if not specs:
            continue
        spec = specs[0]
        existing = [r.raw.strip() for r in spec.rows if not r.is_malformed]
        shown = existing[:_MAX_EXISTING_ROWS_SHOWN]
        bindings = match_variables_to_io(spec.headers, circuit)
        clock_col = next((col for col, b in bindings.items()
                          if b is not None and b.role == "clock"), None)
        target = {
            "file": cov.file,
            "spec_name": spec.name,
            "headers": list(spec.headers),
            "inputs": [{"label": c.label, "bits": c.bit_width()}
                       for c in circuit.inputs() if c.label],
            "outputs": [{"label": c.label, "bits": c.bit_width()}
                        for c in circuit.outputs() if c.label],
            "existing_rows": shown,
            "existing_rows_omitted": max(0, len(existing) - len(shown)),
            "has_clock": clock_col is not None,
            "clock_col": clock_col,
            "has_program_rom": False,
        }
        rom = _program_rom_of(circuit)
        if rom is not None:
            words, addr_bits = rom
            target["has_program_rom"] = True
            target["program_words"] = [f"{w:x}" for w in words]
            target["rom_capacity_left"] = max(0, (1 << addr_bits) - len(words))
            from dlc.l3 import manifest as mf
            m = mf.find_manifest({c.file for c in report.circuits},
                                 element_names=set(report.element_names))
            pc = mf.program_categories(m, words) if m else None
            if pc is not None:
                target["program_categories_present"] = pc["present"]
                target["program_categories_missing"] = pc["missing"]
                if pc["missing"]:
                    ex = mf.category_word_examples(m, pc["missing"], words)
                    if ex:
                        target["program_word_examples"] = ex
            halt = _program_halt_info(m, words, spec) if m else None
            if halt:
                target["program_halt"] = halt
        targets.append(target)
    return targets


def _program_halt_info(manifest: dict, words: list[int], spec) -> dict | None:
    from dlc.l3 import manifest as mf
    h = mf.program_halt_index(manifest, words)
    if h is None:
        return None
    observe = (manifest.get("program_decode") or {}).get("observe") or {}
    pc_col = observe.get("pc_port")
    headers = list(spec.headers)
    rows = [r for r in spec.rows if not r.is_malformed]
    insert_before = len(rows)
    if pc_col in headers:
        col = headers.index(pc_col)
        for i in range(len(rows) - 1, -1, -1):
            cells = rows[i].raw.split("#", 1)[0].split()
            tok = _tokenize(cells[col]) if col < len(cells) else None
            if tok is not None and tok.kind == "int" and tok.value == 4 * h:
                insert_before = i
            else:
                break
    return {"insert_at": h, "halt_pc": 4 * h,
            "insert_before_row": insert_before,
            "pc_col": pc_col if pc_col in headers else None}


def _program_rom_of(circuit) -> tuple[list[int], int] | None:
    """(existing_words, addr_bits) of the single program ROM, else None."""
    from dlc.l3.manifest import program_rom_words
    return program_rom_words(circuit)


def build_prompt(report: TreeCoverageReport, targets: list[dict]) -> str:
    template = _load_prompt()
    slim = report.to_dict()
    for c in slim["circuits"]:
        c.pop("flags", None)
        c.pop("path", None)
    return (template
            .replace("<<REPORT_JSON>>", json.dumps(slim, indent=1))
            .replace("<<TARGETS_JSON>>", json.dumps(targets, indent=1)))

def parse_proposals(text: str) -> list[dict]:
    if not text:
        return []
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return []
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    raw = obj.get("proposals")
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for p in raw:
        if not isinstance(p, dict):
            continue
        rows = p.get("rows")
        if not isinstance(rows, list):
            continue
        rows = [r.strip() for r in rows if isinstance(r, str) and r.strip()]
        if not rows:
            continue
        entry = {
            "file": str(p.get("file", "")),
            "spec_name": str(p.get("spec_name", "")),
            "rows": rows,
            "why": str(p.get("why", "")).strip(),
        }
        pw = p.get("program_words")
        if isinstance(pw, list):
            pw = [str(w).strip() for w in pw if str(w).strip()]
            if pw:
                entry["program_words"] = pw
        out.append(entry)
    return out


def _row_key(raw: str, headers: list[str]) -> tuple:
    cells = raw.split("#", 1)[0].split()
    key = []
    for cell in cells[:len(headers)]:
        tok = _tokenize(cell)
        key.append(("v", tok.value) if tok.kind == "int" else ("r", tok.raw.upper()))
    return tuple(key)


def validate_and_dedupe(
    proposals: list[dict], targets: list[dict], manifest: dict | None = None,
) -> tuple[list[dict], list[dict]]:
    by_file = {t["file"]: t for t in targets}
    seen: dict[str, set] = {t["file"]: set() for t in targets}
    for t in targets:
        for raw in t["existing_rows"]:
            seen[t["file"]].add(_row_key(raw, t["headers"]))

    valid: list[dict] = []
    rejected: list[dict] = []
    total = 0
    for p in proposals:
        t = by_file.get(p["file"])
        if t is None or p["spec_name"] != t["spec_name"]:
            rejected.append({**p, "reason": "unknown target file or testcase"})
            continue
        if p.get("program_words"):
            ok_entry, reason = _validate_program_group(p, t, manifest)
            if ok_entry is not None:
                valid.append(ok_entry)
            else:
                rejected.append({**p, "reason": reason})
            continue
        good_rows: list[str] = []
        bad: list[tuple[str, str]] = []
        for raw in p["rows"]:
            if total + len(good_rows) >= _MAX_TOTAL_ROWS:
                bad.append((raw, f"over the {_MAX_TOTAL_ROWS}-row cap"))
                continue
            try:
                _validate_against_headers(raw, t["headers"], p["spec_name"])
            except ValueError as exc:
                bad.append((raw, str(exc)))
                continue
            k = _row_key(raw, t["headers"])
            if k in seen[p["file"]]:
                bad.append((raw, "duplicate of an existing or proposed row"))
                continue
            seen[p["file"]].add(k)
            good_rows.append(raw)
        if good_rows:
            total += len(good_rows)
            valid.append({**p, "rows": good_rows})
        for raw, reason in bad:
            rejected.append({"file": p["file"], "spec_name": p["spec_name"],
                             "rows": [raw], "why": p.get("why", ""),
                             "reason": reason})
    return valid, rejected


def _validate_program_group(
    p: dict, t: dict, manifest: dict | None = None,
) -> tuple[dict | None, str]:
    from dlc.l3.oracle import parse_program_words
    if not (t.get("has_clock") and t.get("has_program_rom")):
        return None, ("program_words are only valid for clocked targets "
                      "with a program ROM")
    try:
        words = parse_program_words(p["program_words"])
    except ValueError as exc:
        return None, str(exc)
    if not 1 <= len(words) <= _MAX_PROGRAM_WORDS:
        return None, f"program extension must be 1..{_MAX_PROGRAM_WORDS} words"
    if len(words) > t.get("rom_capacity_left", 0):
        return None, "program extension does not fit in the ROM"
    if len(p["rows"]) != len(words):
        return None, (f"needs exactly one row per program word "
                      f"({len(words)} word(s), {len(p['rows'])} row(s))")

    existing = set()
    try:
        existing = {int(w, 16) for w in t.get("program_words", [])}
    except ValueError:
        pass
    seen_new: set[int] = set()
    from dlc.l3 import manifest as mf
    can_decode = bool((manifest or {}).get("program_decode"))
    word_info: list[dict] = []
    for w in words:
        if w in existing:
            d = mf.decode_program_word(manifest, w) if can_decode else None
            cat = f" (category '{d['category']}')" if d and d["category"] else ""
            return None, (f"word {w:x} duplicates an instruction already in "
                          f"the program{cat} — nothing new would execute")
        if w in seen_new:
            return None, f"word {w:x} appears twice in the extension"
        seen_new.add(w)
        if can_decode:
            d = mf.decode_program_word(manifest, w)
            if not d or not d["category"]:
                return None, (f"word {w:x} is not an instruction this lab "
                              f"defines — the lab ISA cannot execute it")
            lazy = mf.lazy_word_reason(manifest, w)
            if lazy:
                return None, (f"word {w:x} ({d['category']}) is a lazy "
                              f"test — {lazy}")
            missing = t.get("program_categories_missing") or []
            word_info.append({
                "word": f"{w:x}",
                "category": d["category"],
                "closes_gap": d["category"] in missing,
            })

    clk = t.get("clock_col")
    for raw in p["rows"]:
        try:
            _validate_against_headers(raw, t["headers"], p["spec_name"])
        except ValueError as exc:
            return None, str(exc)
        cells = dict(zip(t["headers"], raw.split("#", 1)[0].split()))
        if clk and cells.get(clk, "").upper() != "C":
            return None, (f"every program-extension row must clock the "
                          f"circuit: expected C in column {clk!r}: {raw!r}")
    entry = {**p, "rows": list(p["rows"]),
             "program_words": [f"{w:x}" for w in words]}
    if word_info:
        entry["word_info"] = word_info
    if can_decode:
        entry, reason = _ensure_readbacks(entry, t, manifest, existing)
        if entry is None:
            return None, reason
    halt = t.get("program_halt")
    if halt:
        entry = {**entry, "insert_at": halt["insert_at"],
                 "insert_before_row": halt["insert_before_row"],
                 "pc_col": halt.get("pc_col"),
                 "pc_shift": 4 * len(entry["program_words"])}
    return entry, ""


def _ensure_readbacks(
    entry: dict, t: dict, manifest: dict | None, existing_words: set[int],
) -> tuple[dict | None, str]:
    from dlc.l3 import manifest as mf
    words = [int(w, 16) for w in entry["program_words"]]
    decoded = [mf.decode_program_word(manifest, w) for w in words]

    def _cls(d):
        return (mf._CLASS_OF.get(d["fields"].get("opcode"))
                if d and d["category"] else None)

    def _reads(d, reg):
        c = _cls(d)
        return bool(c) and (
            (c in mf._READS_RS1 and d["fields"].get("rs1") == reg)
            or (c in mf._READS_RS2 and d["fields"].get("rs2") == reg))

    unobserved: list[int] = []
    for i, d in enumerate(decoded):
        if _cls(d) not in mf._WRITES_RD:
            continue
        rd = d["fields"].get("rd") or 0
        if rd == 0:
            continue
        read_later = any(_reads(d2, rd) for d2 in decoded[i + 1:])
        if not read_later and rd not in unobserved:
            unobserved.append(rd)
    if not unobserved:
        return entry, ""

    regs = ", ".join(f"x{r}" for r in unobserved)
    observe = ((manifest or {}).get("program_decode") or {}).get("observe") or {}
    rs1_col = observe.get("rs1_port")
    headers = t["headers"]
    if not rs1_col or rs1_col not in headers:
        return None, (f"the extension writes {regs} but nothing ever reads "
                      f"the result back — the write is unobservable; add a "
                      f"read-back word (addi x0, xN, 0) per result register")

    known = mf.constant_registers(
        manifest, [int(w, 16) for w in t.get("program_words", [])] + words,
        appended=len(words))
    if any(r not in known for r in unobserved):
        return None, (f"the extension writes {regs} without reading the "
                      f"result back, and the read-back value could not be "
                      f"derived deterministically — add the read-back words "
                      f"yourself")

    auto_words: list[int] = []
    auto_rows: list[str] = []
    auto_info: list[dict] = []
    seen = set(existing_words) | set(words)
    clk = t.get("clock_col")
    rs2_col = observe.get("rs2_port")
    for r in unobserved:
        w = mf.encode_category_word(manifest, "addi", rd=0, rs1=r, imm=0)
        if w is None or w in seen:
            return None, (f"the extension writes x{r} but never reads it "
                          f"back, and the automatic read-back word could "
                          f"not be added — add it yourself")
        seen.add(w)
        v = mf._signed32(known[r])
        cells = []
        for h in headers:
            if h == clk:
                cells.append("C")
            elif h == rs1_col:
                cells.append(str(v) if v >= 0 else f"({v})")
            elif rs2_col and h == rs2_col:
                cells.append("0")
            else:
                cells.append("X")
        auto_words.append(w)
        auto_rows.append(" ".join(cells))
        auto_info.append({"word": f"{w:x}", "category": "addi",
                          "closes_gap": False, "auto_readback": True,
                          "observes": f"x{r}"})

    total = len(words) + len(auto_words)
    if total > _MAX_PROGRAM_WORDS:
        return None, (f"with read-backs for {regs} the extension needs "
                      f"{total} words — over the {_MAX_PROGRAM_WORDS}-word "
                      f"cap; propose fewer gap words and read each back")
    if total > t.get("rom_capacity_left", 0):
        return None, (f"with read-backs for {regs} the extension does not "
                      f"fit in the ROM")
    out = {**entry,
           "program_words": entry["program_words"] + [f"{w:x}" for w in auto_words],
           "rows": entry["rows"] + auto_rows,
           "word_info": entry.get("word_info", []) + auto_info}
    return out, ""


def _validate_against_headers(raw: str, headers: list[str], spec_name: str) -> None:
    class _HeaderOnly:
        pass
    shim = _HeaderOnly()
    shim.headers = list(headers)
    shim.name = spec_name
    validate_rows(shim, [InjectedRow(raw=raw)])


def propose_rows(
    dig_path: str,
    *,
    model: str | None = None,
    api_key: str | None = None,
    call=None,
) -> dict:
    if call is None:
        call = call_llm
    report = scan_tree_coverage(dig_path)
    if report.total_flags > 0:
        return {"ok": False, "proposals": [], "rejected": [],
                "model": None,
                "error": ("Tests and circuit still disagree somewhere — "
                          "resolve that before asking for new rows."),
                "notes": []}
    if report.select_gate:
        gaps = "; ".join(
            f"{e['file']}: input '{e['input']}' value"
            f"{'s' if len(e['missing']) != 1 else ''} "
            f"{', '.join(map(str, e['missing']))} "
            f"(Multiplexer[{e['component_index']}])"
            for e in report.select_gate)
        return {"ok": False, "proposals": [], "rejected": [],
                "model": None, "select_gate": report.select_gate,
                "error": ("Every select value needs at least one of YOUR "
                          "test rows before the coach can extend them — "
                          f"never exercised: {gaps}. Write one row per "
                          "listed value stating what the circuit SHOULD "
                          "output, then re-run the Coverage Coach."),
                "notes": []}
    targets = build_targets(report)
    if not targets:
        return {"ok": False, "proposals": [], "rejected": [],
                "model": None,
                "error": "No testcase anywhere in this tree to extend.",
                "notes": []}

    from dlc.l3 import manifest as mf
    m = mf.find_manifest({t["file"] for t in targets},
                         element_names=set(report.element_names))
    if m:
        for t in targets:
            cats = (m.get("categories") or {}).get(t["file"])
            if cats:
                t["categories"] = cats
                if m.get("description"):
                    t["category_conventions"] = m["description"]

    complete = _complete_targets(report, targets)
    if complete:
        targets = [t for t in targets if t["file"] not in complete]
        if not targets:
            return {"ok": True, "proposals": [], "rejected": [],
                    "model": None, "error": None,
                    "all_categories_covered": sorted(complete),
                    "notes": [complete[f] for f in sorted(complete)]}

    prompt = build_prompt(report, targets)
    used_model = model or _propose_model()
    resp = call(prompt, model=used_model, max_tokens=4000,
                feature="modeB")
    if not resp.get("ok"):
        return {"ok": False, "proposals": [], "rejected": [],
                "model": used_model,
                "error": resp.get("error") or "Model call failed.",
                "notes": []}

    proposals = parse_proposals(resp.get("text") or "")
    if not proposals and (resp.get("text") or "").strip():
        resp2 = call(prompt + "\n\nREMINDER: output ONLY the JSON object "
                     "now — no analysis text.",
                     model=used_model, max_tokens=4000, feature="modeB")
        if resp2.get("ok"):
            proposals = parse_proposals(resp2.get("text") or "")
    if not proposals:
        return {"ok": True, "proposals": [], "rejected": [],
                "model": used_model, "error": None,
                "notes": ["The coach found nothing trustworthy to propose "
                          "this time — try Propose again."]}
    valid, rejected = validate_and_dedupe(proposals, targets, manifest=m)
    notes: list[str] = []

    paths = {c.file: c.path for c in report.circuits if c.path}
    valid, rejected, notes = _category_gate(valid, rejected, notes, targets, m)
    valid, rejected, notes = _replay_gate(valid, rejected, notes, targets, paths)
    valid, rejected, notes = _reference_gate(valid, rejected, notes, targets)
    valid, rejected, notes = _selfcheck_gate(
        valid, rejected, notes, targets, call, used_model,
    )
    valid, rejected, notes = _synthesis_fallback(
        valid, rejected, notes, targets, m, paths,
    )
    for r in rejected:
        r["kind"] = _classify_reason(r.get("reason", ""))

    return {"ok": True, "proposals": valid, "rejected": rejected,
            "model": used_model, "error": None, "notes": notes}


def _complete_targets(report: TreeCoverageReport,
                      targets: list[dict]) -> dict[str, str]:
    """Files whose lab categories are all exercised already: nothing for
    the coach to propose there. {file: note}."""
    by_file = {c.file: c for c in report.circuits}
    out: dict[str, str] = {}
    for t in targets:
        cov = by_file.get(t["file"])
        row_cats = cov is not None and cov.categories_total > 0
        prog_cats = "program_categories_missing" in t
        if not (row_cats or prog_cats):
            continue
        if row_cats and cov.categories_missing:
            continue
        if prog_cats and t["program_categories_missing"]:
            continue
        if row_cats:
            what = (f"all {cov.categories_total} instruction categories "
                    f"are already exercised by your rows")
        else:
            n = len(t.get("program_categories_present") or [])
            what = (f"the program already executes all {n} instruction "
                    f"categories")
        out[t["file"]] = (f"{t['file']}: {what} — the coach has nothing "
                          f"to add.")
    return out


def _classify_reason(reason: str) -> str:
    r = reason.lower()
    if "lazy test" in r:
        return "lazy"
    if "unobservable" in r or "reads it back" in r or "read-back" in r:
        return "unobserved"
    if "duplicate" in r or "already in the program" in r or "appears twice" in r:
        return "duplicate"
    if ("not an instruction" in r or "instruction set" in r
            or "does not define" in r or "doesn't define" in r):
        return "undefined_op"
    if ("wrong expected value" in r or "lab reference" in r
            or "self-check" in r or "machine state" in r
            or "follows a dropped row" in r):
        return "wrong_expectation"
    return "format"


def _synthesis_fallback(valid, rejected, notes, targets, manifest, paths):
    if not manifest:
        return valid, rejected, notes
    from dlc.l3 import manifest as mf
    for t in targets:
        if not (t.get("has_program_rom")
                and t.get("program_categories_missing")):
            continue
        if any(g["file"] == t["file"] and g.get("program_words")
               for g in valid):
            continue
        try:
            existing = [int(w, 16) for w in t.get("program_words", [])]
            syn = mf.synthesize_program_extension(
                manifest, existing, t["program_categories_missing"],
                t["headers"], t.get("clock_col"),
            )
        except Exception:
            syn = None
        if not syn:
            continue
        p = {"file": t["file"], "spec_name": t["spec_name"], **syn}
        entry, reason = _validate_program_group(p, t, manifest)
        if entry is None:
            notes.append(f"machine-built fallback for {t['file']} was "
                         f"itself rejected: {reason}")
            continue
        entry["synthesized"] = True
        v2, r2, _ = _replay_gate([entry], [], [], targets, paths)
        if v2:
            valid.append(v2[0])
        else:
            rejected.extend(r2)
    return valid, rejected, notes


def _category_gate(valid, rejected, notes, targets, manifest):
    if not manifest:
        return valid, rejected, notes
    from dlc.l3.manifest import _cell_value
    cats_by_file = manifest.get("categories") or {}
    kept: list[dict] = []
    for g in valid:
        cats = cats_by_file.get(g["file"])
        t = next((x for x in targets if x["file"] == g["file"]), None)
        if not cats or t is None or g.get("program_words"):
            kept.append(g)
            continue
        headers = t["headers"]
        pred_cols = set()
        parsed = []
        for cat in cats:
            when = {}
            for col, v in (cat.get("when") or {}).items():
                val = _cell_value(str(v)) if not isinstance(v, int) else v
                if val is not None:
                    when[col] = val
            if when:
                parsed.append(when)
                pred_cols |= set(when)
        if not parsed or any(c not in headers for c in pred_cols):
            kept.append(g)
            continue
        idx = {h: i for i, h in enumerate(headers)}
        good: list[str] = []
        for raw in g["rows"]:
            cells = raw.split("#", 1)[0].split()
            vals = {}
            for col in pred_cols:
                i = idx[col]
                vals[col] = _cell_value(cells[i]) if i < len(cells) else None
            if any(v is None for v in vals.values()):
                good.append(raw)        
                continue
            if any(all(vals.get(c) == w for c, w in when.items())
                   for when in parsed):
                good.append(raw)
                continue
            rejected.append({
                "file": g["file"], "spec_name": g["spec_name"],
                "rows": [raw], "why": g.get("why", ""),
                "reason": ("tests an operation this lab does not define "
                           f"({', '.join(f'{c}={vals[c]}' for c in sorted(vals))}"
                           " matches no defined category) — no test needed"),
            })
        if good:
            kept.append({**g, "rows": good})
    return kept, rejected, notes


def _replay_gate(valid, rejected, notes, targets, paths):
    from dlc.l3 import manifest as mf
    from dlc.l3.coverage import replay_appended_rows
    m = mf.find_manifest({t["file"] for t in targets})
    ref_dir = mf.reference_dir(m)
    by_file = {t["file"]: t for t in targets}
    kept: list[dict] = []
    n_disputed = 0
    for g in valid:
        t = by_file.get(g["file"])
        path = paths.get(g["file"])
        if t is None or path is None or not t.get("has_clock"):
            kept.append(g)
            continue
        ref_file = ref_dir / g["file"] if ref_dir else None
        on_reference = False
        splice = ({"insert_at": g["insert_at"],
                   "insert_before_row": g.get("insert_before_row")}
                  if g.get("insert_at") is not None else {})
        try:
            if ref_file is not None and ref_file.is_file():
                try:
                    verdicts = replay_appended_rows(
                        str(ref_file), g["spec_name"], g["rows"],
                        g.get("program_words"), **splice)
                    on_reference = True
                except Exception:
                    verdicts = replay_appended_rows(
                        path, g["spec_name"], g["rows"],
                        g.get("program_words"), **splice)
            else:
                verdicts = replay_appended_rows(
                    path, g["spec_name"], g["rows"], g.get("program_words"),
                    **splice)
        except Exception:
            kept.append(g)
            continue

        if not on_reference:
            marks = sorted(i for i, v in enumerate(verdicts)
                           if v["verdict"] == "disagrees")
            if marks:
                g = {**g,
                     "disputed_rows": sorted(
                         set(g.get("disputed_rows") or []) | set(marks)),
                     "disputed_details": {
                         **(g.get("disputed_details") or {}),
                         **{str(i): verdicts[i]["detail"] for i in marks}}}
                n_disputed += len(marks)
            kept.append(g)
            continue

        if g.get("program_words"):
            bad = [v for v in verdicts if v["verdict"] == "disagrees"]
            if bad:
                rejected.append({
                    "file": g["file"], "spec_name": g["spec_name"],
                    "rows": [v["row"] for v in bad],
                    "why": g.get("why", ""),
                    "reason": ("expected values don't match the machine "
                               "state at that point in the program"),
                    "details": [{"row": v["row"], "detail": v["detail"]}
                                for v in bad],
                })
            else:
                kept.append(g)
            continue
        good: list[str] = []
        bad_hit = False
        for v in verdicts:
            if bad_hit:
                rejected.append({
                    "file": g["file"], "spec_name": g["spec_name"],
                    "rows": [v["row"]], "why": g.get("why", ""),
                    "reason": ("follows a dropped row — its expected values "
                               "assume that row's state change happened"),
                })
                continue
            if v["verdict"] == "disagrees":
                bad_hit = True
                rejected.append({
                    "file": g["file"], "spec_name": g["spec_name"],
                    "rows": [v["row"]], "why": g.get("why", ""),
                    "reason": ("wrong expected value for the state after "
                               "the existing rows — " + v["detail"]),
                })
                continue
            good.append(v["row"])
        if good:
            kept.append({**g, "rows": good})
    if n_disputed:
        notes.append(
            f"your circuit disagrees with {n_disputed} proposed row(s) at "
            f"that point in the test sequence — delivered as DISPUTED with "
            f"the computed values shown. Either the row's expectation is "
            f"wrong (it may ignore what the circuit holds at that step) or "
            f"your circuit is buggy exactly there; accepting runs the "
            f"truth on the temp copy.")
    return kept, rejected, notes


def _reference_gate(valid, rejected, notes, targets):
    from dlc.l3 import manifest as mf
    m = mf.find_manifest({t["file"] for t in targets})
    ref_dir = mf.reference_dir(m)
    if not ref_dir or not ref_dir.is_dir():
        return valid, rejected, notes
    by_file = {t["file"]: t for t in targets}
    kept: list[dict] = []
    checked = False
    for g in valid:
        ref_file = ref_dir / g["file"]
        t = by_file.get(g["file"])
        if not ref_file.is_file() or t is None:
            kept.append(g)
            continue
        try:
            verdicts = mf.reference_row_verdicts(
                ref_file, t["headers"], g["rows"],
            )
        except Exception:
            kept.append(g)
            continue
        checked = True
        good = [v["row"] for v in verdicts if v["verdict"] != "disagrees"]
        for v in verdicts:
            if v["verdict"] == "disagrees":
                rejected.append({"file": g["file"], "spec_name": g["spec_name"],
                                 "rows": [v["row"]], "why": g.get("why", ""),
                                 "reason": f"disagrees with the lab reference "
                                           f"({v['detail']})"})
        if good:
            kept.append({**g, "rows": good})
    return kept, rejected, notes


_SELFCHECK_PROMPT = "l3_row_selfcheck_v1.txt"


def _selfcheck_gate(valid, rejected, notes, targets, call, used_model):
    by_file = {t["file"]: t for t in targets}
    candidates = []
    payload_rows = []
    for gi, g in enumerate(valid):
        t = by_file.get(g["file"])
        if t is None or t.get("has_clock"):
            continue
        out_cols = [o["label"] for o in t["outputs"]]
        if not out_cols:
            continue
        for ri, raw in enumerate(g["rows"]):
            cells = raw.split("#", 1)[0].split()
            masked = [
                "?" if h in out_cols else (cells[i] if i < len(cells) else "?")
                for i, h in enumerate(t["headers"])
            ]
            payload_rows.append({
                "index": len(payload_rows),
                "file": g["file"],
                "inputs": " ".join(masked),
            })
            candidates.append((gi, ri, t))
    if not candidates:
        return valid, rejected, notes

    t0 = by_file[valid[candidates[0][0]]["file"]]
    template = (_PROMPT_DIR / _SELFCHECK_PROMPT).read_text(encoding="utf-8")
    prompt = (template
              .replace("<<HEADERS_JSON>>", json.dumps(
                  {c[2]["file"]: c[2]["headers"] for c in candidates}))
              .replace("<<OUTPUT_COLS_JSON>>", json.dumps(
                  {c[2]["file"]: [o["label"] for o in c[2]["outputs"]]
                   for c in candidates}))
              .replace("<<ROWS_JSON>>", json.dumps(payload_rows, indent=1)))
    resp = call(prompt, model=used_model, max_tokens=1200,
                feature="modeB")
    if not resp.get("ok"):
        notes.append("self-check call failed — rows pass through to the "
                     "inject verification unchecked.")
        return valid, rejected, notes

    m = re.search(r"\{.*\}", resp.get("text") or "", re.S)
    derived: dict[int, dict] = {}
    if m:
        try:
            for r in json.loads(m.group(0)).get("rows", []):
                if isinstance(r, dict) and isinstance(r.get("outputs"), dict):
                    derived[int(r.get("index", -1))] = r["outputs"]
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    drop: set[tuple[int, int]] = set()
    for pi, (gi, ri, t) in enumerate(candidates):
        outs = derived.get(pi)
        raw = valid[gi]["rows"][ri]
        cells = raw.split("#", 1)[0].split()
        by_col = dict(zip(t["headers"], cells))
        ok = outs is not None
        if outs is not None:
            for col in (o["label"] for o in t["outputs"]):
                want = _row_cell_value(by_col.get(col))
                got = _row_cell_value(outs.get(col))
                if want is None:
                    continue
                if got is None or got != want:
                    ok = False
                    break
        if not ok:
            drop.add((gi, ri))
    if not drop:
        notes.append("self-check confirmed every proposed row.")
        return valid, rejected, notes

    n_disputed = 0
    for gi, g in enumerate(valid):
        marks = sorted(ri for (gj, ri) in drop if gj == gi)
        if marks:
            g["disputed_rows"] = marks
            n_disputed += len(marks)
    notes.append(
        f"self-check could not independently confirm {n_disputed} row(s) — "
        f"delivered as DISPUTED. Accept them only if you are confident they "
        f"match the lab's intent; a disputed row that then fails on the "
        f"temp copy means either the row is wrong (discard it) or your "
        f"circuit has a bug right there (Mode A).")
    return valid, rejected, notes


def _row_cell_value(cell) -> int | None:
    if cell is None:
        return None
    tok = _tokenize(str(cell).strip())
    return tok.value if tok.kind == "int" else None
