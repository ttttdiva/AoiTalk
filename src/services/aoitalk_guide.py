"""Canonical, versioned AoiTalk product guide lifecycle.

The guide is a system-managed subtree in each user's Personal Docs library.  Its
identity is carried by stable ``system_key`` values rather than localized titles.
The repository seed is deterministic; ensuring the hierarchy is safe to retry and
repairs moved, archived, or stale managed rows without touching ordinary Docs.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.models import DocsLibrary, KnowledgeNode, User
from .docs_acl import can_read_node, library_can_write

AOITALK_GUIDE_SYSTEM_KEY = "aoitalk_guide"
AOITALK_GUIDE_CHILD_PREFIX = f"{AOITALK_GUIDE_SYSTEM_KEY}:"
AOITALK_GUIDE_MANAGED_DOMAIN = "aoitalk_guide"
AOITALK_GUIDE_SYNC_TOOL = "aoitalk_guide_sync"
AOITALK_GUIDE_SEED_PATH = (
    Path(__file__).resolve().parents[2] / "resources" / "aoitalk_guide.ja.json"
)

# Keep this fallback in sync with the checked-in seed.  It allows source trees
# packaged without repository data to fail closed instead of silently creating
# an empty guide.
AOITALK_GUIDE_SEED_VERSION = "2026-09-01.2"

logger = logging.getLogger(__name__)


def _normalise_uuid(value: UUID | str | None) -> UUID | None:
    if value is None:
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _canonical_seed_bytes(seed: dict[str, Any]) -> bytes:
    return json.dumps(
        seed,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _seed_hash(seed: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_seed_bytes(seed)).hexdigest()


def load_aoitalk_guide_seed(path: str | Path | None = None) -> dict[str, Any]:
    """Load and validate the deterministic repository seed.

    The returned object is freshly decoded on every call so callers cannot
    mutate the module-level representation.  Missing/malformed seeds raise a
    ``RuntimeError``; callers serving Help must fail closed rather than invent
    product instructions.
    """

    seed_path = Path(path) if path is not None else AOITALK_GUIDE_SEED_PATH
    try:
        raw = seed_path.read_text(encoding="utf-8-sig")
        seed = json.loads(raw)
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError(f"AoiTalk guide seed could not be loaded: {seed_path}") from exc
    if not isinstance(seed, dict):
        raise RuntimeError("AoiTalk guide seed must be a JSON object")
    try:
        schema_version = int(seed.get("schema_version", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Unsupported AoiTalk guide seed schema") from exc
    if schema_version != 1:
        raise RuntimeError("Unsupported AoiTalk guide seed schema")
    version = str(seed.get("seed_version") or "").strip()
    title = str(seed.get("title") or "").strip()
    sections = seed.get("sections")
    intro = seed.get("intro_markdown")
    if not version or not title or not isinstance(intro, str) or not intro.strip():
        raise RuntimeError("AoiTalk guide seed is missing root metadata")
    if not isinstance(sections, list) or not sections:
        raise RuntimeError("AoiTalk guide seed has no sections")
    seen: set[str] = set()
    for index, section in enumerate(sections):
        if not isinstance(section, dict):
            raise RuntimeError(f"AoiTalk guide section {index} is invalid")
        key = str(section.get("key") or "").strip()
        section_title = str(section.get("title") or "").strip()
        markdown = section.get("markdown")
        if not key or key in seen or ":" in key or not section_title:
            raise RuntimeError(f"AoiTalk guide section key {key!r} is invalid")
        if not isinstance(markdown, str) or not markdown.strip():
            raise RuntimeError(f"AoiTalk guide section {key!r} has no content")
        seen.add(key)
    return seed


def load_aoitalk_guide_seed_metadata(
    path: str | Path | None = None,
) -> dict[str, str]:
    """Return stable seed version/hash/path metadata for logs and revisions."""

    seed = load_aoitalk_guide_seed(path)
    seed_path = Path(path) if path is not None else AOITALK_GUIDE_SEED_PATH
    return {
        "seed_version": str(seed["seed_version"]),
        "seed_sha256": _seed_hash(seed),
        "seed_path": str(seed_path),
    }


def _source_refs(seed: dict[str, Any], seed_hash: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "aoitalk_guide_seed",
            "source": "repository",
            "path": str(AOITALK_GUIDE_SEED_PATH),
            "seed_version": str(seed["seed_version"]),
            "sha256": seed_hash,
        }
    ]


def _managed_display_props(
    *,
    existing: Any,
    seed_version: str,
    seed_hash: str,
    system_key: str,
) -> dict[str, Any]:
    props = dict(existing) if isinstance(existing, dict) else {}
    # The managed fields are canonical.  Preserve unrelated display settings so
    # a future UI extension cannot be erased by a guide repair.
    props.update(
        {
            "system_managed": True,
            "managed_domain": AOITALK_GUIDE_MANAGED_DOMAIN,
            "managed_allowed_tools": [AOITALK_GUIDE_SYNC_TOOL],
            "managed_allow_revival": True,
            "managed_source_refs_required": True,
            "hidden_from_sidebar": False,
            # Keep the provenance easy to inspect without decrypting the body;
            # the nested object below carries the node-specific identity too.
            "source": "repository",
            "seed_version": seed_version,
            "seed_sha256": seed_hash,
            "aoitalk_guide": {
                "system_key": system_key,
                "seed_version": seed_version,
                "seed_sha256": seed_hash,
                "source": "repository",
            },
        }
    )
    return props


def _markdown_body(
    markdown: str,
    *,
    system_key: str,
    seed_version: str,
    seed_hash: str,
    label: str | None = None,
) -> dict[str, Any]:
    # The Web Docs writer requires an editable markdown block to carry a
    # string label.  Keep that envelope identical across Python/Next
    # materializers so a backend ensure does not oscillate with a frontend
    # ensure on the next request.
    block_label = _title_mirror(label if label is not None else system_key)
    return {
        "format": "doc_block",
        "block_type": "markdown",
        "content": markdown,
        "label": block_label,
        "aoitalk_guide": {
            "system_key": system_key,
            "seed_version": seed_version,
            "seed_sha256": seed_hash,
            "source": "repository",
        },
    }


def _title_mirror(title: str) -> str:
    value = str(title or "").strip()[:500]
    if "\n" in value or "\r" in value:
        raise ValueError("AoiTalk guide title cannot contain newlines")
    return value


async def _acquire_guide_lock(session: AsyncSession, library_id: UUID) -> None:
    """Serialize repairs for one library on PostgreSQL; remain portable in tests.

    The Next.js Docs materializer uses the same ``hashtext`` input. Sharing
    that transaction advisory key prevents a concurrent ``/docs`` bootstrap
    from racing a FastAPI ``/help`` ensure and colliding on the unique
    ``(docs_library_id, system_key)`` constraint.
    """

    try:
        bind = session.get_bind()
        dialect = str(getattr(getattr(bind, "dialect", None), "name", ""))
    except Exception:
        dialect = ""
    if dialect not in {"postgresql", "postgres"}:
        return
    lock_text = f"{library_id}:aoitalk-guide-seed"
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtext(lock_text)))
    )


async def _find_root(
    session: AsyncSession,
    library_id: UUID,
    *,
    lock: bool = False,
) -> KnowledgeNode | None:
    statement = select(KnowledgeNode).where(
        KnowledgeNode.docs_library_id == library_id,
        KnowledgeNode.system_key == AOITALK_GUIDE_SYSTEM_KEY,
    )
    if lock:
        statement = statement.with_for_update()
    result = await session.execute(statement.limit(1))
    return result.scalar_one_or_none()


async def _create_node(
    session: AsyncSession,
    *,
    library: DocsLibrary,
    owner_user_id: UUID,
    parent: KnowledgeNode | None,
    title: str,
    system_key: str,
    markdown: str,
    sort_order: float,
    seed_version: str,
    seed_hash: str,
    source_refs: list[dict[str, Any]],
) -> KnowledgeNode:
    """Create one managed node and record its index/revision through graph API."""

    node = KnowledgeNode(
        id=uuid4(),
        docs_library_id=library.id,
        parent_id=parent.id if parent is not None else None,
        root_page_id=(parent.root_page_id or parent.id) if parent is not None else None,
        project_id=None,
        app_id=None,
        system_key=system_key,
        title=_title_mirror(title),
        is_explicit_blank=False,
        body_text=_title_mirror(title),
        body_json=_markdown_body(
            markdown,
            system_key=system_key,
            seed_version=seed_version,
            seed_hash=seed_hash,
            label=title,
        ),
        node_type="node",
        display_props=_managed_display_props(
            existing=None,
            seed_version=seed_version,
            seed_hash=seed_hash,
            system_key=system_key,
        ),
        sort_order=sort_order,
        created_by=owner_user_id,
        updated_by=owner_user_id,
    )
    session.add(node)
    await session.flush()
    # Import lazily: docs_graph_service imports docs_workspace, which calls this
    # module from docs_workspace.ensure_docs_library.
    from .docs_graph_service import DocsGraphService

    await DocsGraphService(session).record_node_change(
        node,
        owner_user_id,
        "AoiTalk ガイドをseedから作成",
        source_refs,
    )
    await session.flush()
    return node


async def _repair_node(
    session: AsyncSession,
    *,
    node: KnowledgeNode,
    parent: KnowledgeNode | None,
    owner_user_id: UUID,
    title: str,
    system_key: str,
    markdown: str,
    sort_order: float,
    seed_version: str,
    seed_hash: str,
    source_refs: list[dict[str, Any]],
    root: bool,
) -> bool:
    changed = False
    expected_title = _title_mirror(title)
    expected_parent_id = parent.id if parent is not None else None
    # Top-level Docs roots conventionally leave ``root_page_id`` NULL;
    # descendants point at their nearest root.  Keeping this stable prevents a
    # needless repair revision on the second idempotent ensure call.
    expected_root_page_id = (
        (parent.root_page_id or parent.id) if parent is not None else None
    )
    expected_body = _markdown_body(
        markdown,
        system_key=system_key,
        seed_version=seed_version,
        seed_hash=seed_hash,
        label=title,
    )
    expected_props = _managed_display_props(
        existing=getattr(node, "display_props", None),
        seed_version=seed_version,
        seed_hash=seed_hash,
        system_key=system_key,
    )
    if node.parent_id != expected_parent_id:
        node.parent_id = expected_parent_id
        changed = True
    if node.root_page_id != expected_root_page_id:
        node.root_page_id = expected_root_page_id
        changed = True
    if getattr(node, "project_id", None) is not None:
        node.project_id = None
        changed = True
    if getattr(node, "app_id", None) is not None:
        node.app_id = None
        changed = True
    if node.title != expected_title:
        node.title = expected_title
        changed = True
    if node.body_text != expected_title:
        node.body_text = expected_title
        changed = True
    if node.body_json != expected_body:
        node.body_json = expected_body
        changed = True
    if node.display_props != expected_props:
        node.display_props = expected_props
        changed = True
    if getattr(node, "node_type", "node") != "node":
        node.node_type = "node"
        changed = True
    if getattr(node, "is_explicit_blank", False):
        node.is_explicit_blank = False
        changed = True
    if node.sort_order != sort_order:
        node.sort_order = sort_order
        changed = True
    if node.archived_at is not None:
        node.archived_at = None
        changed = True
    if changed:
        node.updated_by = owner_user_id
        node.updated_at = datetime.utcnow()
        from .docs_graph_service import DocsGraphService

        await DocsGraphService(session).record_node_change(
            node,
            owner_user_id,
            "AoiTalk ガイドをseedに合わせて修復",
            source_refs,
        )
        await session.flush()
    return changed


async def ensure_aoitalk_guide_hierarchy(
    session: AsyncSession,
    owner_user_id: UUID | str,
    *,
    library: DocsLibrary | None = None,
) -> KnowledgeNode:
    """Ensure one user's canonical AoiTalk guide root and managed children.

    ``library`` is an internal recursion guard used by
    :func:`docs_workspace.ensure_docs_library`; public callers may omit it and
    the canonical Personal Docs library is resolved/created first.
    """

    actor = _normalise_uuid(owner_user_id)
    if actor is None:
        raise PermissionError("AoiTalk ガイドにはユーザーが必要です")
    if library is None:
        # Lazy import avoids docs_workspace -> guide -> docs_workspace import
        # cycles while preserving the convenient public API.
        from .docs_workspace import ensure_docs_library

        library = await ensure_docs_library(session, owner_user_id=actor)
    if library is None or _normalise_uuid(library.owner_user_id) != actor:
        raise PermissionError("Personal Docsへの書き込み権限がありません")
    if str(getattr(library, "library_type", "personal") or "personal") != "personal":
        raise PermissionError("AoiTalk ガイドはPersonal Docsでのみ管理できます")
    if not isinstance(session, AsyncSession):
        raise RuntimeError("AoiTalk guide lifecycle requires an AsyncSession")

    seed = load_aoitalk_guide_seed()
    seed_version = str(seed["seed_version"])
    seed_hash = _seed_hash(seed)
    source_refs = _source_refs(seed, seed_hash)
    await _acquire_guide_lock(session, library.id)

    # The owner check is explicit even though this function is internal: a
    # future caller must not use a shared/read-only library as a repair target.
    if not await library_can_write(session, library, actor):
        raise PermissionError("Personal Docsへの書き込み権限がありません")

    root = await _find_root(session, library.id, lock=True)
    if root is None:
        # A unique system_key plus the transaction advisory lock handles the
        # normal race.  The nested savepoint also lets a deployment without
        # advisory-lock support recover a concurrent insert cleanly.
        try:
            async with session.begin_nested():
                root = await _create_node(
                    session,
                    library=library,
                    owner_user_id=actor,
                    parent=None,
                    title=str(seed["title"]),
                    system_key=AOITALK_GUIDE_SYSTEM_KEY,
                    markdown=str(seed["intro_markdown"]),
                    sort_order=0.0,
                    seed_version=seed_version,
                    seed_hash=seed_hash,
                    source_refs=source_refs,
                )
        except IntegrityError:
            root = await _find_root(session, library.id, lock=True)
            if root is None:
                raise
    else:
        await _repair_node(
            session,
            node=root,
            parent=None,
            owner_user_id=actor,
            title=str(seed["title"]),
            system_key=AOITALK_GUIDE_SYSTEM_KEY,
            markdown=str(seed["intro_markdown"]),
            sort_order=0.0,
            seed_version=seed_version,
            seed_hash=seed_hash,
            source_refs=source_refs,
            root=True,
        )

    if root is None:  # pragma: no cover - defensive for malformed session doubles
        raise RuntimeError("AoiTalk guide root could not be materialized")

    expected_children: dict[str, dict[str, Any]] = {}
    for index, section in enumerate(seed["sections"], start=1):
        key = str(section["key"])
        expected_children[f"{AOITALK_GUIDE_CHILD_PREFIX}{key}"] = {
            "title": str(section["title"]),
            "markdown": str(section["markdown"]),
            "sort_order": float(section.get("sort_order", index * 10) or index * 10),
        }
    child_result = await session.execute(
        select(KnowledgeNode)
        .where(
            KnowledgeNode.docs_library_id == library.id,
            KnowledgeNode.system_key.in_(list(expected_children)),
        )
        .with_for_update()
    )
    children_by_key = {str(node.system_key): node for node in child_result.scalars().all()}

    for system_key, spec in expected_children.items():
        child = children_by_key.get(system_key)
        if child is None:
            await _create_node(
                session,
                library=library,
                owner_user_id=actor,
                parent=root,
                title=spec["title"],
                system_key=system_key,
                markdown=spec["markdown"],
                sort_order=spec["sort_order"],
                seed_version=seed_version,
                seed_hash=seed_hash,
                source_refs=source_refs,
            )
        else:
            await _repair_node(
                session,
                node=child,
                parent=root,
                owner_user_id=actor,
                title=spec["title"],
                system_key=system_key,
                markdown=spec["markdown"],
                sort_order=spec["sort_order"],
                seed_version=seed_version,
                seed_hash=seed_hash,
                source_refs=source_refs,
                root=False,
            )

    known_keys = {AOITALK_GUIDE_SYSTEM_KEY, *expected_children}
    stale_result = await session.execute(
        select(KnowledgeNode)
        .where(
            KnowledgeNode.docs_library_id == library.id,
            KnowledgeNode.system_key.like(f"{AOITALK_GUIDE_CHILD_PREFIX}%"),
            KnowledgeNode.system_key.not_in(list(known_keys)),
            KnowledgeNode.archived_at.is_(None),
        )
        .with_for_update()
    )
    from .docs_graph_service import DocsGraphService

    graph = DocsGraphService(session)
    for stale in stale_result.scalars().all():
        stale.archived_at = datetime.utcnow()
        stale.updated_by = actor
        await graph.record_node_change(
            stale,
            actor,
            "古いAoiTalk ガイド章をアーカイブ",
            source_refs,
        )
    await session.flush()
    return root


async def read_aoitalk_guide_subtree(
    session: AsyncSession,
    owner_user_id: UUID | str,
    *,
    section_key: str | None = None,
    section_keys: list[str] | tuple[str, ...] | None = None,
    library: DocsLibrary | None = None,
) -> list[KnowledgeNode]:
    """Read only the canonical guide root and optional exact child section.

    This helper is deliberately read-only and never calls ``ensure``.  The
    caller can distinguish an unavailable/corrupt guide from ordinary Docs by
    receiving an empty list.  No title search, broad Docs search, or Project
    context is consulted.
    """

    actor = _normalise_uuid(owner_user_id)
    if actor is None:
        return []
    if library is None:
        result = await session.execute(
            select(DocsLibrary)
            .where(
                DocsLibrary.owner_user_id == actor,
                DocsLibrary.library_type == "personal",
                DocsLibrary.name == "Personal Docs",
            )
            .order_by(DocsLibrary.created_at)
            .limit(1)
        )
        library = result.scalar_one_or_none()
    if library is None or _normalise_uuid(library.owner_user_id) != actor:
        return []
    root = await _find_root(session, library.id, lock=False)
    if root is None or root.archived_at is not None:
        return []
    try:
        seed = load_aoitalk_guide_seed()
    except RuntimeError:
        return []
    seed_version = str(seed["seed_version"])
    seed_hash = _seed_hash(seed)
    if root.parent_id is not None or root.root_page_id is not None:
        return []
    if not _node_matches_seed(
        root,
        system_key=AOITALK_GUIDE_SYSTEM_KEY,
        title=str(seed["title"]),
        markdown=str(seed["intro_markdown"]),
        seed_version=seed_version,
        seed_hash=seed_hash,
        parent_id=None,
    ):
        return []
    try:
        root_readable = await can_read_node(
            session, root, actor, library=library, include_archived=False
        )
    except Exception:
        root_readable = False
    if not root_readable:
        return []

    nodes = [root]
    bounded_read = section_key is not None or section_keys is not None
    if section_key is not None and section_keys is not None:
        return []
    if section_key is not None:
        key = str(section_key).strip()
        if not key or ":" in key:
            return []
        requested_section_keys = [key]
    elif section_keys is not None:
        requested_section_keys = []
        for raw_key in section_keys:
            key = str(raw_key or "").strip()
            if not key or ":" in key or key in requested_section_keys:
                return []
            requested_section_keys.append(key)
        if not requested_section_keys:
            return []
    else:
        requested_section_keys = [
            str(item["key"]) for item in seed.get("sections", [])
        ]
    section_specs = {
        str(item["key"]): item for item in seed.get("sections", [])
    }
    if any(key not in section_specs for key in requested_section_keys):
        return []
    requested_system_keys = [
        f"{AOITALK_GUIDE_CHILD_PREFIX}{key}" for key in requested_section_keys
    ]
    result = await session.execute(
        select(KnowledgeNode)
        .where(
            KnowledgeNode.docs_library_id == library.id,
            KnowledgeNode.parent_id == root.id,
            KnowledgeNode.system_key.in_(requested_system_keys),
            KnowledgeNode.archived_at.is_(None),
        )
        .order_by(KnowledgeNode.sort_order, KnowledgeNode.created_at)
    )
    for node in result.scalars().all():
        key = str(node.system_key or "").removeprefix(AOITALK_GUIDE_CHILD_PREFIX)
        spec = section_specs.get(key)
        if spec is None or not _node_matches_seed(
            node,
            system_key=f"{AOITALK_GUIDE_CHILD_PREFIX}{key}",
            title=str(spec["title"]),
            markdown=str(spec["markdown"]),
            seed_version=seed_version,
            seed_hash=seed_hash,
            parent_id=root.id,
        ):
            continue
        try:
            readable = await can_read_node(
                session, node, actor, library=library, include_archived=False
            )
        except Exception:
            readable = False
        if readable:
            nodes.append(node)
    # If a requested section vanished/was moved, do not return a root-only
    # answer that could be mistaken for grounded section content.
    if bounded_read and len(nodes) != 1 + len(requested_section_keys):
        return []
    # A full-guide read is only valid when every repository section is present
    # and validated.  This keeps a partially materialized/tampered subtree from
    # being presented as a complete manual even if the caller skipped ensure.
    if not bounded_read and len(nodes) != 1 + len(requested_section_keys):
        return []
    return nodes


def aoitalk_guide_markdown(node: KnowledgeNode) -> str:
    """Extract canonical markdown content from a guide node, fail closed."""

    try:
        body_value = getattr(node, "body_json", None)
    except Exception:
        return ""
    body = body_value if isinstance(body_value, dict) else {}
    if body.get("format") != "doc_block" or body.get("block_type") != "markdown":
        return ""
    content = body.get("content")
    return content if isinstance(content, str) else ""


def _node_matches_seed(
    node: KnowledgeNode,
    *,
    system_key: str,
    title: str,
    markdown: str,
    seed_version: str,
    seed_hash: str,
    parent_id: UUID | None,
) -> bool:
    """Validate persisted identity/content before exposing it to Help."""

    if str(getattr(node, "system_key", "") or "") != system_key:
        return False
    if str(getattr(node, "title", "") or "") != _title_mirror(title):
        return False
    if getattr(node, "parent_id", None) != parent_id:
        return False
    if getattr(node, "project_id", None) is not None or getattr(node, "app_id", None) is not None:
        return False
    if getattr(node, "archived_at", None) is not None:
        return False
    if aoitalk_guide_markdown(node) != markdown:
        return False
    props = getattr(node, "display_props", None)
    if not isinstance(props, dict):
        return False
    if (
        props.get("system_managed") is not True
        or props.get("managed_domain") != AOITALK_GUIDE_MANAGED_DOMAIN
        or props.get("hidden_from_sidebar") is not False
        or props.get("seed_version") != seed_version
        or props.get("seed_sha256") != seed_hash
    ):
        return False
    body = getattr(node, "body_json", None)
    if not isinstance(body, dict):
        return False
    provenance = body.get("aoitalk_guide")
    return isinstance(provenance, dict) and provenance.get("seed_version") == seed_version and provenance.get("seed_sha256") == seed_hash


async def backfill_aoitalk_guides(db_manager: Any) -> dict[str, Any]:
    """Ensure the canonical Guide exists for every persisted user.

    Startup deliberately performs this as a bounded foreground migration,
    rather than scheduling it as a background task.  The initial user-id scan
    uses one short-lived session and each user is then processed in an
    independent transaction so one corrupt row cannot leave the remaining
    users without a Guide.  Individual failures are returned to the caller;
    the startup policy decides whether they are fatal for the active profile.
    """

    if db_manager is None:
        return {"users": 0, "ensured": 0, "failed": 0, "errors": []}

    scan_session = await db_manager.get_session()
    try:
        result = await scan_session.execute(select(User.id).order_by(User.id))
        owner_ids = [value for value in result.scalars().all() if value is not None]
    finally:
        await scan_session.close()

    errors: list[dict[str, str]] = []
    ensured = 0
    for owner_id in owner_ids:
        session = None
        try:
            session = await db_manager.get_session()
            from .docs_workspace import ensure_docs_library

            await ensure_docs_library(session, owner_user_id=owner_id)
            await session.commit()
            ensured += 1
        except Exception as exc:  # noqa: BLE001 - per-user isolation is intentional
            if session is not None:
                try:
                    await session.rollback()
                except Exception:
                    logger.debug(
                        "AoiTalk guide backfill rollback failed for user %s",
                        owner_id,
                        exc_info=True,
                    )
            logger.exception("AoiTalk guide backfill failed for user %s", owner_id)
            errors.append({"user_id": str(owner_id), "error": str(exc)})
        finally:
            if session is not None:
                await session.close()

    return {
        "users": len(owner_ids),
        "ensured": ensured,
        "failed": len(errors),
        "errors": errors,
    }


__all__ = [
    "AOITALK_GUIDE_CHILD_PREFIX",
    "AOITALK_GUIDE_MANAGED_DOMAIN",
    "AOITALK_GUIDE_SEED_PATH",
    "AOITALK_GUIDE_SEED_VERSION",
    "AOITALK_GUIDE_SYSTEM_KEY",
    "AOITALK_GUIDE_SYNC_TOOL",
    "aoitalk_guide_markdown",
    "backfill_aoitalk_guides",
    "ensure_aoitalk_guide_hierarchy",
    "load_aoitalk_guide_seed",
    "load_aoitalk_guide_seed_metadata",
    "read_aoitalk_guide_subtree",
]
