"""Tests for extract_device.py — moving a device into a file of its own.

Config-only housekeeping, but it rewrites hand-maintained YAML in place, so the
things worth pinning are that nothing is lost from the source file and nothing
is silently overwritten in the target.
"""
import textwrap

import pytest
import yaml

import extract_device


SHARED = textwrap.dedent("""\
    devices:
      SM1_UTA1:
        info: SM1/UTA1/GA4
        admins:
        - ops
        fields:
          T:
            topic: GA4/temperature   # keep this comment
          H:
            topic: GA4/humidity

      SM1_CDZ1:
        info: SM1/UTA1/GA4
        admins:
        - ops
        fields:
          I:
            topic: R2Env/cdz/I1
          IF:
            topic: R2Env/cdz/I1_fast
            signal: true
    """)


def _tree(tmp_path, text=SHARED, name="SM1_UTA1.yaml"):
    d = tmp_path / "sensors.d"
    d.mkdir()
    (d / name).write_text(text)
    return d


def test_the_block_moves_out_whole(tmp_path, monkeypatch):
    d = _tree(tmp_path)
    monkeypatch.setattr("sys.argv", ["extract_device.py", "SM1_CDZ1", "--dir", str(d)])
    extract_device.main()

    kept = yaml.safe_load((d / "SM1_UTA1.yaml").read_text())["devices"]
    moved = yaml.safe_load((d / "SM1_CDZ1.yaml").read_text())["devices"]
    assert list(kept) == ["SM1_UTA1"]
    assert list(moved) == ["SM1_CDZ1"]
    assert sorted(moved["SM1_CDZ1"]["fields"]) == ["I", "IF"]
    assert moved["SM1_CDZ1"]["admins"] == ["ops"]
    # the device that stays keeps its own body, comments included
    assert "# keep this comment" in (d / "SM1_UTA1.yaml").read_text()


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    d = _tree(tmp_path)
    monkeypatch.setattr(
        "sys.argv", ["extract_device.py", "SM1_CDZ1", "--dir", str(d), "--dry-run"]
    )
    extract_device.main()
    assert (d / "SM1_UTA1.yaml").read_text() == SHARED
    assert not (d / "SM1_CDZ1.yaml").exists()


def test_a_device_already_alone_is_refused(tmp_path, monkeypatch):
    # the no-op case: extracting it would produce a file holding `devices:` and
    # nothing else, which the loader rejects
    d = _tree(tmp_path)
    monkeypatch.setattr("sys.argv", ["extract_device.py", "SM1_CDZ1", "--dir", str(d)])
    extract_device.main()
    monkeypatch.setattr("sys.argv", ["extract_device.py", "SM1_CDZ1", "--dir", str(d)])
    with pytest.raises(SystemExit):
        extract_device.main()


def test_existing_target_is_never_overwritten(tmp_path, monkeypatch):
    d = _tree(tmp_path)
    (d / "SM1_CDZ1.yaml").write_text("devices: {}\n")
    monkeypatch.setattr("sys.argv", ["extract_device.py", "SM1_CDZ1", "--dir", str(d)])
    with pytest.raises(SystemExit):
        extract_device.main()
    assert (d / "SM1_CDZ1.yaml").read_text() == "devices: {}\n"
    assert (d / "SM1_UTA1.yaml").read_text() == SHARED


def test_unknown_device_is_refused(tmp_path, monkeypatch):
    d = _tree(tmp_path)
    monkeypatch.setattr("sys.argv", ["extract_device.py", "SM9", "--dir", str(d)])
    with pytest.raises(SystemExit):
        extract_device.main()


def test_extracting_the_first_of_three_keeps_the_rest(tmp_path):
    # the block runs to the next device key, not to end of file
    text = SHARED + (
        "\n"
        "  SM1_UTA2:\n"
        "    info: SM1/UTA2\n"
        "    fields:\n"
        "      T:\n"
        "        topic: GA5/temperature\n"
    )
    kept, block = extract_device.extract(text, "SM1_UTA1")
    assert list(yaml.safe_load(kept)["devices"]) == ["SM1_CDZ1", "SM1_UTA2"]
    assert list(yaml.safe_load("devices:\n" + block)["devices"]) == ["SM1_UTA1"]
