"""Owner-authenticated access to87's editor and one scoped production project.

Only fixed tool assets are executable. Project uploads are data validated by87;
no production server, arbitrary repository browsing, or public project URL exists.
"""
from __future__ import annotations

import asyncio
import json
import hashlib
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from ..services.storage_roots import assert_no_links
from . import production, service

MAX_SETTINGS = 256 * 1024
_info_cache: dict[tuple[str, str], tuple[int, float, dict]] = {}
_info_lock = asyncio.Lock()
STATIC_FILES = frozenset({
    "editor.html", "css/editor.css", "motions/demo.json", "js/editor.js", "js/standalone-settings.js",
    *{"js/core/" + name + ".js" for name in ("model", "continuous-eyes", "puppet", "deform", "physics", "motion", "params", "renderer")},
    *{"js/runtime/" + name + ".js" for name in ("index", "host", "engine", "playback-settings", "playback-settings-schema", "playback-settings-panel")},
    *{"js/runtime-baseline/" + name + ".js" for name in ("deform", "physics", "motion", "params")},
})
HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
           "Referrer-Policy": "no-referrer",
           "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'"}


async def scope(character_id: str, owner: str, build_id):
    async with await service.get_db_session() as db:
        rig, build = await service.owned_build(db, character_id, owner, build_id)
        expected_character = str(rig.character_id)
        config = dict(build.config or {})
        if service.model_format(build) != "seethrough-rig" or not config.get("production_path"):
            raise service.RigError("この候補には再編集できる制作プロジェクトがありません", 409)
    saved = await production.settings()
    production.require_personal()
    if saved.get("enabled") is not True:
        raise service.RigError("制作連携がOFFです。完成モデルの再生には影響しません", 409)
    tool = production.effective_path(saved)
    if str(tool) != config["production_path"]:
        raise service.RigError("元の制作環境が設定されていません。完成モデルは引き続き再生できます", 409)
    project_id = str(uuid.UUID(config["project_id"]))
    projects = tool / "projects"
    project = projects / project_id
    assert_no_links(project, allow_missing=False)
    marker = project / "project.json"
    assert_no_links(marker, allow_missing=False)
    stamp = marker.stat().st_mtime_ns
    key = (str(tool), project_id)
    async with _info_lock:
        cached = _info_cache.get(key)
        if cached and cached[0] == stamp and cached[1] > time.monotonic():
            result = cached[2]
        else:
            result = await production.cli(tool, "project-editor-info", ["--project", project_id, "--projects-root", str(projects)], timeout=30)
            if len(_info_cache) >= 64:
                _info_cache.clear()
            _info_cache[key] = (stamp, time.monotonic() + 3, result)
    if (result.get("project_id") != project_id or result.get("owner") != owner
            or result.get("character") != expected_character
            or result.get("source_sha256") != config.get("source_sha256")):
        raise service.RigError("制作プロジェクトの所有者・入力が一致しません", 409)
    return tool, project, result, saved


async def edit_url(app_instance, request, character_id, owner, build_id):
    await scope(character_id, owner, build_id)
    # Relative product page, never the phone/browser's localhost filesystem.
    return "/settings/rig-editor/" + str(character_id) + "/" + str(build_id)


async def asset(character_id, owner, build_id, name):
    if name in STATIC_FILES:
        tool, _project, _info, _saved = await scope(character_id, owner, build_id)
        path = tool / "web" / name
        assert_no_links(path, allow_missing=False)
        if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            raise service.RigError("編集アプリのファイルが不正です", 404)
        return FileResponse(path, headers=HEADERS)
    prefix = "models/project/"
    if not name.startswith(prefix):
        raise service.RigError("編集用ファイルが見つかりません", 404)
    basename = name[len(prefix):]
    if not basename or "/" in basename or "\\" in basename or basename in {".", ".."}:
        raise service.RigError("編集用ファイルが不正です", 404)
    _tool, project, info, _saved = await scope(character_id, owner, build_id)
    relative = info.get("files", {}).get(basename)
    if basename == "settings.json" and relative is None:
        return JSONResponse({}, headers=HEADERS)
    if not isinstance(relative, str):
        raise service.RigError("編集用ファイルが見つかりません", 404)
    path = project / relative
    assert_no_links(path, allow_missing=False)
    if not path.resolve().is_relative_to(project.resolve()) or not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
        raise service.RigError("編集用ファイルの範囲が不正です", 409)
    raw = await asyncio.to_thread(path.read_bytes)
    if len(raw) > 32 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != info.get("hashes", {}).get(basename):
        _info_cache.pop((str(_tool), project.name), None)
        raise service.RigError("制作中のファイルが変更されました。編集画面を読み直してください", 409)
    media_type = "application/json" if basename.endswith(".json") else "image/webp" if basename.endswith(".webp") else "image/png"
    return Response(raw, media_type=media_type, headers=HEADERS)


async def save_settings(character_id, owner, build_id, data: bytes):
    if len(data) > MAX_SETTINGS:
        raise service.RigError("編集設定が大きすぎます", 413)
    tool, project, _info, _saved = await scope(character_id, owner, build_id)
    incoming = project / "requests"
    assert_no_links(incoming, allow_missing=True)
    incoming.mkdir(exist_ok=True)
    path = incoming / ("editor-" + uuid.uuid4().hex + ".json")
    try:
        with path.open("xb") as stream:
            stream.write(data)
        return await production.cli(tool, "project-editor-save", ["--project", project.name,
             "--projects-root", str(project.parent), "--settings-file", str(path)], timeout=60)
    finally:
        _info_cache.pop((str(tool), project.name), None)
        path.unlink(missing_ok=True)


def register_editor_routes(router, owner, call):
    @router.get("/builds/{build_id}/editor-info")
    async def get_info(character_id: str, build_id: uuid.UUID, principal: str = Depends(owner)):
        _tool, _project, info, _saved = await call(scope, character_id, principal, build_id)
        return {"core_ref": info["core_ref"], "status": info["status"]}

    @router.get("/builds/{build_id}/editor/{name:path}")
    async def get_asset(character_id: str, build_id: uuid.UUID, name: str, principal: str = Depends(owner)):
        return await call(asset, character_id, principal, build_id, name)

    @router.post("/builds/{build_id}/editor/api/settings")
    async def post_settings(request: Request, character_id: str, build_id: uuid.UUID, principal: str = Depends(owner)):
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_SETTINGS:
                raise HTTPException(413, "編集設定が大きすぎます")
        return await call(save_settings, character_id, principal, build_id, bytes(data))


async def external_url(relative: str) -> str | None:
    """Reuse the existing administrator-configured web_interface.public_url.

    Native opens the normal authenticated product page in the browser. Its
    bearer token is never appended to a URL or shared with the editor.
    """
    import ipaddress
    from urllib.parse import urlsplit

    config = await production.config_snapshot()
    web = config.get("web_interface", {})
    value = config.get("web_interface.public_url") or (web.get("public_url") if isinstance(web, dict) else None)
    if not isinstance(value, str) or not relative.startswith("/settings/rig-editor/"):
        return None
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            return None
        if parsed.hostname.lower() == "localhost" or parsed.hostname.lower().endswith(".localhost"):
            return None
        try:
            address = ipaddress.ip_address(parsed.hostname)
            if address.is_loopback or address.is_unspecified:
                return None
        except ValueError:
            pass
        return value.strip().rstrip("/") + relative
    except ValueError:
        return None
