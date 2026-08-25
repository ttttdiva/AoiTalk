"""Finalize MiniMax H3 semantic categories and editable explanations.

This is a bounded second-stage Docs operation.  It keeps every existing topic,
fact, URL wrapper, and typed block ID; only category/explanation nodes are
created, and two explicitly named QA nodes are soft-archived.  The default
mode executes a read-only transaction and writes a new dry-run audit.  Database
mutation is available only through ``--apply`` with that audit as a manifest.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import multiprocessing
import os
import sys
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote_plus
from uuid import UUID, uuid5

from dotenv import load_dotenv
from sqlalchemy import literal, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

if multiprocessing.current_process().name == "MainProcess":
    from src.memory.models import DocsLibrary, KnowledgeNode, KnowledgeRevision
    from src.services.docs_acl import can_write_node
    from src.services.docs_graph_service import DocsGraphService


ROOT_ID = UUID("4a3c2921-1a3a-4242-aab3-74b5794e9d7f")
AUDIT_DIR = ROOT_DIR / "docs" / "audits" / "minimax_h3_content_hierarchy_20260822"
DEFAULT_MANIFEST = AUDIT_DIR / "dry_run.json"
CATEGORY_NAMESPACE = uuid5(ROOT_ID, "minimax-h3-content-categories-v1")
EXPLANATION_NAMESPACE = uuid5(ROOT_ID, "minimax-h3-content-explanations-v1")

CATEGORIES: "OrderedDict[str, dict[str, Any]]" = OrderedDict(
    (
        ("参考・未確認", {"sort_order": 16.5, "groups": ("G01", "G05")}),
        ("ワークフロー", {"sort_order": 18.5, "groups": ("G02", "G09")}),
        ("モデル", {"sort_order": 20.5, "groups": ("G03", "G06", "G07")}),
        ("公式・実装", {"sort_order": 22.5, "groups": ("G04", "G11")}),
        ("プロンプト・制作技法", {"sort_order": 23.25, "groups": ("G13",)}),
        ("後加工", {"sort_order": 35.5, "groups": ("G08",)}),
        ("LoRA", {"sort_order": 39.5, "groups": ("G10",)}),
        ("後加工・補助リソース", {"sort_order": 45.5, "groups": ("G12",)}),
    )
)

TOPICS: "OrderedDict[str, dict[str, Any]]" = OrderedDict(
    (
        ("G01", {"id": "71019796-9c90-4f1b-9686-68c7f47cfdb5", "category": "参考・未確認", "title": "Kijai MiniMax H3 / ComfyUIディスカッション"}),
        ("G02", {"id": "e67bfbf2-d3e4-4025-b953-4d1e6044cf1a", "category": "ワークフロー", "title": "MiniMax H3 EZ/Turbo RTXアップスケール・LTX Refineワークフロー"}),
        ("G03", {"id": "2ba28918-e937-426b-85ec-7d256d190e48", "category": "モデル", "title": "H3LT X2 Riding POV I2Vモデル"}),
        ("G04", {"id": "dcc72c6b-d026-4851-8084-19b8d095272a", "category": "公式・実装", "title": "MiniMax H3公式リポジトリ／prompt-writingスキル"}),
        ("G05", {"id": "329e8fa1-4811-5ddc-bce3-ef1045f829be", "category": "参考・未確認", "title": "MiniMax H3キャラクター入れ替えテスト"}),
        ("G06", {"id": "8b5e5e3d-52b7-489d-ad29-762b94a220e3", "category": "モデル", "title": "ClipProj-MiniMax-H3埋め込みアダプタ"}),
        ("G07", {"id": "6ce7dbe4-adef-4cb8-a140-7efa8edbfb08", "category": "モデル", "title": "10eros Max INT8 Ref2VAモデル", "semantic_confidence": "medium"}),
        ("G08", {"id": "5db7f4c2-e7a6-4e3a-972a-c38d4b8b4575", "category": "後加工", "title": "MiniMax H3 Latent Upscaler", "semantic_confidence": "medium"}),
        ("G09", {"id": "ed402bdb-385d-43c0-be1b-69555f40ae5f", "category": "ワークフロー", "title": "DaSiWa MiniMax H3 continue-from-clipワークフロー"}),
        ("G10", {"id": "6cc990c1-686f-4903-af9d-8d4baea2fe47", "category": "LoRA", "title": "MiniMax H3キャラクターLoRA学習（AI Toolkit）"}),
        ("G11", {"id": "94b6330a-3073-40a4-a6d3-aed49b9a15e8", "category": "公式・実装", "title": "ComfyUI-H3-Multishotリポジトリ"}),
        ("G12", {"id": "7f65079a-61c7-5a32-a890-bff52adb89ba", "category": "後加工・補助リソース", "title": "DaSiWaワークフロー補助リソース（Spectrum／Motion Context／Latent Upscaler）", "semantic_confidence": "high", "source_association_confidence": "medium"}),
        ("G13", {"id": "0cdec5fa-4a2c-5ce6-bce3-0c3e1d56a13c", "category": "プロンプト・制作技法", "title": "MiniMax H3参照素材プロンプト構文（未整理）", "semantic_confidence": "low", "unresolved": True}),
    )
)

EXPLANATIONS = {
    "G01": "KijaiのMiniMax-H3_comfy配布ページと関連discussionをまとめる参照項目。具体的な議論内容は保存済み本文から確定できないため要約しない。",
    "G02": "MiniMax H3向けにRTX UpscaleとLTX Refineを組み合わせるCivitaiワークフロー。EZ/Turbo構成を掲げているが、倍率・速度・画質差は根拠不足なので書かない。",
    "G03": "Riding POV系I2V向けとして保存されたモデル項目。モデル説明とは別に、取得済みの開始用prompt例を再利用可能な原文として保持する。",
    "G04": "MiniMax-AI公式repositoryとh3-prompt-writingスキルの導入情報。保存済みinstall commandを改変せず実行用資料として残す。",
    "G05": "MiniMax H3でのキャラクター入れ替えテストに関するX/Reddit参照。Xは取得履歴があるがsemantic本文が未整理、Redditは本文取得不能のため内容を推測しない。",
    "G06": "Qwen3-VL-4Bの埋め込みを学習済み線形変換で32B互換5120次元へ変換するPoC。保存済み記録ではテキストエンコーダVRAMが約15.7GB→5.2GB、32B固有知識や多言語音声性能の一部喪失が報告されている。",
    "G07": "10Eros_Max beta2をMiniMax H3のRef2VA向けにINT8 ConvRot化した実験的モデル。video referenceを使う用途を想定した配布物として公開されている。",
    "G08": "MiniMax H3の24-channel latentをlatent spaceのままアップスケールするためのモデル。低解像度生成→latent upscale→refineという流れを想定し、途中のVAE decode/encodeによるround-tripを避ける構成になっている。",
    "G09": "MiniMax H3で既存clipから生成を継続する用途として公開されたDaSiWaのCivitaiワークフロー。保存済み証拠から詳細手順までは復元しない。",
    "G10": "保存済み記録ではAI Toolkit 0.12.8を使用し、学習画像24枚（既存20＋補助4）、LoRA rank/alphaはいずれも16。これら3 factをそのままtopic直下に置く。",
    "G11": "MiniMax H3で複数shotを連続生成するmulti-shot chain向けのComfyUI repository。複数のshotをつないで扱う実装として公開されている。",
    "G12": "DaSiWaのMiniMax H3 workflowで利用する補助リソース群。Spectrum、Motion Context / Masked AV continuation、LBH Latent Upscalerのnode/modelを、workflowを補助する関連実装としてまとめて管理する。",
    "G13": "SubjectとPicture/Video参照を対応付ける入力例と、「参照画像をkeyframeとして使わない」指定を保存する。構文仕様として公式確認できていないため未整理を維持する。",
}

# Existing direct children whose visible labels are generic or ambiguous.  URL
# descendants are retained; only the parent label changes.
CHILD_RENAMES: "OrderedDict[str, dict[str, str]]" = OrderedDict(
    (
        ("00bbfa4e-4be2-45ac-8b2d-466448a38c28", {"group": "G01", "old": "関連ディスカッション", "new": "関連ディスカッション（内容未整理）"}),
        ("3670b7cd-1c61-48cc-aef6-da44c9575fb9", {"group": "G01", "old": "出典", "new": "Hugging Face配布元"}),
        ("17919297-c453-450c-b6b1-65cdcb96dd0d", {"group": "G02", "old": "ワークフロー配布ページ", "new": "Civitaiワークフロー配布ページ（内容未整理）"}),
        ("c24b9fbb-8fc1-4ecd-a492-6c2e4f1cfa05", {"group": "G02", "old": "出典", "new": "対象modelVersion 3208667"}),
        ("37fd866f-b48a-4a0f-8241-a8e2747a7494", {"group": "G03", "old": "モデルページ", "new": "Civitaiモデルページ"}),
        ("fcb72e7c-ea29-49bf-800c-7fc60072307c", {"group": "G04", "old": "出典", "new": "MiniMax H3公式リポジトリ"}),
        ("9c54a88b-a874-4e7a-a5a6-e9e9ed30acfe", {"group": "G05", "old": "X投稿", "new": "X投稿（内容未整理）"}),
        ("cd245f05-32a6-48d8-8346-7d28bfc4cec0", {"group": "G05", "old": "Reddit投稿", "new": "Reddit投稿（本文未取得）"}),
        ("50610985-4885-45cc-8118-92a4ea4bb3ec", {"group": "G06", "old": "ClipProj-MiniMax-H3", "new": "Hugging Face配布ページ"}),
        ("6ce588d5-315b-4569-8ae6-3b88e338a19c", {"group": "G07", "old": "モデルページ", "new": "Civitaiモデルページ（内容未整理）"}),
        ("0c03bc37-18d4-456d-9420-b2b88ec207d2", {"group": "G08", "old": "モデル配布ページ", "new": "Hugging Faceモデル配布ページ（内容未整理）"}),
        ("b00f3de7-ae61-452b-b6a8-435feed81b29", {"group": "G09", "old": "ワークフローURL", "new": "Civitaiワークフロー配布ページ（内容未整理）"}),
        ("b6f737d3-d67d-409c-a900-85dfcfe0928f", {"group": "G10", "old": "出典", "new": "X出典"}),
        ("74c07964-99a2-4492-99df-48f468037aac", {"group": "G11", "old": "GitHubリポジトリ", "new": "GitHubリポジトリ"}),
        ("d5336184-da99-4dc5-a6da-d78c463703d4", {"group": "G12", "old": "Spectrumリポジトリ", "new": "Spectrum MiniMax H3リポジトリ"}),
        ("bb461cb4-18fc-4dd4-996d-8d1154e081c7", {"group": "G12", "old": "Masked AV継続リポジトリ", "new": "Motion Context / Masked AV継続リポジトリ"}),
        ("815e2dc4-bc71-401a-a959-f6da347c5a01", {"group": "G12", "old": "Latent Upscalerノードリポジトリ", "new": "Latent Upscaler ComfyUIノードリポジトリ"}),
        ("eb8a6065-b8f4-4c71-b911-3596bf486bfb", {"group": "G12", "old": "Latent Upscalerモデル", "new": "Latent Upscalerモデル（Hugging Face）"}),
        ("db291b46-5220-4281-8ead-f0cae79c4540", {"group": "G12", "old": "出典", "new": "元ワークフロー出典"}),
    )
)

DUPLICATE_ARCHIVE = OrderedDict(
    (
        ("90fcb1f6-115b-4c78-bc87-4c7f1f1e1737", {"title": "出典", "child_id": "9f54b0b3-95c0-4b13-87b5-a65baa899217", "child_title": "https://civitai.red/models/2446218/h3ltx2-riding-pov-i2v?modelVersionId=3203205"}),
        ("6ad736eb-55db-4edb-9164-2444b1ddb21c", {"title": "出典", "child_id": "02f07181-1a4b-4676-b2f1-0b54aa7960cc", "child_title": "https://huggingface.co/NicoLab28/ClipProj-MiniMax-H3"}),
        ("38726611-319e-48de-952e-020f1aaeeebb", {"title": "出典", "child_id": "1a6cfe34-4440-4116-b700-03a45922ab73", "child_title": "https://civitai.red/models/2868392/10erosmaxint8ref2vabeta2?modelVersionId=3240646"}),
        ("b597836b-07d4-4fd3-a080-d601d28b81cc", {"title": "出典", "child_id": "f2a6dfdd-5785-468c-86f4-e34cd9b7919c", "child_title": "https://huggingface.co/LBH-123-AI/Minimax_h3_latent_Upscaler"}),
        ("aef0ffc4-2d28-4b7b-8dcf-9b8f05156639", {"title": "出典", "child_id": "0e28e5c6-9885-423a-b9b0-180e71ac49e2", "child_title": "https://civitai.red/models/2861921/minimax-h3-dasiwa-continue-from-clip-nativeaddguidespectrumfastpreview?modelVersionId=3232954"}),
        ("c43d267e-08cc-4c56-86b1-3174e1e47b9e", {"title": "出典", "child_id": "a9dde08d-a87a-407d-a9c5-45cc823f52d6", "child_title": "https://github.com/jlucasmcrell/ComfyUI-H3-Multishot"}),
        ("e58f5e1c-d72c-4ba3-8b8c-b89fc3ac63b0", {"title": "出典", "child_id": "a9c18863-89d6-446e-962c-cd435ae22b3d", "child_title": "https://x.com/i/status/2086006928845787574"}),
        ("97337e1e-6b05-4fba-8961-091423722703", {"title": "元リンク（本文を取得できず内容は未確認）", "child_id": "3116007b-4cf0-46e9-8215-04e1e871ad71", "child_title": "https://www.reddit.com/r/comfyui/comments/1vinc36/testing_character_swap_with_minimax_h3"}),
    )
)
G05_MOVED_URLS: dict[str, dict[str, str]] = {}
G05_ARCHIVE = {node_id: spec["title"] for node_id, spec in DUPLICATE_ARCHIVE.items() if node_id in {"e58f5e1c-d72c-4ba3-8b8c-b89fc3ac63b0", "97337e1e-6b05-4fba-8961-091423722703"}}

URL_WRAPPERS = {
    "00bbfa4e-4be2-45ac-8b2d-466448a38c28": ("7f034735-bfdb-4655-9afb-e9a5353ea62e", "https://huggingface.co/Kijai/MiniMax-H3_comfy/discussions/1#6a76008c52e2e58fe11280f9"),
    "3670b7cd-1c61-48cc-aef6-da44c9575fb9": ("973e01a4-6ed2-494a-8bca-8f0518c57d03", "https://huggingface.co/Kijai/MiniMax-H3_comfy"),
    "17919297-c453-450c-b6b1-65cdcb96dd0d": ("ff3cd58f-51fa-4c48-aee9-05c515f5d882", "https://civitai.com/models/2831976/minimax-h3-ez-or-turbo-optimal-rtx-upscale-ltx-refine"),
    "c24b9fbb-8fc1-4ecd-a492-6c2e4f1cfa05": ("581dcc7b-016a-4c7d-a22c-a093e30fe8cd", "https://civitai.com/models/2831976/minimax-h3-ez-or-turbo-optimal-rtx-upscale-ltx-refine?modelVersionId=3208667"),
    "37fd866f-b48a-4a0f-8241-a8e2747a7494": ("ce99c299-b02f-42ac-954f-c66782cebae2", "https://civitai.red/models/2446218/h3ltx2-riding-pov-i2v?modelVersionId=3203205"),
    "fcb72e7c-ea29-49bf-800c-7fc60072307c": ("e2e5c724-bfda-498f-b7c4-8ef4318ea16d", "https://github.com/MiniMax-AI/MiniMax-H3"),
    "9c54a88b-a874-4e7a-a5a6-e9e9ed30acfe": ("e46ec92c-83f2-4b20-9983-de609c1f89d5", "https://x.com/i/status/2086006928845787574"),
    "cd245f05-32a6-48d8-8346-7d28bfc4cec0": ("550a41f4-abcd-4ef0-8103-8cc57f5d2524", "https://www.reddit.com/r/comfyui/comments/1vinc36/testing_character_swap_with_minimax_h3/"),
    "50610985-4885-45cc-8118-92a4ea4bb3ec": ("a5668969-8192-47e5-809b-7d7158102bd0", "https://huggingface.co/NicoLab28/ClipProj-MiniMax-H3"),
    "6ce588d5-315b-4569-8ae6-3b88e338a19c": ("9246fa00-6578-4cc4-8861-052c182362aa", "https://civitai.red/models/2868392/10erosmaxint8ref2vabeta2?modelVersionId=3240646"),
    "0c03bc37-18d4-456d-9420-b2b88ec207d2": ("d9b46785-715c-4bdd-b37f-47ed56576925", "https://huggingface.co/LBH-123-AI/Minimax_h3_latent_Upscaler"),
    "b00f3de7-ae61-452b-b6a8-435feed81b29": ("fc8270de-5d3e-44cd-9284-32c998d0c8ef", "https://civitai.red/models/2861921/minimax-h3-dasiwa-continue-from-clip-nativeaddguidespectrumfastpreview?modelVersionId=3232954"),
    "b6f737d3-d67d-409c-a900-85dfcfe0928f": ("985c5899-739a-4ba2-9a60-999993718c61", "https://x.com/AIiswonder/status/2086927792659378414"),
    "74c07964-99a2-4492-99df-48f468037aac": ("e3ee3217-42c9-43d7-8d6f-4c560221e71d", "https://github.com/jlucasmcrell/ComfyUI-H3-Multishot"),
    "d5336184-da99-4dc5-a6da-d78c463703d4": ("e393e69a-18d2-4b7b-aa28-ac451442197a", "https://github.com/xmarre/ComfyUI-Spectrum-MiniMax-H3"),
    "bb461cb4-18fc-4dd4-996d-8d1154e081c7": ("ec739db6-8f0a-4127-9f19-efaeae16c042", "https://github.com/seitanism/ComfyUI-H3-Motion-Context-MultiRef"),
    "815e2dc4-bc71-401a-a959-f6da347c5a01": ("aa674e28-6712-4762-aad7-614f9a73cdab", "https://github.com/LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler"),
    "eb8a6065-b8f4-4c71-b911-3596bf486bfb": ("7099430e-1e36-4c9b-9af1-809be63ec7a7", "https://huggingface.co/LBH-123-AI/Minimax_h3_latent_Upscaler"),
    "db291b46-5220-4281-8ead-f0cae79c4540": ("fbb9e443-5239-44b6-b073-075f65ca921f", "https://civitai.red/models/2861921/minimax-h3-dasiwa-continue-from-clip-nativeaddguidespectrumfastpreview?modelVersionId=3232954"),
}

FACT_IDS = {
    "G06": ("d92153ce-f247-4df0-9592-bfc465591120", "bf83c21d-4447-4d47-96ce-c223d0d2a323", "efb7117d-5a22-4f11-bab4-c6273a161102", "5ee90035-f410-4eee-8c57-41e3c2b363d6"),
    "G10": ("d72527b1-1265-4226-9844-f5447cfbdd7b", "332e518b-5d1b-4be2-a874-f73fa6edca1a", "a7d46456-825d-402a-be3b-8de0bdf901e5"),
}
TYPED_IDS = ("3af3f1a2-f198-4fa2-847c-ceb68322b5b9", "9f9fa354-6125-4c52-be09-903afc76c65c", "532e459d-4074-4df4-aaed-9ca1657f4343", "3437ee3c-1a69-4d15-baa4-4c76880c35ac", "b77bc7a6-ea55-488d-911d-fc37635bef7f")
TYPED_HASHES = frozenset({"b4592ec241d852c1d03963011604e7992de5b61696dd54195324519b855c5419", "a338b514110b0e4e4975e87d0681b247be648ef7aaafdb2302c9e1f35275e2ec", "7d0c19e87d5d389ca9df5f9397146ab5840cbe2103340e8ce61589dd94ca98d9", "2a6f5e05771afa5560275f066a4ab6624cf0b8e8d6cc58f16a2abb6a968fc224", "99d7e6a4dab022a557436f52f10950ad4a8b1b81e722536531fe05ef70cccbfb"})
QA_NODES = {
    "2dbf88e4-a1a8-4545-b990-0ea6fcfe379b": "QA MiniMax H3 - explicit target topic A — 2026-08-22 restart",
    "e3c0d6ea-5510-4ef3-8360-c71f7d09b487": "QA MiniMax H3 explicit target topic B - 2026-08-22 restart",
}

SNAPSHOT_FIELDS = ("id", "parent_id", "root_page_id", "project_id", "docs_library_id", "archived_at", "title", "sort_order", "created_at", "body_text", "body_json_digest")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else (str(value) if value is not None else None)


def _uuid(value: Any) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def category_id(name: str) -> UUID:
    return uuid5(CATEGORY_NAMESPACE, name)


def explanation_id(topic_id: UUID | str) -> UUID:
    return uuid5(EXPLANATION_NAMESPACE, str(topic_id))


def _snapshot(node: Any) -> dict[str, Any]:
    return {"id": str(node.id), "parent_id": str(node.parent_id) if node.parent_id else None, "root_page_id": str(node.root_page_id) if node.root_page_id else None, "project_id": str(node.project_id) if node.project_id else None, "docs_library_id": str(node.docs_library_id) if node.docs_library_id else None, "archived_at": _iso(getattr(node, "archived_at", None)), "title": node.title, "sort_order": float(node.sort_order or 0), "created_at": _iso(getattr(node, "created_at", None)), "updated_at": _iso(getattr(node, "updated_at", None)), "body_text": node.body_text or "", "body_json_digest": _digest(node.body_json or {})}


def _compare_snapshot(node: Any, expected: dict[str, Any], *, label: str) -> list[str]:
    actual = _snapshot(node)
    errors = []
    for field in SNAPSHOT_FIELDS:
        if field not in expected:
            errors.append(f"{label}: snapshot missing {field}")
        elif field == "sort_order" and float(actual[field]) != float(expected[field]):
            errors.append(f"{label}: {field} changed")
        elif field != "sort_order" and actual[field] != expected[field]:
            errors.append(f"{label}: {field} changed")
    return errors


def _subtree_query(root_id: UUID, library_id: UUID):
    subtree = select(KnowledgeNode.id.label("node_id"), literal(0).label("depth")).where(KnowledgeNode.id == root_id, KnowledgeNode.docs_library_id == library_id).cte("h3_hierarchy_subtree", recursive=True)
    parent_alias = subtree.alias()
    subtree = subtree.union_all(select(KnowledgeNode.id.label("node_id"), (parent_alias.c.depth + 1).label("depth")).where(KnowledgeNode.parent_id == parent_alias.c.node_id, KnowledgeNode.docs_library_id == library_id, parent_alias.c.depth < 512))
    return select(KnowledgeNode).where(KnowledgeNode.id.in_(select(subtree.c.node_id)))


def _db_url() -> str:
    load_dotenv(ROOT_DIR / ".env")
    user = quote_plus(os.getenv("POSTGRES_USER", "aoitalk")); password = quote_plus(os.getenv("POSTGRES_PASSWORD", "")); host = os.getenv("POSTGRES_HOST", "127.0.0.1"); port = os.getenv("POSTGRES_PORT", "5432"); database = quote_plus(os.getenv("POSTGRES_DB", "aoitalk_memory"))
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{database}"


async def _load_state(session: AsyncSession) -> dict[str, Any]:
    root = await session.get(KnowledgeNode, ROOT_ID)
    if root is None or root.archived_at is not None:
        raise RuntimeError("MiniMax H3 root missing or archived")
    library = await session.get(DocsLibrary, root.docs_library_id)
    if library is None or library.owner_user_id is None:
        raise RuntimeError("Docs library owner missing")
    rows = list((await session.execute(_subtree_query(ROOT_ID, root.docs_library_id))).scalars().all())
    nodes = {row.id: row for row in rows}
    revisions = list((await session.execute(select(KnowledgeRevision).where(KnowledgeRevision.node_id.in_(list(nodes))).order_by(KnowledgeRevision.created_at.desc(), KnowledgeRevision.id.desc()))).scalars().all())
    latest = {}
    for revision in revisions:
        latest.setdefault(revision.node_id, revision)
    return {"root": root, "nodes": nodes, "library": library, "revisions": revisions, "revision_by_id": {revision.id: revision for revision in revisions}, "latest_revisions": latest}


def _latest(state: dict[str, Any], node_id: UUID) -> Any:
    row = state["latest_revisions"].get(node_id)
    if row is None:
        raise RuntimeError(f"latest revision missing: {node_id}")
    return row


def _topic_source_refs(state: dict[str, Any], topic: Any) -> list[dict[str, Any]]:
    """Resolve original direct refs from the semantic-finalization marker."""
    body = topic.body_json or {}
    clip = body.get("clip_ingest") if isinstance(body, dict) else None
    repair = clip.get("repair") if isinstance(clip, dict) else None
    revision_id = repair.get("source_revision_id") if isinstance(repair, dict) else None
    if revision_id:
        try:
            revision = state["revision_by_id"].get(_uuid(revision_id))
        except (TypeError, ValueError):
            revision = None
        if revision is not None:
            return copy.deepcopy(revision.source_refs_json or [])
    return copy.deepcopy((_latest(state, topic.id).source_refs_json or []))


def _descendant(node: Any, ancestor_ids: Iterable[UUID], nodes: dict[UUID, Any]) -> bool:
    wanted = set(ancestor_ids); current = node; seen = set()
    while current is not None and current.id not in seen:
        if current.parent_id in wanted:
            return True
        seen.add(current.id)
        current = nodes.get(current.parent_id)
    return False


def _in_root(node: Any, nodes: dict[UUID, Any]) -> bool:
    current = node; seen = set()
    while current is not None and current.id not in seen:
        if current.id == ROOT_ID:
            return True
        seen.add(current.id); current = nodes.get(current.parent_id)
    return False


def _typed_blocks(nodes: dict[UUID, Any]) -> dict[str, dict[str, Any]]:
    result = {}
    for node in nodes.values():
        body = node.body_json or {}
        if not isinstance(body, dict) or body.get("format") != "doc_block" or not isinstance(body.get("content"), str):
            continue
        content = body["content"]; digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        block = {"sha256": digest, "content": content, "block_type": body.get("block_type"), "label": body.get("label"), "node_id": str(node.id)}
        if digest in result and result[digest]["content"] != content:
            raise RuntimeError(f"typed hash collision: {digest}")
        result.setdefault(digest, block)
    return result


def _typed_errors(nodes: dict[UUID, Any]) -> list[str]:
    blocks = _typed_blocks(nodes); errors = []
    if set(blocks) != TYPED_HASHES:
        errors.append("typed SHA set mismatch")
    return errors


def _topic_nodes(state: dict[str, Any]) -> dict[str, Any]:
    return {group: state["nodes"].get(_uuid(spec["id"])) for group, spec in TOPICS.items()}


def _category_nodes(state: dict[str, Any]) -> dict[str, Any]:
    return {name: state["nodes"].get(category_id(name)) for name in CATEGORIES}


def _explanation_nodes(state: dict[str, Any]) -> dict[str, Any]:
    return {group: state["nodes"].get(explanation_id(spec["id"])) for group, spec in TOPICS.items()}


def _qa_errors(state: dict[str, Any]) -> list[str]:
    errors = []
    for node_id, title in QA_NODES.items():
        node = state["nodes"].get(_uuid(node_id))
        if node is None:
            errors.append(f"QA node missing: {node_id}")
            continue
        if node.title != title or node.parent_id != ROOT_ID:
            errors.append(f"QA precondition mismatch: {node_id}")
        if any(child.parent_id == node.id for child in state["nodes"].values()):
            errors.append(f"QA node has descendants: {node_id}")
    return errors


def _duplicate_archive_errors(state: dict[str, Any], *, final: bool = False) -> list[str]:
    errors: list[str] = []
    for node_id, spec in DUPLICATE_ARCHIVE.items():
        node = state["nodes"].get(_uuid(node_id))
        child = state["nodes"].get(_uuid(spec["child_id"]))
        if node is None or child is None:
            errors.append(f"duplicate archive node missing: {node_id}")
            continue
        if node.title != spec["title"] or not _in_root(node, state["nodes"]):
            errors.append(f"duplicate archive precondition mismatch: {node_id}")
        if child.parent_id != node.id or child.title != spec["child_title"]:
            errors.append(f"duplicate URL child precondition mismatch: {spec['child_id']}")
        if final:
            if node.archived_at is None or child.archived_at is None:
                errors.append(f"duplicate archive node is not archived: {node_id}")
        elif node.archived_at is not None or child.archived_at is not None:
            errors.append(f"duplicate archive node already archived: {node_id}")
    return errors


def _validate_urls(state: dict[str, Any], *, final: bool = False) -> list[str]:
    errors = []
    for parent_id, (wrapper_id, url) in URL_WRAPPERS.items():
        moved = next((value for value in G05_MOVED_URLS.values() if value["old_parent"] == parent_id), None)
        if moved is not None:
            # The old generic parent is archived after its URL child moves;
            # all other wrappers retain their original parent.
            expected_parent_id = moved["new_parent"] if final else moved["old_parent"]
            parent = state["nodes"].get(_uuid(expected_parent_id))
        else:
            parent = state["nodes"].get(_uuid(parent_id))
        wrapper = state["nodes"].get(_uuid(wrapper_id))
        if parent is None or wrapper is None:
            errors.append(f"wrapper missing: {parent_id}/{wrapper_id}")
            continue
        if wrapper.parent_id != parent.id or wrapper.title != url:
            errors.append(f"wrapper placement/url mismatch: {wrapper_id}")
    return errors


def _affected_ids(state: dict[str, Any]) -> set[UUID]:
    ids = {ROOT_ID, *(_uuid(spec["id"]) for spec in TOPICS.values()), *(_uuid(value) for value in CHILD_RENAMES), *(_uuid(value) for value in DUPLICATE_ARCHIVE), *(_uuid(spec["child_id"]) for spec in DUPLICATE_ARCHIVE.values()), *(_uuid(value) for values in FACT_IDS.values() for value in values), *(_uuid(value) for value in TYPED_IDS), *(_uuid(value) for value in QA_NODES)}
    ids.update(wrapper_id for wrapper_id, _url in ((_uuid(value[0]), value[1]) for value in URL_WRAPPERS.values()))
    ids.update(category_id(name) for name in CATEGORIES if state["nodes"].get(category_id(name)))
    ids.update(explanation_id(spec["id"]) for spec in TOPICS.values() if state["nodes"].get(explanation_id(spec["id"])))
    return ids


def _already_applied(state: dict[str, Any]) -> bool:
    nodes = state["nodes"]
    if _qa_errors(state) or _duplicate_archive_errors(state, final=True) or any(nodes[_uuid(node_id)].archived_at is None for node_id in QA_NODES):
        return False
    if _typed_errors(nodes) or _validate_urls(state, final=True):
        return False
    categories = _category_nodes(state); topics = _topic_nodes(state); explanations = _explanation_nodes(state)
    for name, spec in CATEGORIES.items():
        category = categories[name]
        if category is None or category.parent_id != ROOT_ID or category.title != name:
            return False
    for group, spec in TOPICS.items():
        topic = topics[group]; category = categories[spec["category"]]; explanation = explanations[group]
        if topic is None or topic.parent_id != category.id or topic.title != spec["title"]:
            return False
        if explanation is None or explanation.parent_id != topic.id or explanation.title != EXPLANATIONS[group] or explanation.body_text != EXPLANATIONS[group]:
            return False
        explanation_revision = state["latest_revisions"].get(explanation.id)
        if explanation_revision is None or _canonical(explanation_revision.source_refs_json or []) != _canonical(_topic_source_refs(state, topic)):
            return False
        if category.body_json:
            return False
    for node_id, spec in CHILD_RENAMES.items():
        if nodes.get(_uuid(node_id)) is None or nodes[_uuid(node_id)].title != spec["new"]:
            return False
    return True


def build_plan(state: dict[str, Any]) -> dict[str, Any]:
    nodes = state["nodes"]; root = state["root"]
    if root.id != ROOT_ID:
        raise RuntimeError("target root mismatch")
    already_state = _already_applied(state)
    if _typed_errors(nodes):
        raise RuntimeError("typed block precondition failed")
    errors = [] if already_state else (_qa_errors(state) + _duplicate_archive_errors(state, final=False) + _validate_urls(state, final=False))
    topics = _topic_nodes(state)
    for group, spec in TOPICS.items():
        topic = topics[group]
        if topic is None or not _in_root(topic, nodes):
            errors.append(f"topic missing/outside root: {group}")
        elif topic.title not in {spec["title"], "MiniMax H3 Latent Upscaler配布"}:
            errors.append(f"topic title mismatch: {group}")
    for node_id, spec in CHILD_RENAMES.items():
        node = nodes.get(_uuid(node_id))
        if node is None or not _in_root(node, nodes):
            errors.append(f"child missing/outside root: {node_id}")
        elif node.title not in {spec["old"], spec["new"]}:
            errors.append(f"child title mismatch: {node_id}")
    for group, ids in FACT_IDS.items():
        for node_id in ids:
            if _uuid(node_id) not in nodes:
                errors.append(f"fact missing: {node_id}")
    if errors:
        raise RuntimeError("content hierarchy precondition failed: " + "; ".join(errors))
    before = {}
    for node_id in _affected_ids(state):
        node = nodes.get(node_id)
        if node is None:
            continue
        snap = _snapshot(node)
        revision = state["latest_revisions"].get(node_id)
        if revision is not None:
            snap["source_refs"] = copy.deepcopy(revision.source_refs_json or [])
            snap["latest_revision_id"] = str(revision.id)
        before[str(node_id)] = snap
    categories = _category_nodes(state); explanations = _explanation_nodes(state)
    fact_titles = {node_id: nodes[_uuid(node_id)].title for ids in FACT_IDS.values() for node_id in ids}
    plan = {"mode": "dry_run", "status": "already_applied" if already_state else "dry_run_ready", "root_id": str(ROOT_ID), "generated_at": datetime.now(timezone.utc).isoformat(), "root_before": _snapshot(root), "categories": {name: {"id": str(category_id(name)), "title": name, "sort_order": spec["sort_order"], "exists": categories[name] is not None} for name, spec in CATEGORIES.items()}, "topic_paths": {group: f"MiniMax H3 / {spec['category']}" for group, spec in TOPICS.items()}, "topic_titles": {group: spec["title"] for group, spec in TOPICS.items()}, "semantic_confidence": {group: spec.get("semantic_confidence", "medium") for group, spec in TOPICS.items()}, "source_association_confidence": {group: spec.get("source_association_confidence") for group, spec in TOPICS.items() if spec.get("source_association_confidence")}, "unresolved": {group: bool(spec.get("unresolved")) for group, spec in TOPICS.items()}, "explanations": {group: {"id": str(explanation_id(spec["id"])), "title": EXPLANATIONS[group], "source_refs": _topic_source_refs(state, topics[group]), "exists": explanations[group] is not None} for group, spec in TOPICS.items()}, "child_renames": {node_id: dict(spec) for node_id, spec in CHILD_RENAMES.items()}, "g05_moves": {}, "duplicate_archive": copy.deepcopy(DUPLICATE_ARCHIVE), "before": before, "typed_blocks": _typed_blocks(nodes), "typed_sha256": sorted(_typed_blocks(nodes)), "fact_titles": fact_titles, "qa_archive": QA_NODES, "g14": {"action": "metadata_only_fold", "independent_topic": False, "topic_count": 0}, "move_revision_policy": "DocsGraphService.move_node structural revision is followed by a provenance revision with original source_refs; latest source_refs remain exact", "no_new_summary_nodes": True}
    return plan


def _compare_manifest(state: dict[str, Any], manifest: dict[str, Any]) -> list[str]:
    errors = []
    if manifest.get("root_before"):
        errors.extend(_compare_snapshot(state["root"], manifest["root_before"], label="root"))
    for node_id, expected in manifest.get("before", {}).items():
        node = state["nodes"].get(_uuid(node_id))
        if node is None:
            errors.append(f"node missing: {node_id}"); continue
        errors.extend(_compare_snapshot(node, expected, label=node_id))
        expected_refs = expected.get("source_refs")
        if expected_refs is not None:
            latest = state["latest_revisions"].get(_uuid(node_id))
            if latest is None or _canonical(latest.source_refs_json or []) != _canonical(expected_refs or []):
                errors.append(f"source_refs changed: {node_id}")
    errors.extend(_typed_errors(state["nodes"])); errors.extend(_validate_urls(state, final=False)); errors.extend(_qa_errors(state)); errors.extend(_duplicate_archive_errors(state, final=False))
    for node_id, expected_title in (manifest.get("fact_titles") or {}).items():
        node = state["nodes"].get(_uuid(node_id))
        if node is None or node.title != expected_title:
            errors.append(f"fact title changed: {node_id}")
    return errors


def _manifest_validate(manifest: dict[str, Any]) -> None:
    if manifest.get("root_id") != str(ROOT_ID) or manifest.get("status") not in {"dry_run_ready", "already_applied"}:
        raise RuntimeError("invalid hierarchy dry-run manifest")
    if set(manifest.get("topic_titles", {})) != set(TOPICS) or set(manifest.get("child_renames", {})) != set(CHILD_RENAMES):
        raise RuntimeError("hierarchy manifest mapping mismatch")
    if not isinstance(manifest.get("root_before"), dict) or set(manifest.get("fact_titles", {})) != {value for ids in FACT_IDS.values() for value in ids}:
        raise RuntimeError("hierarchy manifest root/fact snapshots missing")
    if set(manifest.get("typed_sha256", [])) != TYPED_HASHES:
        raise RuntimeError("hierarchy manifest typed hash mismatch")
    if set(manifest.get("g05_moves", {})) != set() or set(manifest.get("duplicate_archive", {})) != set(DUPLICATE_ARCHIVE):
        raise RuntimeError("duplicate archive mapping mismatch")


async def _lock_rows(session: AsyncSession, ids: set[UUID]) -> dict[UUID, Any]:
    rows = list((await session.execute(select(KnowledgeNode).where(KnowledgeNode.id.in_(list(ids))).with_for_update())).scalars().all())
    found = {row.id: row for row in rows}; missing = ids - found.keys()
    if missing:
        raise RuntimeError("FOR UPDATE missing: " + ", ".join(sorted(map(str, missing))))
    return found


async def _apply(session: AsyncSession, state: dict[str, Any], manifest: dict[str, Any]) -> None:
    await _lock_rows(session, _affected_ids(state) | {ROOT_ID} | {category_id(name) for name in CATEGORIES if state["nodes"].get(category_id(name))})
    session.expire_all(); locked = await _load_state(session)
    errors = _compare_manifest(locked, manifest)
    if errors and not _already_applied(locked):
        raise RuntimeError("lock-after precondition failed: " + "; ".join(errors))
    if _already_applied(locked):
        return
    root = locked["root"]; library = locked["library"]; actor = library.owner_user_id
    if not await can_write_node(session, root, actor, library=library):
        raise RuntimeError("hierarchy actor lacks root write ACL")
    docs = DocsGraphService(session); categories = {}
    for name, spec in CATEGORIES.items():
        category = locked["nodes"].get(category_id(name))
        if category is None:
            category = await docs.create_node(docs_library_id=root.docs_library_id, user_id=actor, title=name, parent=root, project_id=root.project_id, system_key=f"minimax_h3_category:{name}", body_json={}, source_refs=[], sort_order=spec["sort_order"], node_id=category_id(name))
        categories[name] = category
    topics = _topic_nodes(locked)
    for group, spec in TOPICS.items():
        topic = topics[group]; category = categories[spec["category"]]; current_topic_refs = copy.deepcopy((_latest(locked, topic.id).source_refs_json or [])); explanation_refs = _topic_source_refs(locked, topic)
        if topic.title != spec["title"]:
            await docs.update_node(node=topic, user_id=actor, title=spec["title"], body_json=copy.deepcopy(topic.body_json or {}), source_refs=current_topic_refs, change_summary=f"content hierarchy: {group} topic title")
        if topic.parent_id != category.id:
            await docs.move_node(node=topic, new_parent=category, user_id=actor)
            await docs.record_node_change(topic, actor, f"content hierarchy: move {group} topic", current_topic_refs)
        explanation = locked["nodes"].get(explanation_id(topic.id))
        if explanation is None:
            await docs.create_node(docs_library_id=root.docs_library_id, user_id=actor, title=EXPLANATIONS[group], parent=topic, project_id=root.project_id, body_json={}, source_refs=explanation_refs, sort_order=0.0, node_id=explanation_id(topic.id))
    for node_id, spec in CHILD_RENAMES.items():
        node = locked["nodes"][_uuid(node_id)]
        if node.title == spec["new"]:
            continue
        refs = copy.deepcopy((_latest(locked, node.id).source_refs_json or []))
        await docs.update_node(node=node, user_id=actor, title=spec["new"], body_json=copy.deepcopy(node.body_json or {}), source_refs=refs, change_summary=f"content hierarchy: {spec['group']} child label")
    # Remove duplicate wrapper+URL branches only after recording their exact
    # pre-state.  The canonical X/Reddit branches remain untouched.
    for node_id, duplicate in DUPLICATE_ARCHIVE.items():
        parent = locked["nodes"][_uuid(node_id)]
        child = locked["nodes"][_uuid(duplicate["child_id"])]
        child_refs = copy.deepcopy((_latest(locked, child.id).source_refs_json or []))
        parent_refs = copy.deepcopy((_latest(locked, parent.id).source_refs_json or []))
        if child.archived_at is None:
            await docs.archive_node(node=child, user_id=actor)
            await docs.record_node_change(child, actor, "content hierarchy: archive duplicate URL branch", child_refs)
        if parent.archived_at is None:
            await docs.archive_node(node=parent, user_id=actor)
            await docs.record_node_change(parent, actor, "content hierarchy: archive duplicate wrapper", parent_refs)
    for node_id, title in QA_NODES.items():
        node = locked["nodes"][_uuid(node_id)]
        if node.archived_at is None:
            refs = copy.deepcopy((_latest(locked, node.id).source_refs_json or []))
            await docs.archive_node(node=node, user_id=actor)
            await docs.record_node_change(node, actor, "content hierarchy: archive QA node", refs)
    await session.flush()


def _post_verify(state: dict[str, Any], manifest: dict[str, Any] | None = None) -> list[str]:
    """Explicitly verify every visible hierarchy invariant in a fresh session."""
    errors: list[str] = []
    nodes = state["nodes"]
    root = state["root"]
    if manifest and manifest.get("root_before"):
        errors.extend(_compare_snapshot(root, manifest["root_before"], label="root"))
    categories = _category_nodes(state)
    topics = _topic_nodes(state)
    explanations = _explanation_nodes(state)
    for name, spec in CATEGORIES.items():
        category = categories.get(name)
        if category is None or category.id != category_id(name) or category.parent_id != ROOT_ID or category.title != name:
            errors.append(f"category mismatch: {name}")
        elif category.body_json:
            errors.append(f"category body must remain empty: {name}")
    for group, spec in TOPICS.items():
        topic = topics.get(group); category = categories.get(spec["category"]); explanation = explanations.get(group)
        if topic is None or category is None or topic.parent_id != category.id or topic.title != spec["title"]:
            errors.append(f"topic/category mismatch: {group}")
        if manifest and topic is not None:
            expected_topic_refs = manifest.get("before", {}).get(str(topic.id), {}).get("source_refs")
            latest_topic = state["latest_revisions"].get(topic.id)
            if expected_topic_refs is not None and (latest_topic is None or _canonical(latest_topic.source_refs_json or []) != _canonical(expected_topic_refs or [])):
                errors.append(f"topic source_refs mismatch: {group}")
        if explanation is None or explanation.id != explanation_id(topic.id if topic else spec["id"]):
            errors.append(f"explanation missing: {group}")
        else:
            if explanation.parent_id != topic.id or explanation.title != EXPLANATIONS[group] or explanation.body_text != EXPLANATIONS[group] or explanation.body_json:
                errors.append(f"explanation text/parent mismatch: {group}")
            expected_refs = (manifest.get("explanations", {}).get(group, {}).get("source_refs") if manifest else None)
            latest = state["latest_revisions"].get(explanation.id)
            if expected_refs is not None and (latest is None or _canonical(latest.source_refs_json or []) != _canonical(expected_refs or [])):
                errors.append(f"explanation source_refs mismatch: {group}")
    for node_id, spec in CHILD_RENAMES.items():
        node = nodes.get(_uuid(node_id)); topic = topics.get(spec["group"])
        if node is None or topic is None or node.title != spec["new"] or node.parent_id != topic.id:
            errors.append(f"child rename/parent mismatch: {node_id}")
    for parent_id, (wrapper_id, url) in URL_WRAPPERS.items():
        wrapper = nodes.get(_uuid(wrapper_id))
        expected_parent = nodes.get(_uuid(parent_id))
        if wrapper is None or expected_parent is None or wrapper.parent_id != expected_parent.id or wrapper.title != url:
            errors.append(f"URL wrapper placement mismatch: {wrapper_id}")
    errors.extend(_duplicate_archive_errors(state, final=True))
    errors.extend(_qa_errors(state))
    for node_id in QA_NODES:
        node = nodes.get(_uuid(node_id))
        if node is None or node.archived_at is None:
            errors.append(f"QA node not archived: {node_id}")
    for node_id, expected_title in (manifest.get("fact_titles", {}) if manifest else {}).items():
        node = nodes.get(_uuid(node_id))
        if node is None or node.title != expected_title:
            errors.append(f"fact title changed: {node_id}")
    if manifest:
        source_checked = set(CHILD_RENAMES) | set(DUPLICATE_ARCHIVE) | {spec["child_id"] for spec in DUPLICATE_ARCHIVE.values()} | set(QA_NODES)
        for node_id in source_checked:
            expected = manifest.get("before", {}).get(node_id, {}).get("source_refs")
            latest = state["latest_revisions"].get(_uuid(node_id))
            if expected is not None and (latest is None or _canonical(latest.source_refs_json or []) != _canonical(expected or [])):
                errors.append(f"child source_refs mismatch: {node_id}")
    typed = _typed_blocks(nodes)
    if set(typed) != TYPED_HASHES:
        errors.append("typed SHA set mismatch")
    elif manifest:
        for digest, expected in manifest.get("typed_blocks", {}).items():
            if typed.get(digest, {}).get("content") != expected.get("content"):
                errors.append(f"typed content changed: {digest}")
    if any((node.body_json or {}).get("clip_ingest", {}).get("semantic_finalization", {}).get("group_id") == "G14" for node in nodes.values() if isinstance(node.body_json, dict)):
        errors.append("G14 visible topic exists")
    return errors


def _markdown(report: dict[str, Any]) -> str:
    lines = ["# MiniMax H3 content hierarchy audit", f"- mode: `{report.get('mode')}`", f"- status: `{report.get('status')}`", f"- root: `{report.get('root_id')}`", "- G14: `no visible topic`", "- QA archive: `soft archive only`", "", "## Topic paths"]
    lines.extend(f"- {group}: `{path}`" for group, path in (report.get("topic_paths") or {}).items())
    lines.append("\n## Explanations")
    lines.extend(f"- {group}: {data.get('title')}" for group, data in (report.get("explanations") or {}).items())
    lines.append("\n## Typed SHA-256")
    lines.extend(f"- `{digest}`" for digest in report.get("typed_sha256") or [])
    return "\n".join(lines) + "\n"


def write_audit(report: dict[str, Any], audit_dir: Path = AUDIT_DIR) -> tuple[Path, Path, Path]:
    audit_dir.mkdir(parents=True, exist_ok=True)
    readme = audit_dir / "README.md"
    is_apply = report.get("mode") == "apply"
    is_already = report.get("mode") == "dry_run" and report.get("status") == "already_applied"
    json_path = audit_dir / ("apply_audit.json" if is_apply else "already_applied_audit.json" if is_already else "dry_run.json")
    md_path = audit_dir / ("apply_audit.md" if is_apply else "already_applied_audit.md" if is_already else "dry_run.md")
    if not readme.exists():
        readme.write_text("# MiniMax H3 content hierarchy\n\nHistorical audits are immutable.\n", encoding="utf-8")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8"); md_path.write_text(_markdown(report), encoding="utf-8")
    return readme, json_path, md_path


async def run(*, apply: bool = False, manifest_path: Path = DEFAULT_MANIFEST, audit_dir: Path = AUDIT_DIR) -> dict[str, Any]:
    if apply and not manifest_path.exists():
        raise RuntimeError("--apply requires a prior hierarchy dry-run manifest")
    manifest = None
    if apply:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")); _manifest_validate(manifest)
    engine = create_async_engine(_db_url(), poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            try:
                if not apply:
                    await session.execute(text("SET TRANSACTION READ ONLY"))
                state = await _load_state(session)
                if not apply:
                    report = build_plan(state); await session.rollback(); return report
                if _already_applied(state):
                    report = dict(manifest or {}); report.update({"mode": "apply", "status": "already_applied"}); await session.rollback(); return report
                errors = _compare_manifest(state, manifest or {})
                if errors:
                    raise RuntimeError("apply precondition failed: " + "; ".join(errors))
                await _apply(session, state, manifest or {}); await session.commit()
            except Exception:
                await session.rollback(); raise
        async with AsyncSession(engine, expire_on_commit=False) as verify:
            verified = await _load_state(verify); errors = _post_verify(verified, manifest)
            if errors:
                raise RuntimeError("post verification failed: " + "; ".join(errors))
            report = dict(manifest or {}); report.update({"mode": "apply", "status": "applied", "verified_at": datetime.now(timezone.utc).isoformat()}); return report
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--apply", action="store_true"); parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST); parser.add_argument("--audit-dir", type=Path, default=AUDIT_DIR); args = parser.parse_args(argv)
    try:
        report = asyncio.run(run(apply=args.apply, manifest_path=args.manifest, audit_dir=args.audit_dir)); paths = write_audit(report, args.audit_dir); print(json.dumps({"status": report.get("status"), "audit": [str(path) for path in paths]}, ensure_ascii=False, indent=2)); return 0
    except Exception as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False, indent=2)); return 1


if __name__ == "__main__":
    raise SystemExit(main())
