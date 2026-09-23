"""The CLI surface: validate, and run --dry-run (which needs no driver installed)."""
from __future__ import annotations

import json

from conftest import claude_cell_data, manifest_data, write_manifest

from mcp_e2e_harness.cli import main


def test_validate_ok(tmp_path, capsys):
    path = write_manifest(tmp_path, manifest_data())
    assert main(["validate", "--manifest", str(path)]) == 0
    out = capsys.readouterr().out
    assert "manifest loads clean" in out


def test_validate_reports_manifest_errors(tmp_path, capsys):
    data = manifest_data()
    data["bogus"] = 1
    path = write_manifest(tmp_path, data)
    try:
        main(["validate", "--manifest", str(path)])
    except SystemExit as exc:
        assert "unknown key" in str(exc)
    else:
        raise AssertionError("expected SystemExit")


def test_validate_lints_checks(tmp_path, capsys):
    data = manifest_data(checks=[{"id": "bad", "applies_to": {"tool": "*"},
                                  "assert": {"matches": {"pointer": "/t", "regex": "("}}}])
    path = write_manifest(tmp_path, data)
    assert main(["validate", "--manifest", str(path)]) == 1
    assert "error" in capsys.readouterr().out


def test_run_dry_run_via_cli(tmp_path, capsys):
    path = write_manifest(tmp_path, claude_cell_data())
    rc = main(["run", "--manifest", str(path), "--run-dir", str(tmp_path / "run"), "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "never batched" in out
    run_manifest = json.loads((tmp_path / "run" / "run-manifest.json").read_text())
    assert run_manifest["dry_run"] is True
    # The dry run wrote and asserted a real claude argv.
    assert "--strict-mcp-config" in run_manifest["results"][0]["command"]


def test_probe_driver_unknown_driver_exits_2(capsys):
    assert main(["probe-driver", "--driver", "mystery-cli"]) == 2
    assert "unknown driver" in capsys.readouterr().out


def test_run_fatal_config_error_exits_2(tmp_path, capsys):
    data = claude_cell_data()
    path = write_manifest(tmp_path, data)
    rc = main(["run", "--manifest", str(path), "--run-dir", str(tmp_path / "run"),
               "--cells", "no-such-cell", "--dry-run"])
    assert rc == 2
    assert "FATAL" in capsys.readouterr().out
