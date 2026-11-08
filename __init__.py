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

logger = logging.getLogger("hermes_plugins.env_brief")

# Absolute, profile-independent (owner ruling: system-level, not per-profile).
# Deliberately NOT Path.home()/.hermes: under a multiplexed gateway each
# profile has its own HERMES_HOME; this path is the one shared data file.
_ENV_BRIEF_PATH = "/root/.hermes/env-brief.yaml"

_PLACEHOLDER = (
    "## Local Environment Brief\n"
    "(no /root/.hermes/env-brief.yaml found - host facts unavailable; "
    "query tools for live system details)"
)

_CLOCK_NOTE = (
    "For the current date/time, query tools (e.g. `date`); "
    "the system-prompt timestamp line is frozen at session start by design."
)


def _render_brief() -> str:
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
    logger.info("env-brief section registered (source=%s)", _ENV_BRIEF_PATH)

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
        except Exception:
            pass

    ctx.register_hook("on_session_start", _on_session_start)
