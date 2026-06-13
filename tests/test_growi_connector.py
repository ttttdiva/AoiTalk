"""GROWI コネクタの応答パース・URL組み立て・パスフィルタの単体テスト。

ネットワークやDBを必要としない純粋ロジックのみを検証する。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from src.knowledge.growi_client import GrowiClient, GrowiPage, build_page_url
from src.knowledge.service import KnowledgeService


# ---------------------------------------------------------------------------
# 応答パース（GROWI のバージョン差吸収）
# ---------------------------------------------------------------------------
def test_extract_pages_handles_v3_and_classic_shapes():
    assert GrowiClient._extract_pages({"pages": [{"path": "/a"}]}) == [{"path": "/a"}]
    assert GrowiClient._extract_pages({"data": [{"path": "/b"}]}) == [{"path": "/b"}]
    assert GrowiClient._extract_pages({"unexpected": 1}) == []
    assert GrowiClient._extract_pages(None) == []


def test_parse_page_item_extracts_id_path_and_revision():
    item = {
        "_id": "page123",
        "path": "/営業/オンボーディング",
        "revision": {"_id": "rev789"},
        "updatedAt": "2026-06-01T10:00:00.000Z",
    }
    page = GrowiClient._parse_page_item(item)
    assert page is not None
    assert page.page_id == "page123"
    assert page.path == "/営業/オンボーディング"
    assert page.revision_id == "rev789"
    assert page.change_key == "rev789"


def test_parse_page_item_falls_back_to_updated_at_for_change_key():
    page = GrowiClient._parse_page_item(
        {"id": "p1", "path": "/x", "updated_at": "2026-06-02T00:00:00Z"}
    )
    assert page is not None
    assert page.revision_id is None
    assert page.change_key == "2026-06-02T00:00:00Z"


def test_parse_page_item_returns_none_without_id_or_path():
    assert GrowiClient._parse_page_item({"path": "/no-id"}) is None
    assert GrowiClient._parse_page_item({"_id": "no-path"}) is None


def test_extract_body_handles_wrapped_and_flat_revisions():
    assert (
        GrowiClient._extract_body({"page": {"revision": {"body": "# 本文"}}}) == "# 本文"
    )
    assert GrowiClient._extract_body({"revision": {"body": "flat"}}) == "flat"
    assert GrowiClient._extract_body({"page": {"body": "legacy"}}) == "legacy"
    assert GrowiClient._extract_body({"nothing": True}) is None


# ---------------------------------------------------------------------------
# URL 組み立て
# ---------------------------------------------------------------------------
def test_build_page_url_encodes_path_and_trims_base():
    assert build_page_url("https://wiki.example.com/", "/Sales/Plan") == (
        "https://wiki.example.com/Sales/Plan"
    )
    # 日本語パスはURLエンコードされる
    url = build_page_url("https://wiki.example.com", "/営業")
    assert url.startswith("https://wiki.example.com/")
    assert "%E5%96%B6%E6%A5%AD" in url


def test_build_page_url_adds_leading_slash():
    assert build_page_url("https://w", "Foo") == "https://w/Foo"


# ---------------------------------------------------------------------------
# パスフィルタ
# ---------------------------------------------------------------------------
def test_growi_path_matches_default_excludes_trash_and_user():
    include = ["*"]
    exclude = ["/trash/*", "/trash", "/user/*"]
    assert KnowledgeService._growi_path_matches("/営業/手順", include, exclude) is True
    assert KnowledgeService._growi_path_matches("/trash/old", include, exclude) is False
    assert KnowledgeService._growi_path_matches("/user/alice/memo", include, exclude) is False


def test_growi_path_matches_normalizes_leading_slash():
    assert KnowledgeService._growi_path_matches("Foo", ["*"], []) is True


# ---------------------------------------------------------------------------
# 日時パース
# ---------------------------------------------------------------------------
def test_parse_iso_datetime_handles_z_suffix_and_invalid():
    parsed = KnowledgeService._parse_iso_datetime("2026-06-01T10:00:00.000Z")
    assert isinstance(parsed, datetime)
    assert parsed.tzinfo is None
    assert KnowledgeService._parse_iso_datetime(None) is None
    assert KnowledgeService._parse_iso_datetime("not-a-date") is None


# ---------------------------------------------------------------------------
# 出典URL（検索結果）
# ---------------------------------------------------------------------------
def test_document_url_returns_growi_page_url_only_for_growi_sources():
    from src.memory.models import KnowledgeDocument, KnowledgeSource

    growi = KnowledgeSource.__new__(KnowledgeSource)
    growi.source_type = "growi"
    growi.root_path = "https://wiki.example.com"

    local = KnowledgeSource.__new__(KnowledgeSource)
    local.source_type = "local_dir"
    local.root_path = "/data/docs"

    document = KnowledgeDocument.__new__(KnowledgeDocument)
    document.id = uuid.uuid4()
    document.path = "/Sales/Plan"

    assert KnowledgeService._document_url(growi, document) == (
        "https://wiki.example.com/Sales/Plan"
    )
    assert KnowledgeService._document_url(local, document) is None
