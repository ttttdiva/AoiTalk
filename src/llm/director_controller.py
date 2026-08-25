"""Web 版 ChatGPT を Director として使う1ターンの純コード制御。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
from os import PathLike
from pathlib import Path, PurePosixPath
import re
from typing import Any, Awaitable, Callable, Iterable, Sequence

from ..assistant.chat_turn_persistence import ChatTurnPersistence
from ..services.agent_run_service import (
    AgentRunService,
    reset_current_agent_run_id,
    set_current_agent_run_id,
)
from ..services.project_context import (
    ProjectContextResolver,
    format_project_context_for_chat_prompt,
    reset_runtime_project_context,
    set_runtime_project_context,
)
from ..services.agent_team_v3 import (
    agent_team_scope_active,
    agent_team_v3_subagents,
    agent_team_v3_teams,
)
from ..services.app_storage import get_workspaces_root
from ..services.outbound_privacy_service import (
    current_effective_privacy_mode,
    effective_privacy_mode,
)
from ..services.agent_run_publication import ParentGitPublicationController
from ..services.agent_run_scope_service import TrustedParentRunContext
from ..security.agent_run_scope import AgentRunScope
from .chatgpt_web_provider import (
    ChatGPTWebBusyError,
    ChatGPTWebNeedsHumanError,
    ChatGPTWebProvider,
    ChatGPTWebUIInteractionError,
)
from .manager import TargetConfig, create_llm_client
from .generation_policy import generation_policy_for_profile
from .planning_policy import is_planning_operator_fanout_forbidden
from .prompts import build_unified_instructions

DIRECTOR_MARKER = "【AoiTalkへの作業依頼】"
SESSION_CONVERSATION_URL_KEY = "chatgpt_web_conversation_url"
AUDIT_TEXT_LIMIT = 20_000
OPERATOR_RESULT_LIMIT = 20_000

_CLI_TOOL_PROMPT_PROVIDERS = frozenset(
    {"antigravity-cli", "claude-cli", "codex-cli", "grok-cli"}
)
_NATIVE_TOOL_PROMPT_PROVIDERS = frozenset(
    {
        "openai",
        "openrouter",
        "deepseek",
        "deepinfra",
        "kimi",
        "gemini",
        "ollama",
        "openai_compatible_local",
        # A routing profile selects the concrete provider after this prompt
        # is built.  Its candidates are native function-tool providers unless
        # a CLI candidate is selected; CLI candidates append their own
        # textual protocol in CLILLMClient, so the native contract is safe as
        # the shared base for both candidates.
        "routing-profile",
    }
)

HUMAN_ACTION_MESSAGE = (
    "ChatGPT接続に人手が必要です。設定画面から"
    "「ChatGPT設定ブラウザを開く」でログイン状態を確認してください。"
)

ROUND_LIMIT_MESSAGE = (
    "Director と Operator の往復が設定された上限に達したため、"
    "このターンを停止しました。設定画面の最大往復回数を増やすか、"
    "依頼を小さく分けてもう一度お試しください。"
)

PLANNING_CONSOLIDATE_OPERATOR_PROMPT = (
    "Planning mode is active for this turn. Return exactly one "
    f"{DIRECTOR_MARKER} block that covers the entire user request with a single "
    "integrated plan. Do not split work across multiple operator blocks until "
    "the plan is approved and execution begins."
)

SESSION_SAVE_ERROR_MESSAGE = (
    "ChatGPT会話の引き継ぎ情報をAoiTalkへ保存できなかったため、"
    "このターンを開始せず停止しました。時間をおいてもう一度お試しください。"
)

DIRECTOR_PREAMBLE = """あなたは「AoiTalk」というアシスタントシステムの Director（統括役）です。
ユーザーとの会話はこのチャットで行われ、あなたの返答は AoiTalk が受け取って処理します。

- 調査・実装・ファイル操作などの実作業は、あなた自身ではなく AoiTalk 側の実行エージェント（Operator）が行います。
- 作業をさせたい時は、返答の中に次の見出し行から始まるブロックを書いてください。
  【AoiTalkへの作業依頼】
  - ブロックは見出し行から、次の見出しまたは返答末尾までです。
  - 互いに独立して並列実行してよい作業は、見出しを分けて複数ブロックに書いてください。1ブロックが1つの Operator に丸ごと渡ります。
  - 依頼は自然な文章で構いません。目的・対象・期待する成果物・完了条件を書いてください。
- 実行結果は、次のメッセージで【Operator実行結果】として届きます。不十分ならやり直しを依頼してください。
- 見出しを含まない返答は「ユーザーへの最終回答」として、そのままユーザーに表示されます。
- 実際に作業を依頼する時以外は【AoiTalkへの作業依頼】という文字列を絶対に書かないでください。言及する時は「作業依頼ブロック」と呼んでください。
- AoiTalk 側の主な能力: ファイルの読み書き・検索、コマンド実行、Web検索、案件（プロジェクト）管理、Docs（ドキュメントベース）操作、過去会話の検索、専門サブエージェントへの委譲。
- 最終回答は、ユーザーが読む文章としてユーザーの言語（通常は日本語）で書いてください。"""

OPERATOR_INSTRUCTIONS = """あなたは AoiTalk の Operator（実行役）です。
Director が自然文で書いた作業依頼を、利用可能なツールを使って最後まで実行してください。
依頼の目的・成果物・完了条件を優先し、必要な調査、変更、検証を行ってください。
最後の返答は Director が判断できる具体的な ExecutionReport とし、実行内容、結果、
検証、未完了事項を簡潔に報告してください。ユーザーへの会話文は書かないでください。"""

_MARKER_LINE_RE = re.compile(
    rf"(?m)^[ \t]*{re.escape(DIRECTOR_MARKER)}[ \t]*(?:\r?\n|$)"
)


def _operator_tool_prompt_protocol(client: Any) -> str:
    """Return the tool-call protocol for a Director-created Operator.

    Director builds one shared instruction block before the concrete operator
    turn starts.  API/local clients consume function calls from their provider
    response, while CLI backends parse AoiTalk's textual marker.  Keep fake or
    unknown clients on the legacy default rather than guessing that their
    provider can consume native function calls.
    """

    cli_backend = getattr(client, "cli_backend", None)
    provider_getter = getattr(cli_backend, "get_provider_name", None)
    if callable(provider_getter):
        try:
            provider = str(provider_getter() or "").strip().lower()
        except Exception:
            provider = ""
        # A client exposing a CLI backend always uses the textual parser;
        # backend display names vary (e.g. "Codex CLI" / "Claude Code").
        if provider in _CLI_TOOL_PROMPT_PROVIDERS or provider:
            return "legacy"

    provider = str(getattr(client, "provider_label", "") or "").strip().lower()
    if provider in _CLI_TOOL_PROMPT_PROVIDERS:
        return "legacy"
    if provider in _NATIVE_TOOL_PROMPT_PROVIDERS:
        return "native"

    # Some native clients intentionally do not expose provider_label.  These
    # capability/attribute checks cover Gemini, Ollama, and local OpenAI-
    # compatible clients without importing concrete classes here.
    capabilities = getattr(client, "capabilities", None)
    if bool(getattr(capabilities, "supports_tools", False)):
        return "native"
    if any(
        hasattr(client, attribute)
        for attribute in (
            "_turn_runner",
            "_native_tool_calling_enabled",
            "enable_tools",
            "tools",
        )
    ):
        return "native"
    return "legacy"


@dataclass(frozen=True)
class ParsedDirectorReply:
    progress: str
    blocks: tuple[str, ...]

    @property
    def is_final(self) -> bool:
        return not self.blocks


def parse_director_reply(reply: str) -> ParsedDirectorReply:
    """マーカー行の前を経過説明、各マーカー区間を Operator 依頼に分ける。"""
    text = str(reply or "")
    matches = list(_MARKER_LINE_RE.finditer(text))
    if not matches:
        return ParsedDirectorReply(progress="", blocks=())
    progress = text[: matches[0].start()].strip()
    blocks: list[str] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        blocks.append(text[match.end() : end].strip())
    return ParsedDirectorReply(progress=progress, blocks=tuple(blocks))


def format_reminder(project_name: str) -> str:
    name = str(project_name or "").strip() or "未選択"
    return (
        "（形式リマインダー: 作業依頼は【AoiTalkへの作業依頼】見出しのブロックで。"
        "見出しの無い返答は最終回答としてそのままユーザーへ転送されます。"
        f"現在のプロジェクト: {name}）"
    )


def format_bootstrap_history(history: Sequence[dict[str, Any]]) -> str:
    lines = ["## これまでの会話の引き継ぎ"]
    for item in history:
        role = str(item.get("role") or "")
        if role not in {"user", "assistant"}:
            continue
        label = "ユーザー" if role == "user" else "アシスタント"
        content = str(item.get("content") or "").strip()
        if content:
            lines.append(f"\n### {label}\n{content}")
    if len(lines) == 1:
        lines.append("\n（引き継ぐ会話はありません）")
    return "\n".join(lines)


def _attachment_paths(
    attachments: Any,
    *,
    project_id: str | None,
    user_id: str | None,
) -> list[Path]:
    """Web添付を、現在のproject/user workspace配下だけに解決する。"""
    if not isinstance(attachments, list):
        return []
    workspace_root = get_workspaces_root().resolve()
    allowed_prefixes: dict[str, Path] = {}
    if project_id:
        prefix = f"_projects/project_{project_id}"
        allowed_prefixes[prefix] = (workspace_root / prefix).resolve()
    if user_id:
        prefix = f"_users/user_{user_id}"
        allowed_prefixes[prefix] = (workspace_root / prefix).resolve()

    result: list[Path] = []
    for item in attachments:
        raw = item.get("path") if isinstance(item, dict) else item
        if not isinstance(raw, (str, Path)):
            continue
        text = str(raw).strip().replace("\\", "/")
        relative = PurePosixPath(text)
        if (
            not text
            or relative.is_absolute()
            or ".." in relative.parts
        ):
            continue
        matched_root: Path | None = None
        for prefix, root in allowed_prefixes.items():
            if text == prefix or text.startswith(prefix + "/"):
                matched_root = root
                break
        if matched_root is None:
            continue
        candidate = (workspace_root / Path(*relative.parts)).resolve()
        if (
            candidate.is_relative_to(matched_root)
            and candidate.is_file()
            and candidate not in result
        ):
            result.append(candidate)
    return result


def _director_files(paths: Iterable[Path]) -> list[Path]:
    image_suffixes = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
    return [path for path in paths if path.suffix.lower() in image_suffixes and path.is_file()]


def _clip(text: Any, limit: int = AUDIT_TEXT_LIMIT) -> str:
    value = str(text or "")
    return value if len(value) <= limit else value[:limit] + "\n...(省略)"


def _director_graph_records(
    config: Any,
    *,
    session_context: dict[str, Any] | None = None,
    project_metadata: dict[str, Any] | None = None,
    generation_profile: str | None = None,
) -> list[dict[str, Any]]:
    """Return the active v3 Team/Subagent graph for Director context.

    Director is a Web-only orchestration layer, not another Agent Team route.
    It therefore consumes the canonical v3 graph as read-only context and
    never projects an obsolete topology roster into its prompt.
    """

    try:
        # Director reads the canonical v3 graph directly.  Migration readers
        # and compatibility aliases must never be consulted from this runtime
        # path.
        scope_resolver = agent_team_scope_active
        teams_reader = agent_team_v3_teams
        subagents_reader = agent_team_v3_subagents

        session = dict(session_context or {})
        project = dict(project_metadata or {})
        context_tags = session.get("context_tags")
        scope = scope_resolver(
            config,
            project=project,
            session=session,
            generation_profile=generation_profile,
            app_target_id=(
                session.get("app_target_id")
                or project.get("app_target_id")
                or project.get("app_id")
                or project.get("target_id")
            ),
            development_status=(
                session.get("development_status")
                or project.get("development_status")
                or project.get("status")
            ),
            story_mode=session.get("story_mode"),
            context_tags=context_tags,
            trpg_context=session.get("trpg_context")
            if isinstance(session.get("trpg_context"), bool)
            else None,
        )
        active_team_ids = {
            str(item).strip()
            for item in (scope.get("active_team_ids") or [])
            if str(item).strip()
        }
        teams = teams_reader(config)
        subagents = {
            str(item.get("subagent_id") or ""): item
            for item in subagents_reader(config, include_disabled=False)
            if str(item.get("subagent_id") or "").strip()
        }
        records: list[dict[str, Any]] = []
        for team in teams:
            team_id = str(team.get("team_id") or "").strip()
            if not team_id or not team.get("enabled", True):
                continue
            # If the resolver has no active set (for example while an older
            # config is being loaded), do not leak all inactive teams into the
            # Director prompt.  Always-enabled teams remain visible.
            activation = team.get("activation") or {}
            mode = str(activation.get("mode") or "always").strip().lower()
            if active_team_ids and team_id not in active_team_ids:
                continue
            if not active_team_ids and mode != "always":
                continue
            children: list[dict[str, Any]] = []
            for subagent_id in team.get("subagent_ids") or []:
                subagent = subagents.get(str(subagent_id).strip())
                if not subagent or not subagent.get("enabled", True):
                    continue
                children.append(
                    {
                        "name": str(subagent.get("name") or subagent_id).strip(),
                        "description": str(subagent.get("description") or "").strip(),
                    }
                )
            if children:
                records.append(
                    {
                        "name": str(team.get("name") or team_id).strip(),
                        "description": str(team.get("description") or "").strip(),
                        "subagents": children,
                    }
                )
        return records
    except Exception as exc:
        # A malformed/migrating config must not prevent Director from serving
        # the user.  Returning an empty graph is explicit and keeps obsolete
        # topology identifiers out of the prompt.
        print(f"[DirectorController] Agent Team graph resolution skipped: {exc}")
        return []


def _format_director_graph(records: Sequence[dict[str, Any]]) -> str:
    """Render a compact, user-facing graph (names and short uses only)."""

    if not records:
        return (
            "## 利用可能なAgent Team\n"
            "現在利用可能なTeam/Subagent情報はありません。必要なら通常の機能を直接使ってください。"
        )
    lines = ["## 利用可能なAgent Team"]
    for team in records:
        name = str(team.get("name") or "Team").strip()
        description = str(team.get("description") or "").strip()
        lines.append(f"### {name}" + (f" — {description}" if description else ""))
        for subagent in team.get("subagents") or []:
            sub_name = str(subagent.get("name") or "Subagent").strip()
            sub_description = str(subagent.get("description") or "").strip()
            detail = sub_description or "用途は依頼に応じて判断"
            lines.append(f"- {sub_name}: {detail}")
    lines.append(
        "適切なTeam/Subagentがある場合だけ作業依頼ブロックで委譲し、Web検索などの共通機能は直接使ってください。"
    )
    return "\n".join(lines)


ProviderFactory = Callable[[Any], ChatGPTWebProvider]
OperatorRunner = Callable[[str, int, int], Awaitable[str]]
ProgressCallback = Callable[[str, dict[str, Any]], Awaitable[None] | None]


class DirectorTurnController:
    def __init__(
        self,
        *,
        config: Any,
        session_id: str | None,
        user_id: str | None,
        project_id: str | None,
        project_name: str,
        parent_run_id: str | None,
        generation_profile: str | None = None,
        chat_persistence: ChatTurnPersistence | None = None,
        agent_run_service: AgentRunService | None = None,
        provider_factory: ProviderFactory = ChatGPTWebProvider,
        operator_runner: OperatorRunner | None = None,
        progress_callback: ProgressCallback | None = None,
        session_context: dict[str, Any] | None = None,
        project_metadata: dict[str, Any] | None = None,
        repository_root: str | PathLike[str] | None = None,
        run_scope: AgentRunScope | None = None,
        publication_controller: ParentGitPublicationController | None = None,
        trusted_parent_context: Any | None = None,
        qa_browser_coordinator: Any | None = None,
        require_parent_scope: bool = False,
    ) -> None:
        self.config = config
        self.session_id = session_id
        self.user_id = user_id
        self.project_id = project_id
        self.project_name = project_name
        self.parent_run_id = parent_run_id
        self.generation_profile = generation_profile
        self.chat_persistence = chat_persistence or ChatTurnPersistence()
        self.agent_run_service = agent_run_service
        self.provider_factory = provider_factory
        self.operator_runner = operator_runner
        self.progress_callback = progress_callback
        self.session_context = dict(session_context or {})
        self.project_metadata = dict(project_metadata or {})
        if trusted_parent_context is not None and not isinstance(
            trusted_parent_context,
            TrustedParentRunContext,
        ):
            raise TypeError(
                "trusted_parent_context must come from the parent scope factory"
            )
        self.trusted_parent_context: TrustedParentRunContext | None = (
            trusted_parent_context
        )
        self.qa_browser_coordinator = qa_browser_coordinator
        self.require_parent_scope = bool(require_parent_scope)
        if publication_controller is not None and (
            repository_root is not None
            or run_scope is not None
            or trusted_parent_context is not None
        ):
            raise ValueError(
                "publication_controller cannot be combined with repository scope arguments"
            )
        self.publication_controller = publication_controller
        if self.publication_controller is None and (
            repository_root is not None
            or run_scope is not None
            or trusted_parent_context is not None
        ):
            self.publication_controller = ParentGitPublicationController(
                repository_root,
                run_scope=run_scope,
                run_id=parent_run_id,
                agent_run_service=agent_run_service,
                parent_run_id=parent_run_id,
                trusted_parent_context=trusted_parent_context,
            )
        # 複数Operatorが同時に親runへ書き込んでもsequence採番を競合させない。
        self._parent_event_lock = asyncio.Lock()
        self.max_rounds = max(
            1,
            int(config.get("chatgpt_web.max_rounds_per_turn", 20) or 20),
        )

    def _agent_graph_records(self) -> list[dict[str, Any]]:
        return _director_graph_records(
            self.config,
            session_context=self.session_context,
            project_metadata=self.project_metadata,
            generation_profile=self.generation_profile,
        )

    def _agent_graph_prompt(self) -> str:
        return _format_director_graph(self._agent_graph_records())

    def _operator_agent_metadata(self) -> dict[str, Any]:
        """Canonical graph metadata for an unselected Director Operator.

        A Director work block is natural language and does not identify a
        particular Subagent.  Keep the child run explicit rather than falling
        back to historical topology keys; the graph snapshot lets the
        run inspector explain what was available at dispatch time.
        """

        # A caller may provide an explicit canonical selection when a future
        # Director surface gains one.  Until then, natural-language blocks
        # remain intentionally unselected and are recorded as null.
        team_id = str(self.session_context.get("team_id") or "").strip() or None
        subagent_id = (
            str(self.session_context.get("subagent_id") or "").strip() or None
        )
        execution_profile_id = (
            str(self.session_context.get("execution_profile_id") or "").strip() or None
        )
        return {
            "team_id": team_id,
            "subagent_id": subagent_id,
            "llm_profile_id": None,
            "execution_profile_id": execution_profile_id,
            "agent_graph": self._agent_graph_records(),
            "selection": "selected" if any((team_id, subagent_id, execution_profile_id)) else "unselected",
        }

    async def run(
        self,
        user_message: str,
        *,
        attachments: Any = None,
        history: Sequence[dict[str, Any]] = (),
    ) -> str:
        mode = effective_privacy_mode(
            self.config,
            session_context=self.session_context or None,
            project_metadata=self.project_metadata or None,
        )
        if mode != "direct" or current_effective_privacy_mode(self.config) != "direct":
            raise RuntimeError(
                "保護クラウド / ローカル限定モードでは、Web版ChatGPT Directorは使用できません。"
            )
        if self.publication_controller is not None:
            # Capture the immutable Git baseline before the Director can start
            # any Operator child.  The controller is parent-owned and never
            # passed to a worker tool registry.
            await self.publication_controller.start_async()
        authorized_project_id: str | None = None
        if attachments and self.project_id and self.user_id:
            try:
                project_context = await ProjectContextResolver().get_project_context(
                    str(self.project_id),
                    user_id=str(self.user_id),
                )
            except Exception:
                project_context = None
            resolved_project_id = str(
                (project_context or {}).get("id") or ""
            ).strip()
            if resolved_project_id == str(self.project_id):
                authorized_project_id = resolved_project_id
        paths = _attachment_paths(
            attachments,
            project_id=authorized_project_id,
            user_id=self.user_id,
        )
        attachment_note = ""
        if paths:
            attachment_note = "\n\n添付ファイル（Operator が原本を参照できます）:\n" + "\n".join(
                f"- {path}" for path in paths
            )
        reminder = format_reminder(self.project_name)
        provider = self.provider_factory(self.config)
        try:
            async with provider.operation("director"):
                context = await self.chat_persistence.load_session_context(
                    self.session_id
                )
                conversation_url = str(
                    context.get(SESSION_CONVERSATION_URL_KEY) or ""
                ).strip()
                await provider.open_conversation(conversation_url or None)
                if conversation_url and not provider.current_conversation_url():
                    # 削除済み等で URL が新規画面へ戻った場合はブートストラップし直す。
                    conversation_url = ""
                    await provider.open_conversation(None)

                if not conversation_url:
                    bootstrap = (
                        DIRECTOR_PREAMBLE
                        + "\n\n"
                        + self._agent_graph_prompt()
                        + "\n\n"
                        + format_bootstrap_history(history)
                    )
                    await self._send_and_record(
                        provider,
                        bootstrap,
                        event_label="bootstrap",
                    )
                    conversation_url = provider.current_conversation_url() or ""
                    if not conversation_url:
                        raise ChatGPTWebUIInteractionError(
                            "初回送信後のChatGPT会話URLを確認できません。"
                        )
                    saved = await self.chat_persistence.update_session_context(
                        self.session_id,
                        {SESSION_CONVERSATION_URL_KEY: conversation_url},
                    )
                    if not saved:
                        await self._record_parent_event(
                            "director.session_save_failed",
                            message=SESSION_SAVE_ERROR_MESSAGE,
                            payload={"conversation_url": conversation_url},
                        )
                        return SESSION_SAVE_ERROR_MESSAGE

                prompt = (
                    f"{reminder}\n\n"
                    f"{self._agent_graph_prompt()}\n\n"
                    f"{user_message}{attachment_note}"
                )
                reply = await self._send_and_record(
                    provider,
                    prompt,
                    files=_director_files(paths),
                    event_label="user",
                    round_index=1,
                )

                for round_index in range(1, self.max_rounds + 1):
                    parsed = parse_director_reply(reply)
                    await self._record_parent_event(
                        "director.reply_received",
                        message=(
                            f"Director応答を受信（作業依頼 {len(parsed.blocks)}件）"
                        ),
                        payload={
                            "round": round_index,
                            "marker_count": len(parsed.blocks),
                            "progress_preview": _clip(parsed.progress, 1_200),
                            "reply": _clip(reply),
                        },
                    )
                    if parsed.is_final:
                        await self._record_parent_event(
                            "director.final_answer",
                            message="Directorが最終回答を作成",
                            payload={"round": round_index, "answer": _clip(reply)},
                        )
                        return reply

                    if (
                        is_planning_operator_fanout_forbidden()
                        and len(parsed.blocks) > 1
                    ):
                        await self._record_parent_event(
                            "director.planning_consolidate_requested",
                            message=(
                                "Planning中のため複数Operator依頼を統合要求"
                            ),
                            payload={
                                "round": round_index,
                                "operator_total": len(parsed.blocks),
                            },
                        )
                        reply = await self._send_and_record(
                            provider,
                            f"{reminder}\n\n{PLANNING_CONSOLIDATE_OPERATOR_PROMPT}",
                            event_label="planning_consolidate",
                            round_index=round_index,
                        )
                        continue

                    for index, block in enumerate(parsed.blocks, start=1):
                        await self._record_parent_event(
                            "director.operator_started",
                            message=(
                                f"Operator {index}/{len(parsed.blocks)} を実行開始"
                            ),
                            payload={
                                "operator_index": index,
                                "operator_total": len(parsed.blocks),
                                "request_preview": _clip(block, 1_200),
                            },
                        )
                    raw_reports = await asyncio.gather(
                        *(
                            self._run_operator(block, index, len(parsed.blocks))
                            for index, block in enumerate(parsed.blocks, start=1)
                        ),
                        return_exceptions=True,
                    )
                    reports = [
                        (
                            f"Operator実行に失敗しました: {report}"
                            if isinstance(report, Exception)
                            else str(report)
                        )
                        for report in raw_reports
                    ]
                    for index, report in enumerate(reports, start=1):
                        failed = report.startswith("Operator実行に失敗しました:")
                        await self._record_parent_event(
                            (
                                "director.operator_failed"
                                if failed
                                else "director.operator_succeeded"
                            ),
                            message=(
                                f"Operator {index}/{len(reports)} "
                                + ("の実行に失敗" if failed else "が実行完了")
                            ),
                            payload={
                                "operator_index": index,
                                "operator_total": len(reports),
                                "result_preview": _clip(report, 1_200),
                            },
                        )
                    if round_index >= self.max_rounds:
                        await self._record_parent_event(
                            "director.round_limit_reached",
                            message=ROUND_LIMIT_MESSAGE,
                            payload={"round": round_index, "limit": self.max_rounds},
                        )
                        return ROUND_LIMIT_MESSAGE
                    report_prompt = self._format_operator_reports(
                        reminder,
                        parsed.blocks,
                        reports,
                    )
                    reply = await self._send_and_record(
                        provider,
                        report_prompt,
                        event_label="operator_results",
                        round_index=round_index + 1,
                    )
        # DOM/Playwright failures are ordinary provider errors.  Keep this
        # guard explicit so a future provider hierarchy cannot accidentally
        # route them through the human-escalation branch below.
        except ChatGPTWebUIInteractionError:
            raise
        except ChatGPTWebNeedsHumanError:
            await self._record_parent_event(
                "director.needs_human",
                message=HUMAN_ACTION_MESSAGE,
                payload={},
            )
            return HUMAN_ACTION_MESSAGE
        except ChatGPTWebBusyError as exc:
            message = str(exc)
            await self._record_parent_event(
                "director.busy",
                message=message,
                payload={},
            )
            return message
        finally:
            # A scoped background server/REPL is a descendant of the parent
            # AgentRun even after an Operator report has returned.  Drain it
            # at the actual Director lifecycle boundary on success, error, or
            # cancellation; publication preflight remains a second safety
            # gate for callers that publish without running this controller.
            lifecycle_scope = (
                self.trusted_parent_context.scope
                if self.trusted_parent_context is not None
                else getattr(self.publication_controller, "run_scope", None)
            )
            if lifecycle_scope is not None:
                try:
                    from ..services.agent_run_background_jobs import (
                        finish_agent_run_background_jobs,
                    )

                    await asyncio.to_thread(
                        finish_agent_run_background_jobs,
                        lifecycle_scope,
                    )
                except Exception as exc:
                    # Do not hide the Director result, but keep the lifecycle
                    # failure visible to the parent audit stream/log.
                    try:
                        await self._record_parent_event(
                            "director.background_cleanup_failed",
                            message=str(exc),
                            payload={"run_id": lifecycle_scope.run_id},
                        )
                    except Exception:
                        pass
                    raise RuntimeError(
                        "Director background descendants could not be drained"
                    ) from exc

    async def _send_and_record(
        self,
        provider: ChatGPTWebProvider,
        prompt: str,
        *,
        files: list[Path] | None = None,
        event_label: str,
        round_index: int | None = None,
    ) -> str:
        await self._record_parent_event(
            "director.round_started",
            message=f"Directorへ送信（{event_label}）",
            payload={
                "round": round_index,
                "kind": event_label,
                "prompt": _clip(prompt),
                "files": [str(path) for path in files or []],
            },
        )
        reply = await provider.send(prompt, files=files)
        await self._record_parent_event(
            "director.raw_reply",
            message=f"Directorから受信（{event_label}）",
            payload={
                "round": round_index,
                "kind": event_label,
                "reply": _clip(reply),
            },
        )
        return reply

    async def _run_operator(self, block: str, index: int, total: int) -> str:
        if (
            self.require_parent_scope
            and self.trusted_parent_context is None
        ):
            await self._record_parent_event(
                "director.operator_scope_required",
                message="Operator実行にはparent AgentRunScopeが必要です",
                payload={"operator_index": index, "operator_total": total},
            )
            return (
                "Operator実行を停止しました: 親Controllerが明示的な"
                "repository scopeを提供していません"
            )
        if self.operator_runner is not None:
            if self.trusted_parent_context is None:
                return str(await self.operator_runner(block, index, total))
            from ..security.agent_run_scope import run_scope_context

            with run_scope_context(self.trusted_parent_context.scope):
                return str(await self.operator_runner(block, index, total))
        return await self._run_default_operator(block, index, total)

    async def _run_default_operator(
        self,
        block: str,
        index: int,
        total: int,
    ) -> str:
        if self.publication_controller is not None:
            await self.publication_controller.start_async()
        service = self.agent_run_service
        child_run_id: str | None = None
        operator_trusted_context = self.trusted_parent_context
        operator_qa_coordinator = self.qa_browser_coordinator
        if (
            self.require_parent_scope
            and operator_trusted_context is None
        ):
            # A production Director Operator never receives repository-capable
            # tools through the legacy unscoped host path.  Ordinary test and
            # compatibility embedders may inject a custom operator runner or
            # fake service, but the real AgentRun path requires the explicit
            # parent repository setting/factory.
            await self._record_parent_event(
                "director.operator_scope_required",
                message="Operator実行にはparent AgentRunScopeが必要です",
                payload={"operator_index": index, "operator_total": total},
            )
            return (
                "Operator実行を停止しました: 親Controllerが明示的な"
                "repository scopeを提供していません"
            )
        provider_name = str(self.config.get("llm_provider", "") or "")
        model_name = str(self.config.get("llm_model", "") or "")
        if service is not None and self.parent_run_id:
            operator_metadata = {
                **self._operator_agent_metadata(),
                "operator_index": index,
                "operator_total": total,
                "request": _clip(block),
            }
            if self.trusted_parent_context is not None:
                # Keep the immutable root baseline attached to the durable
                # Director→Operator edge.  The child run itself is still
                # parented by ``self.parent_run_id`` in AgentRunService.
                operator_metadata.update(
                    self.trusted_parent_context.child_metadata()
                )
            publication_state = (
                self.publication_controller.state
                if self.publication_controller is not None
                else None
            )
            if publication_state is not None:
                operator_metadata["git_publication"] = publication_state.as_dict()
            child = await service.create_run(
                parent_run_id=self.parent_run_id,
                objective=block,
                run_type="director_operator",
                title=f"Director Operator {index}/{total}",
                provider=provider_name,
                model=model_name,
                metadata=operator_metadata,
            )
            child_run_id = str((child or {}).get("id") or "") or None
            if child_run_id:
                if self.trusted_parent_context is not None:
                    # The Operator is an explicitly issued child of the
                    # Director parent.  Bind its ID while preserving the
                    # root parent_run_id/scope/baseline in all child metadata.
                    operator_trusted_context = (
                        self.trusted_parent_context.with_child_run(child_run_id)
                    )
                await service.create_edge(
                    parent_run_id=self.parent_run_id,
                    child_run_id=child_run_id,
                    purpose="director_operator",
                    metadata={"index": index, "total": total},
                )
                await service.mark_running(
                    child_run_id,
                    message="Directorの作業依頼を実行中",
                    provider=provider_name,
                    model=model_name,
                )

        run_token = (
            set_current_agent_run_id(child_run_id) if child_run_id else None
        )
        run_scope_token = None
        if operator_trusted_context is not None:
            # The Operator itself is a scoped leaf, not merely a carrier of
            # scope metadata for a later Agent Team child.  Bind the immutable
            # scope around its direct tools/CLI/background calls as well.
            from ..security.agent_run_scope import bind_run_scope

            run_scope_token = bind_run_scope(operator_trusted_context.scope)
        target_config = TargetConfig(
            self.config,
            {
                "runtime.register_process_cleanup": False,
                "conversation_state.mode": "stateless",
                "memory": {"enabled": False},
                "use_tools": True,
                # DirectorのOperatorは既存Workerへ再委譲できるフル構成にする。
                "agent_team.delegation_enabled": True,
            },
        )
        client: Any = None
        project_context_token = None
        original_project_resolver: Any = None
        original_project_resolver_sync: Any = None
        try:
            client = create_llm_client(target_config)
            client.generation_policy = generation_policy_for_profile(
                self.generation_profile
            )
            if self.project_id:
                client.current_project_id = self.project_id
            client.current_include_project_context = True
            client.external_persistence_enabled = True
            if hasattr(client, "set_session_context") and self.user_id:
                client.set_session_context(
                    user_id=self.user_id,
                    metadata={"platform": "web", "actor_type": "director_operator"},
                )

            if (
                operator_trusted_context is not None
                or operator_qa_coordinator is not None
            ):
                # ``AgentLLMClient`` resolves the selected Project afresh for
                # each turn.  Wrap that server-side resolver so the opaque
                # parent marker survives the DB lookup without exposing a
                # model/project path as a scope authority.
                original_project_resolver = getattr(
                    client,
                    "_resolve_project_context",
                    None,
                )
                if callable(original_project_resolver):

                    async def _resolve_operator_project_context(
                        _resolver: Any = original_project_resolver,
                    ) -> dict[str, Any]:
                        resolved = _resolver()
                        if inspect.isawaitable(resolved):
                            resolved = await resolved
                        base_context = resolved if isinstance(resolved, dict) else {}
                        if operator_trusted_context is not None:
                            base_context = operator_trusted_context.inject_into_project_context(
                                base_context
                            )
                        if operator_qa_coordinator is not None:
                            base_context = operator_qa_coordinator.inject_into_project_context(
                                base_context
                            )
                        return base_context

                    try:
                        client._resolve_project_context = (
                            _resolve_operator_project_context
                        )
                    except Exception:
                        # The ambient context below still covers lightweight
                        # clients that do not permit instance attributes.
                        original_project_resolver = None
                original_project_resolver_sync = getattr(
                    client,
                    "_resolve_project_context_sync",
                    None,
                )
                if callable(original_project_resolver_sync):

                    def _resolve_operator_project_context_sync(
                        _resolver: Any = original_project_resolver_sync,
                    ) -> dict[str, Any]:
                        resolved = _resolver()
                        base_context = resolved if isinstance(resolved, dict) else {}
                        if operator_trusted_context is not None:
                            base_context = operator_trusted_context.inject_into_project_context(
                                base_context
                            )
                        if operator_qa_coordinator is not None:
                            base_context = operator_qa_coordinator.inject_into_project_context(
                                base_context
                            )
                        return base_context

                    try:
                        client._resolve_project_context_sync = (
                            _resolve_operator_project_context_sync
                        )
                    except Exception:
                        original_project_resolver_sync = None
                operator_context: dict[str, Any] = {}
                if operator_trusted_context is not None:
                    operator_context = operator_trusted_context.inject_into_project_context(
                        operator_context
                    )
                if operator_qa_coordinator is not None:
                    operator_context = operator_qa_coordinator.inject_into_project_context(
                        operator_context
                    )
                project_context_token = set_runtime_project_context(operator_context)

            base_instructions = build_unified_instructions(
                character_name=str(
                    getattr(client, "character_name", "Assistant") or "Assistant"
                ),
                config=target_config,
                include_static_tool_reference=False,
                tool_protocol=_operator_tool_prompt_protocol(client),
            )
            if hasattr(client, "set_system_prompt"):
                client.set_system_prompt(
                    f"{base_instructions}\n\n{OPERATOR_INSTRUCTIONS}"
                )

            project_block = ""
            resolver = getattr(client, "_resolve_project_context", None)
            if callable(resolver):
                resolved = resolver()
                if inspect.isawaitable(resolved):
                    resolved = await resolved
                project_block = format_project_context_for_chat_prompt(
                    resolved if isinstance(resolved, dict) else None
                )
            operator_prompt = (
                f"## Directorからの作業依頼\n{block}"
                + (f"\n\n{project_block}" if project_block else "")
            )

            async def stream_callback(event_type: str, data: dict[str, Any]) -> None:
                if service is not None and child_run_id:
                    await service.record_event(
                        child_run_id,
                        f"stream.{event_type}",
                        status=str(data.get("status") or "") or None,
                        message=str(data.get("message") or "") or None,
                        payload=data,
                    )
                # Operator自身の本文・思考・stream lifecycleは子runだけに残す。
                # 親へ複製するとDirectorの本文表示や停止時の保存内容へ混入する。
                private_operator_events = {
                    "stream_start",
                    "stream_token",
                    "stream_end",
                    "stream_cancelled",
                    "response",
                    "conversation_persisted",
                    "assistant_text",
                    "thinking",
                }
                if event_type in private_operator_events:
                    return
                message = str(data.get("message") or "").strip()
                operator_payload = {
                    "actor_key": f"director_operator_{index}",
                    "actor_type": "director_operator",
                    "actor_label": f"Operator {index}/{total}",
                    "child_run_id": child_run_id,
                    "operator_index": index,
                    "operator_total": total,
                    "status": str(data.get("status") or "").strip() or None,
                    "message": message or None,
                }
                for key in ("tool", "operation_id", "tool_call_id"):
                    value = str(data.get(key) or "").strip()
                    if value:
                        operator_payload[key] = value
                await self._persist_parent_event(
                    f"stream.{event_type}",
                    status=str(data.get("status") or "") or None,
                    message=message or None,
                    payload=operator_payload,
                )
                await self._notify_progress(event_type, operator_payload)

            generator = getattr(client, "generate_response_async", None)
            if not callable(generator):
                raise RuntimeError(
                    f"Operator provider {provider_name} は非同期生成に対応していません"
                )
            supports_stream_callback = True
            try:
                signature = inspect.signature(generator)
                parameters = signature.parameters
                supports_stream_callback = (
                    "stream_callback" in parameters
                    or any(
                        parameter.kind == inspect.Parameter.VAR_KEYWORD
                        for parameter in parameters.values()
                    )
                )
            except (TypeError, ValueError):
                pass
            if supports_stream_callback:
                result = await generator(
                    operator_prompt,
                    stream_callback=stream_callback,
                )
            else:
                result = await generator(operator_prompt)
            report = str(result or "")
            if service is not None and child_run_id:
                await service.complete_run(
                    child_run_id,
                    result={"output": _clip(report, OPERATOR_RESULT_LIMIT)},
                    message="Directorの作業依頼を完了",
                )
            return report
        except asyncio.CancelledError:
            if service is not None and child_run_id:
                try:
                    await asyncio.shield(
                        service.cancel_run(
                            child_run_id,
                            message="Directorの作業依頼を停止",
                        )
                    )
                except Exception as exc:
                    print(
                        "[DirectorController] Operator子runの停止記録に失敗: "
                        f"{exc}"
                    )
            raise
        except Exception as exc:
            if service is not None and child_run_id:
                await service.fail_run(
                    child_run_id,
                    str(exc),
                    result={"error": str(exc)},
                )
            return f"Operator実行に失敗しました: {exc}"
        finally:
            if client is not None:
                cleanup = getattr(client, "cleanup", None)
                if callable(cleanup):
                    result = cleanup()
                    if inspect.isawaitable(result):
                        await result
                if original_project_resolver is not None:
                    try:
                        client._resolve_project_context = original_project_resolver
                    except Exception:
                        pass
                if original_project_resolver_sync is not None:
                    try:
                        client._resolve_project_context_sync = (
                            original_project_resolver_sync
                        )
                    except Exception:
                        pass
            if project_context_token is not None:
                reset_runtime_project_context(project_context_token)
            if run_scope_token is not None:
                from ..security.agent_run_scope import reset_run_scope

                reset_run_scope(run_scope_token)
            if run_token is not None:
                reset_current_agent_run_id(run_token)

    def publication_preflight(self, **kwargs: Any) -> Any:
        """Evaluate the parent-only Git publication gate for this Director run."""

        if self.publication_controller is None:
            raise RuntimeError(
                "repository publication is unavailable without an explicit parent run scope"
            )
        return self.publication_controller.evaluate(**kwargs)

    def publish_changes(self, transport: Callable[[Any], Any], **kwargs: Any) -> Any:
        """Invoke a parent commit/push transport only after preflight approval."""

        if self.publication_controller is None:
            raise RuntimeError(
                "repository publication is unavailable without an explicit parent run scope"
            )
        return self.publication_controller.publish(transport, **kwargs)

    async def publish_changes_async(
        self,
        transport: Callable[[Any], Any],
        **kwargs: Any,
    ) -> Any:
        """Async parent publication transport counterpart."""

        if self.publication_controller is None:
            raise RuntimeError(
                "repository publication is unavailable without an explicit parent run scope"
            )
        return await self.publication_controller.publish_async(transport, **kwargs)

    def _format_operator_reports(
        self,
        reminder: str,
        blocks: Sequence[str],
        reports: Sequence[str],
    ) -> str:
        sections = [reminder]
        total = len(reports)
        for index, (block, report) in enumerate(zip(blocks, reports), start=1):
            preview = " ".join(block.split())[:100]
            sections.append(
                f"【Operator実行結果 {index}/{total}】（{preview}）\n{report}"
            )
        return "\n\n".join(sections)

    async def _record_parent_event(
        self,
        event_type: str,
        *,
        message: str,
        payload: dict[str, Any],
    ) -> None:
        event_payload = {
            "actor_key": "director",
            "actor_type": "director",
            "actor_label": "Director",
            **payload,
        }
        await self._persist_parent_event(
            event_type,
            message=message,
            payload=event_payload,
        )
        live_payload = {
            "status": event_type,
            "message": message,
            "director_event_type": event_type,
            "actor_key": "director",
            "actor_type": "director",
            "actor_label": "Director",
        }
        for key in (
            "round",
            "kind",
            "marker_count",
            "operator_index",
            "operator_total",
            "child_run_id",
        ):
            if key in payload:
                live_payload[key] = payload[key]
        await self._notify_progress("status_update", live_payload)

    async def _persist_parent_event(
        self,
        event_type: str,
        *,
        status: str | None = None,
        message: str | None = None,
        payload: dict[str, Any],
    ) -> None:
        service = self.agent_run_service
        if service is None or not self.parent_run_id:
            return
        async with self._parent_event_lock:
            await service.record_event(
                self.parent_run_id,
                event_type,
                status=status,
                message=message,
                payload=payload,
            )

    async def _notify_progress(
        self,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        callback = self.progress_callback
        if callback is None:
            return
        try:
            result = callback(event_type, dict(payload))
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            # 進捗表示の失敗で Director / Operator 本体を中断しない。
            print(f"[DirectorTurnController] 進捗イベント送信エラー: {exc}")
