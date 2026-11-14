"""env-brief: system-level host-facts section in the session system prompt.

Owner rulings (2026-11-08):
- data is SYSTEM-LEVEL (one file, every profile reads the same bytes) ->
  absolute path, never resolved through HERMES_HOME (which is per-profile
  under a multiplexed gateway);
- exact time/date stay in tools, not in the prompt: the native
  "Conversation started:" line is frozen by design ("cache stability, not
  live-clock-keeping"), so we add a one-line pointer instead of a live clock;
- prefix-cache safety: render is a pure function of the yaml bytes, so the
  section is byte-stable across a session; at compact the core re-renders
  (invalidate_system_prompt) but re-renders are verbatim-identical while the
  file is unchanged -> zero prompt drift, edited file hot-applies at the
  next compact boundary without a gateway restart.
"""
from __future__ import annotations

import logging
import os as _os
import platform as _platform

logger = logging.getLogger("hermes_plugins.env_brief")

# Data-file location: platform-adaptive, resolved ONCE at import (system-level,
# one shared file every profile reads). Never the per-profile HERMES_HOME.
#
# Owner ruling 2026-11-08 (system-level, one shared file) + 2026-11-14
# (platform auto-adapt, this change): the data file is NOT under the
# per-profile HERMES_HOME (which differs across profiles under a multiplexed
# gateway). It lives under the SHARED HERMES root — the parent of the
# profiles/ dir, or the dir that already holds env-brief.yaml. Resolution:
#
#   1. $HERMES_HOME is the LIVE profile home. Walk UP from it to the first
#      ancestor that either (a) contains env-brief.yaml, or (b) contains a
#      `profiles/` subdir (the shared root of a multiplexed install). That
#      ancestor is the shared root; the data file is <shared_root>/env-brief.yaml.
#      Every profile under the same gateway resolves to the SAME shared root
#      (they all sit under the same profiles/ dir), so the file is truly
#      shared across profiles.
#   2. No HERMES_HOME / walk finds nothing -> ~/.hermes/env-brief.yaml
#      (Path.home()/".hermes"; Windows: USERPROFILE).
#   3. Windows-only extra fallback: $APPDATA/hermes/env-brief.yaml (the dir
#      the old hardcoded C:\Users\user\AppData\Local\hermes pointed at).
#
# _merge_probe_into_yaml creates the dir on demand (mkstemp dir=dirname), so a
# fresh host writes into whichever dir resolution lands on; the next session
# reads the same dir (the resolution is a pure function of the env, stable
# across processes).
_IS_WINDOWS = _platform.system() == "Windows"


def _shared_root_for_profile_home(profile_home: str):
    """Walk up from a profile home to the shared HERMES root. Returns the
    first ancestor that holds env-brief.yaml OR a profiles/ subdir, or None
    when neither is found (the caller falls back to ~/.hermes)."""
    import pathlib
    p = pathlib.Path(profile_home)
    for _ in range(8):  # bounded: a path is never more than ~8 levels deep
        if (p / "env-brief.yaml").exists():
            return p
        if (p / "profiles").is_dir():
            return p
        parent = p.parent
        if parent == p:  # reached the filesystem root
            return None
        p = parent
    return None


def _resolve_env_brief_path() -> str:
    import pathlib
    home_hermes = _os.environ.get("HERMES_HOME", "").strip()
    # 1. shared root of the live profile home (walk-up), if one is found
    if home_hermes:
        shared = _shared_root_for_profile_home(home_hermes)
        if shared is not None:
            return str(shared / "env-brief.yaml")
    # 2. ~/.hermes
    candidates = [pathlib.Path.home() / ".hermes" / "env-brief.yaml"]
    # 3. Windows APPDATA fallback
    if _IS_WINDOWS:
        appdata = _os.environ.get("APPDATA", "").strip()
        if appdata:
            candidates.append(pathlib.Path(appdata) / "hermes" / "env-brief.yaml")
    for c in candidates:
        if c.parent.exists():
            return str(c)
    return str(candidates[0])


_ENV_BRIEF_PATH = _resolve_env_brief_path()

_PLACEHOLDER = (
    "## Local Environment Brief\n"
    f"(no {_ENV_BRIEF_PATH} found - host facts unavailable; "
    "query tools for live system details)"
)

_CLOCK_NOTE = (
    "For the current date/time, query tools (e.g. `date`); "
    "the system-prompt timestamp line is frozen at session start by design."
)


def _render_brief(session_info=None) -> str:
    """Pure function of the yaml bytes; any failure degrades to a placeholder
    (never raises into the prompt build - the native fail-open contract)."""
    try:
        import yaml  # pyyaml ships with the hermes runtime (config.yaml parser)
        with open(_ENV_BRIEF_PATH, encoding="utf-8-sig") as f:
            data = yaml.safe_load(f)
    except Exception as exc:  # missing file / bad yaml / no pyyaml
        logger.warning("env-brief render fell back to placeholder: %s", exc)
        return _PLACEHOLDER
    if not isinstance(data, dict) or not data:
        return _PLACEHOLDER

    lines = ["## Local Environment Brief (system-level; re-read at compact if the file changed)"]
    for key, value in data.items():
        if isinstance(value, dict):
            subs = " / ".join(f"{k}={v}" for k, v in value.items() if v is not None)
            lines.append(f"- {key}: {subs}" if subs else f"- {key}")
        elif isinstance(value, (list, tuple)):
            lines.append(f"- {key}: {', '.join(str(x) for x in value)}")
        elif value is not None:
            lines.append(f"- {key}: {value}")
    lines.append(_CLOCK_NOTE)
    return "\n".join(lines)


def _env_time(_args) -> dict:
    """Real system clock, on demand. Call when the exact current time is
    needed; never guess it from the frozen session-start line."""
    from datetime import datetime

    now = datetime.now().astimezone()
    return {
        "now": now.strftime("%Y-%m-%d %H:%M:%S"),
        "timezone": str(now.tzinfo),
        "iso": now.isoformat(),
    }


# ---------------------------------------------------------------------------
# Machine-identity probe + merge-back (owner rulings 2026-11-08..12):
# - plugin scope is machine/OS/hardware/permissions, NOT toolchain (no python
#   path/version fields; that layer was explicitly ruled out);
# - trigger signal = host + os + arch, ALL must be present AND match the live
#   machine; a missing/empty field, or a value that belongs to a DIFFERENT
#   machine (stale file copied across hosts), triggers a re-probe;
# - owner ruling (2026-11-12, "don't allow manual override on these 3"): the
#   3 trigger fields are ALWAYS overwritten with the live probe value when it
#   differs - no hand-written value survives a mismatch. Every OTHER probed
#   field (cpu/memory/disk/permissions etc.) only fills in when missing/empty
#   and never clobbers an existing value.
# ---------------------------------------------------------------------------
import os as _os
import platform as _platform
import subprocess
import tempfile

_TRIGGER_FIELDS = ("host", "os", "arch")

def _probe_machine_linux() -> dict:
    """POSIX probes (Linux/macOS): one subprocess per key, each fails open to
    a missing key (never a raise into the hook)."""
    import subprocess
    probes = {
        "host": ["hostname"],
        "os": ["uname", "-sr"],
        "arch": ["uname", "-m"],
        "cpu_cores": ["nproc"],
        "memory_total": ["sh", "-c", "free -h | awk '/^Mem:/{print $2}'"],
        "disk_root_avail": ["sh", "-c", "df -h / | awk 'NR==2{print $4}'"],
        "shell": ["sh", "-c", "getent passwd \"$(whoami)\" | cut -d: -f7"],
        "user": ["whoami"],
        "timezone": ["sh", "-c", "date +%z"],
        "timezone_ianna": ["sh", "-c", "readlink -f /etc/localtime 2>/dev/null | sed 's|.*/zoneinfo/||' || cat /etc/timezone 2>/dev/null"],
        "user_is_root": ["sh", "-c", "test $(id -u) -eq 0 && echo true || echo false"],
        "sudo_available": ["sh", "-c", "command -v sudo >/dev/null 2>&1 && echo true || echo false"],
        "home_writable": ["sh", "-c", "test -w $(printenv HOME || echo /root) && echo true || echo false"],
    }
    data: dict = {}
    for key, cmd in probes.items():
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            val = out.stdout.strip()
            if val:
                data[key] = val
        except Exception:
            pass
    try:
        out = subprocess.run(
            ["sh", "-c", "grep -oE 'container=.*' /run/.containerenv 2>/dev/null | head -1 | cut -d= -f2"],
            capture_output=True, text=True, timeout=5)
        if out.stdout.strip():
            data["container"] = out.stdout.strip()
    except Exception:
        pass
    return data


def _probe_machine_windows() -> dict:
    """Windows probes (the discarded 2bc7d36 'Windows compatibility' branch,
    generalized — no hardcoded C:\\Users\\user). host/os/arch are always probed
    (os from platform, arch from PROCESSOR_ARCHITECTURE / platform.machine);
    the trigger fields are guaranteed present on either OS so
    _trigger_probe works identically."""
    import socket
    data: dict = {}
    data["host"] = socket.gethostname()
    data["os"] = _platform.system()  # "Windows"
    data["arch"] = _os.environ.get("PROCESSOR_ARCHITECTURE") or _platform.machine()
    data["user"] = _os.environ.get("USERNAME", "")
    # shell: cmd.exe is the Windows shell equivalent
    data["shell"] = "cmd.exe"
    # timezone: wmic is deprecated on modern Win10/11; use the registry via
    # PowerShell only when available, else fall back to the tz offset via
    # `date` (cmd has no `date +%z`).
    try:
        import subprocess
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-TimeZone).Id"],
            capture_output=True, text=True, timeout=10)
        if out.stdout.strip():
            data["timezone_ianna"] = out.stdout.strip()
    except Exception:
        pass
    try:
        import subprocess
        out = subprocess.run(
            ["python", "-c",
             "import datetime; print(datetime.datetime.now().astimezone().strftime('%z'))"],
            capture_output=True, text=True, timeout=10)
        val = out.stdout.strip()
        if val:
            data["timezone"] = val
    except Exception:
        pass
    # user_is_root / sudo_available: not a meaningful distinction on Windows
    # (no sudo, admin vs non-admin is the analogue). Probe admin via
    # `net session` (errors when not admin) — fail-soft to "false".
    try:
        import subprocess
        out = subprocess.run(
            ["net", "session"],
            capture_output=True, text=True, timeout=5)
        data["user_is_root"] = "true" if out.returncode == 0 else "false"
    except Exception:
        data["user_is_root"] = "false"
    data["sudo_available"] = "false"  # no sudo on Windows
    # home_writable: USERPROFILE is the Windows home
    home = _os.environ.get("USERPROFILE", "")
    data["home_writable"] = "true" if home and _os.path.isdir(home) else "false"
    return data


def _probe_machine() -> dict:
    """OS-dispatched probe: the single public entrypoint both _trigger_probe
    and _merge_probe_into_yaml call. Platform branch is chosen at call time
    (not import) so a Windows host running the Linux-built plugin still gets
    the Windows probes (and vice-versa if the binary were cross-compiled)."""
    if _IS_WINDOWS:
        return _probe_machine_windows()
    return _probe_machine_linux()


def _trigger_probe(existing: dict) -> bool:
    """True if a re-probe is warranted: any trigger field missing/empty, or
    present but not matching the live machine (covers the 'stale yaml copied
    across hosts' case, not just 'field deleted by hand')."""
    for k in _TRIGGER_FIELDS:
        if not existing.get(k):
            return True
    live = _probe_machine()
    return any(existing.get(k) != live.get(k) for k in _TRIGGER_FIELDS)


def _derive_timezone_hint(merged: dict) -> str:
    """user_timezone_hint is generated code-side from probed values, never
    hand-typed: offset comes from the 'timezone' probe (+0800), the IANA name
    from 'timezone_ianna' (Asia/Kuala_Lumpur) - no guessing between the many
    zones that share a UTC+8 offset."""
    offset = merged.get("timezone", "")
    ianna = merged.get("timezone_ianna", "")
    if not offset and not ianna:
        return ""
    offset = offset.strip()
    if len(offset) == 5 and offset[1:2].isdigit():
        offset_hhmm = f"{offset[1:3]}:{offset[3:5]}"
    else:
        offset_hhmm = ""
    label = ianna or (f"UTC{offset_hhmm}" if offset_hhmm else "unknown timezone")
    return (
        f"用户所在时区是 {label}"
        + (f"（UTC{offset_hhmm}）" if offset_hhmm else "")
        + "；任何涉及当前日期/时间的回复都必须用这个时区，不要用 UTC 或模型训练的默认时区去猜。"
    )


_FIXED_HEADER = (
    "# System-level host facts (one file, shared by every profile under the\n"
    "# multiplexed gateway). Read by the env-brief plugin into each session's\n"
    "# system prompt; takes effect at session start, and hot-applies at the next\n"
    "# compact boundary (pure render of these bytes -> no prefix-cache drift)."
)


def _leading_comment_block(path: str) -> str:
    """Leading pure-comment/blank lines of the file, verbatim. yaml.safe_dump
    drops YAML comments (they are not data) on any rewrite, so this is the
    only way to keep hand-written header notes alive across a merge. Returns
    the fixed header when the file does not yet exist (e.g. first-run delete
    + reprobe flow, owner ruling 2026-11-12: no manual fields at all)."""
    if not _os.path.exists(path):
        return _FIXED_HEADER
    with open(path, encoding="utf-8-sig") as f:
        lines = f.readlines()
    block: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            block.append(line.rstrip("\n"))
        else:
            break
    return "\n".join(block) if block else _FIXED_HEADER


def _merge_probe_into_yaml(path: str) -> None:
    """Two-tier write (owner ruling 2026-11-12: trigger fields have NO manual
    override - live value wins; everything else is fill-only):
    - host/os/arch: ALWAYS overwrite with the live probe value when it differs.
    - all other probed fields: fill only when missing/empty, never clobber.
    - the file's leading comment/blank block is preserved verbatim across the
      rewrite (yaml.safe_dump alone would drop it - see
      ``_leading_comment_block``).
    Atomic write (temp file + os.replace) under an exclusive lock so two
    multiplexed gateway processes racing the same session start cannot
    interleave a half-written file. Lock primitive is platform-adaptive:
    fcntl.flock on POSIX, msvcrt.locking on Windows (the same dual-primitive
    pattern the code_map plugin uses for its cross-process locks)."""
    import yaml

    header = _leading_comment_block(path)

    existing: dict = {}
    if _os.path.exists(path):
        try:
            with open(path, encoding="utf-8-sig") as f:
                loaded = yaml.safe_load(f)
            if isinstance(loaded, dict):
                existing = loaded
        except Exception:
            existing = {}  # unreadable/corrupt file: rebuild from probe, keep nothing

    probe = _probe_machine()
    merged = dict(existing)
    changed = False

    # Tier 1: trigger fields - live value always wins when it differs
    for k in _TRIGGER_FIELDS:
        if k in probe and merged.get(k) != probe[k]:
            merged[k] = probe[k]
            changed = True

    # Tier 2: everything else - fill only if missing/empty
    for k, v in probe.items():
        if k in _TRIGGER_FIELDS:
            continue
        if not merged.get(k):
            merged[k] = v
            changed = True

    # Generated field (owner ruling 2026-11-12: no hand-written fields at all):
    # user_timezone_hint is derived from the two probed timezone fields,
    # regenerated whenever either changes so it can never go stale vs the
    # machine it describes.
    hint = _derive_timezone_hint(merged)
    if hint and merged.get("user_timezone_hint") != hint:
        merged["user_timezone_hint"] = hint
        changed = True

    if not changed:
        return

    fd, tmp = tempfile.mkstemp(dir=_os.path.dirname(path) or ".", suffix=".yaml.tmp")
    try:
        with _os.fdopen(fd, "w") as f:
            _exclusive_lock(f)
            dumped = yaml.safe_dump(merged, allow_unicode=True, sort_keys=False)
            if header:
                f.write(header + "\n\n")
            f.write(dumped)
            _exclusive_unlock(f)  # msvcrt requires explicit unlock; no-op on POSIX
        _os.replace(tmp, path)
    except BaseException:
        if _os.path.exists(tmp):
            _os.remove(tmp)
        raise


def _exclusive_lock(f) -> None:
    """Platform-adaptive exclusive lock on an open file handle: fcntl.flock on
    POSIX, msvcrt.locking on Windows. The lock is held for the duration of
    the with-block; released by the caller's fd close (flock is released on
    close; msvcrt.locking is released explicitly in the same with-block via
    the paired _exclusive_unlock). Fail-soft: if neither primitive is
    importable (exotic host), degrade to a no-op with a warning (the content
    merge gate remains the backstop, same discipline as code_map's
    _shard_lock)."""
    global _LOCK_KIND
    if _LOCK_KIND is None:
        try:
            import fcntl
            _LOCK_KIND = "fcntl"
        except Exception:
            try:
                import msvcrt  # noqa: F401
                _LOCK_KIND = "msvcrt"
            except Exception:
                _LOCK_KIND = "none"
                logger.warning("env-brief: no cross-process lock primitive "
                               "(fcntl/msvcrt) - relying on the in-process "
                               "merge gate only")
    if _LOCK_KIND == "fcntl":
        import fcntl
        fcntl.flock(f, fcntl.LOCK_EX)
    elif _LOCK_KIND == "msvcrt":
        import msvcrt
        # Windows locking() reserves a byte-range; reserve byte 0 on an empty
        # temp file (the file is 0 bytes at mkstemp time, so byte 0 is a
        # valid 1-byte reservation that no other writer can hold).
        f.seek(0)
        # Ensure at least 1 byte exists so the lock has a range to reserve.
        # The temp file is written AFTER the lock is acquired (the dump below),
        # so at this point it is still 0 bytes; write a single NUL as the
        # lock anchor, then truncate back to 0 before the real dump so the
        # final file has no stray NUL.
        f.write(b"\0")
        f.flush()
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)


def _exclusive_unlock(f) -> None:
    """Release the platform-adaptive lock taken by _exclusive_lock. msvcrt
    requires an explicit unlock (LK_UNLCK) on the same byte-range; fcntl
    releases on close so this is a no-op on POSIX."""
    if _LOCK_KIND == "msvcrt":
        import msvcrt
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:
            pass  # already released / fd closed


_LOCK_KIND: str | None = None


_ENV_TIME_SCHEMA = {
    "type": "object",
    "properties": {},
    "description": (
        "No arguments. Returns the host's real current date/time and timezone "
        "(system clock). Use when the exact current time is required; do not "
        "infer it from the frozen session-start timestamp."
    ),
}


def register(ctx) -> None:
    ctx.register_system_prompt_section("env-brief", _render_brief, position="after_memory")
    logger.info("env-brief section registered (source=%s, os=%s)", _ENV_BRIEF_PATH, _platform.system())

    # On-demand live clock (owner ruling B, 2026-11-08): a fresh tool name, so
    # no built-in shadowing -> no tools.override gate; the static
    # user_timezone_hint fact stays in the frozen system section above, this
    # tool is only paid when the model actually needs the precise moment.
    handle = ctx.register_tool(
        name="env_time",
        toolset="env_brief",
        schema=_ENV_TIME_SCHEMA,
        handler=_env_time,
        description="Get the host's current date/time/timezone from the real system clock.",
    )
    logger.info("env-brief tool registered (env_time): %s", handle is not None)

    # Session-start heartbeat so the on_session_start capability we declare in
    # plugin.yaml is real (genuine, not borrowed). This also stamps the
    # gateway log on the next session start with the data file the brief
    # was rendered from, so operators can tell at a glance whether a
    # session got the real brief or the placeholder.
    def _on_session_start(**_kwargs):
        try:
            logger.info(
                "env-brief on_session_start: source=%s", _ENV_BRIEF_PATH
            )
            import yaml

            existing: dict = {}
            if _os.path.exists(_ENV_BRIEF_PATH):
                with open(_ENV_BRIEF_PATH, encoding="utf-8-sig") as f:
                    loaded = yaml.safe_load(f)
                if isinstance(loaded, dict):
                    existing = loaded
            if _trigger_probe(existing):
                _merge_probe_into_yaml(_ENV_BRIEF_PATH)
                logger.info("env-brief on_session_start: probe+merge updated %s", _ENV_BRIEF_PATH)
        except Exception:
            pass

    ctx.register_hook("on_session_start", _on_session_start)
