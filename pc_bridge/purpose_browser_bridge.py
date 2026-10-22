"""Fixed general-helper and media-profile routes; no failure fallback."""
from .browser_commands import GENERAL_BROWSER_COMMANDS, MEDIA_PREPARATION_COMMANDS
from .edge import EdgeBridgeError


class PurposeBrowserBridge:
    browser_backend = "agent_browser"
    media_preparation_backend = "managed_profile"

    def __init__(self, general, preparation):
        self.general, self.preparation = general, preparation

    def _request(self, command, params):
        if command in MEDIA_PREPARATION_COMMANDS:
            return self.preparation._request(command, params)
        if isinstance(command, str) and command.startswith("media_prepare_"):
            raise EdgeBridgeError("media_prepare_command_rejected")
        if command not in GENERAL_BROWSER_COMMANDS:
            raise EdgeBridgeError("agent_browser_command_unsupported")
        return self.general._request(command, params)

    def available(self):
        return self.general.available()

    def media_available(self):
        return self.preparation.available()

    def diagnostics(self, purpose="general"):
        backend = self._backend(purpose)
        return {**backend.diagnostics(), "backend": self.browser_backend if purpose == "general"
                else self.media_preparation_backend}

    def _backend(self, purpose):
        if purpose == "general":
            return self.general
        if purpose == "media":
            return self.preparation
        raise EdgeBridgeError("bridge_browser_purpose_invalid")

    def local_setup(self, action, params, *, purpose="general"):
        return self._backend(purpose).local_setup(action, params)

    def bind_connection(self, owner, epoch, profiles):
        self.preparation.bind_connection(owner, epoch, profiles)

    def close(self):
        failed = False
        # Always attempt both owned cleanups, even if one cannot confirm exit.
        for backend in (self.general, self.preparation):
            try:
                failed |= backend.close() is False
            except Exception:
                failed = True
        if failed:
            raise EdgeBridgeError("bridge_browser_close_unconfirmed")
        return True

    def suspend(self):
        failed = False
        for backend in (self.general, self.preparation):
            try:
                suspend = getattr(backend, "suspend", backend.close)
                failed |= suspend() is False
            except Exception:
                failed = True
        if failed:
            raise EdgeBridgeError("bridge_browser_close_unconfirmed")
