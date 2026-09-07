"""
Gradescope-style official-testcase injection for test runs.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET


def _official_testcase(filename: str) -> str | None:
    from dlc.l3 import official_store
    return official_store.get_content(filename)


def file_test_status(circuit, filename: str) -> str | None:
    from dlc.l3 import official_store
    from dlc.testing.spec import extract_test_specs

    if official_store.get_content(filename) is None:
        return None
    for comp in circuit.components:
        if comp.element_name != "Testcase":
            continue
        raw = comp.attributes.get("Testdata", "")
        if not isinstance(raw, str) or not raw.strip():
            continue
        if official_store.status_for(filename, raw) == "official":
            return "official"
    specs = [s for s in extract_test_specs(circuit) if s.rows]
    return "modified" if specs else "missing"

INJECTED_TEST_LABEL = "official"


def _replace_testcases(root: ET.Element, content: str) -> bool:
    ves = root.find("visualElements")
    if ves is None:
        return False
    pos_x, pos_y = 0, 0
    for ve in list(ves.findall("visualElement")):
        if ve.findtext("elementName") == "Testcase":
            pos = ve.find("pos")
            if pos is not None:
                pos_x = pos.get("x", "0")
                pos_y = pos.get("y", "0")
            ves.remove(ve)
    target = ET.SubElement(ves, "visualElement")
    ET.SubElement(target, "elementName").text = "Testcase"
    attrs = ET.SubElement(target, "elementAttributes")
    lbl = ET.SubElement(attrs, "entry")
    ET.SubElement(lbl, "string").text = "Label"
    ET.SubElement(lbl, "string").text = INJECTED_TEST_LABEL
    entry = ET.SubElement(attrs, "entry")
    ET.SubElement(entry, "string").text = "Testdata"
    td = ET.SubElement(entry, "testData")
    ET.SubElement(td, "dataString").text = content
    pos = ET.SubElement(target, "pos")
    pos.set("x", str(pos_x))
    pos.set("y", str(pos_y))
    return True


def _fill_empty_roms(root: ET.Element, data: str) -> int:
    if not (data or "").strip():
        return 0
    filled = 0
    for ve in root.iter("visualElement"):
        if ve.findtext("elementName") != "ROM":
            continue
        entries = _entry_map(ve)
        cur = entries.get("Data")
        if cur is not None and (cur.text or "").strip():
            continue
        attrs = ve.find("elementAttributes")
        if attrs is None:
            attrs = ET.SubElement(ve, "elementAttributes")
        if cur is None:
            entry = ET.SubElement(attrs, "entry")
            ET.SubElement(entry, "string").text = "Data"
            ET.SubElement(entry, "data").text = data
        else:
            cur.text = data
        filled += 1
    return filled


def _entry_map(element: ET.Element) -> dict[str, ET.Element]:
    out: dict[str, ET.Element] = {}
    for entry in element.findall("./elementAttributes/entry"):
        kids = list(entry)
        if len(kids) == 2 and kids[0].tag == "string" and kids[0].text:
            out[kids[0].text] = kids[1]
    return out


def _data_words(raw, fmt: str = "hex") -> list[int]:
    base = {"hex": 16, "bin": 2, "oct": 8, "dec": 10, "def": 10}.get(
        str(fmt or "hex").lower(), 16)

    def one(tok: str) -> int:
        try:
            return int(tok, base)
        except ValueError:
            try:
                return int(tok, 16)
            except ValueError:
                return 0

    words: list[int] = []
    for tok in str(raw or "").replace(",", " ").split():
        count = 1
        if "*" in tok:
            head, _, tok = tok.partition("*")
            try:
                count = max(int(head, 10), 1)
            except ValueError:
                count = 1
        words.extend([one(tok)] * count)
    while words and words[-1] == 0:
        words.pop()
    return words


def _program_roms(circuit) -> list[int]:
    roms = [i for i, c in enumerate(circuit.components)
            if c.element_name == "ROM"]
    flagged = [i for i in roms
               if circuit.components[i].attributes.get("isProgramMemory")]
    return flagged or roms


ROM_GATE_MESSAGES = {
    "missing": (
        "This lab runs the course program from an instruction-memory ROM, "
        "but this file has no ROM. Add the ROM (Program Memory checked), "
        "enter the course program, run the tests again, then come back."),
    "empty": (
        "ROM '{rom}' is empty, so the circuit has no program to run. Enter "
        "the course program into the ROM (double-click it in Digital), run "
        "the tests again, then come back."),
    "mismatch": (
        "ROM '{rom}' does not hold the course program: {differing} of "
        "{expected} words differ, first at address {address} (your word "
        "there is {word}). Fix the ROM contents before debugging the "
        "datapath — with the wrong program every row can fail for the "
        "wrong reason."),
}


def check_program_rom(path: str, filename: str) -> dict | None:
    try:
        from dlc.l3.official_store import get_runtime_payload
        from dlc.parser.dig_parser import parse_dig_file

        base = os.path.basename(str(filename or ""))
        if base.startswith(".dlc_injected__"):
            base = base[len(".dlc_injected__"):]
        official = get_runtime_payload(base, "rom")
        if not official:
            return None
        want = _data_words(official, "hex")
        circuit = parse_dig_file(path)
        roms = _program_roms(circuit)
        if not roms:
            return {"status": "missing", "words_expected": len(want),
                    "message": ROM_GATE_MESSAGES["missing"]}
        for idx in roms:
            comp = circuit.components[idx]
            name = comp.label or f"ROM[{idx}]"
            got = _data_words(comp.attributes.get("Data", ""),
                              comp.attributes.get("intFormat", "hex"))
            info = {"rom": name, "component_index": idx,
                    "words_expected": len(want), "words_found": len(got)}
            if not got:
                return {"status": "empty", **info,
                        "message": ROM_GATE_MESSAGES["empty"].format(rom=name)}
            if got != want:
                span = max(len(got), len(want))
                bad = [a for a in range(span)
                       if (got[a] if a < len(got) else 0)
                       != (want[a] if a < len(want) else 0)]
                first = bad[0]
                word = got[first] if first < len(got) else 0
                return {"status": "mismatch", **info,
                        "differing": len(bad), "first_bad_address": first,
                        "message": ROM_GATE_MESSAGES["mismatch"].format(
                            rom=name, differing=len(bad), expected=len(want),
                            address=first, word=f"{word:x}")}
        return None
    except Exception:
        return None


def prepare_injected_run(path: str, filename: str) -> tuple[str | None, list[str]]:
    try:
        from dlc.parser.dig_parser import parse_dig_file
        from dlc.l3.official_store import get_runtime_payload

        circuit = parse_dig_file(path)
        status = file_test_status(circuit, filename)
        tree = ET.parse(path)
        root = tree.getroot()
        changed = False
        notes: list[str] = []

        if status in ("missing", "modified"):
            official = _official_testcase(filename)
            if official and _replace_testcases(root, official):
                changed = True
                if status == "missing":
                    notes.append(
                        "official testcase injected (this file has no "
                        "test rows — Gradescope grades with the official "
                        "tests)")
                else:
                    notes.append(
                        "official testcase injected in place of this "
                        "file's modified testcase (Gradescope grades "
                        "with the official tests)")

        rom_data = get_runtime_payload(filename, "rom")
        if rom_data:
            n = _fill_empty_roms(root, rom_data)
            if n:
                changed = True
                plural = "s" if n != 1 else ""
                notes.append(
                    f"the course program was loaded into {n} empty "
                    f"ROM{plural} for this test run so your logic could be "
                    f"tested — your file still has the ROM unprogrammed; "
                    f"enter the course program before submitting (the "
                    f"debugger only runs once the ROM holds it)")

        if not changed:
            return None, []
        d, base = os.path.split(path)
        temp_path = os.path.join(d, f".dlc_injected__{base}")
        tree.write(temp_path, encoding="utf-8", xml_declaration=True)
        return temp_path, notes
    except Exception:
        return None, []


def inject_official_tests_in_place(path: str, filename: str) -> list[str]:
    try:
        from dlc.parser.dig_parser import parse_dig_file
        circuit = parse_dig_file(path)
        status = file_test_status(circuit, filename)
        if status not in ("missing", "modified"):
            return []
        official = _official_testcase(filename)
        if not official:
            return []
        tree = ET.parse(path)
        if not _replace_testcases(tree.getroot(), official):
            return []
        tree.write(path, encoding="utf-8", xml_declaration=True)
        if status == "missing":
            return ["official testcase injected (this file has no test "
                    "rows — Gradescope grades with the official tests)"]
        return ["official testcase injected in place of the modified "
                "testcase (Gradescope grades with the official tests)"]
    except Exception:
        return []


def cleanup_injected(temp_path: str | None) -> None:
    if not temp_path:
        return
    try:
        os.unlink(temp_path)
    except OSError:
        pass
