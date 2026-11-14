# env-brief

Hermes Agent plugin: **system-level host facts in the system prompt + a live-clock tool** (`env_time`), with **auto-probe** of the machine's identity, and a **single shared data file** that stays byte-stable under a multiplexed (one-gateway, many-profiles) Hermes setup.

One YAML file → one frozen section in every session's system prompt → zero prompt drift, prefix-cache friendly, hot-applies at the next compact boundary.

## Why it exists

By design, a Hermes session's system prompt freezes its "Conversation started:" timestamp at session start ("cache stability, not live-clock-keeping"), and knows nothing about the host machine itself. LLMs therefore have no way to know:

- which host / OS / arch / CPU / memory / disk they're actually running on,
- who the user is, whether they're root, what their timezone is,

unless they go dig it out with `terminal`/`date` every time — and guessing the timezone from a UTC+8 offset alone is actively wrong.

env-brief fixes this at the **prompt layer**, not the conversation layer:

1. A small YAML data file (default: `~/.hermes/env-brief.yaml`) holds the facts.
2. `register()` injects a rendered section into each session's system prompt (`ctx.register_system_prompt_section`), positioned after the memory section.
3. The section is a **pure function of the file's bytes** — no timestamps, no per-session state — so it is byte-identical across the whole session and across compactions. Edit the file, and the change hot-applies at the next compact boundary; no gateway restart needed.
4. Exact, on-demand time is a **tool call** (`env_time`), not a live clock burned into the prompt — you only pay for it when the model actually needs the precise moment.

### Two-layer time model (design decision)

| Layer | What | When it's fresh |
|---|---|---|
| **Frozen** | `## Local Environment Brief` section in the system prompt (host facts + timezone hint) | Rendered once per session build; re-rendered **verbatim** at compact while the file is unchanged |
| **Dynamic** | `env_time` tool → `{now, timezone, iso}` from the real system clock | Only when the model calls it |

This is the "90% of the time on-demand is enough; the other 10% gets a real clock, cheaply" trade-off.

## How it works

```
~/.hermes/env-brief.yaml          ← one file, system-level (not per-profile)
        │
        ▼  (on_session_start)
   machine probe + merge-back      ← auto-fills facts, detects stale/copied files
        │
        ▼  (render, pure fn of bytes)
   "## Local Environment Brief…"  ← injected into the system prompt, after_memory
        │
        └─ env_time tool          ← real clock, on demand
```

### Auto-probe + merge-back

On every session start, env-brief probes the machine (one short subprocess per fact: `hostname`, `uname -sr`, `nproc`, `free -h`, `df -h`, login shell, `whoami`, timezone, root/sudo/home-writable checks, container detection) and merges the result into the YAML file:

- **Trigger fields** (`host`, `os`, `arch`) are **always overwritten** with the live value when it differs — no hand-written value survives a mismatch. This also catches the "I copied an old `env-brief.yaml` from another machine" case: if the live `host`/`os`/`arch` don't match the file, it's treated as stale and re-probed.
- **All other fields** are **fill-only**: a missing/empty field gets probed; an existing field is never clobbered by the probe.
- `user_timezone_hint` is **code-generated, never hand-typed**: it's derived from the probed `timezone` (numeric offset) + `timezone_ianna` (IANA name) pair, so it can't go stale relative to the machine it describes.
- The write is **atomic** (`tempfile` + `os.replace`) under an **exclusive `fcntl.flock`**, so two multiplexed gateway processes racing the same session start can't interleave a half-written file.
- The file's leading comment block (the header explaining what the file is) is preserved verbatim across every rewrite — `yaml.safe_dump` alone would drop it, so the plugin re-attaches it explicitly.
- Probe scope is **machine/OS/hardware/permissions only** — deliberately no toolchain (no python path/version fields).

### Why "one file, shared by every profile"

Under Hermes' default multiplexed gateway (`gateway.multiplex_profiles: true`), one gateway process serves many profiles, and **each profile has its own `HERMES_HOME`** (`~/.hermes/profiles/<name>`). A host fact like "this box is `hermes2`, 4 cores, 12 GiB, root, UTC+8" is a property of the *machine*, not of a profile — so the data file lives at an **absolute, profile-independent path** (default `~/.hermes/env-brief.yaml`, i.e. the default profile home / shared root), and is deliberately **not** resolved through the per-profile `HERMES_HOME`. Every profile's sessions read the same bytes.

### Failure contract

Rendering **fails open**: a missing file, malformed YAML, or missing PyYAML all degrade to a small placeholder section (`"(no env-brief.yaml found…)"`) — they never raise into the prompt build. The same applies to the session-start probe/merge: any exception is swallowed, the session still starts.

## Features

- `## Local Environment Brief` system-prompt section, injected per session (after the memory section), byte-stable, hot-applies at compact
- `env_time` tool — real system clock: `{"now": "%Y-%m-%d %H:%M:%S", "timezone": "<tzinfo>", "iso": "<ISO-8601>"}`, no arguments
- Auto-probe of 13 machine facts on session start (`host`, `os`, `arch`, `cpu_cores`, `memory_total`, `disk_root_avail`, `shell`, `user`, `timezone`, `timezone_ianna`, `user_is_root`, `sudo_available`, `home_writable`, + optional `container`)
- Stale-file detection across hosts (trigger-field mismatch ⇒ re-probe)
- Atomic, lock-protected, comment-preserving YAML merges
- Generated, never-stale `user_timezone_hint` field
- Fail-open rendering and probing (never blocks session start)

## Requirements

- Hermes Agent (the `register(ctx)` plugin API: `register_system_prompt_section`, `register_tool`, `register_hook`)
- Python 3 with `yaml` (PyYAML) — ships with the Hermes runtime; if it's ever missing, the section degrades to a placeholder, nothing crashes
- Linux host (probe commands assume `uname`/`nproc`/`free`/`df`/`getent`; on other platforms the affected fields simply come back missing and stay unfilled)

## Install

### 1. Get the code

The cleanest single-copy layout is: install **once** into the shared (default) profile's plugins dir, and for every *named* profile, put a **symlink** (or skip it — see the multiplex section below) instead of a second clone.

```bash
# default profile (shared plugins dir)
mkdir -p ~/.hermes/plugins
git clone https://github.com/stennyc/env-brief.git ~/.hermes/plugins/env-brief
```

For each additional profile, link rather than re-clone (a second real tree = a second copy that drifts):

```bash
ln -s ~/.hermes/plugins/env-brief ~/.hermes/profiles/<name>/plugins/env-brief
```

> **Alternative, zero-copy:** under the default multiplexed gateway, install it **only** in the default profile and enable it **only** in the default profile's `config.yaml`. Named profiles' sessions, served by the same multiplexer process, resolve their per-profile data through the shared HERMES_HOME override machinery — and this plugin's data file is an absolute path that never resolves through HERMES_HOME at all, so there is nothing per-profile to install. One repo, one checkout, zero duplicate copies. See "Multiplex mode" below.

### 2. Enable it

```bash
hermes config set plugins.enabled env-brief    # or add `env-brief` under plugins.enabled in config.yaml
hermes gateway restart                          # or `hermes -p <name> gateway restart` for a named profile's own gateway
```

### 3. Create (or auto-create) the data file

```yaml
# ~/.hermes/env-brief.yaml
host: myhost
os: Linux 6.17.9-1-pve
arch: x86_64
cpu_cores: '4'
memory_total: 12Gi
user: me
timezone: +0800
timezone_ianna: Asia/Kuala_Lumpur
# … any other key: value pair you want in the section
```

You don't strictly have to create it: the first session start auto-probes the machine and writes the file for you (trigger fields + fill-only fields + the generated `user_timezone_hint`). You *can* add any extra key you like — those are fill-only/never-clobbered by the probe.

### 4. Verify

Start a session and check the system prompt for a `## Local Environment Brief (system-level; …)` block, or ask the model to call `env_time`.

## Multiplex mode (one gateway, many profiles) — and how to avoid duplicate copies

Hermes' default gateway mode is **multiplexing**: one gateway process (whichever profile launched it) serves messages for *every* profile on the machine.

```bash
hermes config set gateway.multiplex_profiles true
hermes gateway restart
```

What that means for env-brief specifically:

1. **The data file is system-level by design.** It's an absolute path, never resolved through the per-profile `HERMES_HOME` (which *is* what changes per profile under a multiplexed gateway). Every profile's sessions, served by the one multiplexer, read the same file. There is exactly one source of truth per machine — that's the whole point of putting it here instead of in a per-profile dir.
2. **The probe/merge is race-safe.** Two profiles' sessions can start in the same multiplexer process (or even two standalone gateways, one per profile, if you run in the non-multiplex mode) at overlapping times and both try to re-probe the machine at session start. The merge is done under an exclusive `fcntl.flock` on the temp file, atomically renamed into place — no interleaved half-writes.
3. **Don't install it N times, N = number of profiles.** A real git checkout per profile (default profile + `profiles/coder` + `profiles/devops` + …) gives you N independent copies that each need their own `git pull` to stay in sync — that's the "multiple copies" problem this section exists to avoid. Use **one** of:
   - **Recommended (zero-copy):** install once in the default profile's `plugins/`, enable it only there. The multiplexer serves all profiles from that one process; the data file is absolute-path, profile-independent; nothing per-profile needs to exist.
   - **Symlink fan-out:** install once in the default profile; `ln -s` it into each named profile's `plugins/` dir if you still want each profile to list it in its own `plugins.enabled` (e.g. because you run standalone per-profile gateways, not the multiplexer). One checkout, many links, no drift.
4. **Secondary profiles must not start their own gateway while multiplexing is on.** This is a Hermes-wide rule, not env-brief-specific: under multiplex mode, `hermes -p <name> gateway start` *attaches* to the host multiplexer rather than spawning a second gateway process; only the default profile's lifecycle verbs (`hermes gateway restart`, etc.) manage the one shared process. Follow the same rule when you "restart the gateway" after installing/enabling env-brief — restart the *host* gateway, not a second per-profile one.

If you are **not** in multiplex mode (one standalone gateway per profile, each with its own process), the same rules still apply: keep **one** checkout + symlinks, not N checkouts, so the probe/merge code is byte-identical everywhere the data file gets touched.

## Agent install prompt (copy/paste)

The following prompt is designed to be handed to an agent (Hermes, Claude Code, etc.) as a one-shot install instruction. It covers the multiplex gateway case and explicitly prevents a multi-copy install.

```text
Install the Hermes plugin `env-brief` (https://github.com/stennyc/env-brief) on this machine.

Rules — do NOT create multiple copies of the plugin:
1. Decide first whether the host runs in multiplex mode: check `grep multiplex_profiles` in
   the default profile's config.yaml (~/.hermes/config.yaml). If it's true (or unset on a
   multi-profile install, where it's on by default), there is ONE gateway process serving ALL
   profiles — install the plugin ONCE, in the default profile's shared plugins dir
   (~/.hermes/plugins/env-brief), and do NOT copy or clone it into any
   ~/.hermes/profiles/<name>/plugins/ dir. If per-profile enablement is still wanted, use a
   SYMLINK, not a second clone: ln -s ~/.hermes/plugins/env-brief
   ~/.hermes/profiles/<name>/plugins/env-brief. Never git-clone a second real tree per profile.
2. If multiplex mode is OFF (standalone per-profile gateways), still use one checkout in the
   default profile + symlinks into each named profile — never N real checkouts.
3. The plugin's data file is SYSTEM-LEVEL: it lives at ~/.hermes/env-brief.yaml (the shared /
   default-profile root, an absolute path that is deliberately NOT resolved through the
   per-profile HERMES_HOME that a multiplexed gateway switches on per served profile). Do not
   create a separate env-brief.yaml per profile — one machine, one file.

Steps:
4. git clone https://github.com/stennyc/env-brief.git ~/.hermes/plugins/env-brief
   (skip if it already exists there — do NOT overwrite an existing checkout).
5. For each named profile that needs it and does NOT already have env-brief in its plugins
   dir: ln -s ~/.hermes/plugins/env-brief ~/.hermes/profiles/<name>/plugins/env-brief
   (create the target's plugins/ dir first if needed; skip the link if the dir/entry already
   exists — never replace a real dir with a link or vice versa without checking first).
6. Enable it: add `env-brief` to plugins.enabled in the config.yaml of every profile that
   should run it (default profile's config.yaml at minimum; named profiles' config.yaml only
   if you chose the symlink fan-out in step 5).
7. Create/seed the shared data file ~/.hermes/env-brief.yaml if it doesn't exist (a minimal
   key: value stub is fine — the plugin auto-probes host/os/arch/cpu/memory/disk/user/
   timezone/root/sudo/home-writable on the next session start and fills anything missing;
   the three trigger fields host/os/arch are always overwritten with live values when they
   don't match, so a stale file copied from another machine self-heals).
8. Restart the gateway exactly ONCE, at the host level:
   - multiplex mode ON  → `hermes gateway restart` (this is the single multiplexer process;
     do NOT run `hermes -p <name> gateway start` for secondary profiles — under multiplex
     mode those attach to the host multiplexer, they must not spawn a second gateway).
   - multiplex mode OFF → restart each profile's own standalone gateway, one at a time.
9. Verify: start a session (any profile), confirm the system prompt now contains a
   "## Local Environment Brief (system-level; …)" section, confirm ~/.hermes/env-brief.yaml
   was created/auto-updated on first session start, and confirm `env_time` is available as a
   tool (call it once to get the host's real current date/time/timezone).
10. Done. Report back: the path of the single checkout, which profiles got symlinks vs. a
    real copy, which profiles' config.yaml you enabled it in, the restart command you ran,
    and the env-brief section you saw in the session's system prompt.
```

## Files

| File | Purpose |
|---|---|
| `plugin.yaml` | Hermes plugin manifest: name, version, description, `provides_hooks: [on_session_start]`, enabled |
| `__init__.py` | The whole plugin: `register(ctx)` (section + `env_time` tool + session-start probe/merge hook), probe/merge code, pure-fn renderer |
| `~/.hermes/env-brief.yaml` *(user data, not in this repo)* | The single shared facts file the section is rendered from |
