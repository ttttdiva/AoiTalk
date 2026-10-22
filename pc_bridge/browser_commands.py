"""Closed, purpose-specific Bridge browser command contracts."""

GENERAL_BROWSER_COMMANDS = frozenset({
    "resolve_tab", "observe", "act", "navigate", "back", "scroll", "verify",
    "crowdworks_inspect", "end_run",
})
MEDIA_PREPARATION_COMMANDS = frozenset({
    "media_prepare_capabilities", "media_prepare_context", "media_prepare_readiness",
    "media_prepare_text", "media_prepare_youtube",
    *(f"media_prepare_{prefix}asset_{phase}" for prefix in ("", "youtube_")
      for phase in ("begin", "append", "finish", "inspect")),
})

DEDICATED_BROWSER_COMMANDS = frozenset({
    "resolve_tab", "observe", "act", "verify", "navigate", "back", "scroll", "end_run",
    "auth_status", "pause_login", "resume_login", "cancel_login", "show",
    "checkpoint_save", "checkpoint_load", "checkpoint_clear",
    "jev_run",
})
DEDICATED_BROWSER_PREFIX = "dedicated_browser_"


def preparation_backend(bridge):
    # Older extension-preparation evidence retains its own transport contract.
    return getattr(bridge, "media_preparation_backend", None) or getattr(bridge, "browser_backend", None)
