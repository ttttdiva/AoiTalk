"""画面録画+音声説明から Skill ドラフトを自動生成するサービス。

Anthropic Cowork の "Record a skill" 相当のバックエンド。webm 録画を受け取り、
ffmpeg で音声とフレームを抽出し、既存の ``MediaRecognitionService``（音声文字起こし /
画面フレーム要約）と LLM 1 回の生成を組み合わせて SKILL.md ドラフトを作る。

状態機械は録画ディレクトリ内の ``metadata.json`` で管理する:
    uploaded -> analyzing -> draft_ready | failed

このモジュールは ``main.py`` から状態を import せず、設定と保存先だけに依存する。
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

logger = logging.getLogger(__name__)


def _turn_context_scope(metadata: dict[str, Any]) -> dict[str, Optional[str]]:
    """Resolve the immutable usage scope for one recording analysis.

    Recording metadata is the durable source of identity for background work.
    Legacy recordings may not contain the newer fields, so the task-local
    context is used only as a backwards-compatible fallback.  The returned
    mapping is a fresh object and is never stored on a shared service/client.
    """

    try:
        from .turn_context import get_turn_context

        current = get_turn_context()
    except Exception:  # pragma: no cover - import/runtime compatibility
        current = None

    def _value(key: str, *aliases: str) -> Optional[str]:
        for candidate in (key, *aliases):
            value = metadata.get(candidate)
            if value is not None and str(value).strip():
                return str(value).strip()
        for candidate in (key, *aliases):
            value = getattr(current, candidate, None) if current is not None else None
            if value is not None and str(value).strip():
                return str(value).strip()
        return None

    # Keep the historical no-auth accounting identity while preserving the
    # authenticated principal whenever one is available.
    user_id = _value("user_id") or "default_user"
    return {
        "user_id": user_id,
        "project_id": _value("project_id"),
        "session_id": _value("session_id", "conversation_session_id"),
    }

# ステータス（状態機械）
STATUS_UPLOADED = "uploaded"
STATUS_ANALYZING = "analyzing"
STATUS_DRAFT_READY = "draft_ready"
STATUS_FAILED = "failed"
VALID_STATUSES = {STATUS_UPLOADED, STATUS_ANALYZING, STATUS_DRAFT_READY, STATUS_FAILED}

# 保存ファイル名
VIDEO_FILENAME = "recording.webm"
METADATA_FILENAME = "metadata.json"
DRAFT_FILENAME = "draft.json"

# アップロード上限（500MB）
MAX_UPLOAD_BYTES = 500 * 1024 * 1024
# フレーム抽出の上限枚数と幅
MAX_FRAMES = 16
FRAME_WIDTH = 1280
# シーン変化検出のしきい値
SCENE_THRESHOLD = 0.25

# ドラフトに含める SKILL.md の必須セクション見出し（順序固定）
SKILL_SECTIONS = ("目的", "入力", "手順", "判断規則", "完了条件")

# 秘匿情報らしき文字列を検出する簡易パターン（ドラフト混入の最終防波堤）
_SECRET_PATTERNS = (
    re.compile(r"password", re.IGNORECASE),
    re.compile(r"passwd", re.IGNORECASE),
    re.compile(r"api[_\-\s]?key", re.IGNORECASE),
    re.compile(r"secret", re.IGNORECASE),
    re.compile(r"token", re.IGNORECASE),
    re.compile(r"パスワード"),
)


class SkillRecordingError(Exception):
    """スキル録画処理のエラー。"""


class RecordingNotFoundError(SkillRecordingError):
    """指定された録画が存在しない。"""


# ---------------------------------------------------------------------------
# 純粋関数（SKILL.md の生成 / パース）— テスト対象
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(
    r"---\r?\n(?P<frontmatter>.*?)\r?\n---(?:\r?\n)?(?P<body>.*)",
    flags=re.DOTALL,
)


def _slugify_name(value: str) -> str:
    """スキル名に使える安全な slug を作る。"""
    slug = re.sub(r"[^0-9A-Za-z_\-]+", "-", (value or "").strip())
    slug = re.sub(r"-{2,}", "-", slug).strip("-").lower()
    return slug[:60] or "recorded-skill"


def normalize_trigger_mode(value: Any) -> str:
    """trigger_mode を manual/auto/both のいずれかへ正規化する。"""
    text = str(value or "").strip().lower()
    return text if text in {"manual", "auto", "both"} else "both"


def build_skill_markdown(
    *,
    name: str,
    description: str,
    trigger_mode: str,
    bound_tools: list[str],
    body: str,
) -> str:
    """frontmatter + 本文の SKILL.md 文字列を組み立てる。"""
    frontmatter = {
        "name": name,
        "description": description,
        "trigger_mode": normalize_trigger_mode(trigger_mode),
        "bound_tools": list(bound_tools or []),
    }
    dumped = yaml.safe_dump(
        frontmatter,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    ).strip()
    return f"---\n{dumped}\n---\n\n{(body or '').strip()}\n"


def parse_skill_markdown(markdown: str) -> dict[str, Any]:
    """SKILL.md の frontmatter と本文を解析して dict を返す。

    frontmatter が無い場合は全体を本文として扱う。
    """
    text = markdown or ""
    match = _FRONTMATTER_RE.fullmatch(text.strip())
    if match is None:
        return {
            "name": "",
            "description": "",
            "trigger_mode": "both",
            "bound_tools": [],
            "body": text.strip(),
        }
    data = yaml.safe_load(match.group("frontmatter")) or {}
    if not isinstance(data, dict):
        data = {}
    bound = data.get("bound_tools") or []
    if not isinstance(bound, list):
        bound = []
    return {
        "name": str(data.get("name") or "").strip(),
        "description": str(data.get("description") or "").strip(),
        "trigger_mode": normalize_trigger_mode(data.get("trigger_mode")),
        "bound_tools": [str(item) for item in bound if isinstance(item, str)],
        "body": match.group("body").strip(),
    }


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    """dict / Config どちらでもドット区切りキーで値を読む。"""
    if config is None:
        return default
    if isinstance(config, dict):
        value: Any = config
        for part in key.split("."):
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                return default
        return value
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except TypeError:
            pass
    return default


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@dataclass
class FrameNote:
    """1 フレームの時刻と画面要約。"""

    time_sec: float
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {"time_sec": round(float(self.time_sec), 2), "note": self.note}


class SkillRecordingService:
    """録画のストレージ管理と解析パイプラインを担うサービス。"""

    def __init__(
        self,
        config: Any = None,
        *,
        storage_dir: str | os.PathLike[str] | None = None,
        media_service_factory: Optional[Callable[[Any], Any]] = None,
    ):
        self.config = config
        self._storage_override = storage_dir
        # MediaRecognitionService を差し替え可能にする（テストでスタブ化するため）。
        self._media_service_factory = media_service_factory

    # -- ストレージ ---------------------------------------------------------

    def get_storage_root(self) -> Path:
        """録画保存のルートディレクトリを返す（存在しなければ作成しない）。"""
        configured = (
            self._storage_override
            or os.environ.get("AOITALK_SKILL_RECORDINGS_DIR")
            or _config_get(self.config, "skill_recording.storage_dir", None)
            or "data/skill_recordings"
        )
        root = Path(str(configured)).expanduser()
        if not root.is_absolute():
            root = _repo_root() / root
        return root.resolve()

    @staticmethod
    def new_recording_id() -> str:
        return uuid.uuid4().hex

    def recording_dir(self, recording_id: str) -> Path:
        """録画 ID からディレクトリを解決する（パストラバーサル対策込み）。"""
        if not re.fullmatch(r"[0-9A-Za-z_\-]+", recording_id or ""):
            raise RecordingNotFoundError("録画が見つかりません")
        root = self.get_storage_root()
        target = (root / recording_id).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise RecordingNotFoundError("録画が見つかりません") from exc
        return target

    def video_path(self, recording_id: str) -> Path:
        return self.recording_dir(recording_id) / VIDEO_FILENAME

    def _metadata_path(self, recording_id: str) -> Path:
        return self.recording_dir(recording_id) / METADATA_FILENAME

    def _draft_path(self, recording_id: str) -> Path:
        return self.recording_dir(recording_id) / DRAFT_FILENAME

    # -- メタデータ（状態機械）---------------------------------------------

    def read_metadata(self, recording_id: str) -> dict[str, Any]:
        path = self._metadata_path(recording_id)
        if not path.exists():
            raise RecordingNotFoundError("録画が見つかりません")
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_metadata(self, recording_id: str, metadata: dict[str, Any]) -> None:
        path = self._metadata_path(recording_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        metadata["updated_at"] = datetime.now(timezone.utc).isoformat()
        path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def create_uploaded(
        self,
        recording_id: str,
        *,
        title: str,
        project_id: Optional[str],
        size_bytes: int,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """アップロード完了時のメタデータを status=uploaded で作成する。"""
        metadata = {
            "id": recording_id,
            "status": STATUS_UPLOADED,
            "title": (title or "").strip() or "録画スキル",
            "project_id": project_id or None,
            "user_id": str(user_id) if user_id else None,
            "session_id": str(session_id) if session_id else None,
            "error": None,
            "size_bytes": int(size_bytes),
            "video_filename": VIDEO_FILENAME,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write_metadata(recording_id, metadata)
        return metadata

    def _set_status(
        self,
        recording_id: str,
        status: str,
        *,
        error: Optional[str] = None,
    ) -> dict[str, Any]:
        metadata = self.read_metadata(recording_id)
        metadata["status"] = status
        metadata["error"] = error
        self._write_metadata(recording_id, metadata)
        return metadata

    def status_view(self, recording_id: str) -> dict[str, Any]:
        """GET 用のステータス表現を返す。"""
        metadata = self.read_metadata(recording_id)
        return {
            "id": metadata.get("id", recording_id),
            "status": metadata.get("status", STATUS_UPLOADED),
            "error": metadata.get("error"),
            "title": metadata.get("title"),
            "project_id": metadata.get("project_id"),
            "created_at": metadata.get("created_at"),
        }

    def get_draft(self, recording_id: str) -> Optional[dict[str, Any]]:
        """draft_ready のとき draft を返す。それ以外は None。"""
        metadata = self.read_metadata(recording_id)
        if metadata.get("status") != STATUS_DRAFT_READY:
            return None
        path = self._draft_path(recording_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def delete_recording(self, recording_id: str) -> bool:
        """録画ディレクトリと成果物を削除する。"""
        target = self.recording_dir(recording_id)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
            return True
        return False

    # -- 解析の起動 ---------------------------------------------------------

    def begin_analysis(self, recording_id: str) -> bool:
        """解析開始を冪等にマークする。

        uploaded / failed からのみ analyzing へ遷移させ、新規開始なら True を返す。
        既に analyzing / draft_ready の場合は False（二重起動しない）。
        """
        metadata = self.read_metadata(recording_id)
        status = metadata.get("status")
        if status in {STATUS_ANALYZING, STATUS_DRAFT_READY}:
            return False
        self._set_status(recording_id, STATUS_ANALYZING, error=None)
        return True

    async def analyze(self, recording_id: str) -> None:
        """解析パイプラインを実行し、draft_ready か failed へ遷移させる。"""
        turn_token = None
        try:
            metadata = self.read_metadata(recording_id)
            # Background analysis has no request object.  Bind the durable
            # recording identity to this task only; ContextVar propagation
            # also reaches ``asyncio.to_thread`` used by ffmpeg/provider calls.
            from .turn_context import reset_turn_context, set_turn_context

            scope = _turn_context_scope(metadata)
            turn_token = set_turn_context(**scope)
            video_path = self.video_path(recording_id)
            if not video_path.exists():
                raise SkillRecordingError("録画ファイルが見つかりません")

            media_factory = self._make_media_service
            try:
                media_parameters = inspect.signature(media_factory).parameters
            except (TypeError, ValueError):
                media_parameters = {}
            if "usage_context" in media_parameters or any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in media_parameters.values()
            ):
                media = media_factory(scope)
            else:
                # Keep compatibility with tests/deployments that replace the
                # internal maker with a legacy zero-argument callable.
                media = media_factory()

            # 1. 音声抽出 -> 文字起こし
            transcript = await self._transcribe(media, video_path, metadata)

            # 2. フレーム抽出 -> 画面要約
            frame_notes = await self._summarize_frames(media, video_path)

            # 3. 文字起こし + フレーム要約を統合して SKILL.md ドラフト生成
            draft = await self._generate_draft(
                metadata=metadata,
                transcript=transcript,
                frame_notes=frame_notes,
            )

            self._draft_path(recording_id).write_text(
                json.dumps(draft, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            self._set_status(recording_id, STATUS_DRAFT_READY, error=None)
            logger.info("[SkillRecording] 解析完了: %s", recording_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[SkillRecording] 解析失敗: %s", recording_id)
            try:
                self._set_status(recording_id, STATUS_FAILED, error=str(exc))
            except Exception:  # noqa: BLE001
                logger.debug("[SkillRecording] failed 状態の書き込みにも失敗", exc_info=True)
        finally:
            # Always restore the caller's task-local context, including when
            # metadata, ffmpeg, media recognition, or LLM generation fails.
            if turn_token is not None:
                try:
                    from .turn_context import reset_turn_context

                    reset_turn_context(turn_token)
                except Exception:  # pragma: no cover - defensive cleanup
                    logger.debug("[SkillRecording] turn context reset failed", exc_info=True)

    def _make_media_service(
        self,
        usage_context: Optional[dict[str, Optional[str]]] = None,
    ) -> Any:
        """Create a media recognizer with request-local usage scope.

        Test/deployment factories from before the usage-scope API commonly
        accept only ``config``.  Inspect the callable instead of catching a
        broad ``TypeError`` so an error raised *inside* a factory is not
        accidentally retried and hidden.
        """

        if self._media_service_factory is not None:
            factory = self._media_service_factory
            try:
                parameters = inspect.signature(factory).parameters
            except (TypeError, ValueError):
                parameters = {}
            accepts_scope = (
                "usage_context" in parameters
                or any(
                    parameter.kind == inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters.values()
                )
            )
            if accepts_scope:
                return factory(self.config, usage_context=usage_context)
            return factory(self.config)
        from .media_recognition_service import MediaRecognitionService

        return MediaRecognitionService(self.config, usage_context=usage_context)

    # -- パイプライン各段 ---------------------------------------------------

    async def _transcribe(
        self,
        media: Any,
        video_path: Path,
        metadata: dict[str, Any],
    ) -> str:
        """音声を抽出し既存の音声認識経路で文字起こしする。失敗しても空文字で継続。"""
        try:
            audio_data_url = await asyncio.to_thread(self._extract_audio_data_url, video_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[SkillRecording] 音声抽出に失敗（文字起こしをスキップ）: %s", exc)
            return ""
        if not audio_data_url:
            return ""
        result = await media.recognize_audio(
            "この操作説明の音声を話者を区別して文字起こししてください。",
            {"name": "recording-audio", "data": audio_data_url},
        )
        if getattr(result, "error", ""):
            logger.warning("[SkillRecording] 文字起こしエラー: %s", result.error)
            return ""
        return str(getattr(result, "result", "") or "").strip()

    async def _summarize_frames(self, media: Any, video_path: Path) -> list[FrameNote]:
        """フレームを抽出し、各フレームの画面内容を要約する。"""
        frames = await asyncio.to_thread(self._extract_frames, video_path)
        if not frames:
            return []
        images = [
            {
                "name": f"frame-{index:02d}-{time_sec:.1f}s",
                "data": "data:image/jpeg;base64," + base64.b64encode(jpg_bytes).decode("ascii"),
            }
            for index, (time_sec, jpg_bytes) in enumerate(frames)
        ]
        results = await media.recognize_images(
            "この操作画面のフレームで何が行われているかを簡潔に日本語で説明してください。"
            "可視テキスト（メニュー名・ボタン名・入力値）も含めてください。"
            "ただしパスワードやトークンなど秘匿情報らしき文字列は書かないでください。",
            images,
        )
        notes: list[FrameNote] = []
        for (time_sec, _bytes), result in zip(frames, results):
            note = str(getattr(result, "result", "") or "").strip()
            if getattr(result, "error", ""):
                continue
            if note:
                notes.append(FrameNote(time_sec=time_sec, note=_redact_secrets(note)))
        return notes

    async def _generate_draft(
        self,
        *,
        metadata: dict[str, Any],
        transcript: str,
        frame_notes: list[FrameNote],
    ) -> dict[str, Any]:
        """文字起こし + フレーム要約から SKILL.md ドラフトを 1 回の LLM 呼び出しで作る。"""
        available_tools = self._available_tool_names()
        prompt = self._build_generation_prompt(
            title=str(metadata.get("title") or ""),
            transcript=transcript,
            frame_notes=frame_notes,
            available_tools=available_tools,
        )
        raw = await self._call_llm(prompt)
        parsed = parse_skill_markdown(raw)

        default_name = _slugify_name(metadata.get("title") or "recorded-skill")
        name = _slugify_name(parsed.get("name") or default_name)
        description = parsed.get("description") or str(metadata.get("title") or "録画から生成したスキル")
        trigger_mode = normalize_trigger_mode(parsed.get("trigger_mode"))
        bound_tools = self._filter_bound_tools(parsed.get("bound_tools") or [], available_tools)
        body = parsed.get("body") or ""

        # 正規化した frontmatter で SKILL.md を組み直す。
        markdown = build_skill_markdown(
            name=name,
            description=description,
            trigger_mode=trigger_mode,
            bound_tools=bound_tools,
            body=body,
        )
        return {
            "name": name,
            "description": description,
            "markdown": markdown,
            "trigger_mode": trigger_mode,
            "bound_tools": bound_tools,
            "transcript": transcript,
            "frame_notes": [fn.to_dict() for fn in frame_notes],
        }

    async def _call_llm(self, prompt: str) -> str:
        """既存の LLM クライアントで 1 回だけテキスト生成する。"""

        def _run() -> str:
            from ..llm.manager import create_llm_client
            from .turn_context import get_turn_context

            client = create_llm_client(self.config)
            turn = get_turn_context()
            user_id = getattr(turn, "user_id", None) or "default_user"
            session_id = getattr(turn, "session_id", None)
            project_id = getattr(turn, "project_id", None)

            # ``create_llm_client`` deliberately returns a fresh provider
            # client for this analysis.  Apply the scope to that instance only
            # so its normal TokenUsage path records the authenticated user,
            # session, and project without mutating a singleton/shared client.
            metadata = {
                key: value
                for key, value in (
                    ("session_id", session_id),
                    ("project_id", project_id),
                )
                if value
            }
            setter = getattr(client, "set_session_context", None)
            if callable(setter):
                try:
                    setter(user_id=str(user_id), metadata=metadata or None)
                except TypeError:
                    # Small test/third-party clients may expose only user_id.
                    setter(user_id=str(user_id))
            else:
                # Keep compatibility with simple provider adapters that do
                # not expose the optional setter, while still confining all
                # writes to this fresh client instance.
                try:
                    setattr(client, "session_user_id", str(user_id))
                except Exception:
                    pass
            for attribute, value in (
                ("current_session_id", session_id),
                ("current_project_id", project_id),
            ):
                if value is None:
                    continue
                try:
                    setattr(client, attribute, str(value))
                except Exception:
                    logger.debug(
                        "[SkillRecording] LLM usage scope %s の設定に失敗",
                        attribute,
                        exc_info=True,
                    )

            generate = getattr(client, "generate_response", None)
            if callable(generate):
                return str(generate(prompt, stream=False) or "")
            fallback = getattr(client, "generate", None)
            if callable(fallback):
                return str(fallback(prompt) or "")
            raise SkillRecordingError("LLM クライアントがテキスト生成に対応していません")

        return await asyncio.to_thread(_run)

    def _available_tool_names(self) -> list[str]:
        """ツールレジストリに実在するツール名一覧を返す。"""
        try:
            from ..tools.registry import get_registry

            return list(get_registry().get_names())
        except Exception as exc:  # noqa: BLE001
            logger.debug("[SkillRecording] ツールレジストリ取得に失敗: %s", exc)
            return []

    @staticmethod
    def _filter_bound_tools(proposed: list[str], available: list[str]) -> list[str]:
        """実在するツール名だけを残す（実在しない提案は除去）。"""
        allowed = set(available)
        seen: set[str] = set()
        result: list[str] = []
        for name in proposed:
            key = str(name).strip()
            if key and key in allowed and key not in seen:
                seen.add(key)
                result.append(key)
        return result

    @staticmethod
    def _build_generation_prompt(
        *,
        title: str,
        transcript: str,
        frame_notes: list[FrameNote],
        available_tools: list[str],
    ) -> str:
        timeline = "\n".join(
            f"- [{fn.time_sec:.1f}s] {fn.note}" for fn in frame_notes
        ) or "(フレーム要約なし)"
        tools_block = ", ".join(available_tools) if available_tools else "(利用可能なツールなし)"
        transcript_block = transcript.strip() or "(音声の文字起こしなし)"
        sections = " / ".join(f"# {name}" for name in SKILL_SECTIONS)
        return f"""あなたは操作録画から再利用可能な Skill 定義（SKILL.md）を作る専門家です。
以下の「音声の文字起こし」と「画面フレームの時系列要約」から、この操作手順を
別のエージェントが再現できる SKILL.md を日本語で 1 つ作成してください。

# 出力形式（厳守）
- 先頭に YAML frontmatter を置く。キーは name / description / trigger_mode / bound_tools。
  - name: 英小文字・数字・ハイフンの slug（例: reset-user-password）
  - description: このスキルを使う場面が分かる 1 文
  - trigger_mode: manual / auto / both のいずれか
  - bound_tools: 下の「利用可能なツール」に存在する名前だけの配列。無ければ空配列 []。
- frontmatter の後に本文として次の見出しを順に置く: {sections}
  - 「# 手順」は録画から読み取れる具体的な操作を順序立てて書く。

# 制約
- 実在しないツール名を bound_tools に書かない。
- パスワード・トークン・APIキーなど秘匿情報らしき文字列は本文にも frontmatter にも書かない。
  もし要約に含まれていても、伏せ字や「(認証情報)」に置き換える。
- 憶測で手順を捏造せず、素材から読み取れる範囲で書く。

# 録画タイトル
{title or "(無題)"}

# 利用可能なツール
{tools_block}

# 音声の文字起こし
{transcript_block}

# 画面フレームの時系列要約
{timeline}
"""

    # -- ffmpeg 抽出 --------------------------------------------------------

    def _extract_audio_data_url(self, video_path: Path) -> str:
        """録画から音声を 16kHz mono WAV で抽出し data URL を返す。"""
        try:
            completed = subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(video_path),
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-f",
                    "wav",
                    "pipe:1",
                ],
                check=True,
                capture_output=True,
            )
        except FileNotFoundError as exc:
            raise SkillRecordingError(
                "ffmpeg が見つかりません。録画の音声を抽出できません。"
            ) from exc
        except subprocess.CalledProcessError as exc:
            # 音声トラックが無い録画などはここに来る。呼び出し側で空扱い。
            raise SkillRecordingError("録画から音声を抽出できませんでした") from exc
        if not completed.stdout:
            return ""
        return "data:audio/wav;base64," + base64.b64encode(completed.stdout).decode("ascii")

    def _extract_frames(self, video_path: Path) -> list[tuple[float, bytes]]:
        """シーン変化検出 + 一定間隔フォールバックでフレームを抽出する。

        返り値は (時刻秒, JPEGバイト列) のリスト（最大 MAX_FRAMES 枚、幅 FRAME_WIDTH）。
        """
        duration = self._probe_duration(video_path)
        timestamps = self._select_frame_timestamps(video_path, duration)
        frames: list[tuple[float, bytes]] = []
        for time_sec in timestamps:
            jpg = self._grab_frame(video_path, time_sec)
            if jpg:
                frames.append((time_sec, jpg))
            if len(frames) >= MAX_FRAMES:
                break
        return frames

    def _probe_duration(self, video_path: Path) -> Optional[float]:
        """録画の長さ（秒）を取得する。取得できなければ None。"""
        try:
            completed = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(video_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            value = (completed.stdout or "").strip()
            return float(value) if value else None
        except (FileNotFoundError, subprocess.CalledProcessError, ValueError):
            return None

    def _select_frame_timestamps(
        self,
        video_path: Path,
        duration: Optional[float],
    ) -> list[float]:
        """シーン変化時刻と一定間隔時刻を統合したフレーム抽出時刻を返す。"""
        scene_times = self._detect_scene_times(video_path)
        interval_times: list[float] = []
        if duration and duration > 0:
            # 一定間隔フォールバック（先頭付近を必ず含める）。
            count = min(MAX_FRAMES, max(4, int(duration // 5) + 1))
            step = duration / (count + 1)
            interval_times = [round(step * (i + 1), 2) for i in range(count)]
            interval_times.insert(0, 0.5)
        elif not scene_times:
            # 長さ不明かつシーン検出なし: 固定の候補時刻で試す。
            interval_times = [0.5, 2.0, 5.0, 10.0, 20.0, 40.0, 60.0, 90.0]

        merged: list[float] = []
        seen: set[int] = set()
        for time_sec in sorted(set(scene_times) | set(interval_times)):
            if time_sec < 0:
                continue
            if duration and time_sec >= duration:
                continue
            bucket = int(round(time_sec))  # 1秒粒度で重複排除
            if bucket in seen:
                continue
            seen.add(bucket)
            merged.append(round(time_sec, 2))
        return merged[:MAX_FRAMES]

    def _detect_scene_times(self, video_path: Path) -> list[float]:
        """シーン変化のタイムスタンプ（pts_time）を検出する。"""
        try:
            with_meta = subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-i",
                    str(video_path),
                    "-filter_complex",
                    f"select='gt(scene,{SCENE_THRESHOLD})',metadata=print",
                    "-an",
                    "-f",
                    "null",
                    "-",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise SkillRecordingError(
                "ffmpeg が見つかりません。録画のフレームを抽出できません。"
            ) from exc
        # metadata=print は stderr に "pts_time:<value>" を出す。
        times: list[float] = []
        for match in re.finditer(r"pts_time:([0-9.]+)", with_meta.stderr or ""):
            try:
                times.append(float(match.group(1)))
            except ValueError:
                continue
        return times

    def _grab_frame(self, video_path: Path, time_sec: float) -> Optional[bytes]:
        """指定時刻の 1 フレームを幅 FRAME_WIDTH の JPEG で取り出す。"""
        try:
            completed = subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    f"{max(time_sec, 0):.3f}",
                    "-i",
                    str(video_path),
                    "-frames:v",
                    "1",
                    "-vf",
                    f"scale={FRAME_WIDTH}:-2",
                    "-f",
                    "image2",
                    "-vcodec",
                    "mjpeg",
                    "pipe:1",
                ],
                check=False,
                capture_output=True,
            )
        except FileNotFoundError as exc:
            raise SkillRecordingError(
                "ffmpeg が見つかりません。録画のフレームを抽出できません。"
            ) from exc
        return completed.stdout or None

    # -- 保存（スキル化）---------------------------------------------------

    def save_skill(
        self,
        recording_id: str,
        *,
        name: str,
        description: str,
        markdown: str,
        trigger_mode: str,
        target: str,
        project_id: Optional[str] = None,
        delete_recording: bool = True,
    ) -> dict[str, Any]:
        """ドラフトをグローバル / プロジェクトのスキルとして保存する。"""
        # 録画の存在確認（不正 ID を弾く）。
        self.read_metadata(recording_id)

        clean_name = _slugify_name(name)
        if not clean_name:
            raise SkillRecordingError("スキル名が不正です")
        clean_trigger = normalize_trigger_mode(trigger_mode)
        parsed = parse_skill_markdown(markdown)
        bound_tools = self._filter_bound_tools(
            parsed.get("bound_tools") or [],
            self._available_tool_names(),
        )
        body = parsed.get("body") or markdown.strip()

        if target == "global":
            saved = self._save_global(
                name=clean_name,
                description=description,
                markdown=markdown,
                trigger_mode=clean_trigger,
                bound_tools=bound_tools,
            )
        elif target == "project":
            if not project_id:
                raise SkillRecordingError("プロジェクト保存には project_id が必要です")
            saved = self._save_project(
                project_id=project_id,
                name=clean_name,
                description=description,
                trigger_mode=clean_trigger,
                bound_tools=bound_tools,
                body=body,
            )
        else:
            raise SkillRecordingError(f"不正な保存先です: {target}")

        if delete_recording:
            self.delete_recording(recording_id)
            saved["recording_deleted"] = True
        else:
            saved["recording_deleted"] = False
        return saved

    def _save_global(
        self,
        *,
        name: str,
        description: str,
        markdown: str,
        trigger_mode: str,
        bound_tools: list[str],
    ) -> dict[str, Any]:
        from ..skills.models import SkillDefinition, SkillTriggerMode
        from ..skills.loader import save_skill_to_yaml
        from ..skills.registry import register_skill

        try:
            mode = SkillTriggerMode(trigger_mode)
        except ValueError:
            mode = SkillTriggerMode.BOTH
        skill = SkillDefinition(
            name=name,
            description=description,
            prompt_template=markdown,
            trigger_mode=mode,
            bound_tools=bound_tools,
        )
        if not save_skill_to_yaml(skill):
            raise SkillRecordingError("スキル YAML の保存に失敗しました")
        register_skill(skill)
        return {
            "target": "global",
            "name": name,
            "path": str((self._global_skills_dir() / f"{name}.yaml")),
        }

    @staticmethod
    def _global_skills_dir() -> Path:
        from ..skills.loader import SKILLS_DIR

        return SKILLS_DIR

    def _save_project(
        self,
        *,
        project_id: str,
        name: str,
        description: str,
        trigger_mode: str,
        bound_tools: list[str],
        body: str,
    ) -> dict[str, Any]:
        from uuid import UUID
        from ..services.project_workspace_cleanup import get_project_workspace_path
        from ..skills.loader import load_project_skills

        try:
            project_uuid = UUID(str(project_id))
        except (ValueError, TypeError) as exc:
            raise SkillRecordingError("project_id が不正です") from exc

        # 名前は単一のパスセグメントに限定する（区切り文字・親参照を拒否）。
        if not name or "/" in name or "\\" in name or name in {".", ".."}:
            raise SkillRecordingError("スキル名が不正です")

        workspace = get_project_workspace_path(project_uuid)
        skills_root = (workspace / ".agents" / "skills").resolve()
        skill_dir = (skills_root / name).resolve()
        # パストラバーサル対策: スキルディレクトリ直下に収まることを検証する。
        try:
            skill_dir.relative_to(skills_root)
        except ValueError as exc:
            raise SkillRecordingError("スキルの保存先が workspace 外を指しています") from exc

        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_md_path = skill_dir / "SKILL.md"
        skill_md_path.write_text(
            build_skill_markdown(
                name=name,
                description=description,
                trigger_mode=trigger_mode,
                bound_tools=bound_tools,
                body=body,
            ),
            encoding="utf-8",
        )
        # レジストリへ反映。
        load_project_skills(str(project_id))
        return {
            "target": "project",
            "name": name,
            "project_id": project_id,
            "path": str(skill_md_path),
        }


def _redact_secrets(text: str) -> str:
    """秘匿情報らしき語を含む行を伏せる。"""
    lines = text.splitlines()
    cleaned: list[str] = []
    for line in lines:
        if any(pattern.search(line) for pattern in _SECRET_PATTERNS):
            cleaned.append("(認証情報らしき記述を伏せました)")
        else:
            cleaned.append(line)
    return "\n".join(cleaned)
