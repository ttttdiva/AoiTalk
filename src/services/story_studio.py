"""Scenario Studio のドメインサービスと副作用のない組み立て関数。

このモジュールは Docs のノード投影や旧 scenario テーブルを参照しない。API 層から
呼び出すサービスに加え、route / graph / context / model の決定的な関数を公開する。
"""

from __future__ import annotations

import hashlib
import inspect
import asyncio
import copy
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Literal, Mapping, Sequence, get_args
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.database import get_db_session

from ..memory.models.story import (
    StoryCharacter,
    StoryEpisode,
    StoryEpisodeRevision,
    StoryGenerationJob,
    StoryLink,
    StoryNote,
    StoryRulebook,
    StorySearchIndex,
    StoryWork,
    StoryWorkCharacter,
    StoryWorkRulebook,
    StoryWritingSession,
)

logger = logging.getLogger(__name__)

# §6.2 のリビジョン origin 語彙。API 層の入力検証はこの型を共有する。
StoryRevisionOrigin = Literal[
    "import",
    "manual",
    "checkpoint",
    "pre_ai",
    "ai_generate",
    "ai_edit",
    "pre_restore",
    "restore",
    "auto",
]
STORY_REVISION_ORIGINS: frozenset[str] = frozenset(get_args(StoryRevisionOrigin))

# AI 適用の origin。直前に pre_ai を積み、結果は常に 1 本残す（§6.2）。
STORY_AI_REVISION_ORIGINS: frozenset[str] = frozenset({"ai_generate", "ai_edit"})

# §6.2 の created_by 語彙。
StoryRevisionAuthor = Literal["user", "ai"]

# §6.2 オートセーブ: 前回リビジョンからこの間隔を超えたときだけ origin='auto' を 1 本積む。
STORY_AUTO_REVISION_INTERVAL = timedelta(minutes=15)

# §8.4 の自動要約。暴走出力が後続の文脈（⑤-b）を圧迫しないよう上限を設ける。
STORY_SUMMARY_MAX_CHARS = 400


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _sid(value: Any) -> str:
    raw = _get(value, "id", value)
    return str(raw)


def _uuid(value: Any) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _as_model_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        raw = dict(value)
    else:
        raw = {
        key: getattr(value, key)
        for key in ("provider", "provider_label", "model", "model_name", "base_url", "api_key", "api_key_ref", "credential_profile", "reasoning_effort", "effort", "inherit")
        if hasattr(value, key)
        }
    for target, aliases in {
        "provider": ("provider", "provider_label"),
        "model": ("model", "model_name"),
        "reasoning_effort": ("reasoning_effort", "effort"),
    }.items():
        if raw.get(target) in (None, ""):
            for alias in aliases[1:]:
                if raw.get(alias) not in (None, ""):
                    raw[target] = raw[alias]
                    break
    profile = raw.get("credential_profile")
    if raw.get("api_key_ref") in (None, "") and profile not in (None, ""):
        if isinstance(profile, Mapping):
            profile = profile.get("id") or profile.get("credential_profile_id")
        else:
            profile = getattr(profile, "id", profile)
        if profile not in (None, ""):
            raw["api_key_ref"] = profile
    return raw


def _clean_model_spec(value: Any) -> dict[str, Any]:
    """モデル指定を秘密情報なしの軽量 DTO に正規化する。"""

    raw = _as_model_dict(value)
    result: dict[str, Any] = {}
    for key in ("provider", "model", "base_url", "api_key_ref", "reasoning_effort"):
        item = raw.get(key)
        if key == "api_key_ref" and item in (None, ""):
            item = raw.get("credential_profile")
        if item not in (None, ""):
            result[key] = str(item)
    # 既存設定の api_key は値を返さず、保存もしない。
    if "api_key" in raw and raw.get("api_key_ref") and "api_key_ref" not in result:
        result["api_key_ref"] = str(raw["api_key_ref"])
    return result


def story_user_choices(ui_state: Mapping[str, Any] | None) -> dict[str, str]:
    """Return the branch choices stored in ``story_works.ui_state``.

    The public schema stores the selected route as an ordered ``current_route``
    array.  Older callers used a ``choices`` mapping, so reads accept both
    shapes while all new writes can remain canonical and array-based.
    """

    state = dict(ui_state or {})
    current_route = state.get("current_route")
    if isinstance(current_route, Mapping):
        return {
            str(key): str(value)
            for key, value in current_route.items()
            if value not in (None, "")
        }
    if isinstance(current_route, list):
        return {
            str(current_route[index]): str(current_route[index + 1])
            for index in range(len(current_route) - 1)
            if current_route[index] not in (None, "") and current_route[index + 1] not in (None, "")
        }
    choices = state.get("choices")
    if isinstance(choices, Mapping):
        return {str(key): str(value) for key, value in choices.items() if value not in (None, "")}
    return {}


@dataclass(frozen=True)
class StoryModelResolution:
    provider: str = ""
    model: str = ""
    base_url: str = ""
    api_key_ref: str = ""
    reasoning_effort: str = ""
    layer: str = "main_llm_inherited"

    def to_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "api_key_ref": self.api_key_ref,
            "reasoning_effort": self.reasoning_effort,
            "layer": self.layer,
        }


def _resolve_story_model_impl(
    runtime_override: Mapping[str, Any] | None = None,
    work_override: Mapping[str, Any] | None = None,
    writing_class: Mapping[str, Any] | None = None,
    main_llm: Mapping[str, Any] | Any | None = None,
) -> dict[str, str]:
    """§8.8 の 3 層解決を行う純関数。

    層をまたいだフィールド単位のマージは行わず、一つの層を丸ごと採用する。
    """

    def provided(value: Any) -> bool:
        return isinstance(value, Mapping) and any(
            value.get(key) not in (None, "")
            for key in ("provider", "model", "base_url", "api_key_ref", "reasoning_effort")
        )

    if provided(runtime_override):
        selected, layer = runtime_override, "runtime"
    elif provided(work_override):
        selected, layer = work_override, "work"
    else:
        writing = dict(writing_class or {})
        if writing.get("inherit", True):
            selected, layer = main_llm, "main_llm_inherited"
        elif provided(writing):
            selected, layer = writing, "writing_class"
        else:
            selected, layer = main_llm, "main_llm_inherited"
    cleaned = _clean_model_spec(selected)
    return StoryModelResolution(
        provider=cleaned.get("provider", ""),
        model=cleaned.get("model", ""),
        base_url=cleaned.get("base_url", ""),
        api_key_ref=cleaned.get("api_key_ref", ""),
        reasoning_effort=cleaned.get("reasoning_effort", ""),
        layer=layer,
    ).to_dict()


class StoryModelResolver:
    """全 Story AI 経路が共有するモデル解決入口。"""

    @staticmethod
    def resolve(
        runtime_override: Mapping[str, Any] | None = None,
        work_override: Mapping[str, Any] | None = None,
        writing_class: Mapping[str, Any] | None = None,
        main_llm: Mapping[str, Any] | Any | None = None,
    ) -> dict[str, str]:
        return _resolve_story_model_impl(
            runtime_override,
            work_override,
            writing_class,
            main_llm,
        )

    def __call__(
        self,
        runtime_override: Mapping[str, Any] | None = None,
        work_override: Mapping[str, Any] | None = None,
        writing_class: Mapping[str, Any] | None = None,
        main_llm: Mapping[str, Any] | Any | None = None,
    ) -> dict[str, str]:
        return self.resolve(runtime_override, work_override, writing_class, main_llm)


def resolve_story_model(
    runtime_override: Mapping[str, Any] | None = None,
    work_override: Mapping[str, Any] | None = None,
    writing_class: Mapping[str, Any] | None = None,
    main_llm: Mapping[str, Any] | Any | None = None,
) -> dict[str, str]:
    """後方互換の関数入口。実装は StoryModelResolver.resolve のみ。"""

    return StoryModelResolver.resolve(
        runtime_override,
        work_override,
        writing_class,
        main_llm,
    )


def _sorted_links(links: Iterable[Any], from_id: str) -> list[Any]:
    children = [link for link in links if _sid(_get(link, "from_episode_id")) == from_id]
    return sorted(
        children,
        key=lambda item: (
            float(_get(item, "position", 0.0) or 0.0),
            0 if bool(_get(item, "is_primary", False)) else 1,
            _sid(item),
        ),
    )


def resolve_story_route(
    start_episode_id: str | UUID | None,
    links: Iterable[Any],
    user_choices: Mapping[str, str] | None = None,
) -> list[str]:
    """開始点から primary / ユーザー選択を辿ったルートを返す純関数。"""

    if start_episode_id is None:
        return []
    choices = {str(key): str(value) for key, value in (user_choices or {}).items()}
    route: list[str] = []
    current = str(start_episode_id)
    visited: set[str] = set()
    while current and current not in visited:
        route.append(current)
        visited.add(current)
        children = _sorted_links(links, current)
        if not children:
            break
        chosen = choices.get(current)
        child_ids = {_sid(_get(link, "to_episode_id")) for link in children}
        if chosen not in child_ids:
            primary = next(
                (link for link in children if bool(_get(link, "is_primary", False))),
                children[0],
            )
            chosen = _sid(_get(primary, "to_episode_id"))
        current = chosen
    return route


class StoryGraphError(ValueError):
    """グラフ操作の入力違反。``op_index`` は structure ops の位置。"""

    def __init__(self, reason: str, *, op_index: int | None = None):
        self.reason = reason
        self.op_index = op_index
        prefix = f"op[{op_index}]: " if op_index is not None else ""
        super().__init__(prefix + reason)


def validate_story_graph(
    episode_ids: Iterable[Any],
    links: Iterable[Any],
    start_episode_id: Any | None = None,
) -> None:
    """自己ループ・未知ノード・循環を検証する純関数。"""

    nodes = {_sid(item) for item in episode_ids}
    adjacency: dict[str, list[str]] = {node: [] for node in nodes}
    for link in links:
        source = _sid(_get(link, "from_episode_id"))
        target = _sid(_get(link, "to_episode_id"))
        if source not in nodes or target not in nodes:
            raise StoryGraphError("存在しないエピソードへのリンクです")
        if source == target:
            raise StoryGraphError("自己ループは禁止されています")
        adjacency[source].append(target)
    if start_episode_id is not None and str(start_episode_id) not in nodes:
        raise StoryGraphError("開始エピソードが作品に存在しません")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise StoryGraphError("循環するリンクは作成できません")
        if node in visited:
            return
        visiting.add(node)
        for child in adjacency[node]:
            visit(child)
        visiting.remove(node)
        visited.add(node)

    for node in nodes:
        visit(node)


@dataclass(frozen=True)
class StoryContext:
    prompt: str
    injected: tuple[dict[str, str], ...] = field(default_factory=tuple)
    model: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        resolved_model = "/".join(
            item for item in (self.model.get("provider"), self.model.get("model")) if item
        ) or None
        return {
            "prompt": self.prompt,
            "injected": list(self.injected),
            "model": dict(self.model),
            "resolved_model": resolved_model,
            "model_layer": self.model.get("layer") or None,
            "estimated_chars": len(self.prompt),
        }


def _is_older_than(created_at: Any, interval: timedelta) -> bool:
    """``created_at`` が ``interval`` より古いかを判定する。

    ``story_episode_revisions.created_at`` は DB 由来では aware、Python 既定値
    由来では naive になり得るため、比較する現在時刻を合わせてから判定する。
    時刻が取れない行はオートセーブ対象として扱う。
    """

    if not isinstance(created_at, datetime):
        return True
    now = datetime.now(timezone.utc) if created_at.tzinfo is not None else datetime.utcnow()
    return (now - created_at) > interval


def _matches(probe: str, values: Iterable[Any]) -> bool:
    folded = probe.casefold()
    return any(_text(value).strip().casefold() in folded for value in values if _text(value).strip())


def _character_from_join(item: Any) -> Any:
    return _get(item, "character", item)


def _rulebook_from_join(item: Any) -> Any:
    return _get(item, "rulebook", item)


def build_story_context(
    work: Any,
    episode: Any,
    route: Sequence[Any],
    *,
    characters: Iterable[Any] = (),
    work_characters: Iterable[Any] = (),
    rulebooks: Iterable[Any] = (),
    work_rulebooks: Iterable[Any] = (),
    notes: Iterable[Any] = (),
    links: Iterable[Any] = (),
    explicit_ids: Iterable[str] = (),
    budget: int = 24000,
    model: Mapping[str, Any] | None = None,
) -> StoryContext:
    """§8.2 の文脈を決定的に組み立てる純関数。

    ``StoryCharacter.notes`` は意図的に参照せず、description のみを prompt に入れる。
    premise_note は budget の打ち切り対象外とする。
    """

    route_items = list(route)
    episode_id = _sid(episode)
    route_ids = [_sid(item) for item in route_items]
    try:
        target_index = route_ids.index(episode_id)
    except ValueError:
        target_index = len(route_items)
    ancestors = route_items[:target_index]
    injected: list[dict[str, str]] = []
    sections: list[str] = []

    title = _text(_get(work, "title", "作品"))
    work_text = "\n".join(
        item for item in (
            _text(_get(work, "synopsis")),
            _text(_get(work, "plot")),
        ) if item
    )
    sections.append(f"## 作品\n{title}\n{work_text}".rstrip())
    injected.append({"kind": "work", "title": title})
    style = _text(_get(work, "style_guide"))
    if style:
        sections.append(f"## 文体・執筆指示\n{style}")

    enabled_rulebooks = []
    join_map = {
        _sid(_get(item, "rulebook_id")): item
        for item in work_rulebooks
    }
    for item in rulebooks:
        join = join_map.get(_sid(item))
        if join is not None and not bool(_get(join, "enabled", False)):
            continue
        if join is None and work_rulebooks:
            continue
        enabled_rulebooks.append((float(_get(join, "position", 0.0) or 0.0), item))
    for _, rulebook in sorted(enabled_rulebooks, key=lambda pair: pair[0]):
        name = _text(_get(rulebook, "name"))
        sections.append(f"## ルール: {name}\n{_text(_get(rulebook, 'content'))}".rstrip())
        injected.append({"kind": "rulebook", "title": name})

    probe_parts = [_text(_get(episode, "plot"))]
    probe_parts.extend(_text(_get(item, "summary")) for item in ancestors)
    probe_parts.extend(_text(_get(item, "body")) for item in ancestors[-2:])
    probe = "\n".join(probe_parts)
    explicit = {str(item) for item in explicit_ids}
    char_map = {
        _sid(_get(item, "character_id")): item
        for item in work_characters
    }
    char_candidates: list[tuple[str, str]] = []
    for raw_character in characters:
        character = _character_from_join(raw_character)
        cid = _sid(character)
        mode = _text(_get(character, "ai_mode", "keyword"))
        if mode == "off":
            # §8.2 の擬似コードは off を最初に continue する。明示添付でも入れない。
            continue
        values = [_get(character, "name", ""), *(_get(character, "aliases", []) or []), *(_get(character, "keywords", []) or [])]
        hit = mode == "always" or cid in explicit or (mode == "keyword" and _matches(probe, values))
        if mode == "manual" and cid not in explicit:
            hit = False
        if hit:
            role = _text(_get(char_map.get(cid), "role_note"))
            body = _text(_get(character, "description"))
            if role:
                body = f"{body}\n役割メモ: {role}".strip()
            char_candidates.append((_text(_get(character, "name")), body))

    note_candidates: list[tuple[str, str]] = []
    for note in sorted(notes, key=lambda item: float(_get(item, "position", 0.0) or 0.0)):
        mode = _text(_get(note, "ai_mode", "keyword"))
        if mode == "off":
            continue
        values = [_get(note, "title", ""), *(_get(note, "keywords", []) or [])]
        nid = _sid(note)
        hit = mode == "always" or nid in explicit or (mode == "keyword" and _matches(probe, values))
        if mode == "manual" and nid not in explicit:
            hit = False
        if hit:
            note_candidates.append((_text(_get(note, "title")), _text(_get(note, "content"))))

    # ⑤-c premise は予算外。入次数は links から決定する。
    premise_sections: list[tuple[str, str]] = []
    incoming: dict[str, int] = {}
    for link in links:
        incoming[_sid(_get(link, "to_episode_id"))] = incoming.get(_sid(_get(link, "to_episode_id")), 0) + 1
    for ancestor in ancestors:
        if incoming.get(_sid(ancestor), int(_get(ancestor, "in_degree", 0) or 0)) >= 2:
            premise = _text(_get(ancestor, "premise_note"))
            if premise:
                ancestor_title = _text(_get(ancestor, "title"))
                premise_sections.append((f"## 確定事項（{ancestor_title}）\n{premise}", ancestor_title))

    # 予算判定と出力順は分離する。判定は本文 → 要約 → 資料 → 人物の順に行い
    # （優先度の低いものから落ちる）、出力は §8.1 の ①〜⑥ 順に並べ直す。
    used = 0
    truncated = False
    budgeted: dict[str, list[tuple[str, str, str]]] = {"body": [], "summary": [], "note": [], "character": []}

    def add_budgeted(kind: str, heading: str, body: str, *, label: str | None = None) -> None:
        nonlocal used, truncated
        if truncated or not body:
            return
        if used + len(body) > max(0, budget):
            # §8.2 の擬似コードどおり、超過した時点で打ち切る（小さい素材を詰め直さない）。
            truncated = True
            return
        used += len(body)
        budgeted[kind].append((heading, body, label if label is not None else heading))

    for ancestor in reversed(ancestors[-2:]):
        add_budgeted(
            "body",
            f"直前の章: {_text(_get(ancestor, 'title'))}",
            _text(_get(ancestor, "body")),
        )
    for ancestor in ancestors[: len(ancestors) - len(budgeted["body"])]:
        title_text = _text(_get(ancestor, "title"))
        add_budgeted("summary", title_text, _text(_get(ancestor, "summary")))
    for name, body in note_candidates:
        add_budgeted("note", f"設定: {name}", body, label=name)
    for name, body in char_candidates:
        add_budgeted("character", f"登場人物: {name}", body, label=name)

    def emit(kind: str, *, reverse: bool = False) -> None:
        items = list(reversed(budgeted[kind])) if reverse else budgeted[kind]
        for heading, body, label in items:
            sections.append(f"## {heading}\n{body}".rstrip())
            injected.append({"kind": kind, "title": label})

    # ③ 登場人物 → ④ 設定資料 → ⑤ 祖先チェーン（c 前提メモ → a 直近本文 → b 要約）。
    emit("character")
    emit("note")
    for section_text, ancestor_title in premise_sections:
        sections.append(section_text)
        injected.append({"kind": "premise", "title": ancestor_title})
    emit("body", reverse=True)
    emit("summary")

    target_title = _text(_get(episode, "title"))
    target_body = f"## これから書く章\n{target_title}"
    plot = _text(_get(episode, "plot"))
    if plot:
        target_body += f"\n{plot}"
    target_chars = _get(episode, "target_chars") or _get(work, "target_episode_chars", 6000)
    target_body += f"\n目標文字数: {target_chars}"
    if _text(_get(episode, "body")):
        target_body += "\n既存本文の続きから書くこと"
    sections.append(target_body)
    resolved_model = dict(model or {})
    if resolved_model:
        injected.insert(0, {"kind": "model", "title": f"{resolved_model.get('provider', '')}/{resolved_model.get('model', '')}".strip("/")})
    return StoryContext(prompt="\n\n".join(sections), injected=tuple(injected), model=resolved_model)


class StoryConflictError(RuntimeError):
    def __init__(self, episode: StoryEpisode, *, updated_by: str | None = None):
        self.episode = episode
        self.updated_by = updated_by or "user"
        # 409 応答（§6.4）に必要な値はここで控える。API 層は rollback 後に
        # detail を組み立てるため、ORM 属性の expire に巻き込まれないようにする。
        try:
            self.current_etag = episode.body_etag
            updated_at = episode.updated_at
        except Exception:  # pragma: no cover - 期限切れ属性の遅延ロード失敗
            self.current_etag = None
            updated_at = None
        self.updated_at = updated_at.isoformat() if hasattr(updated_at, "isoformat") else None
        super().__init__("本文が別の場所で更新されています")


class StoryNotFoundError(LookupError):
    pass


class StoryRevisionService:
    """本文の etag とリビジョンを一元管理するサービス。"""

    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def body_hash(body: str) -> str:
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    @classmethod
    def body_etag(cls, body: str) -> str:
        return f"sha256:{cls.body_hash(body)}"

    async def create_revision(
        self,
        episode: StoryEpisode,
        *,
        origin: str,
        message: str | None = None,
        created_by: str = "user",
        force: bool = False,
        body: str | None = None,
        title: str | None = None,
        plot: str | None = None,
    ) -> StoryEpisodeRevision | None:
        body_text = episode.body if body is None else body
        digest = self.body_hash(body_text or "")
        latest = await self.session.scalar(
            select(StoryEpisodeRevision)
            .where(StoryEpisodeRevision.episode_id == episode.id)
            .order_by(StoryEpisodeRevision.rev_no.desc())
            .limit(1)
        )
        if latest is not None and latest.body_sha256 == digest and not force:
            return None
        next_no = (latest.rev_no if latest else 0) + 1
        revision = StoryEpisodeRevision(
            episode_id=episode.id,
            rev_no=next_no,
            title=episode.title if title is None else title,
            plot=episode.plot if plot is None else plot,
            body=body_text or "",
            message=message,
            origin=origin,
            body_sha256=digest,
            char_count=len(body_text or ""),
            created_by=created_by,
        )
        self.session.add(revision)
        episode.current_rev_no = next_no
        await self.session.flush()
        return revision

    async def ensure_pre_ai(
        self,
        episode: StoryEpisode,
        *,
        message: str = "AI適用前の状態",
    ) -> StoryEpisodeRevision | None:
        """§6.2 の `pre_ai`。未保存差分がある場合だけ直前状態を 1 本残す。

        直近リビジョンと現在本文の sha が一致する（= 保存済み）なら積まない。
        """

        latest = await self.session.scalar(
            select(StoryEpisodeRevision)
            .where(StoryEpisodeRevision.episode_id == episode.id)
            .order_by(StoryEpisodeRevision.rev_no.desc())
            .limit(1)
        )
        if latest is not None and latest.body_sha256 == self.body_hash(episode.body or ""):
            return None
        return await self.create_revision(
            episode,
            origin="pre_ai",
            message=message,
            created_by="ai",
            force=True,
        )

    async def _autosave_due(self, episode: StoryEpisode) -> bool:
        """§6.2 のオートセーブ条件（前回リビジョンから 15 分超）を満たすか。"""

        latest = await self.session.scalar(
            select(StoryEpisodeRevision)
            .where(StoryEpisodeRevision.episode_id == episode.id)
            .order_by(StoryEpisodeRevision.rev_no.desc())
            .limit(1)
        )
        if latest is None:
            # 履歴が 1 本も無い章は最初のオートセーブで初版を残す。
            return True
        return _is_older_than(latest.created_at, STORY_AUTO_REVISION_INTERVAL)

    async def update_body(
        self,
        episode: StoryEpisode,
        body: str,
        *,
        expected_etag: str,
        commit: bool = True,
        message: str | None = None,
        origin: str = "manual",
        created_by: str = "user",
    ) -> StoryEpisodeRevision | None:
        locked = await self.session.scalar(
            select(StoryEpisode)
            .where(StoryEpisode.id == episode.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        target = locked or episode
        current = target.body or ""
        current_etag = target.body_etag or self.body_etag(current)
        if expected_etag != current_etag:
            latest = await self.session.scalar(
                select(StoryEpisodeRevision)
                .where(StoryEpisodeRevision.episode_id == target.id)
                .order_by(StoryEpisodeRevision.rev_no.desc())
                .limit(1)
            )
            raise StoryConflictError(
                target,
                updated_by=(latest.created_by if latest and latest.created_by else "user"),
            )
        old_body = target.body
        old_etag = target.body_etag
        old_char_count = target.char_count
        old_updated_at = target.updated_at
        try:
            target.body = body
            target.body_etag = self.body_etag(body)
            target.char_count = len(body)
            target.updated_at = datetime.utcnow()
            await self._upsert_search_index(target)
            if not commit:
                # §6.2 オートセーブ: 明示コミットでない保存は、前回リビジョンから
                # 15 分超のときだけ origin='auto' を 1 本積む。sha が同一なら
                # create_revision 側の dedup が None を返して履歴は増えない。
                if not await self._autosave_due(target):
                    await self.session.flush()
                    return None
                revision = await self.create_revision(
                    target,
                    origin="auto",
                    message=message,
                    created_by=created_by,
                    body=body,
                )
                await self.session.flush()
                return revision
            return await self.create_revision(
                target,
                origin=origin,
                message=message,
                created_by=created_by,
                body=body,
                # AI 適用は同一本文が返っても必ず 1 本残す（§6.2 の「常に」）。
                force=origin in STORY_AI_REVISION_ORIGINS,
            )
        except Exception:
            # flush/リビジョン作成に失敗しても呼び出し側が rollback しない
            # 単体サービス利用で in-memory の etag だけが先行しないよう戻す。
            target.body = old_body
            target.body_etag = old_etag
            target.char_count = old_char_count
            target.updated_at = old_updated_at
            raise

    async def _upsert_search_index(self, episode: StoryEpisode) -> StorySearchIndex:
        index = await self.session.get(StorySearchIndex, episode.id)
        if index is None:
            index = StorySearchIndex(
                episode_id=episode.id,
                work_id=episode.work_id,
                title=episode.title,
                body_plain=episode.body or "",
            )
            self.session.add(index)
        else:
            index.title = episode.title
            index.body_plain = episode.body or ""
        await self.session.flush()
        return index

    async def checkpoint(self, episode: StoryEpisode, *, message: str) -> StoryEpisodeRevision:
        revision = await self.create_revision(
            episode,
            origin="checkpoint",
            message=message,
            force=True,
        )
        assert revision is not None
        return revision

    async def restore(self, episode: StoryEpisode, revision: StoryEpisodeRevision) -> tuple[StoryEpisodeRevision, StoryEpisodeRevision]:
        pre = await self.create_revision(
            episode,
            origin="pre_restore",
            message=f"rev {revision.rev_no} 復元前",
            force=True,
        )
        assert pre is not None
        body = revision.body or ""
        episode.body = body
        episode.body_etag = self.body_etag(body)
        episode.char_count = len(body)
        await self._upsert_search_index(episode)
        restored = await self.create_revision(
            episode,
            origin="restore",
            message=f"rev {revision.rev_no} から復元",
            force=True,
        )
        assert restored is not None
        return pre, restored


def _build_search_hit(title: str, body: str, query: str) -> dict[str, Any]:
    needle = query.casefold()
    title_cf = title.casefold()
    body_cf = body.casefold()
    if needle in title_cf:
        field = "title"
        text = title
        start = title_cf.index(needle)
    elif needle in body_cf:
        field = "body"
        text = body
        start = body_cf.index(needle)
    else:
        return {
            "field": "title",
            "match_start": 0,
            "match_end": 0,
            "snippet": title[:120],
        }
    end = start + len(query)
    radius = 40
    snip_start = max(0, start - radius)
    snip_end = min(len(text), end + radius)
    snippet = text[snip_start:snip_end]
    if snip_start > 0:
        snippet = f"…{snippet}"
    if snip_end < len(text):
        snippet = f"{snippet}…"
    return {
        "field": field,
        "match_start": start,
        "match_end": end,
        "snippet": snippet,
    }


class StorySearchService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def search(self, work: StoryWork, query: str, *, limit: int = 50) -> list[dict[str, Any]]:
        needle = _text(query).strip()
        if not needle:
            return []
        pattern = f"%{needle}%"
        rows = (
            await self.session.execute(
                select(StorySearchIndex, StoryEpisode)
                .join(StoryEpisode, StoryEpisode.id == StorySearchIndex.episode_id)
                .where(
                    StorySearchIndex.work_id == work.id,
                    StoryEpisode.archived_at.is_(None),
                    (
                        StorySearchIndex.title.ilike(pattern)
                        | StorySearchIndex.body_plain.ilike(pattern)
                    ),
                )
                .order_by(StoryEpisode.sort_hint, StoryEpisode.created_at)
                .limit(limit)
            )
        ).all()
        results: list[dict[str, Any]] = []
        for index, episode in rows:
            hit = _build_search_hit(index.title, index.body_plain or "", needle)
            results.append(
                {
                    "episode_id": str(episode.id),
                    "title": index.title,
                    "snippet": hit["snippet"],
                    "field": hit["field"],
                    "match_start": hit["match_start"],
                    "match_end": hit["match_end"],
                }
            )
        return results


class StoryWorkService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, work_id: UUID, user_id: UUID, *, include_archived: bool = False) -> StoryWork:
        query = select(StoryWork).where(StoryWork.id == work_id, StoryWork.user_id == user_id)
        if not include_archived:
            query = query.where(StoryWork.archived_at.is_(None))
        work = await self.session.scalar(query)
        if work is None:
            raise StoryNotFoundError("作品が見つかりません")
        return work

    async def list(
        self,
        user_id: UUID,
        *,
        response_metadata: Any | None = None,
    ) -> list[dict[str, Any]]:
        works = list(
            (await self.session.scalars(
                select(StoryWork)
                .where(StoryWork.user_id == user_id, StoryWork.archived_at.is_(None))
                .order_by(StoryWork.updated_at.desc())
            )).all()
        )
        result = []
        for work in works:
            episode_count = await self.session.scalar(
                select(func.count(StoryEpisode.id)).where(
                    StoryEpisode.work_id == work.id, StoryEpisode.archived_at.is_(None)
                )
            )
            char_count = await self.session.scalar(
                select(func.coalesce(func.sum(StoryEpisode.char_count), 0)).where(
                    StoryEpisode.work_id == work.id, StoryEpisode.archived_at.is_(None)
                )
            )
            data = work.to_dict(
                episode_count=int(episode_count or 0),
                char_count=int(char_count or 0),
            )
            if response_metadata is not None:
                data.update(dict(response_metadata(work) or {}))
            result.append(data)
        return result

    async def create(self, user_id: UUID, data: Mapping[str, Any]) -> StoryWork:
        work = StoryWork(
            user_id=user_id,
            title=_text(data.get("title")),
            synopsis=data.get("synopsis"),
            plot=data.get("plot"),
            style_guide=data.get("style_guide"),
            kind=data.get("kind", "novel"),
            status=data.get("status", "planning"),
            target_episode_chars=data.get("target_episode_chars", 6000),
            planned_episode_count=data.get("planned_episode_count"),
            ui_state=dict(data.get("ui_state") or {}),
            model_override=_clean_model_spec(data.get("model_override") or {}),
            image_settings=dict(data.get("image_settings") or {}),
        )
        self.session.add(work)
        await self.session.flush()
        return work


async def clone_story_writing_session_for_conversation(
    source_conversation_id: UUID | str,
    target_conversation_id: UUID | str,
) -> StoryWritingSession | None:
    """会話 fork 時に StoryWritingSession だけを複製する。"""

    source_id = _uuid(source_conversation_id)
    target_id = _uuid(target_conversation_id)
    async with await get_db_session() as session:
        source = await session.scalar(
            select(StoryWritingSession).where(
                StoryWritingSession.conversation_session_id == source_id
            )
        )
        if source is None:
            return None
        clone = StoryWritingSession(
            work_id=source.work_id,
            episode_id=source.episode_id,
            conversation_session_id=target_id,
        )
        session.add(clone)
        await session.commit()
        return clone


class StoryEpisodeService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.revisions = StoryRevisionService(session)

    async def get(self, episode_id: UUID, user_id: UUID) -> StoryEpisode:
        episode = await self.session.scalar(
            select(StoryEpisode)
            .join(StoryWork, StoryWork.id == StoryEpisode.work_id)
            .where(
                StoryEpisode.id == episode_id,
                StoryWork.user_id == user_id,
                StoryWork.archived_at.is_(None),
                StoryEpisode.archived_at.is_(None),
            )
        )
        if episode is None:
            raise StoryNotFoundError("エピソードが見つかりません")
        return episode

    async def list(self, work: StoryWork) -> list[StoryEpisode]:
        return list((await self.session.scalars(
            select(StoryEpisode)
            .where(StoryEpisode.work_id == work.id, StoryEpisode.archived_at.is_(None))
            .order_by(StoryEpisode.sort_hint, StoryEpisode.created_at)
        )).all())

    async def create(self, work: StoryWork, data: Mapping[str, Any], *, after_episode_id: UUID | None = None) -> StoryEpisode:
        body = _text(data.get("body"))
        episode = StoryEpisode(
            work_id=work.id,
            title=_text(data.get("title")),
            plot=data.get("plot"),
            body=body,
            body_etag=self.revisions.body_etag(body),
            summary=data.get("summary"),
            premise_note=data.get("premise_note"),
            status=data.get("status", "unwritten"),
            target_chars=data.get("target_chars"),
            char_count=len(body),
            sort_hint=float(data.get("sort_hint", 0.0) or 0.0),
        )
        self.session.add(episode)
        await self.session.flush()
        await self.revisions._upsert_search_index(episode)
        await self.revisions.create_revision(episode, origin="manual", message="初期版", force=True)
        if work.start_episode_id is None:
            work.start_episode_id = episode.id
        if after_episode_id is not None:
            parent = await self.session.get(StoryEpisode, after_episode_id)
            if parent is None or parent.work_id != work.id:
                raise StoryNotFoundError("接続元エピソードが見つかりません")
            self.session.add(
                StoryLink(
                    work_id=work.id,
                    from_episode_id=parent.id,
                    to_episode_id=episode.id,
                    choice_label=data.get("choice_label"),
                    position=0,
                    is_primary=(
                        await self.session.scalar(
                            select(func.count(StoryLink.id)).where(
                                StoryLink.work_id == work.id,
                                StoryLink.from_episode_id == parent.id,
                            )
                        )
                        or 0
                    )
                    == 0,
                )
            )
        await self.session.flush()
        return episode

    async def update_meta(self, episode: StoryEpisode, data: Mapping[str, Any]) -> StoryEpisode:
        fields = (
            "title", "plot", "premise_note", "status", "target_chars", "map_x", "map_y", "sort_hint"
        )
        for field_name in fields:
            if field_name in data:
                setattr(episode, field_name, data[field_name])
        if "summary" in data:
            episode.summary = data["summary"]
            episode.summary_locked = True
        await self.revisions._upsert_search_index(episode)
        await self.session.flush()
        return episode

    async def get_archived(self, episode_id: UUID, user_id: UUID) -> StoryEpisode:
        episode = await self.session.scalar(
            select(StoryEpisode)
            .join(StoryWork, StoryWork.id == StoryEpisode.work_id)
            .where(
                StoryEpisode.id == episode_id,
                StoryWork.user_id == user_id,
                StoryWork.archived_at.is_(None),
                StoryEpisode.archived_at.is_not(None),
            )
        )
        if episode is None:
            raise StoryNotFoundError("削除済みエピソードが見つかりません")
        return episode

    async def _active_episode(self, work_id: UUID, episode_id: UUID) -> StoryEpisode | None:
        return await self.session.scalar(
            select(StoryEpisode).where(
                StoryEpisode.id == episode_id,
                StoryEpisode.work_id == work_id,
                StoryEpisode.archived_at.is_(None),
            )
        )

    async def delete(self, work: StoryWork, episode: StoryEpisode) -> dict[str, Any]:
        incoming = list((await self.session.scalars(
            select(StoryLink).where(
                StoryLink.work_id == work.id,
                StoryLink.to_episode_id == episode.id,
            )
        )).all())
        outgoing = list((await self.session.scalars(
            select(StoryLink).where(
                StoryLink.work_id == work.id,
                StoryLink.from_episode_id == episode.id,
            )
        )).all())
        restore_token = {
            "incoming": [
                {
                    "from_episode_id": str(link.from_episode_id),
                    "choice_label": link.choice_label,
                    "position": link.position,
                    "is_primary": bool(link.is_primary),
                }
                for link in incoming
            ],
            "outgoing": [
                {
                    "to_episode_id": str(link.to_episode_id),
                    "choice_label": link.choice_label,
                    "position": link.position,
                    "is_primary": bool(link.is_primary),
                }
                for link in outgoing
            ],
            "was_start_episode": work.start_episode_id == episode.id,
            "bridged": [],
        }
        parents = [link.from_episode_id for link in incoming]
        children = [link.to_episode_id for link in outgoing]
        for link in incoming + outgoing:
            await self.session.delete(link)
        await self.session.flush()
        existing = {(link.from_episode_id, link.to_episode_id) for link in (await self.session.scalars(select(StoryLink).where(StoryLink.work_id == work.id))).all()}
        for parent_id in parents:
            for child_id in children:
                if parent_id != child_id and (parent_id, child_id) not in existing:
                    restore_token["bridged"].append(
                        {"from_episode_id": str(parent_id), "to_episode_id": str(child_id)}
                    )
                    self.session.add(StoryLink(work_id=work.id, from_episode_id=parent_id, to_episode_id=child_id, position=0, is_primary=False))
        if work.start_episode_id == episode.id:
            work.start_episode_id = children[0] if children else None
        episode.archived_at = datetime.utcnow()
        await self.session.flush()
        return {"restore_token": restore_token}

    async def restore_archived(
        self,
        work: StoryWork,
        episode: StoryEpisode,
        restore_token: Mapping[str, Any] | None = None,
    ) -> StoryEpisode:
        if episode.archived_at is None:
            raise ValueError("この章は削除されていません")
        episode.archived_at = None
        if restore_token:
            bridged = restore_token.get("bridged") or []
            for item in bridged:
                from_id = _uuid(item.get("from_episode_id"))
                to_id = _uuid(item.get("to_episode_id"))
                link = await self.session.scalar(
                    select(StoryLink).where(
                        StoryLink.work_id == work.id,
                        StoryLink.from_episode_id == from_id,
                        StoryLink.to_episode_id == to_id,
                    )
                )
                if link is not None:
                    await self.session.delete(link)
            await self.session.flush()
            existing = {
                (link.from_episode_id, link.to_episode_id)
                for link in (await self.session.scalars(select(StoryLink).where(StoryLink.work_id == work.id))).all()
            }
            for item in restore_token.get("incoming") or []:
                from_id = _uuid(item.get("from_episode_id"))
                if await self._active_episode(work.id, from_id) is None:
                    continue
                pair = (from_id, episode.id)
                if pair in existing:
                    continue
                self.session.add(
                    StoryLink(
                        work_id=work.id,
                        from_episode_id=from_id,
                        to_episode_id=episode.id,
                        choice_label=item.get("choice_label"),
                        position=float(item.get("position") or 0.0),
                        is_primary=bool(item.get("is_primary")),
                    )
                )
                existing.add(pair)
            for item in restore_token.get("outgoing") or []:
                to_id = _uuid(item.get("to_episode_id"))
                if await self._active_episode(work.id, to_id) is None:
                    continue
                pair = (episode.id, to_id)
                if pair in existing:
                    continue
                self.session.add(
                    StoryLink(
                        work_id=work.id,
                        from_episode_id=episode.id,
                        to_episode_id=to_id,
                        choice_label=item.get("choice_label"),
                        position=float(item.get("position") or 0.0),
                        is_primary=bool(item.get("is_primary")),
                    )
                )
                existing.add(pair)
            if bool(restore_token.get("was_start_episode")):
                work.start_episode_id = episode.id
            episodes = list(
                (await self.session.scalars(select(StoryEpisode).where(StoryEpisode.work_id == work.id))).all()
            )
            links = list(
                (await self.session.scalars(select(StoryLink).where(StoryLink.work_id == work.id))).all()
            )
            validate_story_graph(episodes, links, work.start_episode_id)
        await self.session.flush()
        return episode

    async def split(self, work: StoryWork, episode: StoryEpisode, *, offset: int, new_title: str, expected_etag: str) -> dict[str, Any]:
        locked = await self.session.scalar(
            select(StoryEpisode)
            .where(StoryEpisode.id == episode.id, StoryEpisode.work_id == work.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        episode = locked or episode
        body = episode.body or ""
        if expected_etag != (episode.body_etag or self.revisions.body_etag(body)):
            latest = await self.session.scalar(
                select(StoryEpisodeRevision)
                .where(StoryEpisodeRevision.episode_id == episode.id)
                .order_by(StoryEpisodeRevision.rev_no.desc())
                .limit(1)
            )
            raise StoryConflictError(
                episode,
                updated_by=(latest.created_by if latest and latest.created_by else "user"),
            )
        if offset < 0 or offset > len(body):
            raise ValueError("分割位置が本文の範囲外です")
        old_body = body
        head, tail = body[:offset], body[offset:]
        await self.revisions.create_revision(episode, origin="manual", message="章分割", force=True, body=old_body)
        outgoing = list((await self.session.scalars(select(StoryLink).where(StoryLink.from_episode_id == episode.id))).all())
        created = await self.create(work, {"title": new_title, "plot": episode.plot, "body": tail, "summary": episode.summary, "premise_note": episode.premise_note, "status": episode.status}, after_episode_id=None)
        episode.body = head
        episode.body_etag = self.revisions.body_etag(head)
        episode.char_count = len(head)
        await self.revisions._upsert_search_index(episode)
        # §9.2 の分割契約は「元章に分割前全文を 1 本」だけ。分割後 head の
        # リビジョンは積まない。§6.1 のとおり本文と最新リビジョンは一致しなくて
        # よく（保存 = リビジョン 1 本ではない）、head は現在本文として保持される。
        # create() has an initial revision for tail; make its message match the split contract.
        initial = await self.session.scalar(select(StoryEpisodeRevision).where(StoryEpisodeRevision.episode_id == created.id, StoryEpisodeRevision.rev_no == 1))
        if initial is not None:
            initial.message = "章分割"
            initial.origin = "manual"
        rewired: list[str] = []
        for link in outgoing:
            link.from_episode_id = created.id
            rewired.append(str(link.id))
        primary = StoryLink(work_id=work.id, from_episode_id=episode.id, to_episode_id=created.id, position=0, is_primary=True)
        self.session.add(primary)
        await self.session.flush()
        validate_story_graph(
            [*(await self.session.scalars(select(StoryEpisode).where(StoryEpisode.work_id == work.id))).all()],
            [*(await self.session.scalars(select(StoryLink).where(StoryLink.work_id == work.id))).all()],
            work.start_episode_id,
        )
        return {
            "source": {"id": str(episode.id), "body_etag": episode.body_etag, "char_count": episode.char_count, "current_rev_no": episode.current_rev_no},
            "created": {"id": str(created.id), "body_etag": created.body_etag, "char_count": created.char_count, "current_rev_no": created.current_rev_no},
            "links": {"created": [str(primary.id)], "rewired": rewired},
        }


class StoryGraphService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def graph(self, work: StoryWork) -> dict[str, Any]:
        episodes = list((await self.session.scalars(select(StoryEpisode).where(StoryEpisode.work_id == work.id, StoryEpisode.archived_at.is_(None)).order_by(StoryEpisode.sort_hint, StoryEpisode.created_at))).all())
        links = list((await self.session.scalars(select(StoryLink).where(StoryLink.work_id == work.id))).all())
        return {"episodes": [item.to_dict(include_body=False) for item in episodes], "links": [item.to_dict() for item in links], "start_episode_id": str(work.start_episode_id) if work.start_episode_id else None}

    async def apply(self, work: StoryWork, ops: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        for index, op in enumerate(ops):
            name = op.get("op")
            try:
                if name == "add_link":
                    source, target = _uuid(op["from"]), _uuid(op["to"])
                    if source == target:
                        raise StoryGraphError("自己ループは禁止されています")
                    source_ep = await self.session.get(StoryEpisode, source)
                    target_ep = await self.session.get(StoryEpisode, target)
                    if not source_ep or not target_ep or source_ep.work_id != work.id or target_ep.work_id != work.id:
                        raise StoryGraphError("作品外のエピソードは接続できません")
                    exists = await self.session.scalar(select(StoryLink).where(StoryLink.from_episode_id == source, StoryLink.to_episode_id == target))
                    if exists:
                        raise StoryGraphError("同じリンクは重複作成できません")
                    siblings = list((await self.session.scalars(select(StoryLink).where(StoryLink.from_episode_id == source))).all())
                    make_primary = bool(op.get("is_primary", not siblings))
                    if make_primary:
                        for sibling in siblings:
                            sibling.is_primary = False
                    link = StoryLink(work_id=work.id, from_episode_id=source, to_episode_id=target, choice_label=op.get("choice_label"), position=float(op.get("position", len(siblings))), is_primary=make_primary)
                    self.session.add(link)
                    await self.session.flush()
                    results.append({"op": name, "link_id": str(link.id)})
                elif name == "remove_link":
                    link = await self.session.get(StoryLink, _uuid(op["id"]))
                    if link is None or link.work_id != work.id:
                        raise StoryGraphError("リンクが見つかりません")
                    await self.session.delete(link)
                    results.append({"op": name, "link_id": str(link.id)})
                elif name == "update_link":
                    link = await self.session.get(StoryLink, _uuid(op["id"]))
                    if link is None or link.work_id != work.id:
                        raise StoryGraphError("リンクが見つかりません")
                    if "choice_label" in op:
                        link.choice_label = op["choice_label"]
                    if "position" in op:
                        link.position = float(op["position"])
                    if op.get("is_primary"):
                        siblings = list((await self.session.scalars(select(StoryLink).where(StoryLink.from_episode_id == link.from_episode_id, StoryLink.id != link.id))).all())
                        for sibling in siblings:
                            sibling.is_primary = False
                        link.is_primary = True
                    results.append({"op": name, "link_id": str(link.id)})
                elif name == "insert_between":
                    link = await self.session.get(StoryLink, _uuid(op["link_id"]))
                    middle = await self.session.get(StoryEpisode, _uuid(op["episode_id"]))
                    if link is None or middle is None or link.work_id != work.id or middle.work_id != work.id:
                        raise StoryGraphError("挿入対象が見つかりません")
                    old_primary = bool(link.is_primary)
                    source, target = link.from_episode_id, link.to_episode_id
                    await self.session.delete(link)
                    await self.session.flush()
                    first = StoryLink(work_id=work.id, from_episode_id=source, to_episode_id=middle.id, position=link.position, is_primary=old_primary)
                    second = StoryLink(work_id=work.id, from_episode_id=middle.id, to_episode_id=target, position=0, is_primary=True)
                    self.session.add_all([first, second])
                    await self.session.flush()
                    results.append({"op": name, "created": [str(first.id), str(second.id)]})
                elif name == "set_start":
                    episode = await self.session.get(StoryEpisode, _uuid(op["episode_id"]))
                    if episode is None or episode.work_id != work.id:
                        raise StoryGraphError("開始エピソードが見つかりません")
                    work.start_episode_id = episode.id
                    results.append({"op": name, "start_episode_id": str(episode.id)})
                elif name == "reorder_linear":
                    await self._reorder_linear(work, [ _uuid(item) for item in op.get("episode_ids", []) ])
                    results.append({"op": name})
                elif name == "duplicate_as_branch":
                    result = await self._duplicate_as_branch(work, op)
                    results.append({"op": name, **result})
                else:
                    raise StoryGraphError(f"未対応の構造操作です: {name}")
            except StoryGraphError as exc:
                raise StoryGraphError(exc.reason, op_index=index) from exc
            except (KeyError, ValueError) as exc:
                raise StoryGraphError(f"操作の入力が不正です: {exc}", op_index=index) from exc

        episodes = list((await self.session.scalars(select(StoryEpisode).where(StoryEpisode.work_id == work.id))).all())
        links = list((await self.session.scalars(select(StoryLink).where(StoryLink.work_id == work.id))).all())
        validate_story_graph(episodes, links, work.start_episode_id)
        return {"results": results, **(await self.graph(work))}

    async def _reorder_linear(self, work: StoryWork, desired: list[UUID]) -> None:
        if len(desired) < 2 or len(set(desired)) != len(desired):
            raise StoryGraphError("線形並べ替えには重複しない2章以上が必要です")
        owned_ids = set(
            (
                await self.session.scalars(
                    select(StoryEpisode.id).where(
                        StoryEpisode.work_id == work.id,
                        StoryEpisode.archived_at.is_(None),
                    )
                )
            ).all()
        )
        if not set(desired).issubset(owned_ids):
            raise StoryGraphError("作品外のエピソードは並べ替えできません")
        current = list((await self.session.scalars(select(StoryLink).where(StoryLink.work_id == work.id))).all())
        selected = set(desired)
        internal = [link for link in current if link.from_episode_id in selected and link.to_episode_id in selected]
        if len(internal) != len(desired) - 1:
            raise StoryGraphError("分岐を含む範囲は線形並べ替えできません")
        internal_in = {episode_id: 0 for episode_id in selected}
        internal_out = {episode_id: 0 for episode_id in selected}
        for link in internal:
            internal_in[link.to_episode_id] += 1
            internal_out[link.from_episode_id] += 1
        if any(value > 1 for value in internal_in.values()) or any(value > 1 for value in internal_out.values()):
            raise StoryGraphError("分岐を含む範囲は線形並べ替えできません")
        # §7.3 の線形区間は「出次数 1 かつ次ノードの入次数 1」の連続。範囲外への
        # 分岐・合流を持つ中間ノードは、内部リンクだけ見ると鎖に見えるため除外する。
        total_in = {episode_id: 0 for episode_id in selected}
        total_out = {episode_id: 0 for episode_id in selected}
        for link in current:
            if link.to_episode_id in selected:
                total_in[link.to_episode_id] += 1
            if link.from_episode_id in selected:
                total_out[link.from_episode_id] += 1
        for episode_id in selected:
            is_head = internal_in[episode_id] == 0
            is_tail = internal_out[episode_id] == 0
            if not is_tail and total_out[episode_id] != 1:
                raise StoryGraphError("分岐を含む範囲は線形並べ替えできません")
            if not is_head and total_in[episode_id] != 1:
                raise StoryGraphError("分岐を含む範囲は線形並べ替えできません")
        attrs = {
            (link.from_episode_id, link.to_episode_id): (
                link.choice_label,
                bool(link.is_primary),
                float(link.position or 0),
            )
            for link in internal
        }
        external_primary = {
            link.from_episode_id
            for link in current
            if link.from_episode_id in selected
            and link.to_episode_id not in selected
            and link.is_primary
        }
        for link in internal:
            await self.session.delete(link)
        await self.session.flush()
        for position, (source, target) in enumerate(zip(desired, desired[1:])):
            label, primary, old_position = attrs.get(
                (source, target),
                (None, source not in external_primary, float(position)),
            )
            self.session.add(
                StoryLink(
                    work_id=work.id,
                    from_episode_id=source,
                    to_episode_id=target,
                    choice_label=label,
                    position=float(position),
                    is_primary=bool(primary) if (source, target) in attrs else bool(source not in external_primary),
                )
            )
        await self.session.flush()

    async def _duplicate_as_branch(self, work: StoryWork, op: Mapping[str, Any]) -> dict[str, Any]:
        source = await self.session.get(StoryEpisode, _uuid(op["episode_id"]))
        if source is None or source.work_id != work.id:
            raise StoryGraphError("複製元エピソードが見つかりません")
        incoming = list(
            (
                await self.session.scalars(
                    select(StoryLink).where(
                        StoryLink.work_id == work.id,
                        StoryLink.to_episode_id == source.id,
                    )
                )
            ).all()
        )
        body = source.body or ""
        clone = StoryEpisode(
            work_id=work.id,
            title=op.get("new_title") or f"{source.title}（別パターン）",
            plot=source.plot,
            body=body,
            body_etag=StoryRevisionService.body_etag(body),
            summary=source.summary,
            premise_note=source.premise_note,
            status="draft",
            target_chars=source.target_chars,
            char_count=len(body),
            map_x=(source.map_x + 80 if source.map_x is not None else None),
            map_y=(source.map_y + 80 if source.map_y is not None else None),
            sort_hint=(source.sort_hint + 0.5),
        )
        self.session.add(clone)
        await self.session.flush()
        revision_service = StoryRevisionService(self.session)
        await revision_service._upsert_search_index(clone)
        revision = await revision_service.create_revision(clone, origin="manual", message=f"「{source.title}」から複製", force=True)
        assert revision is not None
        link_ids: list[str] = []
        for parent in incoming:
            link = StoryLink(work_id=work.id, from_episode_id=parent.from_episode_id, to_episode_id=clone.id, choice_label=op.get("choice_label"), position=float(parent.position) + 0.5, is_primary=False)
            self.session.add(link)
            await self.session.flush()
            link_ids.append(str(link.id))
        return {"episode_id": str(clone.id), "link_ids": link_ids, "unplaced": not bool(incoming)}


class StoryJobRunner:
    """ジョブ行の状態遷移を集中管理する。"""

    def __init__(self, session: AsyncSession):
        self.session = session

    _TRANSITIONS = {
        "queued": {"queued", "running", "error", "canceled"},
        "running": {"running", "done", "error", "canceled"},
        "error": {"error", "queued"},
        "canceled": {"canceled", "queued"},
        "done": {"done"},
    }

    async def create(self, work: StoryWork, *, kind: str, payload: Mapping[str, Any], model: Mapping[str, Any] | None = None) -> StoryGenerationJob:
        stored_payload = dict(payload)
        if model is not None:
            stored_payload["model"] = _clean_model_spec(model)
        job = StoryGenerationJob(work_id=work.id, kind=kind, payload=stored_payload, status="queued", progress=stored_payload.get("progress") or {})
        self.session.add(job)
        await self.session.flush()
        return job

    async def cancel(self, job: StoryGenerationJob) -> StoryGenerationJob:
        # 完了済みジョブへのキャンセルは、LLM完了とHTTP再送の競合で通常に
        # 起こり得るため、状態を巻き戻さず現在値をそのまま返す。
        if job.status in {"done", "error", "canceled"}:
            return job
        return await self.transition(job, "canceled")

    async def transition(
        self,
        job: StoryGenerationJob,
        status: str,
        *,
        progress: Mapping[str, Any] | None = None,
        result: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> StoryGenerationJob:
        status = str(status)
        allowed = self._TRANSITIONS.get(job.status, set())
        if status not in allowed:
            raise StoryGraphError(f"ジョブ状態 {job.status} から {status} へ遷移できません")
        now = datetime.utcnow()
        if status == "running" and job.started_at is None:
            job.started_at = now
        if status in {"done", "error", "canceled"}:
            job.finished_at = now
        elif status in {"queued", "running"}:
            job.finished_at = None
        if progress is not None:
            job.progress = dict(progress)
        if result is not None:
            job.result = dict(result)
        if error is not None:
            job.error = error
        elif status not in {"error"}:
            job.error = None
        job.status = status
        await self.session.flush()
        return job

    async def resume(self, job: StoryGenerationJob) -> StoryGenerationJob:
        if job.status not in {"error", "canceled"}:
            raise StoryGraphError("完了済み、または実行中のジョブは再開できません")
        progress = dict(job.progress or {})
        items = []
        for item in progress.get("items", []):
            current = dict(item)
            if current.get("state") != "done":
                current["state"] = "pending"
                current.pop("error", None)
            items.append(current)
        if items:
            progress["items"] = items
            progress["completed"] = sum(1 for item in items if item.get("state") == "done")
        job.payload = dict(job.payload or {})  # resolved model をそのまま維持
        return await self.transition(job, "queued", progress=progress, error=None)

    async def mark_interrupted(self) -> int:
        jobs = list((await self.session.scalars(select(StoryGenerationJob).where(StoryGenerationJob.status == "running"))).all())
        for job in jobs:
            job.status = "error"
            job.error = "interrupted"
            job.finished_at = datetime.utcnow()
        await self.session.flush()
        return len(jobs)


class _StoryJobCanceled(Exception):
    def __init__(self, progress: Mapping[str, Any], result: Mapping[str, Any] | None = None):
        self.progress = dict(progress)
        self.result = dict(result or {})
        super().__init__("ジョブがキャンセルされました")


class _StoryJobFailure(Exception):
    def __init__(self, message: str, progress: Mapping[str, Any]):
        self.progress = dict(progress)
        super().__init__(message)


class _StoryJobStale(Exception):
    """旧 worker が再開後の新しい実行世代を触らないための内部制御。"""


def _redact_job_error(value: Any) -> str:
    """ジョブ行へ保存するエラーから、一般的な実キー形式を除去する。"""

    text = str(value or "")
    text = re.sub(r"(?i)(sk-[A-Za-z0-9_-]{12,}|AIza[A-Za-z0-9_-]{20,})", "[REDACTED]", text)
    return text[:2000] or "ジョブ実行に失敗しました"


def _strip_json_fence(value: str) -> str:
    text = value.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _parse_compose_result(value: str) -> dict[str, Any]:
    """LLM の構成案を API が扱う JSON DTO に限定して取り出す。"""

    text = _strip_json_fence(value)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise StoryGraphError("章構成提案の JSON を解釈できません")
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise StoryGraphError("章構成提案の JSON を解釈できません") from exc
    if isinstance(parsed, list):
        parsed = {"episodes": parsed, "links": []}
    if not isinstance(parsed, Mapping):
        raise StoryGraphError("章構成提案は JSON オブジェクトで返してください")
    if isinstance(parsed.get("proposal"), Mapping):
        parsed = parsed["proposal"]
    episodes_raw = parsed.get("episodes")
    links_raw = parsed.get("links") or []
    if not isinstance(episodes_raw, list) or not isinstance(links_raw, list):
        raise StoryGraphError("章構成提案には episodes と links の配列が必要です")
    episodes: list[dict[str, Any]] = []
    for item in episodes_raw:
        if not isinstance(item, Mapping) or not str(item.get("title") or "").strip():
            raise StoryGraphError("章構成提案の各 episode に title が必要です")
        episodes.append(
            {
                "title": str(item.get("title")).strip(),
                "plot": str(item.get("plot") or ""),
                **({"summary": str(item["summary"])} if item.get("summary") is not None else {}),
            }
        )
    links: list[dict[str, Any]] = []
    for item in links_raw:
        if not isinstance(item, Mapping):
            raise StoryGraphError("章構成提案の links が不正です")
        source = item.get("from", item.get("from_episode_id"))
        target = item.get("to", item.get("to_episode_id"))
        if source is None or target is None:
            raise StoryGraphError("章構成提案の link に from/to が必要です")
        link = {"from": str(source), "to": str(target)}
        if item.get("choice_label") is not None:
            link["choice_label"] = str(item["choice_label"])
        if item.get("is_primary") is not None:
            link["is_primary"] = bool(item["is_primary"])
        links.append(link)
    return {"episodes": episodes, "links": links}


def build_story_summary_prompt(episode: Any, *, work: Any | None = None) -> str:
    """§8.4 の自動要約プロンプトを組み立てる純関数。"""

    parts = [
        "あなたは小説の章要約を作る編集者です。次の章の本文を、後続章の執筆時に"
        "参照するための短い要約にしてください。",
        "条件: 日本語 1〜2 文、120 字以内、前置きや見出しを付けず要約本文だけを返す。"
        "本文に無い出来事を足さない。",
    ]
    if work is not None:
        work_title = _text(_get(work, "title"))
        if work_title:
            parts.append(f"作品: {work_title}")
    title = _text(_get(episode, "title"))
    if title:
        parts.append(f"章タイトル: {title}")
    plot = _text(_get(episode, "plot"))
    if plot:
        parts.append(f"章プロット: {plot}")
    parts.append(f"本文:\n{_text(_get(episode, 'body'))}")
    return "\n\n".join(parts)


def _clean_summary_text(value: str) -> str:
    """LLM 出力を要約列へ入れられる 1 段落テキストに整える。"""

    text = _strip_json_fence(str(value or "")).strip()
    text = re.sub(r"\s*\n+\s*", " ", text).strip()
    if len(text) > STORY_SUMMARY_MAX_CHARS:
        text = text[:STORY_SUMMARY_MAX_CHARS].rstrip()
    return text


class StoryJobExecutor:
    """Story の AI job を asyncio background task から実行する。"""

    def __init__(self, session: AsyncSession, llm_client: Any, *, config: Any | None = None):
        self.session = session
        self.llm_client = llm_client
        self.config = config
        self._target_client: Any | None = None
        self._target_client_key: tuple[str, str, str] | None = None
        self._target_client_owned = False
        self._scoped_base_client_instance: Any | None = None
        self._scoped_base_context_key: tuple[Any, ...] | None = None
        self._usage_context: dict[str, Any] = {}
        self.runner = StoryJobRunner(session)

    def _set_usage_context(self, work: StoryWork | None) -> None:
        """Keep Story's owner scope available to a model-specific client.

        Plain-text generation is intentionally delegated to the provider
        client, which owns usage persistence. When a Story model override
        creates a fresh client, copy the durable work scope before invoking
        that provider so its normal usage recorder can associate the row.
        """

        if work is None:
            self._usage_context = {}
            return
        state = _get(work, "ui_state", {})
        state = state if isinstance(state, Mapping) else {}
        self._usage_context = {
            "user_id": str(_get(work, "user_id") or "") or None,
            "project_id": str(
                _get(work, "project_id")
                or _get(work, "current_project_id")
                or state.get("project_id")
                or ""
            )
            or None,
            "session_id": str(
                _get(work, "session_id")
                or _get(work, "conversation_session_id")
                or state.get("session_id")
                or state.get("conversation_session_id")
                or ""
            )
            or None,
            "agent_name": "story_studio",
            "request_type": "story_studio",
        }

    def _apply_usage_context(self, client: Any) -> None:
        context = self._usage_context
        if not context or client is None:
            return
        user_id = context.get("user_id")
        try:
            set_context = getattr(client, "set_session_context", None)
            if callable(set_context) and user_id:
                # Provider clients differ: some merge ``metadata`` while
                # others replace it. User identity is all their usage
                # recorder needs here; avoid clobbering existing route data.
                set_context(user_id=user_id)
        except Exception:
            logger.debug("Story model client session context setup failed", exc_info=True)
        if context.get("agent_name") and hasattr(client, "character_name"):
            try:
                setattr(client, "character_name", context["agent_name"])
            except Exception:
                logger.debug("Story model client agent setup failed", exc_info=True)
        for attribute in ("current_session_id", "current_project_id"):
            value = context.get(attribute.removeprefix("current_"))
            if value is None or not hasattr(client, attribute):
                continue
            try:
                setattr(client, attribute, value)
            except Exception:
                logger.debug("Story model client %s setup failed", attribute, exc_info=True)

    def _usage_context_key(self) -> tuple[Any, ...]:
        context = self._usage_context
        return tuple(
            context.get(key)
            for key in ("user_id", "session_id", "project_id", "agent_name", "request_type")
        )

    def _get_scoped_base_client(self) -> Any:
        """Return a request-scoped view of the inherited/base client.

        Story jobs run in background tasks and may overlap for different
        users. Mutating the application-wide ``llm_client`` to set a work's
        user/session would race and attribute usage to the wrong owner. A
        shallow client clone is sufficient for the plain/summary APIs (they
        intentionally avoid conversation history); copy the small mutable
        telemetry/state containers so context setup cannot leak back to the
        shared object. The underlying HTTP SDK handle remains shared.
        """

        base = self.llm_client
        if base is None or not self._usage_context:
            return base
        key = self._usage_context_key()
        if (
            self._scoped_base_client_instance is not None
            and self._scoped_base_context_key == key
        ):
            return self._scoped_base_client_instance
        try:
            scoped = copy.copy(base)
        except Exception:
            # Purpose-built test doubles and unusual integrations may reject
            # copying. Do not mutate their shared instance; return it without
            # context rather than introducing a race through a fallback set.
            logger.debug("Story base LLM client clone failed", exc_info=True)
            return base

        for attribute in (
            "session_metadata",
            "_provider_state",
            "_last_route_metadata",
            "_last_generation_metadata",
            "_current_dynamic_context_metadata",
            "_current_dynamic_context",
            "_last_context_snapshots",
            "_model_transcript",
            "_last_model_transcript",
            "_last_usage",
            "_recorded_usage_responses",
        ):
            value = getattr(scoped, attribute, None)
            if isinstance(value, dict):
                setattr(scoped, attribute, dict(value))
            elif isinstance(value, list):
                setattr(scoped, attribute, list(value))
            elif isinstance(value, set):
                setattr(scoped, attribute, set(value))

        self._apply_usage_context(scoped)
        self._scoped_base_client_instance = scoped
        self._scoped_base_context_key = key
        return scoped

    async def _fresh_job(self, job_id: UUID | str, *, lock: bool = False) -> StoryGenerationJob | None:
        statement = (
            select(StoryGenerationJob)
            .where(StoryGenerationJob.id == _uuid(job_id))
            .execution_options(populate_existing=True)
        )
        if lock:
            statement = statement.with_for_update()
        return await self.session.scalar(statement)

    async def _fresh_episode(self, work_id: UUID, episode_id: UUID | str, *, lock: bool = False) -> StoryEpisode:
        statement = select(StoryEpisode).where(
                StoryEpisode.id == _uuid(episode_id),
                StoryEpisode.work_id == work_id,
                StoryEpisode.archived_at.is_(None),
            ).execution_options(populate_existing=True)
        if lock:
            statement = statement.with_for_update()
        episode = await self.session.scalar(statement)
        if episode is None:
            raise StoryNotFoundError("生成対象エピソードが見つかりません")
        return episode

    async def _client_for_model(self, model: Mapping[str, Any] | None) -> Any:
        spec = dict(model or {})
        provider = str(spec.get("provider") or "").strip().lower()
        model_name = str(spec.get("model") or "").strip()
        base_url = str(spec.get("base_url") or "").strip()
        if not provider or not model_name:
            return self._get_scoped_base_client()
        current_key = (
            str(getattr(self.llm_client, "provider", None) or getattr(self.llm_client, "provider_label", "") or "").strip().lower(),
            str(getattr(self.llm_client, "model", None) or getattr(self.llm_client, "model_name", "") or "").strip(),
            str(getattr(self.llm_client, "base_url", "") or "").strip(),
        )
        requested_key = (provider, model_name, base_url)
        if current_key == requested_key:
            return self._get_scoped_base_client()
        if self._target_client is not None and self._target_client_key == requested_key:
            self._apply_usage_context(self._target_client)
            return self._target_client
        if self.config is None:
            # Unit tests and embedded callers may supply a purpose-built fake
            # client without a Config object.  The API still persists and
            # displays the resolved model in that case.
            return self._get_scoped_base_client()
        from ..llm.manager import create_llm_client_for_target

        credential_profile: Any = spec.get("api_key_ref") or None
        if credential_profile:
            try:
                from ..memory.models.free_team import FreeTeamCredentialProfile

                credential_profile = await self.session.get(
                    FreeTeamCredentialProfile,
                    str(credential_profile),
                )
                if credential_profile is None:
                    raise RuntimeError("指定された資格情報プロファイルが見つかりません")
                if not bool(getattr(credential_profile, "enabled", False)) or str(getattr(credential_profile, "status", "")) not in {"ready", "active"}:
                    raise RuntimeError("指定された資格情報プロファイルは利用できません")
                profile_provider = str(getattr(credential_profile, "provider", "") or "").strip().lower()
                if profile_provider and profile_provider != provider:
                    raise RuntimeError("資格情報プロファイルの provider がモデル指定と一致しません")
            except Exception:
                logger.warning("Story 用資格情報の解決に失敗しました", exc_info=True)
                raise
        target = create_llm_client_for_target(
            self.config,
            provider=provider,
            model=model_name,
            credential_profile=credential_profile,
            base_url=base_url,
            effort=str(spec.get("reasoning_effort") or ""),
        )
        self._apply_usage_context(target)
        self._target_client = target
        self._target_client_key = requested_key
        self._target_client_owned = True
        return target

    async def _generate_text(self, prompt: str, model: Mapping[str, Any] | None = None) -> str:
        client = await self._client_for_model(model)
        if client is None:
            raise RuntimeError("LLM client が設定されていません")
        for name in ("generate_plain_text_async", "generate_response_async", "generate_async"):
            method = getattr(client, name, None)
            if not callable(method):
                continue
            result = method(prompt)
            if inspect.isawaitable(result):
                result = await result
            text = str(result or "").strip()
            if text:
                return text
        sync_method = getattr(client, "generate", None) or getattr(client, "generate_response", None)
        if callable(sync_method):
            try:
                result = await asyncio.to_thread(sync_method, prompt, stream=False)
            except TypeError:
                result = await asyncio.to_thread(sync_method, prompt)
            text = str(result or "").strip()
            if text:
                return text
        raise RuntimeError("LLM client が非同期テキスト生成を提供していません")

    async def aclose(self) -> None:
        """モデル指定のために生成した専用 LLM client を閉じる。"""

        if self._target_client_owned and self._target_client is not None:
            close = getattr(self._target_client, "aclose", None) or getattr(self._target_client, "close", None)
            if callable(close):
                try:
                    result = close()
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    logger.warning("Story target LLM client の終了処理に失敗しました", exc_info=True)
        self._target_client = None
        self._target_client_key = None
        self._target_client_owned = False

    async def generate_summary_text(
        self,
        episode: StoryEpisode,
        model: Mapping[str, Any] | None = None,
        *,
        work: StoryWork | None = None,
    ) -> str | None:
        """§8.4 の要約を 1 本生成する。失敗しても例外は投げず None を返す。"""

        if not _text(_get(episode, "body")).strip():
            return None
        self._set_usage_context(work)
        try:
            raw = await self._generate_text(
                build_story_summary_prompt(episode, work=work),
                model or {},
            )
        except Exception:
            logger.warning("Story 要約の生成に失敗しました: %s", getattr(episode, "id", None), exc_info=True)
            return None
        return _clean_summary_text(raw) or None

    async def _maybe_update_summary(
        self,
        episode: StoryEpisode,
        model: Mapping[str, Any] | None = None,
        *,
        work: StoryWork | None = None,
    ) -> None:
        """§8.4: 本文適用の直後に summary_locked=False の章だけ要約を作り直す。

        DB 書き込みは属性代入のみに留め、失敗しても本文適用をロールバックしない。
        """

        if bool(getattr(episode, "summary_locked", False)):
            return
        summary = await self.generate_summary_text(episode, model, work=work)
        if summary:
            episode.summary = summary

    async def _context_for(
        self,
        work: StoryWork,
        episode: StoryEpisode,
        model: Mapping[str, Any],
    ) -> StoryContext:
        episodes = list((await self.session.scalars(
            select(StoryEpisode)
            .where(StoryEpisode.work_id == work.id, StoryEpisode.archived_at.is_(None))
            .order_by(StoryEpisode.sort_hint, StoryEpisode.created_at)
        )).all())
        links = list((await self.session.scalars(
            select(StoryLink).where(StoryLink.work_id == work.id)
        )).all())
        by_id = {_sid(item): item for item in episodes}
        route_ids = resolve_story_route(work.start_episode_id, links, story_user_choices(work.ui_state))
        if str(episode.id) not in route_ids:
            route_ids.append(str(episode.id))
        route = [by_id[item] for item in route_ids if item in by_id]
        if episode not in route:
            route.append(episode)

        work_characters = list((await self.session.scalars(
            select(StoryWorkCharacter).where(StoryWorkCharacter.work_id == work.id)
        )).all())
        character_ids = [item.character_id for item in work_characters]
        characters = list((await self.session.scalars(
            select(StoryCharacter).where(StoryCharacter.id.in_(character_ids))
        )).all()) if character_ids else []
        work_rulebooks = list((await self.session.scalars(
            select(StoryWorkRulebook).where(StoryWorkRulebook.work_id == work.id)
        )).all())
        rulebook_ids = [item.rulebook_id for item in work_rulebooks]
        rulebooks = list((await self.session.scalars(
            select(StoryRulebook).where(StoryRulebook.id.in_(rulebook_ids))
        )).all()) if rulebook_ids else []
        notes = list((await self.session.scalars(
            select(StoryNote).where(StoryNote.work_id == work.id)
        )).all())
        return build_story_context(
            work,
            episode,
            route,
            characters=characters,
            work_characters=work_characters,
            rulebooks=rulebooks,
            work_rulebooks=work_rulebooks,
            notes=notes,
            links=links,
            model=model,
        )

    @staticmethod
    def _progress(job: StoryGenerationJob) -> dict[str, Any]:
        progress = dict(job.progress or {})
        progress.setdefault("total", 1)
        progress.setdefault("completed", 0)
        progress.setdefault("items", [])
        return progress

    async def _prepare_single_progress(self, job: StoryGenerationJob, item_id: str) -> dict[str, Any]:
        progress = self._progress(job)
        items = [dict(item) for item in progress.get("items", []) if isinstance(item, Mapping)]
        if not items:
            items = [{"episode_id": item_id, "state": "running"}]
        else:
            for item in items:
                if str(item.get("episode_id")) == item_id:
                    item["state"] = "running"
                    break
        progress["items"] = items
        await self.runner.transition(job, job.status, progress=progress)
        await self.session.commit()
        return progress

    async def _context_prompt(self, work: StoryWork, episode: StoryEpisode, payload: Mapping[str, Any], model: Mapping[str, Any]) -> tuple[str, str]:
        self._set_usage_context(work)
        context = await self._context_for(work, episode, model)
        instruction = str(payload.get("instruction") or "").strip()
        prompt = (
            f"{context.prompt}\n\n"
            "## 実行指示\n"
            "この章の本文だけを返してください。前置き、Markdown のコードフェンス、"
            "解説、ツール呼出しは不要です。\n"
            f"追加指示: {instruction or 'プロットと文体に従って本文を生成してください。'}"
        )
        return prompt, context.prompt

    async def _apply_generated_body(
        self,
        work: StoryWork,
        episode: StoryEpisode,
        payload: Mapping[str, Any],
        model: Mapping[str, Any],
        *,
        job_id: UUID | None = None,
        claim_token: str | None = None,
    ) -> dict[str, Any]:
        expected_etag = episode.body_etag or StoryRevisionService.body_etag(episode.body or "")
        prompt, _ = await self._context_prompt(work, episode, payload, model)
        body = await self._generate_text(prompt, model)
        if job_id and claim_token:
            current_job = await self._fresh_job(job_id)
            current_token = str((current_job.payload or {}).get("_execution_token") or "") if current_job else ""
            if current_token != claim_token:
                raise _StoryJobStale()
        current = await self._fresh_episode(work.id, episode.id, lock=True)
        current_etag = current.body_etag or StoryRevisionService.body_etag(current.body or "")
        if current_etag != expected_etag:
            raise StoryConflictError(current)
        revision_service = StoryRevisionService(self.session)
        latest = await self.session.scalar(
            select(StoryEpisodeRevision)
            .where(StoryEpisodeRevision.episode_id == current.id)
            .order_by(StoryEpisodeRevision.rev_no.desc())
            .limit(1)
        )
        pre = None
        if latest is None or latest.body_sha256 != revision_service.body_hash(current.body or ""):
            pre = await revision_service.create_revision(
                current,
                origin="pre_ai",
                message="AI生成前の状態",
                created_by="ai",
                force=True,
            )
        revision = await revision_service.update_body(
            current,
            body,
            expected_etag=expected_etag,
            commit=True,
            message="AI生成",
            origin="ai_generate",
            created_by="ai",
        )
        if revision is None:
            revision = await revision_service.create_revision(
                current,
                origin="ai_generate",
                message="AI生成",
                created_by="ai",
                force=True,
                body=body,
            )
        # §8.4: 生成直後の自動要約。ジョブ内の後処理として実行し、失敗は握り潰す。
        await self._maybe_update_summary(current, model, work=work)
        return {
            "episode_id": str(current.id),
            "body": body,
            "body_etag": current.body_etag,
            "char_count": current.char_count,
            "pre_revision": pre.to_dict() if pre else None,
            "revision": revision.to_dict() if revision else None,
        }

    def _schedule_episode_illustrations(
        self,
        work: StoryWork,
        episode: StoryEpisode,
        model: Mapping[str, Any] | None,
    ) -> None:
        from .story_illustration_service import (
            is_image_settings_enabled,
            run_episode_illustrations_background,
        )

        if not is_image_settings_enabled(work.image_settings):
            return

        llm_client = self.llm_client

        async def _runner() -> None:
            try:
                await run_episode_illustrations_background(
                    episode_id=episode.id,
                    work_id=work.id,
                    model=model,
                    config=self.config,
                    llm_client=llm_client,
                )
            except Exception:
                logger.warning("挿絵バックグラウンド起動に失敗: %s", episode.id, exc_info=True)

        asyncio.create_task(_runner())

    async def _run_compose(self, work: StoryWork, job: StoryGenerationJob) -> dict[str, Any]:
        self._set_usage_context(work)
        payload = dict(job.payload or {})
        count = int(payload.get("episode_count") or work.planned_episode_count or 0)
        instruction = str(payload.get("instruction") or "").strip()
        prompt = (
            "あなたは Story Studio の章構成プランナーです。次の作品情報から章構成案を作り、"
            "JSON オブジェクトだけを返してください。形式は "
            '{"episodes":[{"title":"...","plot":"..."}],"links":[{"from":0,"to":1,"choice_label":null}]} です。\n'
            f"作品名: {_text(work.title)}\n概要: {_text(work.synopsis)}\n全体プロット: {_text(work.plot)}\n"
            f"文体: {_text(work.style_guide)}\n予定章数: {count or '適切な数'}\n追加指示: {instruction or 'なし'}"
        )
        raw = await self._generate_text(prompt, payload.get("model") or {})
        return _parse_compose_result(raw)

    async def _run_generate(self, work: StoryWork, job: StoryGenerationJob) -> dict[str, Any]:
        payload = dict(job.payload or {})
        episode_id = payload.get("episode_id")
        if not episode_id:
            raise StoryNotFoundError("生成対象エピソードが指定されていません")
        episode = await self._fresh_episode(work.id, episode_id)
        progress = await self._prepare_single_progress(job, str(episode.id))
        result = await self._apply_generated_body(
            work,
            episode,
            payload,
            payload.get("model") or {},
            job_id=job.id,
            claim_token=str(payload.get("_execution_token") or ""),
        )
        self._schedule_episode_illustrations(work, episode, payload.get("model") or {})
        progress["completed"] = 1
        for item in progress["items"]:
            if str(item.get("episode_id")) == str(episode.id):
                item.update({"state": "done", "chars": result["char_count"]})
        job.progress = progress
        await self.session.flush()
        return result

    async def _run_revise(self, work: StoryWork, job: StoryGenerationJob) -> dict[str, Any]:
        payload = dict(job.payload or {})
        episode_id = payload.get("episode_id")
        if not episode_id:
            raise StoryNotFoundError("修正対象エピソードが指定されていません")
        episode = await self._fresh_episode(work.id, episode_id)
        progress = await self._prepare_single_progress(job, str(episode.id))
        prompt, _ = await self._context_prompt(work, episode, payload, payload.get("model") or {})
        prompt += (
            "\n\n## 修正モード\n"
            "既存本文を追加指示に従って全文書き換えし、提案本文だけを返してください。"
            f"\n既存本文:\n{episode.body or ''}"
        )
        proposal = await self._generate_text(prompt, payload.get("model") or {})
        progress["completed"] = 1
        for item in progress["items"]:
            if str(item.get("episode_id")) == str(episode.id):
                item.update({"state": "done", "chars": len(proposal)})
        job.progress = progress
        await self.session.flush()
        return {
            "episode_id": str(episode.id),
            "proposal": proposal,
            "base_etag": episode.body_etag or StoryRevisionService.body_etag(episode.body or ""),
        }

    async def _run_batch(self, work: StoryWork, job: StoryGenerationJob) -> dict[str, Any]:
        payload = dict(job.payload or {})
        claim_token = str(payload.get("_execution_token") or "")
        requested = [str(item) for item in (payload.get("episode_ids") or [])]
        episodes = list((await self.session.scalars(
            select(StoryEpisode)
            .where(StoryEpisode.work_id == work.id, StoryEpisode.archived_at.is_(None))
            .order_by(StoryEpisode.sort_hint, StoryEpisode.created_at)
        )).all())
        by_id = {_sid(item): item for item in episodes}
        links = list((await self.session.scalars(select(StoryLink).where(StoryLink.work_id == work.id))).all())
        route_ids = resolve_story_route(work.start_episode_id, links, story_user_choices(work.ui_state))
        selected = requested or route_ids
        ordered = [item for item in route_ids if item in selected]
        ordered.extend(item for item in selected if item not in ordered)
        if any(item not in by_id for item in ordered):
            raise StoryNotFoundError("まとめて生成の対象エピソードが作品内にありません")
        progress = self._progress(job)
        progress["total"] = len(ordered)
        existing = {str(item.get("episode_id")): dict(item) for item in progress.get("items", []) if isinstance(item, Mapping)}
        progress["items"] = [existing.get(item, {"episode_id": item, "state": "pending"}) for item in ordered]
        progress["completed"] = sum(1 for item in progress["items"] if item.get("state") == "done")
        job.progress = progress
        await self.session.flush()
        await self.session.commit()

        results: list[dict[str, Any]] = []
        for episode_id in ordered:
            current_job = await self._fresh_job(job.id)
            if current_job is None:
                raise StoryNotFoundError("ジョブが見つかりません")
            if str((current_job.payload or {}).get("_execution_token") or "") != claim_token:
                return {"episodes": results, "_stale": True}
            progress = dict(current_job.progress or progress)
            if current_job.status == "canceled":
                raise _StoryJobCanceled(progress, {"episodes": results})
            item = next(item for item in progress.get("items", []) if str(item.get("episode_id")) == episode_id)
            if item.get("state") == "done":
                continue
            item["state"] = "running"
            current_job.progress = progress
            await self.session.flush()
            await self.session.commit()
            episode = by_id[episode_id]
            try:
                result = await self._apply_generated_body(
                    work,
                    episode,
                    payload,
                    payload.get("model") or {},
                    job_id=job.id,
                    claim_token=claim_token,
                )
            except _StoryJobStale:
                raise
            except Exception as exc:
                progress = dict(current_job.progress or progress)
                item = next(item for item in progress.get("items", []) if str(item.get("episode_id")) == episode_id)
                item.update({"state": "error", "error": _redact_job_error(exc)})
                raise _StoryJobFailure(_redact_job_error(exc), progress) from exc
            self._schedule_episode_illustrations(work, episode, payload.get("model") or {})
            results.append(result)
            progress = dict(current_job.progress or progress)
            item = next(item for item in progress.get("items", []) if str(item.get("episode_id")) == episode_id)
            item.update({"state": "done", "chars": result["char_count"]})
            progress["completed"] = sum(1 for value in progress.get("items", []) if value.get("state") == "done")
            current_job.progress = progress
            await self.session.flush()
            await self.session.commit()
        return {"episodes": results}

    async def run(self, job_id: UUID | str) -> StoryGenerationJob | None:
        # Claim the queued row under a database lock so duplicate background
        # tasks cannot both invoke the LLM for one job.
        job = await self._fresh_job(job_id, lock=True)
        if job is None or job.status != "queued":
            return job
        claim_token = uuid4().hex
        try:
            work = await self.session.get(StoryWork, job.work_id)
            if work is None:
                raise StoryNotFoundError("ジョブの作品が見つかりません")
            self._set_usage_context(work)
            job.payload = {**(job.payload or {}), "_execution_token": claim_token}
            await self.runner.transition(job, "running")
            await self.session.commit()
            if job.kind == "compose":
                result = await self._run_compose(work, job)
            elif job.kind == "generate":
                result = await self._run_generate(work, job)
            elif job.kind == "revise":
                result = await self._run_revise(work, job)
            elif job.kind == "batch":
                result = await self._run_batch(work, job)
            else:
                raise StoryGraphError(f"未対応のジョブ種別です: {job.kind}")
            current = await self._fresh_job(job.id, lock=True)
            if current is None:
                return None
            if str((current.payload or {}).get("_execution_token") or "") != claim_token:
                return current
            if current.status == "canceled":
                current.result = dict(result)
                current.finished_at = datetime.utcnow()
                await self.session.commit()
                return current
            progress = dict(current.progress or {})
            progress["completed"] = progress.get("total", 1)
            for item in progress.get("items", []):
                if item.get("state") == "running":
                    item["state"] = "done"
            await self.runner.transition(current, "done", progress=progress, result=result)
            await self.session.commit()
            return current
        except _StoryJobCanceled as exc:
            await self.session.rollback()
            current = await self._fresh_job(job.id, lock=True)
            if current is not None:
                current.progress = exc.progress
                current.result = exc.result
                current.status = "canceled"
                current.finished_at = datetime.utcnow()
                await self.session.commit()
            return current
        except _StoryJobStale:
            await self.session.rollback()
            return await self._fresh_job(job.id, lock=True)
        except Exception as exc:
            progress = getattr(exc, "progress", None)
            await self.session.rollback()
            current = await self._fresh_job(job.id, lock=True)
            if current is not None and str((current.payload or {}).get("_execution_token") or "") != claim_token:
                return current
            if current is not None and current.status != "canceled":
                await self.runner.transition(
                    current,
                    "error",
                    progress=progress or current.progress or {},
                    error=_redact_job_error(exc),
                )
                await self.session.commit()
            elif current is not None:
                current.error = _redact_job_error(exc)
                current.finished_at = datetime.utcnow()
                await self.session.commit()
            logger.exception("Story job failed: %s", job_id)
            return current
        finally:
            await self.aclose()


class StorySummaryService:
    """§8.4 の要約自動生成。API 層の background task と明示再生成が使う。

    ``summary_locked`` の章は触らない。``force=True``（インスペクタの「AI で要約を
    再生成」）だけがロックを解除して作り直す。
    """

    def __init__(self, session: AsyncSession, llm_client: Any, *, config: Any | None = None):
        self.session = session
        self._executor = StoryJobExecutor(session, llm_client, config=config)

    async def aclose(self) -> None:
        await self._executor.aclose()

    async def generate(
        self,
        episode_id: UUID | str,
        *,
        model: Mapping[str, Any] | None = None,
        force: bool = False,
    ) -> StoryEpisode | None:
        episode = await self.session.get(StoryEpisode, _uuid(episode_id))
        if episode is None or episode.archived_at is not None:
            return None
        if bool(episode.summary_locked):
            if not force:
                return None
            # 明示再生成はロック解除を先に確定させ、生成が失敗しても解除は残す。
            episode.summary_locked = False
            await self.session.commit()
        work = await self.session.get(StoryWork, episode.work_id)
        summary = await self._executor.generate_summary_text(episode, model, work=work)
        if not summary:
            return None
        episode.summary = summary
        episode.summary_locked = False
        await self.session.commit()
        return episode


__all__ = [
    "STORY_AI_REVISION_ORIGINS",
    "STORY_AUTO_REVISION_INTERVAL",
    "STORY_REVISION_ORIGINS",
    "STORY_SUMMARY_MAX_CHARS",
    "StoryContext",
    "StoryConflictError",
    "StoryEpisodeService",
    "StorySearchService",
    "StoryGraphError",
    "StoryGraphService",
    "StoryJobExecutor",
    "StoryJobRunner",
    "StoryModelResolution",
    "StoryModelResolver",
    "StoryNotFoundError",
    "StoryRevisionAuthor",
    "StoryRevisionOrigin",
    "StoryRevisionService",
    "StorySummaryService",
    "StoryWorkService",
    "build_story_context",
    "build_story_summary_prompt",
    "resolve_story_model",
    "resolve_story_route",
    "validate_story_graph",
]
