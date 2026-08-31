"""Secret resolution and the built-in hygiene scan."""
from __future__ import annotations

import pytest
from conftest import manifest_data

from mcp_e2e_harness.secrets import (
    MissingSecretError,
    SecretLeakError,
    assert_clean,
    collect_scan_set,
    resolve_env_map,
)


def test_resolve_plain_and_secret_values():
    resolved, secrets = resolve_env_map(
        {"PLAIN": "value", "KEY": {"$secret": "MY_VAR"}},
        {"MY_VAR": "sekrit-value-123"})
    assert resolved == {"PLAIN": "value", "KEY": "sekrit-value-123"}
    assert secrets == ["sekrit-value-123"]


def test_missing_secret_names_the_var_never_a_value():
    with pytest.raises(MissingSecretError) as exc:
        resolve_env_map({"KEY": {"$secret": "UNSET_VAR_XYZ"}}, {})
    assert "UNSET_VAR_XYZ" in str(exc.value)


def test_collect_scan_set_covers_transport_cells_and_secret_keys():
    data = manifest_data()
    data["server"]["secret_keys"] = ["NAMED_SENSITIVE"]
    data["server"]["transport"]["env"] = {"A": {"$secret": "VAR_A"}}
    data["cells"]["basic"]["env"] = {"B": {"$secret": "VAR_B"}}
    environ = {"VAR_A": "aaaaaaaa-secret", "VAR_B": "bbbbbbbb-secret",
               "NAMED_SENSITIVE": "cccccccc-secret"}
    scan = collect_scan_set(data, environ)
    assert set(scan) == {"aaaaaaaa-secret", "bbbbbbbb-secret", "cccccccc-secret"}


def test_scan_set_drops_short_values_and_dedupes():
    data = manifest_data()
    data["server"]["secret_keys"] = ["SHORT", "DUP1", "DUP2"]
    environ = {"SHORT": "abc", "DUP1": "same-secret-value", "DUP2": "same-secret-value"}
    assert collect_scan_set(data, environ) == ["same-secret-value"]


def test_assert_clean_raises_without_echoing_the_secret():
    with pytest.raises(SecretLeakError) as exc:
        assert_clean("... sekrit-value-123 ...", ["sekrit-value-123"], "trace line 3")
    message = str(exc.value)
    assert "trace line 3" in message
    assert "sekrit-value-123" not in message


def test_assert_clean_passes_clean_text():
    assert_clean("nothing to see", ["sekrit-value-123"], "answer")
