"""HTTP 検証キャッシュ（ETag / 304）共通ヘルパー。

低帯域環境向け改善の一環。対象 GET エンドポイントに弱い ETag を付与し、
クライアントの ``If-None-Match`` が一致した場合は本文なしの ``304 Not Modified``
を返して転送量を削減する。

方針:
- ETag は「対象データの最大 updated_at + 件数」など安価な指標、または本文の
  ハッシュから算出できる。本モジュールでは両対応できるよう、任意の
  シリアライズ可能なペイロードからも、任意の署名パーツからも ETag を作れる。
- ユーザー / workspace 固有データから算出されるため ETag は自然に分離されるが、
  併せて必ず ``Cache-Control: private, no-cache`` を付与する。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Optional

from fastapi import Request
from fastapi.responses import JSONResponse, Response

# 200 応答・304 応答の双方に付与する共通キャッシュ制御ヘッダー。
# private: 共有プロキシにキャッシュさせない（ユーザー固有データのため）。
# no-cache: 毎回サーバーへ再検証させる（ETag 検証を必須化）。
_CACHE_CONTROL = "private, no-cache"


def _digest(raw: bytes) -> str:
    """バイト列から短いハッシュ文字列を得る。"""
    return hashlib.sha256(raw).hexdigest()[:32]


def make_weak_etag_from_parts(parts: Iterable[Any]) -> str:
    """安価な署名パーツ（最大 updated_at・件数など）から弱い ETag を作る。"""
    raw = "|".join("" if p is None else str(p) for p in parts).encode("utf-8")
    return f'W/"{_digest(raw)}"'


def make_weak_etag_from_payload(payload: Any) -> str:
    """シリアライズ可能なペイロード本文から弱い ETag を作る。

    ``sort_keys=True`` により辞書のキー順に依存しない安定したハッシュを得る。
    """
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, default=str
    ).encode("utf-8")
    return f'W/"{_digest(raw)}"'


def if_none_match_satisfied(request: Optional[Request], etag: str) -> bool:
    """リクエストの ``If-None-Match`` が指定 ETag と一致するか判定する。

    ``If-None-Match`` はカンマ区切りの複数トークンや ``*`` を取り得る。
    弱い ETag 比較（W/ 前置の有無を無視）で照合する。
    """
    if request is None:
        return False
    header = request.headers.get("if-none-match")
    if not header:
        return False

    target = _strip_weak(etag)
    for token in header.split(","):
        token = token.strip()
        if token == "*":
            return True
        if _strip_weak(token) == target:
            return True
    return False


def _strip_weak(value: str) -> str:
    """弱い ETag の ``W/`` 前置を取り除いて比較用に正規化する。"""
    value = value.strip()
    if value.startswith("W/"):
        value = value[2:]
    return value.strip()


def not_modified_response(etag: str) -> Response:
    """本文なしの 304 応答を生成する。"""
    return Response(
        status_code=304,
        headers={"ETag": etag, "Cache-Control": _CACHE_CONTROL},
    )


def apply_cache_headers(response: Response, etag: str) -> Response:
    """200 応答に ETag と Cache-Control を付与する。"""
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = _CACHE_CONTROL
    return response


def etag_json_response(
    request: Optional[Request],
    payload: Any,
    *,
    etag: Optional[str] = None,
) -> Response:
    """ETag 付き JSON 応答、または 304 応答を返す共通ショートカット。

    Args:
        request: 現在のリクエスト（``If-None-Match`` 判定に使用）。
        payload: JSON シリアライズ可能な応答本文。
        etag: 事前計算済み ETag。未指定なら payload から算出する。

    Returns:
        ``If-None-Match`` 一致時は 304（空ボディ）、それ以外は ETag ヘッダー付き
        ``JSONResponse``。
    """
    if etag is None:
        etag = make_weak_etag_from_payload(payload)
    if if_none_match_satisfied(request, etag):
        return not_modified_response(etag)
    response = JSONResponse(payload)
    return apply_cache_headers(response, etag)
