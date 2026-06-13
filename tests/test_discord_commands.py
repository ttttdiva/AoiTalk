from src.bot.discord_bot import AoiTalkBot


class FakeConfig:
    def __init__(self):
        self.config = {
            "default_character": "テストキャラ",
            "runtime_feature_permissions": {
                "allowed_discord_user_ids": ["217450236879044609"],
            },
            "discord": {
                "default_mode": "voice",
                "max_history_length": 20,
                "memory_prefill_message_count": 12,
                "session": {
                    "cleanup_interval": 123,
                    "inactive_timeout": 456,
                },
                "sync_commands": True,
                "sync_command_scope": "guild_and_global",
                "voice": {
                    "sample_rate": 24000,
                    "channels": 1,
                    "auto_disconnect_timeout": 789,
                },
            },
        }

    def get(self, key, default=None):
        current = self.config
        for part in str(key).split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current

    def get_available_characters(self):
        return ["テストキャラ", "別キャラ"]


def test_discord_command_tree_exposes_full_command_set():
    bot = AoiTalkBot(FakeConfig())
    names = {command.name for command in bot.tree.get_commands()}

    expected = {
        "character",
        "clear",
        "clear_queue",
        "create_playlist",
        "feature",
        "help",
        "join",
        "leave",
        "mode",
        "nanobanana",
        "nowplaying",
        "pause",
        "play",
        "play_playlist",
        "playlists",
        "previous",
        "queue",
        "queue_playlist",
        "remove_queue",
        "search",
        "setavatar",
        "settings",
        "show_queue",
        "skip",
        "spotify_auth",
        "spotify_code",
        "status",
    }

    assert expected <= names
    assert len(names) == 27


def test_discord_runtime_uses_configured_defaults():
    bot = AoiTalkBot(FakeConfig())

    assert bot.default_mode == "voice"
    assert bot.session_handler.default_mode == "voice"
    assert bot.session_handler.cleanup_interval == 123
    assert bot.session_handler.inactive_timeout == 456
    assert bot.voice_handler.sample_rate == 24000
    assert bot.voice_handler.channels == 1
    assert bot.voice_handler.auto_disconnect_timeout == 789
