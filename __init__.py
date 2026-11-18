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
- Multi-OS (Windows/macOS/Linux) probes + shared-sentinel cross-process
  lock, 2026-11-18 hardening (defect report: C1 text-mode NUL-anchor
  TypeError, C2 private-temp-file lock excluded nobody, M1 uname -sr
  kernel churn, M2 negative-UTC sign loss, M3 missing parent dir).
"""
from __future__ import annotations

import logging
import os as _os
import platform as _platform
import subprocess
import sys
import tempfile

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
# _merge_probe_into_yaml makedirs the dir on demand before the atomic
# mkstemp, so a fresh host writes into whichever dir resolution lands on;
# the next session reads the same dir (the resolution is a pure function of
# the env, stable across processes).
_IS_WINDOWS = _platform.system() == "Windows"


def _shared_root_for_profile_home(profile_home: str):
    """Walk up from a profile home to the shared HERMES root. Returns the
    first ancestor that holds env-brief.yaml OR a profiles/ subdir, or None
    when neither is found (the caller falls back to ~/.hermes)."""
    import pathlib
    p = pathlib.Path(profile_home)
    for _ in range(12):  # bounded: any supported install layout is <= ~12
        # levels deep (F4, rescan 2026-11-18: the old range(8) gave up on
        # realistic 9-11 level layouts and silently fell back to
        # ~/.hermes - a DIFFERENT file than the operator intended);
        if (p / "env-brief.yaml").exists():
            return p
        if (p / "profiles").is_dir():
            return p
        parent = p.parent
        if parent == p:  # reached the filesystem root
            return None
        p = parent
    return None


def _shadowed_by_multiplex_root(root) -> bool:
    """Owner ruling 2026-11-08 'one file per machine' audit (rescan P2,
    2026-11-18): TRUE when the resolved root was found by the LOCAL
    env-brief.yaml check (no profiles/ dir at its own level) while a
    HIGHER ancestor holds a profiles/ dir - i.e. a legacy <=1.0.x
    profile-local data file is shadowing the multiplexed shared root,
    which silently breaks the single-file invariant (every profile reads
    its own copy). Behavior is deliberately UNCHANGED (stability > drift);
    the caller only logs. Walked back within the same 12-level bound."""
    if (root / "profiles").is_dir():
        return False  # the root itself is the shared root - no shadow
    p = root.parent
    for _ in range(12):
        if (p / "profiles").is_dir():
            return True
        if p.parent == p:
            return False
        p = p.parent
    return False


def _resolve_env_brief_path() -> str:
    import pathlib
    home_hermes = _os.environ.get("HERMES_HOME", "").strip()
    # 1. shared root of the live profile home (walk-up), if one is found
    if home_hermes:
        shared = _shared_root_for_profile_home(home_hermes)
        if shared is not None:
            if _shadowed_by_multiplex_root(shared):
                # A legacy profile-local data file (the <=1.0.x installers
                # wrote into HERMES_HOME) is shadowing the multiplexed
                # shared root. Do NOT switch files (stability > drift -
                # the switch would drift the rendered section at the next
                # compact); make the invariant break observable instead.
                logger.warning(
                    "env-brief: resolved data file %s sits in a profile-local "
                    "dir that SHADOWS the multiplexed shared root above it "
                    "(which holds profiles/). One machine should have one "
                    "shared file - delete the legacy copy to restore the "
                    "single-file invariant.",
                    shared / "env-brief.yaml")
            return str(shared / "env-brief.yaml")
        # F4 (rescan 2026-11-18): the walk-up bound (12 levels) was reached
        # without finding env-brief.yaml or a profiles/ dir - fall through
        # to the ~/.hermes / APPDATA fallbacks, but make it observable: a
        # silent fallback to a DIFFERENT file than HERMES_HOME implies is
        # exactly how the old range(8) misbehaved.
        logger.warning(
            "env-brief: walk-up from HERMES_HOME=%s found no shared root "
            "within 12 levels; falling back to the default location",
            home_hermes)
    # 2. ~/.hermes, when that dir already exists (the normal case under a
    #    live hermes runtime, which creates HERMES_HOME at boot).
    home_dir = pathlib.Path.home() / ".hermes"
    if home_dir.exists():
        return str(home_dir / "env-brief.yaml")
    # 3. Windows APPDATA fallback (%APPDATA%/hermes - the dir the old
    #    hardcoded path pointed at), even if the `hermes` subdir does not
    #    exist yet: _merge_probe_into_yaml makedirs it on the first write.
    if _IS_WINDOWS:
        appdata = _os.environ.get("APPDATA", "").strip()
        if appdata:
            return str(pathlib.Path(appdata) / "hermes" / "env-brief.yaml")
    return str(home_dir / "env-brief.yaml")


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
_TRIGGER_FIELDS = ("host", "os", "arch")

# The POSIX probe table is a module-level constant (was local to the
# function) so the cross-platform test suite can run the EXACT expressions
# on any host that has sh - including Windows git-bash - guarding the
# nested-quote macOS fallback chains without needing a POSIX machine.
_POSIX_PROBES: dict = {
    "host": ["hostname"],
    # M1: stable OS-family name, NEVER the kernel release. `uname -sr`
    # churned the trigger field on every kernel upgrade (Tier-1 then
    # rewrote the shared file) and made the rendered 'os' fact
    # version-specific; the Windows probe's stable family name
    # (platform.system() -> "Windows") was the reference behaviour.
    "os": ["uname", "-s"],
    "arch": ["uname", "-m"],
    # macOS has no nproc/free/getent: GNU-first chains with BSD fallbacks
    # (sysctl hw.ncpu / hw.memsize, dscl for the login shell).
    "cpu_cores": ["sh", "-c",
                  "nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null "
                  "|| getconf _NPROCESSORS_ONLN 2>/dev/null"],
    "memory_total": ["sh", "-c",
                     "v=$(free -h 2>/dev/null | awk '/^Mem:/{print $2}'); "
                     "[ -n \"$v\" ] && echo \"$v\" || { b=$(sysctl -n hw.memsize "
                     "2>/dev/null); [ -n \"$b\" ] && echo \"$((b/1073741824))Gi\"; }"],
    "disk_root_avail": ["sh", "-c", "df -h / | awk 'NR==2{print $4}'"],
    "shell": ["sh", "-c",
              "out=$(getent passwd \"$(whoami)\" 2>/dev/null | cut -d: -f7); "
              "[ -n \"$out\" ] || out=$(dscl . -read \"/Users/$(whoami)\" UserShell "
              "2>/dev/null | awk 'NR==1{print $2}'); [ -n \"$out\" ] && echo \"$out\""],
    "user": ["whoami"],
    "timezone": ["sh", "-c", "date +%z"],
    "timezone_ianna": ["sh", "-c",
                       "t=$(readlink -f /etc/localtime 2>/dev/null "
                       "| sed 's|.*/zoneinfo/||' | grep -v '^/'); "
                       "[ -n \"$t\" ] || t=$(readlink /etc/localtime 2>/dev/null "
                       "| sed 's|.*/zoneinfo/||' | grep -v '^/'); "
                       "[ -n \"$t\" ] || t=$(cat /etc/timezone 2>/dev/null); "
                       "[ -n \"$t\" ] && echo \"$t\""],
    "user_is_root": ["sh", "-c", "test $(id -u) -eq 0 && echo true || echo false"],
    "sudo_available": ["sh", "-c", "command -v sudo >/dev/null 2>&1 && echo true || echo false"],
    "home_writable": ["sh", "-c", "test -w $(printenv HOME || echo /root) && echo true || echo false"],
}


def _run_probe_cmd(key: str, cmd, timeout: int = 5):
    """One probe in the pool: run `cmd` with a per-call timeout; returns
    (key, value-or-'') — never raises (a failed probe just drops its key,
    fail-open)."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        val = out.stdout.strip()
    except Exception:
        val = ""
    return key, val


_CONTAINER_PROBE_CMD = ["sh", "-c",
                        "grep -oE 'container=.*' /run/.containerenv 2>/dev/null "
                        "| head -1 | cut -d= -f2"]


def _probe_machine_posix() -> dict:
    """POSIX probes (Linux + macOS), run CONCURRENTLY (thread pool -
    subprocess releases the GIL, so this is real parallelism) under a hard
    20s wall-clock cap: the rescan (2026-11-18, P2-2) showed the serial
    version could stall session start up to ~135s on a wedged host (13
    probes x 10s timeout). Parallel + 5s per-call timeout + 20s deadline
    bounds the worst case at ~20s; probes unfinished at the cap are
    dropped fail-open (missing key -> merge fills from the file or skips;
    no crash, no rewrite). macOS fallbacks: cpu via sysctl/getconf,
    memory via hw.memsize, login shell via dscl (getent/nproc/free are
    GNU-only); the IANA zone falls back readlink -f -> readlink ->
    /etc/timezone across distros."""
    import concurrent.futures
    data: dict = {}
    probes = dict(_POSIX_PROBES)
    probes["container"] = _CONTAINER_PROBE_CMD
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(probes)) as pool:
        futs = [pool.submit(_run_probe_cmd, k, cmd, 5) for k, cmd in probes.items()]
        try:
            for fut in concurrent.futures.as_completed(futs, timeout=20):
                key, val = fut.result()
                if val:
                    data[key] = val
        except concurrent.futures.TimeoutError:
            logger.warning(
                "env-brief: POSIX probe wall-clock cap (20s) hit; unfinished "
                "probes dropped fail-open")
    return data


# Windows tz id -> IANA name (compact, verified entries; extend lazily).
# Rescan P2-3 (2026-11-18): powershell reports a Windows tz id while a
# python probe on the same field would report a different non-IANA name -
# two values for one field. The map unifies them at NEW-fill time: fill-
# only means an existing file value is NEVER clobbered, and unmapped ids
# keep the OS-native name (the hint layer renders either fine). Verified
# on this host 2026-11-18: both ids are +0800 zones and zoneinfo accepts
# the mapped IANA names.
_WIN_TZ_TO_IANA = {
    "Singapore Standard Time": "Asia/Singapore",
    "Malay Peninsula Standard Time": "Asia/Kuala_Lumpur",
}


def _probe_machine_windows() -> dict:
    """Windows probes (the discarded 2bc7d36 'Windows compatibility' branch,
    generalized — no hardcoded C:\\Users\\user). host/os/arch are always
    probed (os from platform, arch from PROCESSOR_ARCHITECTURE /
    platform.machine), so the trigger fields are guaranteed present on
    every OS and _trigger_probe works identically.

    Multi-OS hardening (2026-11-18), batched to TWO subprocesses total
    (was up to four):
      - one powershell call: admin check + IANA timezone. The admin check
        uses WindowsPrincipal.IsInRole(Administrators) - the old
        `net session` check mis-reported non-admin on Windows Home, where
        the LanmanServer service is absent, and cost an extra ~5s
        subprocess every session start.
      - one python call via sys.executable (the LIVE interpreter, not a
        PATH guess that could resolve to a different one): UTC offset +
        cpu cores + free disk on the system drive + total memory. The old
        code probed NONE of cpu/mem/disk on Windows, so a fresh host was
        missing those facts (only files seeded by an earlier revision kept
        them, by fill-only luck).
    Empty values are dropped so `user: ""` never leaks into the yaml
    (L1: the old code stored the USERNAME env var unconditionally)."""
    import socket
    data: dict = {}
    data["host"] = socket.gethostname()
    data["os"] = _platform.system()  # "Windows" - stable family name
    data["arch"] = _os.environ.get("PROCESSOR_ARCHITECTURE") or _platform.machine()
    user = _os.environ.get("USERNAME", "").strip()
    if user:
        data["user"] = user
    # The OS's default shell: COMSPEC is exactly that. The hermes terminal
    # may run git-bash ON TOP of it - that is a runtime fact, not a host
    # fact, so it is intentionally not probed.
    data["shell"] = _os.environ.get("COMSPEC", "").strip() or "cmd.exe"
    data["sudo_available"] = "false"  # no sudo on Windows
    # home_writable: USERPROFILE is the Windows home. Existence/isdir is
    # NOT proof of writability (rescan P3) - use access() to match the
    # POSIX probe's `test -w "$HOME"` semantics.
    home = _os.environ.get("USERPROFILE", "").strip()
    data["home_writable"] = "true" if (home and _os.access(home, _os.W_OK)) else "false"

    # One powershell call -> "False|Malay Peninsula Standard Time"
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "'{0}|{1}' -f [bool]((New-Object Security.Principal.WindowsPrincipal "
             "([Security.Principal.WindowsIdentity]::GetCurrent())).IsInRole("
             "[Security.Principal.WindowsBuiltInRole]::Administrator)), "
             "(Get-TimeZone).Id"],
            capture_output=True, text=True, timeout=15)
        left, _, right = out.stdout.partition("|")
        data["user_is_root"] = "true" if left.strip().lower() == "true" else "false"
        if right.strip():
            # Rescan P2-3: unify a known Windows tz id to its IANA name at
            # new-fill time (fill-only: an existing file value is kept);
            # an unmapped id keeps the native name.
            data["timezone_ianna"] = _WIN_TZ_TO_IANA.get(right.strip(), right.strip())
    except Exception:
        data["user_is_root"] = "false"  # fail-soft: non-admin is the safe default

    # One python call (live interpreter) -> 4 lines:
    # 0: UTC offset (+0800), 1: cpu cores, 2: free disk on the system
    # drive, 3: total memory (GlobalMemoryStatusEx, bytes -> Gi).
    probe_code = (
        "import datetime, os, shutil\n"
        "print(datetime.datetime.now().astimezone().strftime('%z'))\n"
        "print(os.cpu_count() or '')\n"
        "try:\n"
        "    u = shutil.disk_usage(os.environ.get('SystemDrive') or 'C:')\n"
        "    print('%dGi' % (u.free // 1073741824))\n"
        "except Exception:\n"
        "    print('')\n"
        "try:\n"
        "    import ctypes\n"
        "    class _M(ctypes.Structure):\n"
        "        _fields_ = [('L', ctypes.c_ulong), ('ML', ctypes.c_ulong), "
        "('TP', ctypes.c_ulonglong), ('AP', ctypes.c_ulonglong), "
        "('TPF', ctypes.c_ulonglong), ('APF', ctypes.c_ulonglong), "
        "('TV', ctypes.c_ulonglong), ('AV', ctypes.c_ulonglong), "
        "('AEV', ctypes.c_ulonglong)]\n"
        "    m = _M()\n"
        "    m.L = ctypes.sizeof(_M)\n"
        "    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))\n"
        "    print('%dGi' % (m.TP // 1073741824))\n"
        "except Exception:\n"
        "    print('')\n"
    )
    try:
        out = subprocess.run(
            [sys.executable or "python", "-c", probe_code],
            capture_output=True, text=True, timeout=15)
        lines = [ln.strip() for ln in out.stdout.splitlines()]
        if lines and lines[0]:
            data["timezone"] = lines[0]
        if len(lines) > 1 and lines[1]:
            data["cpu_cores"] = lines[1]
        if len(lines) > 2 and lines[2]:
            data["disk_root_avail"] = lines[2]
        if len(lines) > 3 and lines[3]:
            data["memory_total"] = lines[3]
    except Exception as e:
        # Fail-open: tz/cpu/disk/mem simply don't get filled this session
        # (the powershell probe already covered user + ianna); log it so a
        # wedged interpreter is observable instead of a silent 4-fact gap.
        logger.warning(
            "env-brief: live-python probe failed (%s) - tz/cpu/disk/mem "
            "facts not filled this session", e)
    # L1: drop empty values - `user: ""` must never leak into the yaml.
    return {k: v for k, v in data.items() if v not in ("", None)}


def _probe_machine() -> dict:
    """OS-dispatched probe: the single public entrypoint both _trigger_probe
    and _merge_probe_into_yaml call. Platform branch is chosen at call time
    (not import) so a Windows host running the Linux-built plugin still gets
    the Windows probes (and vice-versa if the binary were cross-compiled)."""
    if _IS_WINDOWS:
        return _probe_machine_windows()
    return _probe_machine_posix()


def _trigger_probe(existing: dict, live: dict | None = None) -> bool:
    """True if a re-probe is warranted: any trigger field missing/empty, or
    present but not matching the live machine (covers the 'stale yaml copied
    across hosts' case, not just 'field deleted by hand'). `live` may be
    supplied by the caller so the session-start hook pays for ONE probe,
    not two (the old code probed once in the trigger check and again in
    the merge)."""
    for k in _TRIGGER_FIELDS:
        if not existing.get(k):
            return True
    if live is None:
        live = _probe_machine()
    return any(existing.get(k) != live.get(k) for k in _TRIGGER_FIELDS)


def _derive_timezone_hint(merged: dict) -> str:
    """user_timezone_hint is generated code-side from probed values, never
    hand-typed: offset comes from the 'timezone' probe (+0800 / -0330 /
    +08:00 / 7), the IANA name from 'timezone_ianna' (Asia/Kuala_Lumpur) -
    no guessing between the many zones that share one UTC offset.

    Sign rule (M2 fix, 2026-11-18): a NEGATIVE offset keeps its '-' in the
    rendered label (UTC-03:30 - the old code dropped the sign and rendered
    a -03:30 zone as UTC03:30, i.e. a different zone). A positive offset
    renders WITHOUT the '+' (UTC08:00) so the on-disk form on a UTC+ host
    stays byte-stable across plugin upgrades - no rewrite, no section
    drift (prefix-cache invariant; the old 2026-11-08 '（UTC08:00）' shape
    is the fixed point)."""
    offset = str(merged.get("timezone", "") or "").strip()
    ianna = str(merged.get("timezone_ianna", "") or "").strip()
    # Defense-in-depth against F1: a raw filesystem path must never be
    # rendered as an IANA name. It can reach this function via a hand-edited
    # yaml field even after the probe-side `grep -v '^/'` guard: IANA zone
    # names never start with '/' or '~' and never contain a backslash or
    # colon, so reject exactly those shapes (Windows tz ids like
    # "Malay Peninsula Standard Time" contain none of these either).
    if ianna.startswith(("/", "~")) or "\\" in ianna or ":" in ianna:
        ianna = ""
    if not offset and not ianna:
        return ""
    core = offset[1:] if offset[:1] in ("+", "-") else offset
    sign = offset[:1]
    hhmm = ""
    if core.count(":") == 1:
        h, m = core.split(":", 1)
        if h.isdigit() and m.isdigit():
            hhmm = f"{int(h):02d}:{int(m):02d}"
    elif len(core) == 4 and core.isdigit():
        hhmm = f"{core[:2]}:{core[2:]}"
    elif len(core) <= 2 and core.isdigit():
        hhmm = f"{int(core):02d}:00"
    display_sign = "-" if sign == "-" else ""
    utc = f"UTC{display_sign}{hhmm}" if hhmm else ""
    label = ianna or (utc if utc else "unknown timezone")
    suffix = f"（{utc}）" if utc else ""
    return (
        f"用户所在时区是 {label}" + suffix
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
    only way to keep hand-written header notes alive across a merge.
    - file missing -> the fixed header (first-run delete + reprobe flow,
      owner ruling 2026-11-12: no manual fields at all);
    - file exists but has NO leading comment/blank block -> "" (we never
      inject the fixed header into a header-less file: that would be a
      silent byte change for no reason, breaking the prefix-cache rule)."""
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
    return "\n".join(block)


def _lock_path_for(path: str) -> str:
    """Shared sentinel file every writer serializes on: the sibling
    '<data>.lock' of the data file. Deliberately NOT the per-writer temp
    file (C2, 2026-11-18): locking a private temp file excludes nobody -
    two gateway processes each locked their own brand-new file and still
    raced the read-modify-write; only os.replace's atomicity was actually
    protecting readers."""
    return path + ".lock"


def _merge_probe_into_yaml(path: str, probe: dict | None = None) -> None:
    """Two-tier write (owner ruling 2026-11-12: trigger fields have NO manual
    override - live value wins; everything else is fill-only):
    - host/os/arch: ALWAYS overwrite with the live probe value when it differs.
    - all other probed fields: fill only when missing/empty, never clobber.
    - the file's leading comment/blank block is preserved verbatim across the
      rewrite (yaml.safe_dump alone would drop it - see
      ``_leading_comment_block``).
    Concurrency (2026-11-18 rewrite): the WHOLE read-modify-write is
    serialized on the shared sentinel lock (_lock_path_for), so two
    multiplexed gateway processes racing the same session start cannot lose
    each other's updates. The atomic temp-file + os.replace write is kept as
    the torn-file backstop for readers that read without the lock.
    `probe` may be supplied by the caller (the session-start hook computes it
    once for the trigger check AND the merge - was two full probes)."""
    import yaml

    if probe is None:
        probe = _probe_machine()

    handle = _acquire_shared_lock(_lock_path_for(path))
    try:
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

        # Generated field (owner ruling 2026-11-12: no hand-written fields
        # at all): user_timezone_hint is derived from the two probed
        # timezone fields, regenerated whenever it differs so it can never
        # go stale vs the machine it describes.
        hint = _derive_timezone_hint(merged)
        if hint and merged.get("user_timezone_hint") != hint:
            merged["user_timezone_hint"] = hint
            changed = True

        if not changed:
            return

        # M3: a fresh host's resolved dir may not exist yet; create it so
        # the atomic write can land (the old code relied on mkstemp, which
        # raises FileNotFoundError on a missing dir - silently swallowed by
        # the hook, placeholder forever).
        dirname = _os.path.dirname(path) or "."
        _os.makedirs(dirname, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dirname, suffix=".yaml.tmp")
        try:
            with _os.fdopen(fd, "w", encoding="utf-8") as f:
                if header:
                    f.write(header + "\n\n")
                f.write(yaml.safe_dump(merged, allow_unicode=True, sort_keys=False))
            _os.replace(tmp, path)
        except BaseException:
            if _os.path.exists(tmp):
                _os.remove(tmp)
            raise
    finally:
        _release_shared_lock(handle)


def _acquire_shared_lock(lock_path: str):
    """Cross-process exclusive lock on the SHARED sentinel file (see
    _lock_path_for), held for the whole read-modify-write.
    Primitives: fcntl.flock (POSIX - released on close, no waiting
    limit) / msvcrt byte-0 locking on Windows. The sentinel is opened
    'a+b' (create-if-missing, NEVER truncate), so two racers create the
    same file and contend on it. The msvcrt branch writes a 1-byte
    anchor on a 0-byte file in BINARY mode so the lock has a byte range
    to reserve - the old C1 bug wrote that anchor as BYTES into a
    TEXT-mode handle (hard TypeError, merge dead on Windows) and the
    anchor was superseded by the dump anyway.

    P1 (rescan 2026-11-18, measured on this host): a BLOCKING
    msvcrt.locking LK_LOCK request is aborted by the OS at ~9s with
    EDEADLK ([Errno 36] 'Resource deadlock avoided') - verified: a
    13s holder made the second writer's blocking request fail at
    9.3s, while a 3s holder was waited out fine. The consequence:
    'cannot lose each other's updates' only held for <=~9s contention
    (a slow-disk / AV-scan / network-drive wedge silently un-serialized
    the second writer, which then raced the read-modify-write). The
    msvcrt branch therefore POLLS NON-blocking LK_NBLCK requests
    (0.2s backoff, 30s deadline) instead of one blocking request: a
    wedged holder past the deadline loses to the next session start
    (the hook is fail-open and logs it) rather than racing unlocked.
    Fail-soft: if neither primitive is importable (exotic host),
    degrade to an unlocked write with a warning - os.replace atomicity
    is the torn-file backstop (same discipline as code_map's
    _shard_lock)."""
    global _LOCK_KIND
    if _LOCK_KIND is None:
        try:
            import fcntl  # noqa: F401
            _LOCK_KIND = "fcntl"
        except Exception:
            try:
                import msvcrt  # noqa: F401
                _LOCK_KIND = "msvcrt"
            except Exception:
                _LOCK_KIND = "none"
                logger.warning("env-brief: no cross-process lock primitive "
                               "(fcntl/msvcrt) - relying on the atomic "
                               "replace only")
                return None
    parent = _os.path.dirname(lock_path) or "."
    _os.makedirs(parent, exist_ok=True)
    handle = open(lock_path, "a+b")
    if _LOCK_KIND == "fcntl":
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_EX)
    else:  # msvcrt
        import msvcrt
        import time
        handle.seek(0)
        if _os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        # P1: poll NON-blocking requests - a blocking LK_LOCK dies at the
        # OS's ~9s EDEADLK watchdog (see docstring); NBLCK + backoff has
        # no such limit and keeps the serialization guarantee.
        deadline = time.monotonic() + 30.0
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
    return handle


def _release_shared_lock(handle) -> None:
    """Release the shared lock: explicit LK_UNLCK on the same byte range
    (msvcrt), then close (flock releases on close). No-op when the
    primitive degraded to 'none' (handle is None)."""
    if handle is None:
        return
    if _LOCK_KIND == "msvcrt":
        import msvcrt
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:
            pass  # already released / fd closed
    handle.close()


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
            # Probe ONCE and reuse the result for the trigger check and the
            # merge (the old path probed up to twice per session start).
            missing = not all(existing.get(k) for k in _TRIGGER_FIELDS)
            live = None if missing else _probe_machine()
            if missing or _trigger_probe(existing, live):
                _merge_probe_into_yaml(_ENV_BRIEF_PATH, probe=live)
                logger.info("env-brief on_session_start: probe+merge updated %s", _ENV_BRIEF_PATH)
        except Exception:
            # Fail-open: never break the session - but log it. The bare
            # `pass` here is what hid the C1 TypeError on Windows for a
            # week; a swallowed exception must stay observable.
            logger.warning("env-brief on_session_start probe+merge failed",
                           exc_info=True)

    ctx.register_hook("on_session_start", _on_session_start)
