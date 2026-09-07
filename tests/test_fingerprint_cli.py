"""
Fingerprint helper CLI (dlc/fingerprint.py): turns .dig files into
ready-to-ship official-test entries; the sha1 must be byte-identical to
what the store/manifest machinery matches with.
"""

import json
import subprocess
import sys

from dlc import fingerprint as fp
from dlc.l3.manifest import normalized_test_hash
from dlc.parser.dig_parser import parse_dig_file
from dlc.testing.spec import extract_test_specs

_AND = "data/sample_circuits/tier1_minimal/single_and.dig"


def _and_spec():
    return extract_test_specs(parse_dig_file(_AND))[0]


def test_defaults_shape_written_to_file(tmp_path):
    out = tmp_path / "defaults.json"
    rc = fp.main([_AND, "-o", str(out)])
    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    spec = _and_spec()
    entry = data["single_and.dig"]
    assert entry["content"] == spec.raw_data_string
    assert entry["sha1"] == normalized_test_hash(spec.raw_data_string)
    assert set(entry) == {"content", "sha1"}


def test_hashes_only_prints_manifest_shape(capsys):
    rc = fp.main(["--hashes-only", _AND])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data == {"single_and.dig":
                    normalized_test_hash(_and_spec().raw_data_string)}


def test_entry_feeds_the_official_store(tmp_path, monkeypatch):
    from dlc.l3 import official_store as ost
    out = tmp_path / "defaults.json"
    assert fp.main([_AND, "-o", str(out)]) == 0
    monkeypatch.setenv("DLC_OFFICIAL_DEFAULTS_PATH", str(out))
    assert ost.status_for("single_and.dig",
                          _and_spec().raw_data_string) == "official"
    assert ost.status_for("single_and.dig", "A B Y\n1 1 0") == "modified"


def test_bad_files_are_skipped_with_nonzero_exit(tmp_path, capsys):
    no_tc = tmp_path / "empty.dig"
    no_tc.write_text("<circuit><visualElements/><wires/></circuit>")
    rc = fp.main([str(no_tc), str(tmp_path / "ghost.dig"), _AND])
    err = capsys.readouterr().err
    assert rc == 1
    assert "SKIPPED empty.dig" in err and "SKIPPED ghost.dig" in err
    assert "single_and.dig" in err
    rc = fp.main([str(no_tc)])
    assert rc == 1


_ROM_LAB = (
    '<?xml version="1.0" encoding="utf-8"?><circuit><version>2</version>'
    '<attributes/><visualElements>'
    '<visualElement><elementName>In</elementName><elementAttributes>'
    '<entry><string>Label</string><string>A</string></entry>'
    '</elementAttributes><pos x="0" y="0"/></visualElement>'
    '<visualElement><elementName>ROM</elementName><elementAttributes>'
    '<entry><string>AddrBits</string><int>1</int></entry>'
    '<entry><string>Bits</string><int>4</int></entry>%s'
    '</elementAttributes><pos x="200" y="0"/></visualElement>'
    '<visualElement><elementName>Out</elementName><elementAttributes>'
    '<entry><string>Label</string><string>D</string></entry>'
    '<entry><string>Bits</string><int>4</int></entry>'
    '</elementAttributes><pos x="400" y="20"/></visualElement>'
    '<visualElement><elementName>Testcase</elementName><elementAttributes>'
    '<entry><string>Label</string><string>t</string></entry>'
    '<entry><string>Testdata</string><testData><dataString>A D\n0 5\n1 6'
    '</dataString></testData></entry>'
    '</elementAttributes><pos x="0" y="200"/></visualElement>'
    '</visualElements><wires/></circuit>'
)


def test_with_rom_registers_the_file_rom(tmp_path, capsys):
    import base64
    lab = tmp_path / "romlab.dig"
    lab.write_text(_ROM_LAB % (
        '<entry><string>Data</string><data>5,6</data></entry>'),
        encoding="utf-8")
    out = tmp_path / "entries.json"
    assert fp.main([str(lab), _AND, "--with-rom", "-o", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    entry = data["romlab.dig"]
    assert set(entry) == {"content", "sha1", "runtime"}
    assert json.loads(base64.b64decode(entry["runtime"])) == {"rom": "5,6"}
    assert set(data["single_and.dig"]) == {"content", "sha1"}
    err = capsys.readouterr().err
    assert "ROM 2 words" in err and "no ROM" in err

    empty = tmp_path / "emptyrom.dig"
    empty.write_text(_ROM_LAB % "", encoding="utf-8")
    assert fp.main([str(empty), "--with-rom", "-o", str(out)]) == 1
    assert "SKIPPED emptyrom.dig" in capsys.readouterr().err

    defaults = tmp_path / "defaults.json"
    defaults.write_text(json.dumps({
        "_note": "keep me",
        "other.dig": {"content": "X Y\n0 0", "sha1": "1" * 40,
                      "runtime": "keepblob"},
        "romlab.dig": {"content": "old", "sha1": "2" * 40,
                       "runtime": "oldblob"},
    }), encoding="utf-8")
    assert fp.main([str(lab), "--merge", str(defaults)]) == 0
    merged = json.loads(defaults.read_text(encoding="utf-8"))
    assert merged["_note"] == "keep me"
    assert merged["other.dig"]["runtime"] == "keepblob"
    assert merged["romlab.dig"]["content"] == entry["content"]
    assert merged["romlab.dig"]["runtime"] == "oldblob"
    assert fp.main([str(lab), "--with-rom", "--merge", str(defaults)]) == 0
    merged = json.loads(defaults.read_text(encoding="utf-8"))
    assert merged["romlab.dig"]["runtime"] == entry["runtime"]
    assert "merged 1 entry" in capsys.readouterr().err


def test_runs_as_a_module():
    r = subprocess.run([sys.executable, "-m", "dlc.fingerprint", _AND],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0
    assert "single_and.dig" in json.loads(r.stdout)
    assert "fingerprint" in r.stderr
