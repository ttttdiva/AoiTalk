"""Project folder organizer for durable project information."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.models import Project, ProjectContextPack
from .project_information_docs import update_project_information_doc

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {
    ".txt",
    ".md",
    ".csv",
    ".json",
    ".yaml",
    ".yml",
    ".xml",
    ".log",
    ".docx",
    ".xlsx",
    ".pptx",
    ".pdf",
}

DEFAULT_CATEGORIES: list[dict[str, Any]] = [
    {
        "key": "overview",
        "label": "概要",
        "description": "案件の目的・範囲・前提を置く入口カテゴリ。",
        "sort_order": 0,
    },
    {
        "key": "important_documents",
        "label": "重要資料",
        "description": "作業時に参照する正本資料、台帳、設計書。",
        "sort_order": 10,
    },
    {
        "key": "decisions",
        "label": "決定事項",
        "description": "顧客・社内・ベンダー間で決まったこと。",
        "sort_order": 20,
    },
    {
        "key": "open_questions",
        "label": "要確認",
        "description": "未確定事項、回答待ち、確認依頼。",
        "sort_order": 30,
    },
    {
        "key": "architecture",
        "label": "構成",
        "description": "構成図、接続関係、環境一覧。",
        "sort_order": 40,
    },
    {
        "key": "detail_design",
        "label": "詳細設計",
        "description": "パラメーター、設定値、詳細設計。",
        "sort_order": 50,
    },
    {
        "key": "verification",
        "label": "検証",
        "description": "テスト計画、検証項目、結果報告。",
        "sort_order": 60,
    },
]

CATEGORY_LABELS = {item["key"]: item["label"] for item in DEFAULT_CATEGORIES}


@dataclass
class ScannedProjectFile:
    path: str
    name: str
    extension: str
    size_bytes: int
    modified_at: str
    extracted_text: str = ""
    extract_error: str | None = None


@dataclass
class DraftDocument:
    title: str
    file_path: str
    document_type: str = "document"
    category_key: str = "important_documents"
    role: str = "reference"
    is_primary: bool = False
    ai_access_level: str = "read"
    description: str = ""
    notes: str = ""


@dataclass
class DraftFact:
    title: str
    content: str
    category_key: str = "overview"
    fact_type: str = "fact"
    importance: int = 5
    source_ref: str = ""


@dataclass
class DraftCategory:
    key: str
    label: str
    description: str = ""
    status: str = "active"
    sort_order: float = 100


@dataclass
class OrganizationDraft:
    source_folder: str
    generated_by: str
    summary_md: str
    categories: list[DraftCategory] = field(default_factory=list)
    documents: list[DraftDocument] = field(default_factory=list)
    facts: list[DraftFact] = field(default_factory=list)
    goals: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)


def _clean_text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return text or fallback


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")


def _category_key(value: str, fallback: str = "custom") -> str:
    key = _slug(value)
    return key[:120] if key else fallback[:120]


def _clip(value: str, limit: int) -> str:
    value = value.strip()
    return value if len(value) <= limit else f"{value[:limit].rstrip()}..."


def _looks_like_table_row(value: str) -> bool:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    return any(line.startswith("|") and line.endswith("|") for line in lines)


def normalize_project_folder_path(project_id: UUID | str, value: str | None) -> str:
    """Normalize a project-root-relative folder path from UI or filer paths."""
    text = (value or "").replace("\\", "/").strip("/")
    if not text:
        return ""
    prefix = f"_projects/project_{project_id}/"
    if text == f"_projects/project_{project_id}":
        return ""
    if text.startswith(prefix):
        text = text[len(prefix) :]
    parts = [part for part in text.split("/") if part and part not in {".", ".."}]
    return "/".join(parts)


def resolve_project_folder(
    storage_root: Path,
    project_id: UUID | str,
    requested_path: str | None,
) -> tuple[Path, str]:
    relative_path = normalize_project_folder_path(project_id, requested_path)
    target = (storage_root / relative_path).resolve()
    root = storage_root.resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("保存先がプロジェクトフォルダ外です") from exc
    if not target.exists():
        raise FileNotFoundError("対象フォルダが見つかりません")
    if not target.is_dir():
        raise ValueError("対象はフォルダではありません")
    return target, relative_path


def _extract_file_text(file_path: Path, max_chars: int) -> tuple[str, str | None]:
    ext = file_path.suffix.lower()
    try:
        if ext in {".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".xml", ".log"}:
            return _clip(file_path.read_text(encoding="utf-8", errors="replace"), max_chars), None

        if ext in {".docx", ".xlsx", ".pptx", ".pdf"}:
            from ..tools.file_explorer.file_explorer_service import _convert_office_to_text

            converted = _convert_office_to_text(file_path) or ""
            return _clip(converted, max_chars), None if converted else "本文を抽出できませんでした"
    except Exception as exc:  # pragma: no cover - parser failures vary by environment
        return "", str(exc)

    return "", "未対応のファイル形式です"


def scan_project_folder(
    storage_root: Path,
    project_id: UUID | str,
    requested_path: str | None,
    *,
    max_files: int = 80,
    max_chars_per_file: int = 5000,
) -> tuple[list[ScannedProjectFile], str]:
    target, relative_folder = resolve_project_folder(storage_root, project_id, requested_path)
    root = storage_root.resolve()
    files: list[ScannedProjectFile] = []
    for path in sorted(target.rglob("*"), key=lambda item: str(item).lower()):
        if len(files) >= max_files:
            break
        if not path.is_file() or path.name.startswith("."):
            continue
        ext = path.suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            continue
        stat = path.stat()
        text, error = _extract_file_text(path, max_chars_per_file)
        files.append(
            ScannedProjectFile(
                path=str(path.relative_to(root)).replace("\\", "/"),
                name=path.name,
                extension=ext,
                size_bytes=stat.st_size,
                modified_at=datetime.fromtimestamp(stat.st_mtime).isoformat(),
                extracted_text=text,
                extract_error=error,
            )
        )
    return files, relative_folder


def _document_type(file: ScannedProjectFile) -> str:
    path = file.path.replace("\\", "/").casefold()
    haystack = f"{file.name}\n{file.extracted_text[:1000]}".lower()
    if "/decisions/" in path:
        return "decision_note"
    if "/00_" in path or "/01_" in path:
        return "knowledge"
    if file.extension == ".log":
        return "log"
    if re.search(r"\bwbs\b|工程|作業分解|タスク", haystack):
        return "wbs"
    if "課題" in haystack or "issue" in haystack:
        return "issue"
    if "リスク" in haystack or "risk" in haystack:
        return "risk"
    if any(word in haystack for word in ["構成図", "ネットワーク", "architecture", "diagram"]):
        return "diagram"
    if any(word in haystack for word in ["パラメータ", "parameter", "設定値", "config"]):
        return "parameter_sheet"
    if any(word in haystack for word in ["議事録", "minutes", "meeting"]):
        return "minutes"
    if any(word in haystack for word in ["試験", "検証", "テスト", "verification", "test"]):
        return "verification"
    if any(word in haystack for word in ["設計", "design"]):
        return "design"
    return "document"


def _category_for(file: ScannedProjectFile, document_type: str) -> str:
    haystack = f"{file.name}\n{file.extracted_text[:1000]}".lower()
    if document_type == "decision_note":
        return "decisions"
    if document_type == "knowledge":
        return _category_for_fact_text(haystack, "overview")
    if document_type == "log":
        return _category_for_fact_text(haystack, "existing_configuration")
    if document_type in {"diagram"}:
        return "architecture"
    if document_type in {"parameter_sheet", "design"}:
        return "detail_design"
    if document_type == "verification":
        return "verification"
    if "決定" in haystack or "確定" in haystack:
        return "decisions"
    if any(word in haystack for word in ["要確認", "未定", "確認事項", "todo", "tbd"]):
        return "open_questions"
    return "important_documents"


def _summary_from_text(text: str) -> str:
    lines = [
        line.strip(" \t-・")
        for line in re.split(r"[\r\n]+", text)
        if line.strip(" \t-・")
    ]
    useful = [
        line
        for line in lines
        if len(line) >= 8 and not re.fullmatch(r"[\d\s./:-]+", line)
        and "nan" not in line.casefold()
        and "unnamed:" not in line.casefold()
        and not line.startswith("| ---")
    ][:5]
    return _clip(" / ".join(useful), 500)


def _extract_marker_lines(text: str, markers: tuple[str, ...], limit: int = 5) -> list[str]:
    out: list[str] = []
    for line in re.split(r"[\r\n]+", text):
        stripped = line.strip(" \t-・")
        if _looks_like_noisy_marker_line(stripped):
            continue
        if stripped and any(marker in stripped.lower() for marker in markers):
            out.append(_clip(stripped, 220))
        if len(out) >= limit:
            break
    return out


def _is_reference_sample_file(file: ScannedProjectFile) -> bool:
    path = file.path.replace("\\", "/").casefold()
    name = file.name.casefold()
    return any(
        term.casefold() in path or term.casefold() in name
        for term in ("テスト参考", "参考", "sample", "template")
    )


def _looks_like_noisy_marker_line(text: str) -> bool:
    normalized = text.strip()
    if not normalized:
        return True
    lowered = normalized.casefold()
    if "nan" in lowered:
        return True
    if normalized in {"# TODO", "TODO", "確認事項"}:
        return True
    return False


def _custom_categories_for_files(files: list[ScannedProjectFile]) -> list[DraftCategory]:
    category_specs = [
        (
            "existing_configuration",
            "既存構成",
            "既存VLAN、既存配線、既存SW収容、既存資料の前提を整理する案件専用カテゴリ。",
            ("既存構成", "既存VLAN", "既存配線", "既存SW", "既存機器"),
        ),
        (
            "edge_firewall",
            "Firewall/IPS",
            "導入対象ファイアウォール、HA、モード、OS、段階導入方針を整理するカテゴリ。",
            ("Firewall", "NGFW", "PA-560", "PAN-OS", "IDS", "IPS"),
        ),
        (
            "building_switches",
            "建屋SW",
            "建屋側スイッチ、棟別収容、UTP/光、建屋側接続条件を整理するカテゴリ。",
            ("建屋SW", "建屋", "access switch", "distribution switch"),
        ),
        (
            "control_core_switch",
            "基幹SW",
            "基幹SW、上位側接続、ポート連続化、Port-channel/AE対応を整理するカテゴリ。",
            ("基幹SW", "core switch", "C9500", "Port-channel", "AE"),
        ),
        (
            "issue_management",
            "課題管理",
            "課題管理表、未決事項、確認依頼、回答待ちを整理する案件専用カテゴリ。",
            ("課題管理", "課題一覧", "未決", "要確認", "確認事項"),
        ),
    ]
    haystack = "\n".join(f"{file.name}\n{file.extracted_text[:5000]}" for file in files)
    lowered = haystack.casefold()
    categories: list[DraftCategory] = []
    sort_order = 100
    for key, label, description, terms in category_specs:
        if any(term.casefold() in lowered for term in terms):
            categories.append(
                DraftCategory(
                    key=key,
                    label=label,
                    description=description,
                    sort_order=sort_order,
                )
            )
            sort_order += 10
    return categories


def _category_for_fact_text(text: str, fallback: str) -> str:
    lowered = text.casefold()
    if any(term.casefold() in lowered for term in ("Firewall", "NGFW", "PA-560", "PAN-OS", "IDS", "IPS")):
        return "edge_firewall"
    if any(term.casefold() in lowered for term in ("建屋SW", "建屋", "access switch", "distribution switch")):
        return "building_switches"
    if any(term.casefold() in lowered for term in ("基幹SW", "core switch", "C9500", "Port-channel", "AE")):
        return "control_core_switch"
    if any(term.casefold() in lowered for term in ("既存構成", "既存VLAN", "既存配線", "既存SW", "既存機器")):
        return "existing_configuration"
    if any(term.casefold() in lowered for term in ("課題管理", "課題一覧", "未決", "要確認", "確認事項")):
        return "issue_management"
    return fallback


def heuristic_organize(
    project_name: str,
    source_folder: str,
    files: list[ScannedProjectFile],
) -> OrganizationDraft:
    docs: list[DraftDocument] = []
    facts: list[DraftFact] = []
    decisions: list[str] = []
    open_questions: list[str] = []

    for file in files:
        doc_type = _document_type(file)
        category_key = _category_for(file, doc_type)
        title = Path(file.name).stem
        description = _summary_from_text(file.extracted_text) or f"{file.extension} 資料"
        docs.append(
            DraftDocument(
                title=title,
                file_path=file.path,
                document_type=doc_type,
                category_key=category_key,
                role="primary" if doc_type in {"wbs", "parameter_sheet", "design"} else "reference",
                is_primary=doc_type in {"wbs", "parameter_sheet", "design"},
                ai_access_level="read" if file.extracted_text else "metadata",
                description=description,
                notes=file.extract_error or "",
            )
        )
        if not _is_reference_sample_file(file):
            for item in _extract_marker_lines(file.extracted_text, ("決定", "確定", "承認")):
                if _looks_like_table_row(item):
                    continue
                decisions.append(item)
                facts.append(
                    DraftFact(
                        title=f"決定事項: {Path(file.name).stem}",
                        content=item,
                        category_key="decisions",
                        fact_type="decision",
                        importance=8,
                        source_ref=file.path,
                    )
                )
            for item in _extract_marker_lines(file.extracted_text, ("要確認", "未定", "確認事項", "todo", "tbd")):
                if _looks_like_table_row(item):
                    continue
                open_questions.append(item)
                facts.append(
                    DraftFact(
                        title=f"要確認: {Path(file.name).stem}",
                        content=item,
                        category_key="open_questions",
                        fact_type="open_question",
                        importance=8,
                        source_ref=file.path,
                    )
                )

    folder_label = source_folder or "プロジェクトファイラー直下"
    summary_md = (
        f"{project_name} の `{folder_label}` から {len(files)} 件の資料を確認しました。"
        " 資料は根拠リンクとして扱い、案件そのものの決定事項・要確認事項だけを案件情報候補にします。"
    )
    return OrganizationDraft(
        source_folder=source_folder,
        generated_by="heuristic",
        summary_md=summary_md,
        categories=_custom_categories_for_files(files),
        documents=docs[:120],
        facts=facts[:160],
        decisions=decisions[:20],
        open_questions=open_questions[:20],
    )


def _draft_to_dict(draft: OrganizationDraft) -> dict[str, Any]:
    return {
        "source_folder": draft.source_folder,
        "generated_by": draft.generated_by,
        "summary_md": draft.summary_md,
        "categories": [item.__dict__ for item in draft.categories],
        "documents": [item.__dict__ for item in draft.documents],
        "facts": [item.__dict__ for item in draft.facts],
        "goals": draft.goals,
        "constraints": draft.constraints,
        "decisions": draft.decisions,
        "open_questions": draft.open_questions,
    }


def _parse_json_object(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            return None
        try:
            value = json.loads(match.group(0))
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            return None


def _draft_from_llm_json(
    value: dict[str, Any],
    source_folder: str,
    fallback: OrganizationDraft,
) -> OrganizationDraft:
    custom_category_map: dict[str, DraftCategory] = {
        item.key: item for item in fallback.categories
    }
    for item in value.get("categories", []):
        if not isinstance(item, dict):
            continue
        label = _clip(_clean_text(item.get("label")), 200)
        raw_key = _clean_text(item.get("key") or item.get("category_key"))
        key = _category_key(raw_key, _category_key(label, "custom_category"))
        if not label:
            continue
        if key in CATEGORY_LABELS:
            continue
        custom_category_map[key] = DraftCategory(
            key=key,
            label=label,
            description=_clip(_clean_text(item.get("description")), 1000),
            status=_clean_text(item.get("status"), "active"),
            sort_order=float(item.get("sort_order") or (100 + len(custom_category_map) * 10)),
        )

    allowed_category_keys = set(CATEGORY_LABELS) | set(custom_category_map)

    def _safe_category(value: Any, default: str) -> str:
        key = _category_key(_clean_text(value, default), default)
        return key if key in allowed_category_keys else default

    docs = []
    valid_paths = {doc.file_path for doc in fallback.documents}
    for item in value.get("documents", []):
        if not isinstance(item, dict):
            continue
        file_path = _clean_text(item.get("file_path"))
        if file_path not in valid_paths:
            continue
        docs.append(
            DraftDocument(
                title=_clip(_clean_text(item.get("title"), Path(file_path).stem), 255),
                file_path=file_path,
                document_type=_clip(_clean_text(item.get("document_type"), "document"), 64),
                category_key=_safe_category(item.get("category_key"), "important_documents"),
                role=_clip(_clean_text(item.get("role"), "reference"), 64),
                is_primary=bool(item.get("is_primary")),
                ai_access_level=_clean_text(item.get("ai_access_level"), "read"),
                description=_clip(_clean_text(item.get("description")), 1000),
                notes=_clip(_clean_text(item.get("notes")), 1000),
            )
        )

    facts = []
    for item in value.get("facts", []):
        if not isinstance(item, dict):
            continue
        content = _clean_text(item.get("content"))
        title = _clean_text(item.get("title"))
        if not title or not content:
            continue
        fact_type = _clip(_clean_text(item.get("fact_type"), "fact"), 64)
        if fact_type in {"document_summary", "file_inventory"}:
            continue
        if _looks_like_table_row(content):
            continue
        facts.append(
            DraftFact(
                title=_clip(title, 255),
                content=_clip(content, 3000),
                category_key=_safe_category(item.get("category_key"), "overview"),
                fact_type=fact_type,
                importance=max(1, min(10, int(item.get("importance") or 5))),
                source_ref=_clip(_clean_text(item.get("source_ref")), 1000),
            )
        )

    return OrganizationDraft(
        source_folder=source_folder,
        generated_by="llm",
        summary_md=_clip(_clean_text(value.get("summary_md"), fallback.summary_md), 3000),
        categories=sorted(custom_category_map.values(), key=lambda item: item.sort_order),
        documents=docs or fallback.documents,
        facts=facts,
        goals=[_clip(_clean_text(item), 300) for item in value.get("goals", []) if _clean_text(item)][:20],
        constraints=[
            _clip(_clean_text(item), 300)
            for item in value.get("constraints", [])
            if _clean_text(item)
        ][:20],
        decisions=[
            _clip(_clean_text(item), 300)
            for item in value.get("decisions", [])
            if _clean_text(item)
        ][:20],
        open_questions=[
            _clip(_clean_text(item), 300)
            for item in value.get("open_questions", [])
            if _clean_text(item)
        ][:20],
    )


async def organize_with_llm(
    config: Any,
    project_name: str,
    source_folder: str,
    files: list[ScannedProjectFile],
    fallback: OrganizationDraft,
) -> OrganizationDraft:
    payload = [
        {
            "path": file.path,
            "name": file.name,
            "extension": file.extension,
            "text": _clip(file.extracted_text, 2500),
            "extract_error": file.extract_error,
        }
        for file in files[:30]
    ]
    prompt = f"""
あなたは案件情報整理エージェントです。
プロジェクト「{project_name}」のファイラー資料を読み、案件情報Docs正本へ反映する構造化案を作ってください。

対象フォルダ: {source_folder or "プロジェクトファイラー直下"}

重要:
- 案件情報Docsの主語は資料ではなく案件です。
- documents は根拠資料リンクです。資料名や資料要約を facts に複製しないでください。
- facts には案件の概要、前提、要件、構成、決定事項、要確認事項、リスク、課題、検証条件など、案件そのものの知識だけを入れてください。
- Markdown表、表の1行、CSV行、機器一覧、接続一覧、WBS行は facts に入れないでください。これらは record table / 台帳に分離する対象です。
- fact_type に document_summary または file_inventory を使わないでください。

カテゴリキーは必ず次のいずれかにしてください:
overview, important_documents, decisions, open_questions, architecture, detail_design, verification

Use default categories when they fit, but create project-specific categories
when the documents reveal important recurring domain elements. For firewall
introduction projects, categories such as existing_configuration,
edge_firewall, building_switches, control_core_switch, and issue_management
are valid when supported by the documents.

返答はJSONのみ:
{{
  "summary_md": "案件の短い要約",
  "categories": [
    {{
      "key": "snake_case_ascii_key",
      "label": "案件専用カテゴリ名",
      "description": "このカテゴリに何を保存するか",
      "status": "active",
      "sort_order": 100
    }}
  ],
  "documents": [
    {{
      "title": "資料名",
      "file_path": "プロジェクト相対パス",
      "document_type": "document/design/parameter_sheet/diagram/wbs/issue/risk/minutes/verification",
      "category_key": "important_documents",
      "role": "primary/reference/management/draft",
      "is_primary": false,
      "ai_access_level": "read",
      "description": "資料の用途",
      "notes": ""
    }}
  ],
  "facts": [
    {{
      "title": "案件情報タイトル",
      "content": "案件そのものに長く効く事実。資料要約、表行、進捗だけの情報は入れない",
      "category_key": "overview",
      "fact_type": "fact/decision/open_question/design/verification/requirement/risk/issue/milestone",
      "importance": 1,
      "source_ref": "出典ファイルパス"
    }}
  ],
  "goals": [],
  "constraints": [],
  "decisions": [],
  "open_questions": []
}}

資料:
{json.dumps(payload, ensure_ascii=False)}
""".strip()

    def _generate() -> str:
        from ..llm.manager import create_llm_client

        client = create_llm_client(config)
        return client.generate_response(prompt, stream=False)

    try:
        response = await asyncio.to_thread(_generate)
    except Exception as exc:
        logger.warning("Project information LLM organizer failed: %s", exc)
        return fallback

    parsed = _parse_json_object(response)
    if not parsed:
        return fallback
    return _draft_from_llm_json(parsed, source_folder, fallback)


def _draft_to_markdown(draft: OrganizationDraft, scanned_files: list[ScannedProjectFile]) -> str:
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    folder = draft.source_folder or "プロジェクトファイラー直下"
    lines = [
        f"## 資料整理 {now}",
        "",
        f"- 対象: `{folder}`",
        f"- 走査ファイル数: {len(scanned_files)}",
        f"- 生成方式: {draft.generated_by}",
        "",
    ]

    if draft.summary_md:
        lines.extend(["### サマリー", "", draft.summary_md.strip(), ""])

    if draft.documents:
        lines.extend(["### 根拠資料", ""])
        for document in draft.documents:
            description = f" - {document.description}" if document.description else ""
            lines.append(
                f"- **{document.title}**: `{document.file_path}`"
                f" ({document.document_type}, {document.role}){description}"
            )
        lines.append("")

    if draft.facts:
        lines.extend(["### 案件情報候補", ""])
        for fact in draft.facts:
            source = f" / source: `{fact.source_ref}`" if fact.source_ref else ""
            lines.extend(
                [
                    f"#### {fact.title}",
                    "",
                    fact.content.strip(),
                    "",
                    f"- type: {fact.fact_type}",
                    f"- category: {fact.category_key}",
                    f"- importance: {fact.importance}{source}",
                    "",
                ]
            )

    if draft.decisions:
        lines.extend(["### 決定事項", ""])
        lines.extend(f"- {item}" for item in draft.decisions)
        lines.append("")

    if draft.open_questions:
        lines.extend(["### 要確認", ""])
        lines.extend(f"- {item}" for item in draft.open_questions)
        lines.append("")

    return "\n".join(lines).strip() + "\n"


async def apply_organization_draft(
    session: AsyncSession,
    *,
    project_id: UUID,
    user_id: UUID,
    draft: OrganizationDraft,
    scanned_files: list[ScannedProjectFile],
) -> dict[str, int]:
    project = await session.get(Project, project_id)
    if project is None:
        raise ValueError("Project not found.")

    node = await update_project_information_doc(
        session,
        project=project,
        user_id=user_id,
        append_text=_draft_to_markdown(draft, scanned_files),
        change_summary="フォルダ整理結果を案件情報Docs正本へ反映",
    )

    pack_result = await session.execute(
        select(ProjectContextPack).where(ProjectContextPack.project_id == project_id)
    )
    pack = pack_result.scalar_one_or_none()
    if pack is None:
        pack = ProjectContextPack(id=uuid4(), project_id=project_id)
        session.add(pack)
    pack.summary_md = draft.summary_md
    pack.goals = draft.goals
    pack.constraints = draft.constraints
    pack.decisions = draft.decisions
    pack.open_questions = draft.open_questions
    pack.generated_from = {
        "source": "project_information_organizer",
        "folder": draft.source_folder,
        "generated_by": draft.generated_by,
        "file_count": len(scanned_files),
        "updated_at": datetime.utcnow().isoformat(),
    }
    pack.updated_at = datetime.utcnow()

    await session.commit()
    return {
        "documents": len(draft.documents),
        "facts": len(draft.facts),
        "knowledge_node_id": str(node.id),
    }


async def organize_project_folder(
    session: AsyncSession,
    *,
    project_id: UUID,
    project_name: str,
    user_id: UUID,
    storage_root: Path,
    folder_path: str | None,
    apply: bool = False,
    use_llm: bool = True,
    config: Any = None,
    max_files: int = 80,
    draft_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    files, relative_folder = scan_project_folder(
        storage_root,
        project_id,
        folder_path,
        max_files=max_files,
    )
    fallback = heuristic_organize(project_name, relative_folder, files)
    draft = fallback
    if draft_override and apply:
        draft = _draft_from_llm_json(draft_override, relative_folder, fallback)
        draft.generated_by = _clean_text(
            draft_override.get("generated_by"),
            draft.generated_by,
        )
    elif use_llm and config and files:
        draft = await organize_with_llm(config, project_name, relative_folder, files, fallback)

    applied = {"documents": 0, "facts": 0}
    if apply:
        applied = await apply_organization_draft(
            session,
            project_id=project_id,
            user_id=user_id,
            draft=draft,
            scanned_files=files,
        )

    return {
        "success": True,
        "applied": apply,
        "source_folder": relative_folder,
        "scanned": {
            "files": [file.__dict__ for file in files],
            "count": len(files),
            "max_files": max_files,
        },
        "draft": _draft_to_dict(draft),
        "result": applied,
    }
