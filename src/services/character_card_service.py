"""Character Card V2 エクスポート/インポートサービス

Character Card V2 (TavernAI 互換) フォーマットでの
キャラクターデータの入出力を提供する。
PNG tEXt チャンク "chara" へのBase64埋め込みもサポート。
"""

from __future__ import annotations

import base64
import io
import json
import logging
import struct
import uuid
import zlib
from datetime import datetime
from typing import Any, Dict, Optional, Union

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────
# Character Card V2 フォーマット定義
# ────────────────────────────────────────────

SPEC = "chara_card_v2"
SPEC_VERSION = "2.0"


def _character_to_v2(char: dict) -> dict:
    """内部キャラクターデータを Character Card V2 JSON に変換する。"""
    return {
        "spec": SPEC,
        "spec_version": SPEC_VERSION,
        "data": {
            "name": char.get("name", ""),
            "description": char.get("description", ""),
            "personality": char.get("personality_summary", ""),
            "first_mes": char.get("first_message", ""),
            "mes_example": char.get("example_messages", ""),
            "scenario": char.get("scenario", ""),
            "system_prompt": char.get("system_prompt", ""),
            "creator_notes": "",
            "character_version": "1.0",
            "tags": [],
            "alternate_greetings": char.get("alternate_greetings", []),
            # AoiTalk拡張フィールド
            "extensions": {
                "aoitalk": {
                    "slug": char.get("slug", ""),
                    "character_type": char.get("character_type", "assistant"),
                    "model": char.get("model", ""),
                    "voice_engine": char.get("voice_engine", ""),
                    "voice_name": char.get("voice_name", ""),
                    "voice_id": char.get("voice_id", ""),
                    "speaker_id": char.get("speaker_id"),
                    "voice_parameters": char.get("voice_parameters", {}),
                    "greeting": char.get("greeting", ""),
                    "fallback_reply": char.get("fallback_reply", ""),
                    "recognition_aliases": char.get("recognition_aliases", []),
                    "appearance_tags": char.get("appearance_tags", ""),
                    "negative_tags": char.get("negative_tags", ""),
                    "image_gen_engine": char.get("image_gen_engine", ""),
                    "auto_image_gen": char.get("auto_image_gen", False),
                    "avatar_image_path": char.get("avatar_image_path", ""),
                },
            },
        },
    }


def _v2_to_character_data(v2: dict) -> dict:
    """Character Card V2 JSON から内部キャラクターデータを復元する。"""
    data = v2.get("data", v2)
    ext = data.get("extensions", {}).get("aoitalk", {})

    name = data.get("name", "Unknown")
    slug = ext.get("slug", "")
    if not slug:
        # slug がなければ name から生成
        import re

        slug = re.sub(r"[^a-z0-9_]", "_", name.lower())
        slug = re.sub(r"_+", "_", slug).strip("_")
        if not slug or len(slug) < 3:
            slug = f"imported_{uuid.uuid4().hex[:8]}"

    result = {
        "name": name,
        "slug": slug,
        "character_type": ext.get("character_type", "roleplay"),
        "description": data.get("description", ""),
        "personality_summary": data.get("personality", ""),
        "first_message": data.get("first_mes", ""),
        "example_messages": data.get("mes_example", ""),
        "scenario": data.get("scenario", ""),
        "system_prompt": data.get("system_prompt", ""),
        "alternate_greetings": data.get("alternate_greetings", []),
        # AoiTalk拡張
        "model": ext.get("model", ""),
        "voice_engine": ext.get("voice_engine", ""),
        "voice_name": ext.get("voice_name", ""),
        "voice_id": ext.get("voice_id", ""),
        "speaker_id": ext.get("speaker_id"),
        "voice_parameters": ext.get("voice_parameters", {}),
        "greeting": ext.get("greeting", ""),
        "fallback_reply": ext.get("fallback_reply", ""),
        "recognition_aliases": ext.get("recognition_aliases", []),
        "appearance_tags": ext.get("appearance_tags", ""),
        "negative_tags": ext.get("negative_tags", ""),
        "image_gen_engine": ext.get("image_gen_engine", ""),
        "auto_image_gen": ext.get("auto_image_gen", False),
        "avatar_image_path": ext.get("avatar_image_path", ""),
    }

    return result


# ────────────────────────────────────────────
# エクスポート
# ────────────────────────────────────────────


async def export_character_card_v2(character_id: str) -> dict:
    """キャラクターを Character Card V2 JSON としてエクスポートする。

    Args:
        character_id: キャラクターID (UUID文字列)

    Returns:
        Character Card V2 形式の辞書
    """
    from .character_service import get_character, CharacterNotFoundError

    char = await get_character(character_id)
    return _character_to_v2(char)


async def export_as_png(character_id: str) -> bytes:
    """キャラクターを Character Card V2 PNG としてエクスポートする。

    アバター画像（存在する場合）または1x1デフォルト画像の
    tEXt チャンク "chara" に V2 JSON を Base64 エンコードして埋め込む。

    Args:
        character_id: キャラクターID (UUID文字列)

    Returns:
        PNG バイト列
    """
    from .character_service import get_character

    char = await get_character(character_id)
    v2_json = _character_to_v2(char)

    # V2 JSON を Base64 エンコード
    json_bytes = json.dumps(v2_json, ensure_ascii=False).encode("utf-8")
    b64_data = base64.b64encode(json_bytes).decode("ascii")

    # アバター画像を読み込み、なければデフォルト1x1画像を生成
    png_bytes = _get_avatar_png(char.get("avatar_image_path", ""))

    # tEXt チャンク "chara" を挿入した PNG を返す
    return _embed_text_chunk_in_png(png_bytes, "chara", b64_data)


# ────────────────────────────────────────────
# インポート
# ────────────────────────────────────────────


async def import_character_card_v2(data: Union[dict, bytes]) -> dict:
    """Character Card V2 データからキャラクターを作成する。

    Args:
        data: V2 JSON 辞書、または PNG バイト列

    Returns:
        作成されたキャラクターの辞書
    """
    from .character_service import create_character

    if isinstance(data, (bytes, bytearray)):
        # PNG からtEXtチャンク "chara" を読み取る
        v2_json = _extract_text_chunk_from_png(data, "chara")
        if not v2_json:
            raise ValueError("PNG ファイルに Character Card データが含まれていません")
        # Base64 デコード → JSON パース
        json_bytes = base64.b64decode(v2_json)
        v2_dict = json.loads(json_bytes.decode("utf-8"))
    elif isinstance(data, dict):
        v2_dict = data
    else:
        raise ValueError(f"サポートされていないデータ型: {type(data)}")

    char_data = _v2_to_character_data(v2_dict)
    return await create_character(char_data)


# ────────────────────────────────────────────
# PNG ユーティリティ
# ────────────────────────────────────────────


def _get_avatar_png(avatar_path: str) -> bytes:
    """アバター画像を読み込む。見つからなければ1x1透明PNGを返す。"""
    if avatar_path:
        import os

        if os.path.isfile(avatar_path):
            with open(avatar_path, "rb") as f:
                return f.read()

    # 1x1 透明 PNG を生成
    return _create_minimal_png()


def _create_minimal_png() -> bytes:
    """最小限の1x1透明PNGを生成する。"""
    # IHDR
    width = 1
    height = 1
    bit_depth = 8
    color_type = 6  # RGBA

    ihdr_data = struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0, 0)
    ihdr_crc = zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF
    ihdr_chunk = (
        struct.pack(">I", len(ihdr_data))
        + b"IHDR"
        + ihdr_data
        + struct.pack(">I", ihdr_crc)
    )

    # IDAT (1x1 RGBA: filter_byte(0) + R,G,B,A)
    raw_data = b"\x00\x00\x00\x00\x00"  # filter=None, RGBA=(0,0,0,0)
    compressed = zlib.compress(raw_data)
    idat_crc = zlib.crc32(b"IDAT" + compressed) & 0xFFFFFFFF
    idat_chunk = (
        struct.pack(">I", len(compressed))
        + b"IDAT"
        + compressed
        + struct.pack(">I", idat_crc)
    )

    # IEND
    iend_crc = zlib.crc32(b"IEND") & 0xFFFFFFFF
    iend_chunk = struct.pack(">I", 0) + b"IEND" + struct.pack(">I", iend_crc)

    # PNG signature + chunks
    signature = b"\x89PNG\r\n\x1a\n"
    return signature + ihdr_chunk + idat_chunk + iend_chunk


def _embed_text_chunk_in_png(png_data: bytes, keyword: str, text: str) -> bytes:
    """PNG ファイルの IEND の前に tEXt チャンクを挿入する。"""
    # tEXt チャンクデータ: keyword + null separator + text
    chunk_data = keyword.encode("latin-1") + b"\x00" + text.encode("latin-1")
    chunk_type = b"tEXt"
    chunk_crc = zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
    text_chunk = (
        struct.pack(">I", len(chunk_data))
        + chunk_type
        + chunk_data
        + struct.pack(">I", chunk_crc)
    )

    # IEND チャンクの位置を探す（末尾12バイト: length(4) + "IEND"(4) + crc(4)）
    iend_pos = png_data.rfind(b"IEND")
    if iend_pos < 4:
        # IEND が見つからない場合は末尾に追加
        return png_data + text_chunk

    # IEND チャンクの開始位置（length フィールドの先頭）
    iend_start = iend_pos - 4

    return png_data[:iend_start] + text_chunk + png_data[iend_start:]


def _extract_text_chunk_from_png(png_data: bytes, keyword: str) -> Optional[str]:
    """PNG ファイルから指定 keyword の tEXt チャンクを読み取る。"""
    # PNG signature をスキップ
    pos = 8
    keyword_bytes = keyword.encode("latin-1")

    while pos < len(png_data) - 8:
        if pos + 8 > len(png_data):
            break

        length = struct.unpack(">I", png_data[pos : pos + 4])[0]
        chunk_type = png_data[pos + 4 : pos + 8]
        chunk_data_start = pos + 8
        chunk_data_end = chunk_data_start + length

        if chunk_data_end > len(png_data):
            break

        if chunk_type == b"tEXt":
            chunk_data = png_data[chunk_data_start:chunk_data_end]
            null_pos = chunk_data.find(b"\x00")
            if null_pos >= 0:
                found_keyword = chunk_data[:null_pos]
                if found_keyword == keyword_bytes:
                    text_value = chunk_data[null_pos + 1 :]
                    return text_value.decode("latin-1")

        # 次のチャンクへ (length + type(4) + data(length) + crc(4))
        pos = chunk_data_end + 4  # +4 for CRC

    return None
