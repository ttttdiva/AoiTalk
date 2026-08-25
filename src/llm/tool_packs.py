"""オンデマンドで公開するツール pack の定義とセッション状態。

メインエージェントへ常時公開するのは安定したコア集合だけにし、
低頻度・専門系のツールは pack としてまとめて名前と一行説明だけを
`load_tool_pack` の description に載せる。モデルが必要と判断した時に
`load_tool_pack` を呼ぶと、その pack のツールが実効集合へ加わる。

ロードは会話セッション内で単調増加させる（一度ロードしたら外さない）。
これによりツールスキーマのハッシュ（`conversation_context.stable_cache_key`）の
揺れをセッション中数回に抑える。
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
from typing import Any, Callable, Iterable, Optional

from ..tools.core import ToolDefinition, ToolParam
from ..services.agent_team_v3 import (
    agent_team_scope_active,
    agent_team_v3_context_tags,
    agent_team_v3_delegation_enabled,
    agent_team_v3_teams,
)
from ..services.project_context import (
    get_runtime_project_context,
    project_context_activation_values,
)

LOAD_TOOL_PACK_TOOL_NAME = "load_tool_pack"

# ``None`` means a trusted resolver completed and found no Story session.  A
# private sentinel keeps that state distinct from a caller that did not supply
# a request-local Story result and therefore needs the compatibility resolver.
_STORY_CONTEXT_UNSET = object()

CHARACTER_TOOL_OWNER = "character"
WORKSPACE_MANIFEST_TOOL_PREFIX = "ws_"
APPS_TOOL_OWNER = "apps"
APPS_TOOL_PACK_ID = "apps"
APPS_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "create_app",
        "list_apps",
        "get_app",
        "get_app_context",
        "analyze_app_business",
        "list_app_files",
        "read_app_file",
        "write_app_file",
        "delete_app_file",
        "validate_app_manifest",
        "update_app_manifest",
        "app_git_status",
        "app_git_history",
        "app_git_diff",
        "app_git_restore",
        "build_app_target",
        "test_app_target",
        "run_app_target",
        "package_app_target",
        "stop_app_job",
        "read_app_job_logs",
        "create_app_release",
        "export_app_release",
        "import_app_source_bundle",
        "fork_app",
        "link_app_to_project",
        "unlink_app_from_project",
        "link_app_to_task",
    }
)


@dataclass(frozen=True)
class ToolPack:
    """既定では非公開にしておくツールのまとまり。"""

    pack_id: str
    summary: str
    prompt_fragment: str
    tool_names: frozenset[str] = frozenset()
    owners: frozenset[str] = frozenset()
    name_prefixes: tuple[str, ...] = ()
    manual_load: bool = True

    def matches(self, tool_name: str, owner: str = "") -> bool:
        name = str(tool_name or "")
        if not name:
            return False
        if name in self.tool_names:
            return True
        if owner and owner in self.owners:
            return True
        return any(name.startswith(prefix) for prefix in self.name_prefixes)


# 案件管理のうち台帳 / WBS / 課題管理表の作成・更新・同期系。
# get_project_progress や基本タスク CRUD などの高頻度ツールはコアに残す。
PROJECT_TABLE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "create_record_table",
        "append_record_rows",
        "update_record_row",
        "delete_record_rows",
        "delete_record_table",
        "sync_wbs_tasks",
        "sync_issue_table",
        "get_upcoming_wbs_tasks",
        "summarize_project_requests",
    }
)

_PROJECT_TABLES_FRAGMENT = "\n".join(
    [
        "## 台帳 / WBS / 課題管理表のルール",
        "- WBS は内部 DB 管理の作業分解表として扱う。正本は `WBS.dbtable`。"
        "ユーザー提供の Excel WBS は任意の取り込み元でしかなく、無くても進捗確認は続行する。",
        "- 会話・文書・自分の分解から WBS 項目を作る／更新する時は "
        "`create_record_table` / `append_record_rows` / `update_record_row` を "
        "`WBS.dbtable` に対して使う。Excel を先に要求しない。",
        "- 外部 WBS Excel の取り込み・同期依頼では `sync_wbs_tasks` を呼ぶ。"
        "既定では `WBS.dbtable` へ取り込むだけで、通常のタスク一覧項目は作らない。",
        "- 課題管理表は `課題管理表.dbtable` が正本。プロジェクトに issue_file がある、"
        "ファイラーに新しい課題管理表がある、または課題管理表 / issue / 要確認 が話題になった時は "
        "`sync_issue_table` を含める。",
        "- 明示的な更新・完了依頼では mutation モードで実行する"
        "（`sync_wbs_tasks` / `sync_issue_table` は `dry_run=false`）。"
        "プレビューや dry-run は完了した更新ではない。",
        "- 台帳（レコードテーブル）はDB的な表データ用。Markdown表、CSV/Excel行、機器一覧、"
        "接続一覧、WBS行、課題行、パラメータ行は台帳に入れ、案件情報Docs本文へ生の表行を貼らない。",
        "- 台帳や WBS を更新した後で進捗を答える場合は `get_project_progress` を再実行してから結論を出す。",
    ]
)


DEFERRED_TOOL_PACKS: tuple[ToolPack, ...] = (
    ToolPack(
        # App tools are registered only for a server-resolved App scope.  The
        # pack remains deferred so a normal/project-only turn never publishes
        # the 28 App schemas, while an active App turn auto-loads it below.
        pack_id=APPS_TOOL_PACK_ID,
        summary="選択中AppのManifest、Workspace、Git、Job、Release操作",
        tool_names=APPS_TOOL_NAMES,
        owners=frozenset({APPS_TOOL_OWNER}),
        prompt_fragment=(
            "選択中のApp contextに対して、Manifest、Workspace、Git、Job、"
            "Release、Project/Task binding操作を行う。対象App・Projectの権限、"
            "承認、固定Releaseの読み取り専用境界を必ず維持する。"
        ),
    ),
    ToolPack(
        pack_id="spotify",
        summary="Spotify の再生・検索・プレイリスト操作",
        owners=frozenset({"spotify"}),
        tool_names=frozenset(
            {
                "setup_spotify_auth",
                "set_spotify_auth_code",
                "search_spotify_activity",
                "get_spotify_activity_stats",
                "get_recent_spotify_activity",
                "get_spotify_listening_patterns",
                "search_spotify_music",
                "play_spotify_track",
                "play_song_now",
                "queue_song",
                "pause_spotify",
                "skip_spotify_track",
                "previous_track",
                "get_spotify_status",
                "show_queue",
                "clear_spotify_queue",
                "remove_from_queue",
                "get_spotify_user_playlists",
                "create_playlist",
                "create_playlist_from_queue",
                "add_tracks_to_playlist",
                "add_queue_to_playlist",
                "add_playlist_to_queue",
                "remove_tracks_from_playlist",
                "play_playlist",
            }
        ),
        prompt_fragment=(
            "Spotify direct toolsを使って検索・再生・停止・Queue・Playlist・"
            "Listening activity操作を行い、実行結果を確認してからユーザーへ報告する。"
        ),
    ),
    ToolPack(
        pack_id="media",
        summary="画像生成、YouTube / ニコニコなどのストリーミングメディア操作",
        tool_names=frozenset({"media_assistant"}),
        prompt_fragment=(
            "`media_assistant` に画像生成やメディア再生を依頼する。"
            "画像生成では生成条件（被写体・構図・スタイル）を依頼文へ具体的に書く。"
            "テキストだけで済ませず、必ずツール結果を得てから回答する。"
        ),
    ),
    ToolPack(
        pack_id="project_tables",
        summary="案件の台帳 / WBS / 課題管理表の作成・更新・同期",
        tool_names=PROJECT_TABLE_TOOL_NAMES,
        prompt_fragment=_PROJECT_TABLES_FRAGMENT,
    ),
    ToolPack(
        pack_id="agent_team",
        summary="独立した調査・設計・実装・レビュー担当への委譲",
        tool_names=frozenset({"agent_team_delegate"}),
        prompt_fragment=(
            "独立したスコープを持つ調査・設計・実装・レビューを委譲する。"
            "同じ依頼を複数インスタンスへ重複して渡さず、複数件が必要なら各スコープを"
            "`scopes` に明示する。"
        ),
    ),
    ToolPack(
        pack_id="characters",
        summary="この環境に登録されたキャラクターエージェントへの委譲",
        owners=frozenset({CHARACTER_TOOL_OWNER}),
        prompt_fragment=(
            "各キャラクターツールは、そのキャラクターの人格と担当領域で応答する専門エージェント。"
            "依頼内容を要約せずそのまま渡し、返答をユーザーへ提示する。"
        ),
    ),
    ToolPack(
        pack_id="workspace_tools",
        summary="このプロジェクトの workspace manifest で定義された専用ツール",
        name_prefixes=(WORKSPACE_MANIFEST_TOOL_PREFIX,),
        prompt_fragment=(
            "workspace manifest 由来のツールはプロジェクト固有のスクリプトを実行する。"
            "引数の意味を manifest の説明から確認し、実行前に副作用の有無を判断する。"
        ),
    ),
)

DEFERRED_TOOL_PACKS_BY_ID: dict[str, ToolPack] = {
    pack.pack_id: pack for pack in DEFERRED_TOOL_PACKS
}


def _client_story_context_active(
    client: Any,
    *,
    story_context: Any = _STORY_CONTEXT_UNSET,
) -> bool:
    """Return Story activation from the server-backed structured context.

    Natural-language input is intentionally not consulted here.  Providers
    expose ``_get_story_chat_context_sync`` only after resolving the current
    ``StoryWritingSession`` for the conversation, which makes this a trusted
    per-request signal shared by Team activation and tool exposure.
    """

    if story_context is not _STORY_CONTEXT_UNSET:
        return bool(story_context)
    getter = getattr(client, "_get_story_chat_context_sync", None)
    if not callable(getter):
        return False
    try:
        return bool(getter())
    except Exception:  # noqa: BLE001 - compatibility resolver has no context
        return False


def contextual_agent_team_scope(
    config: Any,
    *,
    client: Any = None,
    project_context: dict[str, Any] | None = None,
    loaded_team_ids: Iterable[str] = (),
    story_context: Any = _STORY_CONTEXT_UNSET,
) -> dict[str, Any]:
    """Resolve canonical Team activation for the current request.

    Apps visibility and App Development Team activation deliberately consume
    this one resolver.  ``project_context`` is the server-resolved runtime
    object; when omitted, the request-local ContextVar is used.  No prose
    keyword or mutable prompt flag can activate either capability.
    """

    project = project_context
    if project is None:
        project = get_runtime_project_context()
    if not isinstance(project, dict):
        project = {}

    activation = project_context_activation_values(project)
    app_context = activation.get("app_context")
    app_context = app_context if isinstance(app_context, dict) else {}
    # An App context is an authorization-bearing object.  Keep the App id as a
    # fallback activation identity when older rows did not persist a target
    # key; Project-only contexts do not have this nested object and therefore
    # never receive the App Development tag.
    app_identity = str(
        app_context.get("id") or app_context.get("app_id") or ""
    ).strip()
    app_target_id = str(
        activation.get("app_target_id")
        or app_context.get("target_key")
        or app_identity
        or ""
    ).strip() or None
    development_status = str(
        activation.get("development_status")
        or app_context.get("development_status")
        or "working"
    ).strip() or None

    # ``agent_team_v3_context_tags`` also accepts legacy top-level
    # ``app_target_id``/``app_id`` fields.  Those fields can be present on a
    # plain Project projection, so do not let them activate App Development
    # unless the trusted nested App scope was resolved as well.
    project_for_tags: dict[str, Any] = project
    if not app_identity:
        project_for_tags = dict(project)
        for key in ("app_target_id", "app_id", "target_id", "development_status", "status"):
            project_for_tags.pop(key, None)
        raw_metadata = project_for_tags.get("metadata")
        if isinstance(raw_metadata, dict):
            metadata = dict(raw_metadata)
            for key in ("app_target_id", "app_id", "development_status", "status"):
                metadata.pop(key, None)
            project_for_tags["metadata"] = metadata

    story_active = _client_story_context_active(
        client,
        story_context=story_context,
    )
    story_mode = "writing" if story_active else None
    tags = agent_team_v3_context_tags(
        project=project_for_tags,
        session={"story_mode": story_mode} if story_mode else None,
        app_target_id=app_target_id if app_identity else None,
        development_status=development_status if app_identity else None,
        story_mode=story_mode,
    )
    scope = agent_team_scope_active(
        config,
        project=project_for_tags,
        context_tags=tags,
        app_target_id=app_target_id if app_identity else None,
        development_status=development_status if app_identity else None,
        story_mode=story_mode,
        loaded_team_ids=loaded_team_ids,
    )

    active_team_ids = {
        str(item).strip()
        for item in scope.get("active_team_ids") or []
        if str(item).strip()
    }
    app_development_team_ids: set[str] = set()
    for team in agent_team_v3_teams(config):
        activation_config = team.get("activation") or {}
        contexts = {
            str(item).strip().lower()
            for item in activation_config.get("contexts", []) or []
            if str(item).strip()
        }
        team_id = str(team.get("team_id") or "").strip()
        team_name = str(team.get("name") or "").strip().casefold().replace(" ", "_")
        if team_id and (
            "app_development" in contexts
            or team_id.casefold() == "app_development"
            or team_name == "app_development"
        ):
            app_development_team_ids.add(team_id)

    return {
        **scope,
        "context_tags": sorted(tags),
        "app_context_active": bool(app_identity),
        "app_development_active": bool(
            app_identity
            and "app_development" in tags
            and bool(active_team_ids & app_development_team_ids)
        ),
        "story_active": "story" in tags,
        "active_team_ids": sorted(active_team_ids),
        "active_subagent_ids": sorted(
            {
                str(item).strip()
                for item in scope.get("active_subagent_ids") or []
                if str(item).strip()
            }
        ),
        "app_target_id": app_target_id,
    }


def contextual_tool_pack_allowed(
    pack_id: str,
    *,
    client: Any = None,
    config: Any = None,
    project_context: dict[str, Any] | None = None,
    loaded_team_ids: Iterable[str] = (),
    contextual_scope: Mapping[str, Any] | None = None,
) -> bool:
    """Check request-local gates for contextual packs.

    Only the Apps pack has an additional gate today.  Keeping the gate here
    lets both the loader enum and final provider exposure enforce the same
    decision, including when a fixed registry is reused across turns.
    """

    if str(pack_id or "").strip() != APPS_TOOL_PACK_ID:
        return True
    effective_config = config
    if effective_config is None:
        effective_config = getattr(client, "config", None)
    if effective_config is not None:
        try:
            apps_section = effective_config.get("apps", {})
            if isinstance(apps_section, dict):
                apps_enabled = apps_section.get("enabled", True)
            else:
                apps_enabled = effective_config.get("apps.enabled", True)
            if not bool(apps_enabled):
                return False
        except (AttributeError, TypeError):
            pass
    scope = contextual_scope
    if scope is None:
        scope = contextual_agent_team_scope(
            effective_config,
            client=client,
            project_context=project_context,
            loaded_team_ids=loaded_team_ids,
        )
    return bool(scope.get("app_development_active"))

# 明示的なユーザーコマンドは意図が確定しているため、モデルの往復なしで自動ロードする。
COMMAND_CAPABILITY_AUTO_LOAD_PACKS: dict[str, tuple[str, ...]] = {
    "wbs_sync": ("project_tables",),
    "project_db_update": ("project_tables",),
    "image_generation": ("media",),
}


def pack_for_tool(tool_name: str, owner: str = "") -> Optional[ToolPack]:
    for pack in DEFERRED_TOOL_PACKS:
        if pack.matches(tool_name, owner):
            return pack
    return None


def is_deferred_tool(tool_name: str, owner: str = "") -> bool:
    return pack_for_tool(tool_name, owner) is not None


class ToolPackSession:
    """1会話セッション分のロード済み pack 集合。

    セッション内では単調増加させ、pack を外すことはしない。
    別セッションへ切り替わったら破棄する。
    """

    def __init__(self) -> None:
        self._loaded: set[str] = set()
        self._session_key: Optional[str] = None
        self._bound = False

    @property
    def loaded(self) -> frozenset[str]:
        return frozenset(self._loaded)

    def rebind(self, session_key: Any) -> None:
        key = str(session_key) if session_key is not None else None
        if self._bound and key == self._session_key:
            return
        if self._bound and key != self._session_key:
            self._loaded.clear()
        self._session_key = key
        self._bound = True

    def is_loaded(self, pack_id: str) -> bool:
        return pack_id in self._loaded

    def load(self, pack_id: str) -> bool:
        """pack をロードする。新たにロードした場合だけ True。"""
        if pack_id not in DEFERRED_TOOL_PACKS_BY_ID:
            return False
        if pack_id in self._loaded:
            return False
        self._loaded.add(pack_id)
        return True

    def load_many(self, pack_ids: Iterable[str]) -> tuple[str, ...]:
        return tuple(pack_id for pack_id in pack_ids if self.load(pack_id))


def tool_pack_session_for_client(client: Any) -> ToolPackSession:
    """クライアントに紐づくロード済み pack 集合を返す（無ければ作る）。"""
    session = getattr(client, "_tool_pack_session", None)
    if not isinstance(session, ToolPackSession):
        session = ToolPackSession()
        try:
            setattr(client, "_tool_pack_session", session)
        except Exception:  # noqa: BLE001  SimpleNamespace 以外の read-only client 対策
            pass
    session.rebind(getattr(client, "current_session_id", None))
    return session


def tool_visible_for_session(
    session: ToolPackSession,
    tool_name: str,
    owner: str = "",
    *,
    client: Any = None,
    config: Any = None,
    project_context: dict[str, Any] | None = None,
    contextual_scope: Mapping[str, Any] | None = None,
) -> bool:
    pack = pack_for_tool(tool_name, owner)
    if pack is None:
        return True
    if not contextual_tool_pack_allowed(
        pack.pack_id,
        client=client,
        config=config,
        project_context=project_context,
        contextual_scope=contextual_scope,
    ):
        return False
    return session.is_loaded(pack.pack_id)


def auto_load_contextual_packs(
    session: ToolPackSession,
    *,
    client: Any = None,
    config: Any = None,
    project_context: dict[str, Any] | None = None,
    contextual_scope: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    """Auto-load Apps/Agent Team packs for trusted App or Story context."""

    effective_config = config if config is not None else getattr(client, "config", None)
    scope = contextual_scope
    if scope is None:
        scope = contextual_agent_team_scope(
            effective_config,
            client=client,
            project_context=project_context,
        )
    pack_ids: list[str] = []
    if scope.get("app_development_active") and contextual_tool_pack_allowed(
        APPS_TOOL_PACK_ID,
        client=client,
        config=effective_config,
        project_context=project_context,
        contextual_scope=scope,
    ):
        pack_ids.append(APPS_TOOL_PACK_ID)
        if agent_team_v3_delegation_enabled(effective_config):
            pack_ids.append("agent_team")
    if scope.get("story_active") and agent_team_v3_delegation_enabled(effective_config):
        if "agent_team" not in pack_ids:
            pack_ids.append("agent_team")
    return session.load_many(pack_ids)


def auto_load_packs_for_command_capabilities(
    session: ToolPackSession,
    capabilities: Iterable[str],
) -> tuple[str, ...]:
    pack_ids: list[str] = []
    for capability in capabilities:
        for pack_id in COMMAND_CAPABILITY_AUTO_LOAD_PACKS.get(str(capability), ()):
            if pack_id not in pack_ids:
                pack_ids.append(pack_id)
    return session.load_many(pack_ids)


def auto_load_packs_for_tool_names(
    session: ToolPackSession,
    tool_names: Iterable[str],
) -> tuple[str, ...]:
    """名指しで要求されたツールが属する pack を自動ロードする。

    狭コンテキスト向けに「このターンで使う許可ツール名」を先に決める経路で使う。
    許可名に deferred ツールが入っているのに pack が未ロードだと、実効集合と
    食い違って公開ツールが 0 件になるため、ここで整合させる。
    """
    pack_ids: list[str] = []
    for tool_name in tool_names:
        pack = pack_for_tool(str(tool_name))
        if pack and pack.pack_id not in pack_ids:
            pack_ids.append(pack.pack_id)
    return session.load_many(pack_ids)


def available_packs_for_registry(
    registry: Any,
    *,
    client: Any = None,
    config: Any = None,
    project_context: dict[str, Any] | None = None,
    contextual_scope: Mapping[str, Any] | None = None,
) -> tuple[ToolPack, ...]:
    """レジストリに実際に登録されている pack だけを返す。"""
    tools = _registry_tools(registry)
    available: list[ToolPack] = []
    for pack in DEFERRED_TOOL_PACKS:
        if not pack.manual_load:
            continue
        if not contextual_tool_pack_allowed(
            pack.pack_id,
            client=client,
            config=config,
            project_context=project_context,
            contextual_scope=contextual_scope,
        ):
            continue
        if any(
            pack.matches(str(getattr(tool, "name", "")), str(getattr(tool, "owner", "")))
            for tool in tools
        ):
            available.append(pack)
    return tuple(available)


def pack_tool_names_in_registry(pack: ToolPack, registry: Any) -> tuple[str, ...]:
    return tuple(
        str(getattr(tool, "name", ""))
        for tool in _registry_tools(registry)
        if pack.matches(
            str(getattr(tool, "name", "")),
            str(getattr(tool, "owner", "")),
        )
    )


def build_load_tool_pack_description(packs: Iterable[ToolPack]) -> str:
    lines = [
        "Load an optional tool pack so its tools become callable from the next "
        "assistant turn in this conversation. Call this when the request needs a "
        "domain that is not in the always-available core tools. Loaded packs stay "
        "loaded for the rest of the session.",
        "",
        "Available packs:",
    ]
    lines.extend(f"- {pack.pack_id}: {pack.summary}" for pack in packs)
    return "\n".join(lines)


def build_load_tool_pack_tool(
    registry: Any,
    session: ToolPackSession,
    *,
    client: Any = None,
    contextual_scope: Mapping[str, Any] | None = None,
) -> Optional[ToolDefinition]:
    """現在のレジストリで実際にロード可能な pack を列挙したメタツールを作る。"""
    packs = available_packs_for_registry(
        registry,
        client=client,
        contextual_scope=contextual_scope,
    )
    if not packs:
        return None
    pack_ids = [pack.pack_id for pack in packs]

    def _current_pack_ids() -> list[str]:
        """Re-resolve contextual availability for a fixed registry invoke.

        A provider may retain the original ``load_tool_pack`` closure while a
        later turn binds a different App/Project context.  The schema is
        rebuilt by the exposure layer, but direct registry execution must use
        the same request-local gate instead of the build-time enum snapshot.
        """

        return [
            pack.pack_id
            for pack in available_packs_for_registry(registry, client=client)
        ]

    def load_tool_pack(pack: str) -> str:
        pack_id = str(pack or "").strip()
        target = DEFERRED_TOOL_PACKS_BY_ID.get(pack_id)
        current_pack_ids = _current_pack_ids()
        if target is None or pack_id not in current_pack_ids or not contextual_tool_pack_allowed(
            pack_id,
            client=client,
        ):
            return (
                f"Unknown tool pack: {pack_id or '(empty)'}. "
                f"Available packs: {', '.join(current_pack_ids)}"
            )
        newly_loaded = session.load(pack_id)
        tool_names = pack_tool_names_in_registry(target, registry)
        header = (
            f"Loaded tool pack `{pack_id}`."
            if newly_loaded
            else f"Tool pack `{pack_id}` was already loaded."
        )
        return "\n".join(
            [
                header,
                f"Tools now available: {', '.join(tool_names) or '(none)'}",
                "",
                target.prompt_fragment,
            ]
        )

    return ToolDefinition(
        name=LOAD_TOOL_PACK_TOOL_NAME,
        description=build_load_tool_pack_description(packs),
        function=load_tool_pack,
        parameters=[
            ToolParam(
                name="pack",
                type="string",
                description="ロードする pack の id",
                required=True,
                enum=list(pack_ids),
            )
        ],
        owner="tool_packs",
        side_effect="none",
        risk="low",
        requires_approval=False,
        supports_parallel=False,
    )


def register_load_tool_pack_tool(
    registry: Any,
    session: ToolPackSession,
    *,
    client: Any = None,
) -> bool:
    if LOAD_TOOL_PACK_TOOL_NAME in registry:
        return False
    tool_def = build_load_tool_pack_tool(registry, session, client=client)
    if tool_def is None:
        return False
    registry.register(tool_def)
    return True


def ensure_load_tool_pack_tool(registry: Any, client: Any) -> bool:
    """クライアントのセッション状態に結び付いた `load_tool_pack` を登録する。

    `build_runtime_tool_registry()` はセッション状態を持たないため、
    メタツールの登録はレジストリを保持するクライアント側で行う。
    """
    try:
        return register_load_tool_pack_tool(
            registry,
            tool_pack_session_for_client(client),
            client=client,
        )
    except Exception:  # noqa: BLE001  テスト用フェイクレジストリなどを壊さない
        return False


def _registry_tools(registry: Any) -> list[Any]:
    get_all: Callable[[], Iterable[Any]] | None = getattr(registry, "get_all", None)
    if callable(get_all):
        return list(get_all())
    if isinstance(registry, dict):
        return list(registry.values())
    return []
