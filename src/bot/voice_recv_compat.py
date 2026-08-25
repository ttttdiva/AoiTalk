"""Optional compatibility boundary for ``discord-ext-voice-recv``.

The Discord text command tree is still importable in a minimal installation;
voice operations report a clear dependency error only when they are used.
"""

from types import SimpleNamespace


try:
    from discord.ext import voice_recv as voice_recv

    VOICE_RECV_AVAILABLE = True
except ImportError:
    VOICE_RECV_AVAILABLE = False

    class _UnavailableAudioSink:
        def __init__(self, *args, **kwargs):
            del args, kwargs

    class _UnavailableVoiceRecvClient:
        def __init__(self, *args, **kwargs):
            del args, kwargs
            raise RuntimeError(
                "Discord voice requires the optional discord-ext-voice-recv package"
            )

    voice_recv = SimpleNamespace(
        AudioSink=_UnavailableAudioSink,
        VoiceRecvClient=_UnavailableVoiceRecvClient,
    )


__all__ = ["VOICE_RECV_AVAILABLE", "voice_recv"]
