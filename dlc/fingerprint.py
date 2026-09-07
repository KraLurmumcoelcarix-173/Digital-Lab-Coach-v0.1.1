"""
Fingerprint helper CLI — instructor tooling for shipping
official tests (and ROM contents) in a fork.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path


def rom_payload(circuit) -> tuple[str, int] | None:
    roms = [c for c in circuit.components if c.element_name == "ROM"]
    flagged = [c for c in roms if c.attributes.get("isProgramMemory")]
    roms = flagged or roms
    if not roms:
        return None
    if len(roms) > 1:
        raise ValueError(f"{len(roms)} ROMs — mark the one to register as "
                         "Program Memory")
    data = str(roms[0].attributes.get("Data", "") or "").strip()
    if not data:
        raise ValueError("the ROM is empty — fill it in the instructor copy")
    blob = base64.b64encode(json.dumps({"rom": data}).encode()).decode()
    return blob, len([t for t in data.replace(",", " ").split() if t])


def fingerprint_file(path: str, with_rom: bool = False) -> dict:
    from dlc.l3.manifest import normalized_test_hash
    from dlc.parser.dig_parser import parse_dig_file
    from dlc.testing.spec import extract_test_specs
    try:
        circuit = parse_dig_file(path)
        specs = extract_test_specs(circuit)
    except Exception as exc:
        raise ValueError(f"could not parse: {exc}")
    if not specs:
        raise ValueError("no testcase in this file")
    spec = specs[0]
    out = {"content": spec.raw_data_string,
           "sha1": normalized_test_hash(spec.raw_data_string),
           "rows": spec.row_count()}
    if with_rom:
        rom = rom_payload(circuit)
        if rom is not None:
            out["runtime"], out["rom_words"] = rom
    return out


def merge_into(defaults_path: str, entries: dict) -> int:
    p = Path(defaults_path)
    existing: dict = {}
    if p.is_file():
        existing = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(existing, dict):
            existing = {}
    for name, entry in entries.items():
        cur = existing.get(name)
        existing[name] = {**(cur if isinstance(cur, dict) else {}), **entry}
    p.write_text(json.dumps(existing, indent=1, ensure_ascii=False) + "\n",
                 encoding="utf-8")
    return len(entries)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m dlc.fingerprint",
        description=("Generate official-test entries from .dig files (the "
                     "entries data/official_tests_defaults.json ships)."))
    ap.add_argument("files", nargs="+", help=".dig files to fingerprint")
    ap.add_argument("-o", "--out",
                    help="write the JSON here instead of stdout")
    ap.add_argument("--merge", metavar="DEFAULTS_JSON",
                    help="update the entries in place inside this file "
                         "(e.g. data/official_tests_defaults.json)")
    ap.add_argument("--with-rom", action="store_true",
                    help="also register each file's ROM contents as the "
                         "entry's runtime blob")
    ap.add_argument("--hashes-only", action="store_true",
                    help="print the manifest official_tests shape "
                         "({filename: sha1}) instead of full entries")
    args = ap.parse_args(argv)
    if args.merge and args.hashes_only:
        ap.error("--merge needs full entries; drop --hashes-only")

    out: dict[str, object] = {}
    failed = 0
    for f in args.files:
        name = Path(f).name
        try:
            e = fingerprint_file(f, with_rom=args.with_rom)
        except ValueError as exc:
            print(f"  SKIPPED {name}: {exc}", file=sys.stderr)
            failed += 1
            continue
        if args.hashes_only:
            out[name] = e["sha1"]
        else:
            entry = {"content": e["content"], "sha1": e["sha1"]}
            if e.get("runtime"):
                entry["runtime"] = e["runtime"]
            out[name] = entry
        rom_txt = (f", ROM {e['rom_words']} words" if e.get("runtime")
                   else (", no ROM" if args.with_rom else ""))
        print(f"  {name:34s} fingerprint {e['sha1'][:12]}  "
              f"({e['rows']} rows{rom_txt})", file=sys.stderr)

    if not out:
        print("nothing fingerprinted.", file=sys.stderr)
        return 1
    text = json.dumps(out, indent=1)
    if args.merge:
        n = merge_into(args.merge, out)
        print(f"merged {n} entr{'y' if n == 1 else 'ies'} into "
              f"{args.merge} — restart the server.", file=sys.stderr)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"wrote {len(out)} entr{'y' if len(out) == 1 else 'ies'} to "
              f"{args.out} — merge into data/official_tests_defaults.json "
              f"(fork) or keep for your records.", file=sys.stderr)
    elif not args.merge:
        print(text)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
