"""Owner-scoped durable production jobs and immutable imported candidates."""
from __future__ import annotations

import asyncio
import hashlib
import io
import uuid
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageOps
from pydantic import Field
from sqlalchemy import func, select, text

from ..memory.database import get_db_session
from ..models.ecc_models import Character
from ..services.generated_media_storage import get_media_root
from .models import CharacterRig, CharacterRigBuild
from .package_import import FORMAT, PackageError, immutable_copy, read_validated_directory, validate_package
from .spec import Record

STAGES = ("queued", "creating", "running", "exporting", "validating", "ready", "needs_input", "cancelled", "failed")
FINAL = {"ready", "failed", "cancelled", "needs_input", "archived"}
# Bound per-owner history metadata independently of the existing byte quotas.
MAX_BUILD_REVISIONS = 256


class RigError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class BuildView(Record):
    id: str
    revision: int
    status: str
    error: str | None
    report: dict
    model_format: str = "legacy-unavailable"
    display_name: str = ""
    capabilities: dict = Field(default_factory=dict)
    project_id: str | None = None
    has_production_project: bool = False


class RigView(Record):
    character_id: str
    active_revision: str | None
    has_source: bool
    provider_ready: bool
    builds: list[BuildView]
    active_format: str | None = None
    has_model: bool = False
    production_enabled: bool = False
    production_status: str = "disabled"


def root_path(relative: str = "") -> Path:
    from ..services.storage_io import StorageError
    try:
        root = get_media_root()
        root.require_online(write=True)
        return root.resolve("aoirig/" + relative, write=True)
    except StorageError as exc:
        raise RigError(str(exc), exc.status_code) from exc


async def character(db, key: str):
    try:
        condition = Character.id == uuid.UUID(key)
    except ValueError:
        condition = Character.slug == key
    value = (await db.execute(select(Character).where(condition))).scalar_one_or_none()
    if value is None:
        raise RigError("キャラクターが見つかりません", 404)
    return value


async def rig_for(db, char_id, owner: str, *, create=False, lock=False):
    if lock:
        await db.execute(select(Character.id).where(Character.id == char_id).with_for_update())
    query = select(CharacterRig).where(CharacterRig.character_id == char_id, CharacterRig.owner_id == owner)
    if lock:
        query = query.with_for_update()
    value = (await db.execute(query)).scalar_one_or_none()
    if value is None and create:
        value = CharacterRig(id=uuid.uuid4(), character_id=char_id, owner_id=owner)
        db.add(value)
        await db.flush()
    return value


def model_format(build):
    value = (build.config or {}).get("model_format")
    if isinstance(value, str) and value.strip():
        return value
    return "legacy-unavailable"


def build_view(build) -> BuildView:
    config, report = build.config or {}, build.report or {}
    return BuildView(id=str(build.id), revision=build.revision, status=build.status, error=build.error,
                     report=report, model_format=model_format(build), display_name=config.get("display_name", ""),
                     capabilities=config.get("capabilities", {}), project_id=config.get("project_id"), has_production_project=bool(config.get("production_path") and config.get("project_id")))


async def status(key: str, owner: str) -> RigView:
    from . import production
    try:
        provider = await production.view()
    except production.ProductionError:
        provider = production.ProductionSettingsView(status="unavailable")
    async with await get_db_session() as db:
        char = await character(db, key)
        rig = await rig_for(db, char.id, owner)
        builds = (await db.execute(select(CharacterRigBuild).where(CharacterRigBuild.rig_id == rig.id, CharacterRigBuild.status != "archived").order_by(CharacterRigBuild.revision.desc()))).scalars().all() if rig else []
        if rig and rig.active_revision and not any(b.id == rig.active_revision for b in builds):
            active = (await db.execute(select(CharacterRigBuild).where(CharacterRigBuild.rig_id == rig.id, CharacterRigBuild.id == rig.active_revision, CharacterRigBuild.status != "archived"))).scalar_one_or_none()
            if active:
                builds.append(active)
        active = next((b for b in builds if rig and b.id == rig.active_revision), None)
        ready_rows = (await db.execute(select(CharacterRigBuild).where(CharacterRigBuild.rig_id == rig.id, CharacterRigBuild.status == "ready"))).scalars().all() if rig else []
        return RigView(character_id=str(char.id), active_revision=str(rig.active_revision) if rig and rig.active_revision else None,
                       has_source=bool(rig and rig.source_path), provider_ready=provider.enabled and provider.ready,
                       builds=[build_view(b) for b in builds], active_format=model_format(active) if active else None,
                       has_model=any(row.asset_path and model_format(row) == FORMAT for row in ready_rows),
                       production_enabled=provider.enabled, production_status=provider.status)


def normalize_image(data: bytes) -> bytes:
    if len(data) > 16 * 1024 * 1024:
        raise RigError("画像は16MB以下にしてください")
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in {"PNG", "JPEG", "WEBP"} or image.width * image.height > 16_000_000 or min(image.size) < 32:
                raise RigError("PNG/JPEG/WebPの画像を指定してください（最大1600万画素）")
            image = ImageOps.exif_transpose(image).convert("RGBA")
            image.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image.save(output, format="PNG")
            return output.getvalue()
    except (OSError, Image.DecompressionBombError) as exc:
        raise RigError("画像を読み込めません") from exc


async def upload(key: str, owner: str, data: bytes) -> None:
    normalized = await asyncio.to_thread(normalize_image, data)
    async with await get_db_session() as db, db.begin():
        char = await character(db, key)
        rig = await rig_for(db, char.id, owner, create=True, lock=True)
        relative = f"{rig.id}/sources/{hashlib.sha256(normalized).hexdigest()}.png"
        path = root_path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            if sum(p.stat().st_size for p in path.parent.glob("*.png")) + len(normalized) > 256 * 1024 * 1024:
                raise RigError("このキャラクターの元画像保存容量が上限に達しました", 413)
            with path.open("xb") as handle:
                handle.write(normalized)
        elif path.read_bytes() != normalized:
            raise RigError("保存済みsource hashが一致しません", 409)
        rig.source_path = relative


async def next_revision(db, rig_id):
    revision = (await db.execute(select(func.max(CharacterRigBuild.revision)).where(CharacterRigBuild.rig_id == rig_id))).scalar_one() or 0
    if revision >= MAX_BUILD_REVISIONS:
        raise RigError(f"このキャラクターの履歴上限（{MAX_BUILD_REVISIONS}件）に達しました", 429)
    return revision + 1


def package_config(package, existing=None):
    return {**(existing or {}), "model_format": FORMAT, "display_name": package.manifest["name"],
            "capabilities": package.capabilities, "project_id": package.manifest["project_id"],
            "package_sha256": package.digest, "manifest": package.manifest,
            "review_required": True}


async def import_package(key, owner, data):
    try:
        package = await asyncio.to_thread(validate_package, data)
    except PackageError as exc:
        raise RigError(str(exc), 422) from exc
    async with await get_db_session() as db, db.begin():
        char = await character(db, key)
        rig = await rig_for(db, char.id, owner, create=True, lock=True)
        builds = (await db.execute(select(CharacterRigBuild).where(CharacterRigBuild.rig_id == rig.id))).scalars().all()
        for build in builds:
            if model_format(build) == FORMAT and (build.config or {}).get("package_sha256") == package.digest:
                if not build.asset_path:
                    raise RigError("保存assetがありません", 409)
                try:
                    existing = await asyncio.to_thread(read_validated_directory, root_path(build.asset_path))
                except (PackageError, OSError) as exc:
                    raise RigError("保存モデルの検証に失敗しました", 409) from exc
                if existing.digest != package.digest:
                    raise RigError("保存asset hashが一致しません", 409)
                if build.status == "archived":
                    build.status = "ready"
                    build.config = {**build.config, "cancel_requested": False}
                return build_view(build)
        revision = await next_revision(db, rig.id)
        occupied = sum(sum(e.get("size", 0) for e in (b.config or {}).get("manifest", {}).get("files", [])) for b in builds if model_format(b) == FORMAT)
        if occupied + sum(map(len, package.files.values())) > 512 * 1024 * 1024:
            raise RigError("このキャラクターのモデル保存容量が上限に達しました", 413)
        relative = f"{rig.id}/packages/{package.digest}"
        try:
            await asyncio.to_thread(immutable_copy, package, root_path(relative))
        except PackageError as exc:
            raise RigError(str(exc), 422) from exc
        build = CharacterRigBuild(id=uuid.uuid4(), rig_id=rig.id, revision=revision,
                                  status="ready", source_path=rig.source_path or "", asset_path=relative,
                                  config=package_config(package), report={"validated": True, "artistic_review_required": True})
        db.add(build)
        await db.flush()
        return build_view(build)


async def enqueue(key: str, owner: str, revision: str | None = None) -> BuildView:
    from . import production
    try:
        saved = await production.settings()
        production.require_personal()
        if saved.get("enabled") is not True:
            raise RigError("製作機能はOFFです。管理者による有効化が必要です", 503)
        path = production.effective_path(saved)
        check = await production.cli(path, "doctor")
        if check.get("ready") is not True:
            raise RigError("製作環境の準備が必要です", 503)
    except production.ProductionError as exc:
        raise RigError(str(exc), exc.status) from exc
    async with await get_db_session() as db, db.begin():
        if db.bind.dialect.name == "postgresql":
            await db.execute(text("SELECT pg_advisory_xact_lock(4102026, 929)"))
        char = await character(db, key)
        rig = await rig_for(db, char.id, owner, lock=True)
        if rig is None or not rig.source_path:
            raise RigError("先にキャラクター画像を登録してください")
        builds = (await db.execute(select(CharacterRigBuild).where(CharacterRigBuild.rig_id == rig.id))).scalars().all()
        pending = next((b for b in builds if model_format(b) == FORMAT and b.status not in FINAL), None)
        if pending:
            return build_view(pending)
        project_id = str(uuid.uuid4())
        if revision:
            try:
                rid = uuid.UUID(revision)
            except ValueError as exc:
                raise RigError("revisionは既存buildのUUIDを指定してください", 422) from exc
            previous = next((b for b in builds if b.id == rid and b.status != "archived"), None)
            if (not previous or model_format(previous) != FORMAT or not (previous.config or {}).get("production_path")
                    or previous.source_path != rig.source_path):
                raise RigError("同じsourceの所有プロジェクトだけ継続できます", 409)
            project_id = previous.config["project_id"]
        count = (await db.execute(select(func.count()).select_from(CharacterRigBuild).where(CharacterRigBuild.status.not_in(FINAL), CharacterRigBuild.config["model_format"].as_string() == FORMAT))).scalar_one()
        if count >= 8:
            raise RigError("生成キューが混み合っています", 429)
        source = root_path(rig.source_path)
        source_hash = await asyncio.to_thread(lambda: hashlib.sha256(source.read_bytes()).hexdigest())
        config = {"model_format": FORMAT, "project_id": project_id, "production_path": str(path),
                  "source_sha256": source_hash, "review_required": True, "cancel_requested": False}
        build = CharacterRigBuild(id=uuid.uuid4(), rig_id=rig.id, revision=await next_revision(db, rig.id),
                                  status="queued", source_path=rig.source_path, config=config, report={})
        db.add(build)
        await db.flush()
        return build_view(build)


async def owned_build(db, key, owner, build_id, lock=False, *, include_archived=False):
    char = await character(db, key)
    rig = await rig_for(db, char.id, owner, lock=lock)
    if not rig:
        raise RigError("モデルがありません", 404)
    query = select(CharacterRigBuild).where(CharacterRigBuild.rig_id == rig.id, CharacterRigBuild.id == build_id)
    if lock:
        query = query.with_for_update()
    build = (await db.execute(query)).scalar_one_or_none()
    if not build or (build.status == "archived" and not include_archived):
        raise RigError("モデルがありません", 404)
    return rig, build


async def archive(key: str, owner: str, build_id: uuid.UUID) -> None:
    """Remove a candidate from use without deleting its files or history.

    Re-importing the same validated package restores it as an inactive candidate.
    The owner/character lock serializes this with activation and imports.
    """
    async with await get_db_session() as db, db.begin():
        rig, build = await owned_build(db, key, owner, build_id, lock=True, include_archived=True)
        if build.status not in FINAL | {"canceled", "unsupported"}:
            raise RigError("制作を中止してからモデルを削除してください", 409)
        if build.status != "archived":
            build.report = {**(build.report or {}), "archived": {
                "at": datetime.now(timezone.utc).isoformat(), "previous_status": build.status}}
            build.status = "archived"
            build.config = {**(build.config or {}), "cancel_requested": True}
        if rig.active_revision == build.id:
            rig.active_revision = None


async def control(key, owner, build_id, action):
    from . import production
    if action == "resume":
        production.require_personal()
        saved = await production.settings()
        if saved.get("enabled") is not True:
            raise RigError("製作機能はOFFです", 503)
    async with await get_db_session() as db, db.begin():
        _rig, build = await owned_build(db, key, owner, build_id, lock=True)
        if model_format(build) != FORMAT or not (build.config or {}).get("production_path"):
            raise RigError("製作ジョブではありません", 409)
        if build.status == "ready":
            raise RigError("完成したimmutable候補は変更できません", 409)
        config = dict(build.config)
        config["cancel_requested"] = action == "cancel"
        if action == "cancel":
            build.status = "cancelled"
        elif action == "resume":
            if build.status not in {"failed", "needs_input", "cancelled"}:
                return build_view(build)
            if str(production.effective_path(saved)) != config["production_path"]:
                raise RigError("製作環境が変更されています。元の設定を復元してください", 409)
            config["resume_requested"] = True
            build.status, build.error = "queued", None
        else:
            raise RigError("未対応の操作です")
        build.config = config
        return build_view(build)


async def activate(key: str, owner: str, revision: uuid.UUID | None) -> None:
    async with await get_db_session() as db, db.begin():
        char = await character(db, key)
        rig = await rig_for(db, char.id, owner, lock=True)
        if rig is None:
            raise RigError("モデルがありません", 404)
        if revision:
            _rig, build = await owned_build(db, key, owner, revision, lock=True)
            if build.status != "ready" or not build.asset_path:
                raise RigError("検証されたモデルだけ使用できます", 409)
            if model_format(build) != FORMAT:
                raise RigError("未対応のモデル形式です", 409)
            try:
                package = await asyncio.to_thread(read_validated_directory, root_path(build.asset_path))
            except (PackageError, OSError) as exc:
                raise RigError("保存モデルの検証に失敗しました", 409) from exc
            if package.digest != (build.config or {}).get("package_sha256"):
                raise RigError("immutable候補のhashが一致しません", 409)
            build.report = {**(build.report or {}), "visual_review": {
                "required": True, "approved": True, "reviewer": owner,
                "reviewed_at": datetime.now(timezone.utc).isoformat()}}
        rig.active_revision = revision


async def asset(key: str, owner: str, revision: uuid.UUID | None, name: str) -> Path:
    async with await get_db_session() as db:
        char = await character(db, key)
        rig = await rig_for(db, char.id, owner)
        if not rig:
            raise RigError("モデルがありません", 404)
        if revision is None and name == "source.png" and rig.source_path:
            return root_path(rig.source_path)
        _rig, build = await owned_build(db, key, owner, revision)
        if build.status != "ready" or not build.asset_path:
            raise RigError("モデルがありません", 404)
        if model_format(build) != FORMAT:
            raise RigError("ファイルが見つかりません", 404)
        from .package_import import assert_regular, flat_name
        try:
            flat_name(name)
            allowed = {"manifest.json", *[e["path"] for e in (build.config or {}).get("manifest", {}).get("files", [])]}
            if name not in allowed:
                raise RigError("ファイルが見つかりません", 404)
            path = root_path(build.asset_path + "/" + name)
            await asyncio.to_thread(assert_regular, path)
            expected = next((e for e in build.config["manifest"]["files"] if e["path"] == name), None)
            expected_hash = expected["sha256"] if expected else build.config["package_sha256"]
            if path.stat().st_size > 128 * 1024 * 1024 or (expected and path.stat().st_size != expected["size"]):
                raise RigError("assetバイト長が一致しません", 409)
            if await asyncio.to_thread(lambda: hashlib.sha256(path.read_bytes()).hexdigest()) != expected_hash:
                raise RigError("asset hashが一致しません", 409)
            return path
        except (PackageError, OSError) as exc:
            raise RigError("ファイルが見つかりません", 404) from exc


async def _playback_context(db, key, owner, build_id, mode, *, lock=False):
    from . import playback_settings as playback
    from .models import CharacterRigPlaybackSettings

    rig, build = await owned_build(db, key, owner, build_id, lock=lock)
    if build.status != "ready" or not build.asset_path or model_format(build) != FORMAT:
        raise RigError("検証済みモデルの再生設定だけ利用できます", 409)
    try:
        package = await asyncio.to_thread(read_validated_directory, root_path(build.asset_path))
    except (PackageError, OSError) as exc:
        raise RigError("保存モデルの検証に失敗しました", 409) from exc
    # Historical package_sha256 is SHA256(manifest.json), never SHA256(zip).
    if package.digest != (build.config or {}).get("package_sha256"):
        raise RigError("immutable候補のmanifest hashが一致しません", 409)
    if mode not in {"model", "conversation-v1", "comparison-v1"}:
        raise RigError("未対応の再生modeです", 422)
    resolved_mode = package.manifest["profile"]["id"] if mode == "model" else mode
    editable = resolved_mode == "conversation-v1" and package.capabilities.get("coreRef") == "canonical"
    row = await db.get(CharacterRigPlaybackSettings, build.id)
    if row is not None and row.manifest_sha256 != package.digest:
        raise RigError("保存再生設定のmodel identityが一致しません", 409)
    defaults = package.playback_defaults
    if not editable:
        # Comparison and historical pinned engines never receive conversation
        # overrides (including authored new defaults that their engine ignores).
        defaults = playback.merge_settings(playback.canonical_defaults(), playback.legacy_profile_settings(package.manifest["profile"]))
        if resolved_mode == "comparison-v1":
            defaults["wind"].update(wind=0, windBlow=0)
    return rig, build, package, row, resolved_mode, editable, defaults


def _playback_view(context):
    from . import playback_settings as playback
    rig, build, package, row, mode, editable, defaults = context
    overrides = row.settings if row is not None and editable else {"version": playback.VERSION}
    try:
        overrides = playback.validate_block(overrides, require_version=True)
        effective = playback.merge_settings(defaults, overrides)
    except playback.PlaybackSettingsError as exc:
        raise RigError("保存再生設定の検証に失敗しました: " + str(exc), 409) from exc
    return playback.PlaybackSettingsView(
        character_id=str(rig.character_id), build_id=str(build.id),
        manifest_sha256=package.digest, mode=mode, editable=editable,
        revision=row.revision if row is not None else 0, schema_version=playback.VERSION,
        defaults=defaults, overrides=overrides, effective=effective,
        updated_at=row.updated_at.isoformat() if row is not None else None,
    )


async def get_playback_settings(key, owner, build_id, mode="model"):
    async with await get_db_session() as db:
        return _playback_view(await _playback_context(db, key, owner, build_id, mode))


async def save_playback_settings(key, owner, build_id, mode, manifest_sha256, expected_revision, settings=None):
    """None resets; an object merges a validated patch into saved overrides.

    The candidate lock serializes creation/update and optimistic revision guards
    stale clients. A reset keeps its revision tombstone to reject stale saves.
    """
    from . import playback_settings as playback
    from .models import CharacterRigPlaybackSettings

    async with await get_db_session() as db, db.begin():
        context = await _playback_context(db, key, owner, build_id, mode, lock=True)
        rig, build, package, row, resolved_mode, editable, defaults = context
        if not editable:
            raise RigError("比較再生と固定された旧engineの設定は変更できません。対応モデルで会話modeを明示してください", 409)
        if manifest_sha256 != package.digest or expected_revision != (row.revision if row is not None else 0):
            raise RigError("再生設定が更新されています。読み直してから保存してください", 409)
        try:
            current = playback.validate_block(row.settings, require_version=True) if row is not None else {"version": playback.VERSION}
            if settings is None:
                overrides = {"version": playback.VERSION}
            else:
                patch = playback.validate_block(settings)
                overrides = {"version": playback.VERSION, **{k: dict(v) for k, v in current.items() if k != "version"}}
                for group, fields in patch.items():
                    if group != "version":
                        overrides.setdefault(group, {}).update(fields)
            playback.merge_settings(defaults, overrides)
        except playback.PlaybackSettingsError as exc:
            raise RigError(str(exc), 422) from exc
        if row is None:
            row = CharacterRigPlaybackSettings(build_id=build.id, manifest_sha256=package.digest, revision=0)
            db.add(row)
        row.settings = overrides
        row.revision += 1
        row.updated_at = datetime.now(timezone.utc)
        await db.flush()
        return _playback_view((rig, build, package, row, resolved_mode, editable, defaults))
