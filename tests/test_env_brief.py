"""Cross-platform tests for the env-brief plugin.

Covers the defect report 2026-11-18:
  C1 text-mode bytes NUL anchor in the Windows lock path (hard TypeError),
  C2 cross-process lock on a private temp file (no exclusion at all),
  M1 Linux `os` trigger field churns on kernel upgrades (uname -sr),
  M2 negative UTC offset loses its sign in user_timezone_hint,
  M3 fresh-host write into a non-existent parent dir silently fails,
  L1/L2 Windows probe: empty-value leak, missing cpu/mem/disk facts,
     `net session` admin false-negative on Windows Home.

Run from the plugin root:  python -m pytest tests/ -v
"""
from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_IS_WINDOWS = platform.system() == "Windows"
TRIGGER = {"host": "H", "os": "OS", "arch": "A"}


def _load_plugin():
    """importlib load (the dir name `env-brief` is not importable by name).
    Executing the module top level is side-effect-free except for the
    import-time _ENV_BRIEF_PATH resolution (pure env function, no I/O)."""
    spec = importlib.util.spec_from_file_location(
        "env_brief_under_test", str(_PLUGIN_ROOT / "__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def eb():
    return _load_plugin()


def _stub(eb, monkeypatch, probe):
    monkeypatch.setattr(eb, "_probe_machine", lambda: dict(probe))


# ---------------------------------------------------------------------------
# M2: timezone hint derivation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("merged,snippet", [
    ({"timezone": "+0800"}, "UTC08:00"),
    ({"timezone": "-0330"}, "UTC-03:30"),          # M2: negative sign kept
    ({"timezone": "+08:00"}, "UTC08:00"),
    ({"timezone": "-05:00"}, "UTC-05:00"),
    ({"timezone": "7"}, "UTC07:00"),
    ({"timezone": "", "timezone_ianna": "Asia/Shanghai"}, "Asia/Shanghai"),
    ({"timezone": "-0330", "timezone_ianna": "America/Sao_Paulo"},
     "America/Sao_Paulo"),
])
def test_hint_labels(eb, merged, snippet):
    hint = eb._derive_timezone_hint(merged)
    assert hint, f"expected a hint for {merged}"
    assert snippet in hint, (snippet, hint)


def test_hint_positive_offset_is_signless_fixed_point(eb):
    # Byte-stability: on a UTC+ host the on-disk form stays 'UTC08:00'
    # (no '+'), so upgrading the plugin never rewrites the shared file
    # and the rendered section never drifts (prefix-cache invariant).
    hint = eb._derive_timezone_hint({"timezone": "+0800"})
    assert "UTC+08:00" not in hint
    assert "UTC08:00" in hint


def test_hint_absent_timezone_is_empty(eb):
    assert eb._derive_timezone_hint({}) == ""


# ---------------------------------------------------------------------------
# trigger probe
# ---------------------------------------------------------------------------
def test_trigger_probe_missing_field(eb):
    assert eb._trigger_probe({}) is True


def test_trigger_probe_mismatch(eb, monkeypatch):
    monkeypatch.setattr(eb, "_probe_machine",
                       lambda: {"host": "new", "os": "OS", "arch": "A"})
    assert eb._trigger_probe({"host": "old", "os": "OS", "arch": "A"}) is True


def test_trigger_probe_match(eb, monkeypatch):
    monkeypatch.setattr(eb, "_probe_machine", lambda: dict(TRIGGER))
    assert eb._trigger_probe(dict(TRIGGER)) is False


def test_trigger_probe_accepts_live_without_reprobing(eb, monkeypatch):
    # The hook computes the live probe ONCE and passes it to both the
    # trigger check and the merge (was: up to two full probes per start).
    calls = []
    monkeypatch.setattr(eb, "_probe_machine",
                        lambda: calls.append(1) or dict(TRIGGER))
    assert eb._trigger_probe(dict(TRIGGER), live=dict(TRIGGER)) is False
    assert calls == []


# ---------------------------------------------------------------------------
# merge semantics (two-tier write + generated hint + header)
# ---------------------------------------------------------------------------
def test_merge_tier1_clobbers_trigger_fields(eb, tmp_path, monkeypatch):
    p = tmp_path / "env-brief.yaml"
    p.write_text("host: oldhost\nos: STALE\narch: x86\ncpu_cores: '4'\n",
                 encoding="utf-8")
    _stub(eb, monkeypatch, {**TRIGGER, "cpu_cores": "8"})
    eb._merge_probe_into_yaml(str(p))
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert data["host"] == "H"
    assert data["os"] == "OS"
    assert data["arch"] == "A"


def test_merge_tier2_fill_only_never_clobbers(eb, tmp_path, monkeypatch):
    p = tmp_path / "env-brief.yaml"
    p.write_text("host: H\nos: OS\narch: A\ncpu_cores: '4'\nuser: olduser\n",
                 encoding="utf-8")
    _stub(eb, monkeypatch,
          {**TRIGGER, "cpu_cores": "8", "memory_total": "16Gi", "user": "newuser"})
    eb._merge_probe_into_yaml(str(p))
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert data["cpu_cores"] == "4"          # existing value never clobbered
    assert data["user"] == "olduser"
    assert data["memory_total"] == "16Gi"    # missing value filled


def test_merge_preserves_leading_comment_header(eb, tmp_path, monkeypatch):
    p = tmp_path / "env-brief.yaml"
    p.write_text("# hand header A\n# hand header B\n\nhost: H\nos: OS\narch: A\n",
                 encoding="utf-8")
    _stub(eb, monkeypatch, {**TRIGGER, "os": "OSX"})
    eb._merge_probe_into_yaml(str(p))
    text = p.read_text(encoding="utf-8")
    assert text.startswith("# hand header A\n# hand header B\n")
    assert yaml.safe_load(text)["os"] == "OSX"


def test_merge_creates_missing_parent_dir(eb, tmp_path, monkeypatch):
    # M3: fresh host - the resolved dir may not exist yet.
    p = tmp_path / "a" / "b" / "env-brief.yaml"
    _stub(eb, monkeypatch, TRIGGER)
    eb._merge_probe_into_yaml(str(p))
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert data["host"] == "H"


def test_merge_idempotent_second_run_is_noop(eb, tmp_path, monkeypatch):
    p = tmp_path / "env-brief.yaml"
    probe = {**TRIGGER, "cpu_cores": "8", "timezone": "+0800",
             "timezone_ianna": "Asia/Shanghai", "user": "u"}
    _stub(eb, monkeypatch, probe)
    eb._merge_probe_into_yaml(str(p))
    first = p.read_bytes()
    eb._merge_probe_into_yaml(str(p))
    assert p.read_bytes() == first


def test_merge_hint_regen_when_stale(eb, tmp_path, monkeypatch):
    p = tmp_path / "env-brief.yaml"
    p.write_text('host: H\nos: OS\narch: A\ntimezone: "+0800"\n'
                 "user_timezone_hint: STALE-HINT\n", encoding="utf-8")
    _stub(eb, monkeypatch, {**TRIGGER, "timezone": "+0800"})
    eb._merge_probe_into_yaml(str(p))
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert "STALE" not in data["user_timezone_hint"]
    assert "UTC08:00" in data["user_timezone_hint"]


# ---------------------------------------------------------------------------
# C1/C2: the shared cross-process lock
# ---------------------------------------------------------------------------
def test_lock_path_is_shared_sentinel(eb):
    # C2 regression: the lock target MUST be the shared sibling sentinel,
    # never the per-writer temp file (a private file excludes no one).
    assert eb._lock_path_for("/x/env-brief.yaml").endswith("env-brief.yaml.lock")


def test_shared_lock_roundtrip(eb, tmp_path):
    # C1 regression: on Windows the old path wrote a bytes NUL anchor into
    # a text-mode handle -> hard TypeError that silently disabled every
    # merge write. This exercises the live primitive of the host.
    lock = str(tmp_path / "env-brief.yaml.lock")
    handle = eb._acquire_shared_lock(lock)
    try:
        assert os.path.exists(lock)
        if getattr(eb, "_LOCK_KIND", "none") != "none":
            assert handle is not None
    finally:
        eb._release_shared_lock(handle)


# ---------------------------------------------------------------------------
# L2/L3: Windows probe shape (batched powershell+python, no net session)
# ---------------------------------------------------------------------------
def test_windows_probe_shape(eb, monkeypatch):
    if not _IS_WINDOWS:
        pytest.skip("windows only")
    monkeypatch.delenv("USERNAME", raising=False)
    data = eb._probe_machine_windows()
    for k in ("host", "os", "arch", "shell", "user_is_root",
              "sudo_available", "home_writable"):
        assert str(data.get(k, "")).strip(), f"missing probe field {k}"
    # L1: empty values are dropped so `user: ""` never leaks into the yaml
    assert all(v not in ("", None) for v in data.values())
    assert "user" not in data or str(data["user"]).strip()
    assert data.get("os") == "Windows"


def test_windows_probe_live_smoke(eb):
    if not _IS_WINDOWS:
        pytest.skip("windows only")
    data = eb._probe_machine_windows()
    for k in ("host", "os", "arch"):
        assert str(data.get(k, "")).strip(), k
    # best-effort: tz fact present when powershell or the live python exist
    if shutil.which("powershell") or sys.executable:
        assert data.get("timezone") or data.get("timezone_ianna"), data


# ---------------------------------------------------------------------------
# M1 + multi-OS: POSIX probe expressions must run clean on any host with sh
# (Windows git-bash included) - guards the nested-quote macOS fallback
# chains (sysctl/dscl branches) without needing a real POSIX machine.
# ---------------------------------------------------------------------------
def test_posix_probe_expressions_run_cleanly(eb):
    sh = shutil.which("sh")
    if not sh:
        pytest.skip("no sh on this host")
    for key, cmd in eb._POSIX_PROBES.items():
        out = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=20).stdout.strip()
        if key == "cpu_cores":
            assert out == "" or out.isdigit(), (key, out)
        elif key == "memory_total":
            assert out == "" or out.endswith("Gi"), (key, out)
        elif key == "os":
            assert out in ("Linux", "Darwin"), (key, out)
        elif key == "user_is_root":
            assert out in ("", "true", "false"), (key, out)


# ---------------------------------------------------------------------------
# path resolution (shared-root walk-up + fallbacks)
# ---------------------------------------------------------------------------
def test_resolve_walks_up_to_yaml_root(eb, tmp_path, monkeypatch):
    shared = tmp_path / "hermes"
    (shared / "profiles" / "coder").mkdir(parents=True)
    (shared / "env-brief.yaml").write_text("host: x\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(shared / "profiles" / "coder"))
    assert eb._resolve_env_brief_path() == str(shared / "env-brief.yaml")


def test_resolve_walks_up_on_profiles_dir_alone(eb, tmp_path, monkeypatch):
    (tmp_path / "hermes" / "profiles").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME",
                      str(tmp_path / "hermes" / "profiles" / "coder"))
    assert eb._resolve_env_brief_path() == str(tmp_path / "hermes" / "env-brief.yaml")


def test_resolve_lower_yaml_wins(eb, tmp_path, monkeypatch):
    prof = tmp_path / "hermes" / "profiles" / "coder"
    prof.mkdir(parents=True)
    (prof / "env-brief.yaml").write_text("host: x\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(prof))
    assert eb._resolve_env_brief_path() == str(prof / "env-brief.yaml")


def test_resolve_fallback_home_hermes(eb, tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    fakehome = tmp_path / "home"
    (fakehome / ".hermes").mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE" if _IS_WINDOWS else "HOME", str(fakehome))
    assert eb._resolve_env_brief_path() == str(fakehome / ".hermes" / "env-brief.yaml")


def test_resolve_fallback_appdata_windows(eb, tmp_path, monkeypatch):
    if not _IS_WINDOWS:
        pytest.skip("windows only")
    monkeypatch.delenv("HERMES_HOME", raising=False)
    fakehome = tmp_path / "home"          # exists, but has no .hermes inside
    fakehome.mkdir()
    monkeypatch.setenv("USERPROFILE", str(fakehome))
    fakeapp = tmp_path / "appdata"
    fakeapp.mkdir()
    monkeypatch.setenv("APPDATA", str(fakeapp))
    assert eb._resolve_env_brief_path() == str(fakeapp / "hermes" / "env-brief.yaml")


# ---------------------------------------------------------------------------
# render: pure function of the yaml bytes (prefix-cache invariant)
# ---------------------------------------------------------------------------
def test_render_is_pure_bytes_function(eb, tmp_path, monkeypatch):
    p = tmp_path / "env-brief.yaml"
    p.write_text("host: H\nos: OS\narch: A\ncpu_cores: 8\n", encoding="utf-8")
    monkeypatch.setattr(eb, "_ENV_BRIEF_PATH", str(p))
    a = eb._render_brief()
    b = eb._render_brief()
    assert a == b
    assert a.startswith("## Local Environment Brief")
    assert "- host: H" in a
    assert "- cpu_cores: 8" in a


def test_render_nested_dict(eb, tmp_path, monkeypatch):
    p = tmp_path / "env-brief.yaml"
    p.write_text("net:\n  ip: 10.0.0.2\n  vlan: 42\ncpu_cores: 8\n",
                 encoding="utf-8")
    monkeypatch.setattr(eb, "_ENV_BRIEF_PATH", str(p))
    assert "- net: ip=10.0.0.2 / vlan=42" in eb._render_brief()


def test_render_missing_file_is_placeholder(eb, tmp_path, monkeypatch):
    monkeypatch.setattr(eb, "_ENV_BRIEF_PATH", str(tmp_path / "nope.yaml"))
    assert "host facts unavailable" in eb._render_brief()


def test_live_data_file_renders_no_placeholder(eb):
    if not os.path.exists(eb._ENV_BRIEF_PATH):
        pytest.skip("no live data file on this host")
    assert "host facts unavailable" not in eb._render_brief()
