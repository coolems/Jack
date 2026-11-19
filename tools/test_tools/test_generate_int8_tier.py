"""Tests: the low tier always names a real model, and int8 is opt-in.

WHY THESE EXIST (2026-11-15 review finding M6): the low tier used to carry a placeholder
model id ('TODO/publish-then-set-owner/Sana_Sprint_0.6B_int8') for a repo that was never
published, gated by a string prefix ('startswith TODO'). That leaked a non-existent repo
into the shipped manifest contract, built junk cache paths in KNOWN_MODEL_IDS, and made
"not published" indistinguishable from "typo'd id". Now an EMPTY field is the only
representation of "no int8 build", the tier table always names a downloadable repo, and
an int8 build (published repo id OR a local folder from scripts/quantize_sana_int8.py)
is an explicit opt-in. These tests pin that shape and the manifest <-> in-file parity
(v22 was exactly this kind of drift bug).

No GPU, no torch, no network: the tool module imports with stdlib only.
"""

import json
import os

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
_TOOL_DIR = os.path.join(_REPO_ROOT, "tools", "generate_tool")

import importlib

gen = importlib.import_module("tools.generate_tool.generate_image")


def _manifest():
    with open(os.path.join(_TOOL_DIR, "tool_manifest.json"), encoding="utf-8") as fh:
        return json.load(fh)


def test_no_placeholder_id_left_anywhere():
    """No 'TODO' sentinel may survive in a LIVE config (code or the tier table).

    The manifest 'notes' field is a changelog and may quote the old placeholder as
    history - only executable/contract values are checked here.
    """
    offenders = []
    with open(os.path.join(_TOOL_DIR, "generate_image.py"), encoding="utf-8") as fh:
        if "TODO/publish" in fh.read():
            offenders.append("generate_image.py")
    for tier, cfg in _manifest()["tier_models"].items():
        if "TODO" in str(cfg.get("model_id", "")):
            offenders.append(f"tier_models.{tier}.model_id")
    assert not offenders, f"placeholder model id still present in: {offenders}"


def test_int8_is_unconfigured_by_default():
    """Shipped defaults: empty fields = no int8 build, so nothing is configured."""
    assert gen._SANA_INT8_06_MODEL_ID == ""
    assert gen._SANA_INT8_06_LOCAL_DIR == ""
    assert gen._int8_build_configured() == ""


def test_low_tier_names_a_real_repo_in_both_tables():
    """The in-file fallback and the shipped manifest must name the real bf16 0.6B repo."""
    low = gen.TIER_MODELS["low"]
    assert low["model_id"] == gen._SANA_06_MODEL_ID
    assert low["dtype"] == "bf16"
    assert low["pipeline"] == "sana_sprint"
    assert low["offload"] is True

    man_low = _manifest()["tier_models"]["low"]
    assert man_low["model_id"] == gen._SANA_06_MODEL_ID
    assert man_low["dtype"] == "bf16"


def test_manifest_and_infile_tier_table_mirror():
    """Manifest 'tier_models' and TIER_MODELS must agree (v22 drift guard)."""
    table = _manifest()["tier_models"]
    assert set(table) == set(gen.TIER_MODELS)
    for tier, cfg in table.items():
        local = gen.TIER_MODELS[tier]
        for key, value in cfg.items():
            assert local.get(key) == value, f"{tier}.{key}: manifest {value!r} vs in-file {local.get(key)!r}"


def test_known_model_ids_are_all_real_repo_ids():
    """Every cache id must be a usable 'owner/name' id (no empty entries)."""
    for mid in gen._known_model_ids():
        assert mid and "/" in mid, f"invalid cache id in _known_model_ids(): {mid!r}"
        assert "TODO" not in mid


def test_known_model_ids_grow_only_when_int8_is_configured(monkeypatch):
    """Configured int8 ids join the list; empty ones never build a phantom cache path."""
    monkeypatch.setattr(gen, "_SANA_INT8_16_MODEL_ID", "owner/Sana_Sprint_1.6B_int8")
    monkeypatch.setattr(gen, "_SANA_INT8_06_MODEL_ID", "")
    ids = gen._known_model_ids()
    assert "owner/Sana_Sprint_1.6B_int8" in ids
    assert "" not in ids


def test_published_repo_id_is_accepted(monkeypatch):
    monkeypatch.setattr(gen, "_SANA_INT8_06_MODEL_ID", "owner/Sana_Sprint_0.6B_int8")
    assert gen._int8_build_configured() == "owner/Sana_Sprint_0.6B_int8"


def test_repo_id_without_owner_is_not_configured(monkeypatch):
    """A bare name (no owner) is not a repo id - treat it as unconfigured, not as a repo."""
    monkeypatch.setattr(gen, "_SANA_INT8_06_MODEL_ID", "Sana_Sprint_0.6B_int8")
    assert gen._int8_build_configured() == ""


def test_complete_local_dir_is_accepted(monkeypatch, tmp_path):
    """A built folder (model_index.json + a scales sidecar) works with no Hub access."""
    (tmp_path / "model_index.json").write_text("{}", encoding="utf-8")
    te = tmp_path / "text_encoder"
    te.mkdir()
    (te / "scales.safetensors").write_bytes(b"x")
    monkeypatch.setattr(gen, "_SANA_INT8_06_LOCAL_DIR", str(tmp_path))
    assert gen._int8_build_configured() == str(tmp_path)


def test_incomplete_local_dir_is_rejected(monkeypatch, tmp_path):
    """A half-built folder must read as 'not configured', never as a loadable build."""
    # no model_index.json at all
    monkeypatch.setattr(gen, "_SANA_INT8_06_LOCAL_DIR", str(tmp_path))
    assert gen._int8_build_configured() == ""
    # index present but no scales sidecar anywhere
    (tmp_path / "model_index.json").write_text("{}", encoding="utf-8")
    (tmp_path / "transformer").mkdir()
    assert gen._int8_build_configured() == ""


def test_local_dir_takes_priority_over_repo_id(monkeypatch, tmp_path):
    """A build on this machine is used instead of downloading the same model from the Hub."""
    (tmp_path / "model_index.json").write_text("{}", encoding="utf-8")
    tf = tmp_path / "transformer"
    tf.mkdir()
    (tf / "scales.safetensors").write_bytes(b"x")
    monkeypatch.setattr(gen, "_SANA_INT8_06_LOCAL_DIR", str(tmp_path))
    monkeypatch.setattr(gen, "_SANA_INT8_06_MODEL_ID", "owner/Sana_Sprint_0.6B_int8")
    assert gen._int8_build_configured() == str(tmp_path)


def test_missing_local_dir_falls_back_to_repo_id(monkeypatch, tmp_path):
    """A configured path that does not exist is not a build - the repo id still applies."""
    monkeypatch.setattr(gen, "_SANA_INT8_06_LOCAL_DIR", str(tmp_path / "nope"))
    monkeypatch.setattr(gen, "_SANA_INT8_06_MODEL_ID", "owner/Sana_Sprint_0.6B_int8")
    assert gen._int8_build_configured() == "owner/Sana_Sprint_0.6B_int8"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "--no-header"]))
