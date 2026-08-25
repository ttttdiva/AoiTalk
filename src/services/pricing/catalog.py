"""料金カタログの読み込み・検証・DB 同期。

料金の正本は `config/pricing_catalog.json`。DB の `pricing_rules` は
そのスナップショットであり、履歴計算のために過去行を保持し続ける。
金額は必ず `Decimal` で扱い、float を一切経由しない。
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_CATALOG_PATH",
    "CatalogValidationError",
    "PricingRuleSnapshot",
    "CatalogDocument",
    "PRICING_KINDS",
    "RATE_KEYS",
    "repo_root",
    "resolve_catalog_path",
    "load_catalog_file",
    "load_catalog_document",
    "validate_catalog",
    "build_snapshots",
    "diff_catalog",
    "sync_catalog_to_db",
    "normalize_decimal_text",
    "quantize_to_storage",
    "to_decimal",
    "rule_content_fingerprint",
    "rules_equal",
    "RATE_SCALE",
    "MULTIPLIER_SCALE",
]

# 契約どおりリポジトリルート相対で保持する（実解決は resolve_catalog_path）
DEFAULT_CATALOG_PATH = Path("config/pricing_catalog.json")

PRICING_KINDS = frozenset(
    {"flat_token", "tiered_token", "provider_reported", "subscription", "local"}
)
TOKEN_KINDS = frozenset({"flat_token", "tiered_token"})
RATE_KEYS = ("input", "cached_input", "output", "cache_write")

# DB の格納桁数。`pricing_rules` の単価列 NUMERIC(18,8) / 倍率 NUMERIC(10,4) に対応する。
RATE_SCALE = Decimal("0.00000001")
MULTIPLIER_SCALE = Decimal("0.0001")


class CatalogValidationError(ValueError):
    """カタログ JSON が契約を満たしていない。"""


def repo_root() -> Path:
    """リポジトリルート（src/services/pricing/ の3つ上）。"""
    return Path(__file__).resolve().parents[3]


def resolve_catalog_path(path: "str | Path | None" = None) -> Path:
    """カタログ JSON の実パスを解決する。相対指定はリポジトリルート基準。"""
    candidate = Path(path) if path is not None else DEFAULT_CATALOG_PATH
    if candidate.is_absolute():
        return candidate
    return repo_root() / candidate


# ────────────────────────────────────────────
# 内容比較（純粋関数）
# ────────────────────────────────────────────


def to_decimal(value: Any) -> Optional[Decimal]:
    """任意の数値表現を Decimal へ。float は文字列経由で誤差を避ける。"""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    try:
        return Decimal(str(value).strip())
    except InvalidOperation:
        return None


def quantize_to_storage(value: Any, scale: Decimal) -> Optional[Decimal]:
    """DB の NUMERIC 桁数へ丸める。

    `pricing_rules` の単価列は `NUMERIC(18,8)`、長文倍率は `NUMERIC(10,4)`。
    保存時に丸められる値を丸めずに比較すると、書いた値と読んだ値が永久に
    一致せず毎回 `updated` になってしまうため、比較も書き込みもこれを通す。
    """
    dec = to_decimal(value)
    if dec is None or not dec.is_finite():
        return dec
    return dec.quantize(scale, rounding=ROUND_HALF_UP)


def normalize_decimal_text(value: Any, scale: Optional[Decimal] = None) -> Optional[str]:
    """料金値を「桁揃えに依存しない」正規表現へ落とす。

    DB の `NUMERIC(18,8)` は `Decimal("0.30000000")` を返し、カタログ側は
    `Decimal("0.30")` を持つ。両者は数値として等しいので同じ文字列へ揃える。
    `str()` / `repr()` で比較すると誤って差分扱いになるため必ずこれを使う。

    `scale` を渡すと、その保存桁数へ丸めてから正規化する（DB に格納できない
    余剰桁で無限に差分が出るのを防ぐ）。
    """
    if value is None:
        return None
    dec = to_decimal(value)
    if dec is None:
        return str(value)
    if not dec.is_finite():
        return str(dec)
    if scale is not None:
        dec = dec.quantize(scale, rounding=ROUND_HALF_UP)
    normalized = dec.normalize()
    if normalized == 0:
        # Decimal("0.00").normalize() は Decimal("0") になるが符号や指数を潰しておく
        return "0"
    return format(normalized, "f")


def _normalize_rate_map(
    raw: Any, scale: Optional[Decimal] = None
) -> Dict[str, Optional[str]]:
    """rates / tool_rates を「キー順非依存・桁揃え非依存」の dict へ。"""
    if not raw:
        return {}
    return {
        str(key): normalize_decimal_text(value, scale)
        for key, value in dict(raw).items()
        if value is not None
    }


def _normalize_tiers(raw: Any) -> List[dict]:
    """tiers を JSON ラウンドトリップ非依存の形へ。"""
    if not raw:
        return []
    normalized: List[dict] = []
    for entry in raw:
        source = dict(entry or {})
        max_tokens = source.get("max_input_tokens")
        normalized.append(
            {
                "max_input_tokens": (None if max_tokens is None else int(max_tokens)),
                "rates": _normalize_rate_map(source.get("rates")),
            }
        )
    return normalized


def rule_content_fingerprint(snapshot: "PricingRuleSnapshot") -> dict:
    """料金内容の同一性判定に使う正規化済み表現（純粋関数）。

    `rule_id` / `catalog_version` / `effective_to` は運用メタ情報なので含めない
    （カタログ版だけ上げた再適用で無駄な UPDATE を起こさないため）。
    数値は Decimal を桁揃え非依存の文字列へ正規化し、JSON ラウンドトリップ後の
    文字列レートとも一致するようにする。
    """
    return {
        "provider": snapshot.provider,
        "canonical_model": snapshot.canonical_model,
        "aliases": sorted(snapshot.aliases),
        "pricing_kind": snapshot.pricing_kind,
        # 単価列は NUMERIC(18,8) なので保存桁数へ丸めてから比較する
        "rates": _normalize_rate_map(snapshot.rates, RATE_SCALE),
        # tiers / tool_rates は JSONB（文字列保存）なので丸めない
        "tiers": _normalize_tiers(snapshot.tiers),
        "long_context": (
            {
                "threshold_tokens": int(snapshot.long_context["threshold_tokens"]),
                "input_multiplier": normalize_decimal_text(
                    snapshot.long_context.get("input_multiplier"), MULTIPLIER_SCALE
                ),
                "output_multiplier": normalize_decimal_text(
                    snapshot.long_context.get("output_multiplier"), MULTIPLIER_SCALE
                ),
            }
            if snapshot.long_context
            else None
        ),
        "tool_rates": _normalize_rate_map(snapshot.tool_rates),
        "effective_from": snapshot.effective_from.isoformat(),
        "source": snapshot.source or "",
    }


def rules_equal(left: "PricingRuleSnapshot", right: "PricingRuleSnapshot") -> bool:
    """2つのルールの料金内容が同一か（桁揃え・JSON 表現差を無視して比較）。"""
    return rule_content_fingerprint(left) == rule_content_fingerprint(right)


# ────────────────────────────────────────────
# データ構造
# ────────────────────────────────────────────


@dataclass(frozen=True)
class PricingRuleSnapshot:
    """1つの料金ルール（有効期間つき）。"""

    rule_id: str
    provider: str
    canonical_model: str
    aliases: Tuple[str, ...]
    pricing_kind: str
    rates: Dict[str, Decimal]
    tiers: Tuple[dict, ...]
    long_context: Optional[dict]
    tool_rates: Dict[str, Decimal]
    effective_from: datetime
    effective_to: Optional[datetime]
    source: str
    catalog_version: str

    @property
    def scope_key(self) -> Tuple[str, str]:
        return (self.provider, self.canonical_model)

    @property
    def unique_key(self) -> Tuple[str, str, datetime]:
        return (self.provider, self.canonical_model, self.effective_from)

    def content_signature(self) -> str:
        """料金内容の同一性判定用シグネチャ（桁揃え・JSON 表現差に非依存）。"""
        blob = json.dumps(
            rule_content_fingerprint(self), ensure_ascii=False, sort_keys=True
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CatalogDocument:
    """カタログ JSON 全体。"""

    catalog_version: str
    schema_version: int
    generated_at: Optional[datetime]
    free_incentive_groups: Dict[str, Tuple[str, ...]]
    rules: Tuple[PricingRuleSnapshot, ...]
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def rule_count(self) -> int:
        return len(self.rules)

    def digest(self) -> str:
        blob = json.dumps(self.raw, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ────────────────────────────────────────────
# パース補助
# ────────────────────────────────────────────


def _parse_dt(value: Any, *, where: str) -> datetime:
    """ISO8601（末尾 Z 可）を UTC naive datetime へ。"""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    if not isinstance(value, str) or not value.strip():
        raise CatalogValidationError(f"{where}: 日時が文字列ではありません: {value!r}")
    text = value.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:  # noqa: PERF203
        raise CatalogValidationError(f"{where}: 日時を解釈できません: {value!r}") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _parse_decimal(value: Any, *, where: str) -> Decimal:
    """料金値を Decimal へ。float は誤差源なので拒否する。"""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool) or isinstance(value, float):
        raise CatalogValidationError(
            f"{where}: 料金値は文字列で書いてください（float 禁止）: {value!r}"
        )
    if isinstance(value, int):
        return Decimal(value)
    if not isinstance(value, str) or not value.strip():
        raise CatalogValidationError(f"{where}: 料金値が不正です: {value!r}")
    try:
        return Decimal(value.strip())
    except InvalidOperation as exc:
        raise CatalogValidationError(f"{where}: 料金値を解釈できません: {value!r}") from exc


def _parse_rates(raw: Any, *, where: str) -> Dict[str, Decimal]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise CatalogValidationError(f"{where}: rates は object である必要があります")
    rates: Dict[str, Decimal] = {}
    for key, value in raw.items():
        if key not in RATE_KEYS:
            raise CatalogValidationError(f"{where}: 未知の rate キー: {key!r}")
        rates[key] = _parse_decimal(value, where=f"{where}.{key}")
    return rates


def _parse_tool_rates(raw: Any, *, where: str) -> Dict[str, Decimal]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise CatalogValidationError(f"{where}: tool_rates は object である必要があります")
    return {
        str(key): _parse_decimal(value, where=f"{where}.{key}")
        for key, value in raw.items()
    }


def _parse_long_context(raw: Any, *, where: str) -> Optional[dict]:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise CatalogValidationError(f"{where}: long_context は object である必要があります")
    if "threshold_tokens" not in raw:
        raise CatalogValidationError(f"{where}: long_context.threshold_tokens が必要です")
    threshold = raw["threshold_tokens"]
    if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold <= 0:
        raise CatalogValidationError(f"{where}: threshold_tokens は正の整数である必要があります")
    return {
        "threshold_tokens": threshold,
        "input_multiplier": _parse_decimal(
            raw.get("input_multiplier", "1"), where=f"{where}.input_multiplier"
        ),
        "output_multiplier": _parse_decimal(
            raw.get("output_multiplier", "1"), where=f"{where}.output_multiplier"
        ),
    }


def _parse_tiers(raw: Any, *, where: str) -> Tuple[dict, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or not raw:
        raise CatalogValidationError(f"{where}: tiers は空でない配列である必要があります")
    tiers: List[dict] = []
    previous_max: Optional[int] = None
    for index, entry in enumerate(raw):
        loc = f"{where}.tiers[{index}]"
        if not isinstance(entry, Mapping):
            raise CatalogValidationError(f"{loc}: object である必要があります")
        max_tokens = entry.get("max_input_tokens", None)
        if max_tokens is not None:
            if isinstance(max_tokens, bool) or not isinstance(max_tokens, int):
                raise CatalogValidationError(f"{loc}: max_input_tokens は整数か null です")
            if max_tokens <= 0:
                raise CatalogValidationError(f"{loc}: max_input_tokens は正の整数です")
            if previous_max is not None and max_tokens <= previous_max:
                raise CatalogValidationError(f"{loc}: max_input_tokens は昇順である必要があります")
            previous_max = max_tokens
        elif index != len(raw) - 1:
            raise CatalogValidationError(f"{loc}: max_input_tokens=null は最終段のみ許可されます")
        rates = _parse_rates(entry.get("rates"), where=loc)
        if "input" not in rates or "output" not in rates:
            raise CatalogValidationError(f"{loc}: rates に input と output が必要です")
        tiers.append({"max_input_tokens": max_tokens, "rates": rates})
    if tiers[-1]["max_input_tokens"] is not None:
        raise CatalogValidationError(f"{where}: 最終段の max_input_tokens は null にしてください")
    return tuple(tiers)


# ────────────────────────────────────────────
# 検証・ロード
# ────────────────────────────────────────────


def validate_catalog(doc: Mapping[str, Any]) -> None:
    """カタログ document を検証する。不正なら CatalogValidationError。"""
    if not isinstance(doc, Mapping):
        raise CatalogValidationError("カタログはオブジェクトである必要があります")

    version = doc.get("catalog_version")
    if not isinstance(version, str) or not version.strip():
        raise CatalogValidationError("catalog_version が必要です")

    schema_version = doc.get("schema_version")
    if schema_version != 1:
        raise CatalogValidationError(f"schema_version は 1 のみ対応です: {schema_version!r}")

    if "generated_at" in doc and doc["generated_at"] is not None:
        _parse_dt(doc["generated_at"], where="generated_at")

    groups = doc.get("free_incentive_groups") or {}
    if not isinstance(groups, Mapping):
        raise CatalogValidationError("free_incentive_groups は object である必要があります")
    for group_name, members in groups.items():
        if group_name not in ("1m", "10m"):
            raise CatalogValidationError(f"未知の free_incentive_group: {group_name!r}")
        if not isinstance(members, Sequence) or isinstance(members, (str, bytes)):
            raise CatalogValidationError(
                f"free_incentive_groups.{group_name} は配列である必要があります"
            )
        for member in members:
            if not isinstance(member, str) or not member.strip():
                raise CatalogValidationError(
                    f"free_incentive_groups.{group_name} に空の要素があります"
                )

    rules = doc.get("rules")
    if not isinstance(rules, Sequence) or isinstance(rules, (str, bytes)) or not rules:
        raise CatalogValidationError("rules は空でない配列である必要があります")

    seen_keys: set = set()
    seen_aliases: set = set()
    for index, rule in enumerate(rules):
        where = f"rules[{index}]"
        if not isinstance(rule, Mapping):
            raise CatalogValidationError(f"{where}: object である必要があります")
        for key in ("rule_id", "provider", "canonical_model", "pricing_kind"):
            value = rule.get(key)
            if not isinstance(value, str) or not value.strip():
                raise CatalogValidationError(f"{where}: {key} が必要です")

        kind = rule["pricing_kind"]
        if kind not in PRICING_KINDS:
            raise CatalogValidationError(f"{where}: 未知の pricing_kind: {kind!r}")

        effective_from = _parse_dt(rule.get("effective_from"), where=f"{where}.effective_from")
        effective_to = (
            _parse_dt(rule["effective_to"], where=f"{where}.effective_to")
            if rule.get("effective_to") is not None
            else None
        )
        if effective_to is not None and effective_to <= effective_from:
            raise CatalogValidationError(f"{where}: effective_to は effective_from より後です")

        provider = rule["provider"].strip().lower()
        canonical = rule["canonical_model"].strip().lower()
        unique_key = (provider, canonical, effective_from)
        if unique_key in seen_keys:
            raise CatalogValidationError(
                f"{where}: (provider, canonical_model, effective_from) が重複しています: {unique_key}"
            )
        seen_keys.add(unique_key)

        aliases = rule.get("aliases") or []
        if not isinstance(aliases, Sequence) or isinstance(aliases, (str, bytes)):
            raise CatalogValidationError(f"{where}: aliases は配列である必要があります")
        for alias in aliases:
            if not isinstance(alias, str) or not alias.strip():
                raise CatalogValidationError(f"{where}: aliases に空の要素があります")
            alias_key = (provider, alias.strip().lower(), effective_from)
            if alias_key in seen_aliases or alias_key in seen_keys:
                raise CatalogValidationError(f"{where}: alias が重複しています: {alias!r}")
            seen_aliases.add(alias_key)

        if kind == "flat_token":
            rates = _parse_rates(rule.get("rates"), where=where)
            if "input" not in rates or "output" not in rates:
                raise CatalogValidationError(f"{where}: rates に input と output が必要です")
        elif kind == "tiered_token":
            _parse_tiers(rule.get("tiers"), where=where)
        else:
            # provider_reported / subscription / local は rates 不要
            _parse_rates(rule.get("rates"), where=where)

        _parse_long_context(rule.get("long_context"), where=where)
        _parse_tool_rates(rule.get("tool_rates"), where=where)


def build_snapshots(doc: Mapping[str, Any]) -> Tuple[PricingRuleSnapshot, ...]:
    """検証済み document から PricingRuleSnapshot 群を組み立てる。"""
    validate_catalog(doc)
    catalog_version = str(doc["catalog_version"]).strip()
    snapshots: List[PricingRuleSnapshot] = []
    for index, rule in enumerate(doc["rules"]):
        where = f"rules[{index}]"
        kind = rule["pricing_kind"]
        rates = _parse_rates(rule.get("rates"), where=where)
        tiers = (
            _parse_tiers(rule.get("tiers"), where=where)
            if kind == "tiered_token"
            else tuple()
        )
        snapshots.append(
            PricingRuleSnapshot(
                rule_id=str(rule["rule_id"]).strip(),
                provider=str(rule["provider"]).strip().lower(),
                canonical_model=str(rule["canonical_model"]).strip().lower(),
                aliases=tuple(
                    sorted({str(a).strip().lower() for a in (rule.get("aliases") or [])})
                ),
                pricing_kind=kind,
                rates=rates if kind != "tiered_token" else {},
                tiers=tiers,
                long_context=_parse_long_context(rule.get("long_context"), where=where),
                tool_rates=_parse_tool_rates(rule.get("tool_rates"), where=where),
                effective_from=_parse_dt(rule["effective_from"], where=where),
                effective_to=(
                    _parse_dt(rule["effective_to"], where=where)
                    if rule.get("effective_to") is not None
                    else None
                ),
                source=str(rule.get("source") or ""),
                catalog_version=str(rule.get("catalog_version") or catalog_version),
            )
        )
    return tuple(snapshots)


def load_catalog_document(doc: Mapping[str, Any]) -> CatalogDocument:
    """メモリ上の dict から CatalogDocument を構築する。"""
    snapshots = build_snapshots(doc)
    groups_raw = doc.get("free_incentive_groups") or {}
    groups = {
        str(name): tuple(str(m).strip().lower() for m in members)
        for name, members in groups_raw.items()
    }
    return CatalogDocument(
        catalog_version=str(doc["catalog_version"]).strip(),
        schema_version=int(doc["schema_version"]),
        generated_at=(
            _parse_dt(doc["generated_at"], where="generated_at")
            if doc.get("generated_at")
            else None
        ),
        free_incentive_groups=groups,
        rules=snapshots,
        raw=dict(doc),
    )


def load_catalog_file(path: "str | Path | None" = None) -> CatalogDocument:
    """カタログ JSON をファイルから読み込む。"""
    target = resolve_catalog_path(path)
    if not target.exists():
        raise CatalogValidationError(f"料金カタログが見つかりません: {target}")
    with target.open("r", encoding="utf-8") as fp:
        raw = json.load(fp)
    return load_catalog_document(raw)


# ────────────────────────────────────────────
# 差分計算（純粋関数）
# ────────────────────────────────────────────


def diff_catalog(
    current: Sequence[PricingRuleSnapshot],
    incoming: Sequence[PricingRuleSnapshot],
) -> dict:
    """既存ルール群と取り込みルール群の差分を求める。

    - 同一 `(provider, canonical_model, effective_from)` かつ内容一致 → ``unchanged``
    - 同一キーで内容差異 → ``updated``
    - 新しい `effective_from` → ``inserted``。直前の有効行は ``closed``（過去 rate は不変）
    - カタログから消えたモデル → ``deactivated``（**削除しない**）
    """
    current_by_key = {snap.unique_key: snap for snap in current}
    incoming_by_key = {snap.unique_key: snap for snap in incoming}

    inserted: List[PricingRuleSnapshot] = []
    updated: List[PricingRuleSnapshot] = []
    unchanged: List[PricingRuleSnapshot] = []
    closed: List[dict] = []

    current_by_scope: Dict[Tuple[str, str], List[PricingRuleSnapshot]] = {}
    for snap in current:
        current_by_scope.setdefault(snap.scope_key, []).append(snap)
    for bucket in current_by_scope.values():
        bucket.sort(key=lambda s: s.effective_from)

    for key, snap in incoming_by_key.items():
        existing = current_by_key.get(key)
        if existing is None:
            inserted.append(snap)
            # 同一 scope の直前有効行を閉じる（過去 rate は書き換えない）
            predecessor = None
            for candidate in current_by_scope.get(snap.scope_key, []):
                if candidate.effective_from >= snap.effective_from:
                    continue
                if (
                    candidate.effective_to is not None
                    and candidate.effective_to <= snap.effective_from
                ):
                    continue
                if predecessor is None or candidate.effective_from > predecessor.effective_from:
                    predecessor = candidate
            if predecessor is not None:
                closed.append(
                    {
                        "rule_id": predecessor.rule_id,
                        "provider": predecessor.provider,
                        "canonical_model": predecessor.canonical_model,
                        "effective_from": predecessor.effective_from,
                        "new_effective_to": snap.effective_from,
                    }
                )
        elif existing.content_signature() == snap.content_signature():
            unchanged.append(snap)
        else:
            updated.append(snap)

    incoming_scopes = {snap.scope_key for snap in incoming}
    deactivated = [
        snap for snap in current if snap.scope_key not in incoming_scopes
    ]

    return {
        "inserted": inserted,
        "updated": updated,
        "unchanged": unchanged,
        "closed": closed,
        # カタログから消えたモデルは is_active=false にするだけで、行は残す
        "deactivated": deactivated,
        "deleted": [],
        "counts": {
            "inserted": len(inserted),
            "updated": len(updated),
            "unchanged": len(unchanged),
            "closed": len(closed),
            "deactivated": len(deactivated),
            "deleted": 0,
        },
    }


# ────────────────────────────────────────────
# DB 同期
# ────────────────────────────────────────────


def _decimal_or_none(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def snapshot_from_row(row: Any, aliases: Sequence[str] = ()) -> PricingRuleSnapshot:
    """`pricing_rules` の1行を PricingRuleSnapshot へ変換する。"""
    rates: Dict[str, Decimal] = {}
    for key, column in (
        ("input", row.input_price_per_1m),
        ("cached_input", row.cached_input_price_per_1m),
        ("output", row.output_price_per_1m),
        ("cache_write", row.cache_write_price_per_1m),
    ):
        value = _decimal_or_none(column)
        if value is not None:
            rates[key] = value

    tiers: List[dict] = []
    for entry in (row.tiers or []):
        tiers.append(
            {
                "max_input_tokens": entry.get("max_input_tokens"),
                "rates": {
                    k: Decimal(str(v)) for k, v in (entry.get("rates") or {}).items()
                },
            }
        )

    long_context = None
    if row.long_context_threshold:
        long_context = {
            "threshold_tokens": int(row.long_context_threshold),
            "input_multiplier": _decimal_or_none(row.long_context_input_multiplier)
            or Decimal("1"),
            "output_multiplier": _decimal_or_none(row.long_context_output_multiplier)
            or Decimal("1"),
        }

    return PricingRuleSnapshot(
        rule_id=row.rule_id,
        provider=row.provider,
        canonical_model=row.canonical_model,
        aliases=tuple(sorted(set(aliases))),
        pricing_kind=row.pricing_kind,
        rates=rates if row.pricing_kind != "tiered_token" else {},
        tiers=tuple(tiers),
        long_context=long_context,
        tool_rates={
            k: Decimal(str(v)) for k, v in (row.tool_rates or {}).items()
        },
        effective_from=row.effective_from,
        effective_to=row.effective_to,
        source=row.source or "",
        catalog_version=row.catalog_version or "",
    )


def _apply_snapshot_to_row(row: Any, snap: PricingRuleSnapshot) -> None:
    row.rule_id = snap.rule_id
    row.pricing_kind = snap.pricing_kind
    # NUMERIC(18,8) へ入る値だけを書く（読み戻しと一致させ、無限 updated を防ぐ）
    row.input_price_per_1m = quantize_to_storage(snap.rates.get("input"), RATE_SCALE)
    row.cached_input_price_per_1m = quantize_to_storage(
        snap.rates.get("cached_input"), RATE_SCALE
    )
    row.cache_write_price_per_1m = quantize_to_storage(
        snap.rates.get("cache_write"), RATE_SCALE
    )
    row.output_price_per_1m = quantize_to_storage(snap.rates.get("output"), RATE_SCALE)
    if snap.long_context:
        row.long_context_threshold = int(snap.long_context["threshold_tokens"])
        row.long_context_input_multiplier = quantize_to_storage(
            snap.long_context["input_multiplier"], MULTIPLIER_SCALE
        )
        row.long_context_output_multiplier = quantize_to_storage(
            snap.long_context["output_multiplier"], MULTIPLIER_SCALE
        )
    else:
        row.long_context_threshold = None
        row.long_context_input_multiplier = None
        row.long_context_output_multiplier = None
    row.tiers = [
        {
            "max_input_tokens": t.get("max_input_tokens"),
            "rates": {k: str(v) for k, v in (t.get("rates") or {}).items()},
        }
        for t in snap.tiers
    ] or None
    row.tool_rates = {k: str(v) for k, v in snap.tool_rates.items()} or None
    row.source = snap.source or None
    row.catalog_version = snap.catalog_version
    row.is_active = True
    row.updated_at = datetime.utcnow()


async def _load_current_rows_and_snapshots(
    session,
) -> Tuple[List[Any], List[PricingRuleSnapshot]]:
    """同一トランザクションの ORM 行とスナップショットを一度だけ読む。

    ``apply_snapshots`` は差分計算後に ORM 行を更新する必要があるため、
    スナップショットだけでなく元の行も保持する。呼び出し側が同じ
    ``AsyncSession`` の中で再利用すれば、同じ ``pricing_rules`` を再度
    SELECT して SQLAlchemy identity map を作り直す必要がない。
    """
    from sqlalchemy import select

    from ...models.ecc_models import PricingModelAlias, PricingRule

    rows = (await session.execute(select(PricingRule))).scalars().all()
    alias_rows = (await session.execute(select(PricingModelAlias))).scalars().all()
    alias_map: Dict[Tuple[str, str, Any], List[str]] = {}
    for alias in alias_rows:
        alias_map.setdefault(
            (alias.provider, alias.canonical_model, alias.effective_from), []
        ).append(alias.alias)
    snapshots = [
        snapshot_from_row(
            row,
            alias_map.get((row.provider, row.canonical_model, row.effective_from), []),
        )
        for row in rows
    ]
    return rows, snapshots


async def _load_current_snapshots(session) -> List[PricingRuleSnapshot]:
    """`pricing_rules` の現行スナップショットを読み込む。"""

    _rows, snapshots = await _load_current_rows_and_snapshots(session)
    return snapshots


async def apply_snapshots(
    session,
    snapshots: Sequence[PricingRuleSnapshot],
    *,
    deactivate_missing: bool = True,
    restrict_to_providers: Optional[set] = None,
) -> dict:
    """スナップショット群を `pricing_rules` / `pricing_model_aliases` へ冪等に反映する。

    `restrict_to_providers` を渡すと、その provider の行だけを無効化対象にする
    （部分取り込みで無関係な provider を巻き込まないため）。
    """
    from sqlalchemy import select

    from ...models.ecc_models import PricingModelAlias, PricingRule

    # 行とスナップショットを同じ SELECT 結果から再利用する。行の更新・
    # flush・alias 同期は従来どおりこの session/transaction 内で行うため、
    # 差分意味・identity map・rollback 契約を変えず重複 SELECT だけを除く。
    rows, current = await _load_current_rows_and_snapshots(session)
    diff = diff_catalog(current, snapshots)

    row_by_key = {
        (r.provider, r.canonical_model, r.effective_from): r for r in rows
    }

    inserted = updated = unchanged = closed = 0

    for snap in diff["unchanged"]:
        unchanged += 1

    for snap in diff["updated"]:
        row = row_by_key.get(snap.unique_key)
        if row is None:
            continue
        _apply_snapshot_to_row(row, snap)
        updated += 1

    for entry in diff["closed"]:
        row = row_by_key.get(
            (entry["provider"], entry["canonical_model"], entry["effective_from"])
        )
        if row is None:
            continue
        row.effective_to = entry["new_effective_to"]
        row.updated_at = datetime.utcnow()
        closed += 1

    for snap in diff["inserted"]:
        row = PricingRule(
            id=uuid.uuid4(),
            provider=snap.provider,
            canonical_model=snap.canonical_model,
            effective_from=snap.effective_from,
            effective_to=snap.effective_to,
            currency="USD",
            created_at=datetime.utcnow(),
        )
        _apply_snapshot_to_row(row, snap)
        session.add(row)
        row_by_key[snap.unique_key] = row
        inserted += 1

    deactivated = 0
    if deactivate_missing:
        incoming_scopes = {s.scope_key for s in snapshots}
        for row in rows:
            if restrict_to_providers is not None and row.provider not in restrict_to_providers:
                continue
            if (row.provider, row.canonical_model) in incoming_scopes:
                if not row.is_active:
                    row.is_active = True
                continue
            # 履歴計算に必要なので削除はしない
            if row.is_active:
                row.is_active = False
                deactivated += 1

    await session.flush()

    # alias の同期
    alias_rows = (await session.execute(select(PricingModelAlias))).scalars().all()
    alias_by_key = {
        (a.provider, a.alias, a.effective_from): a for a in alias_rows
    }
    desired: Dict[Tuple[str, str, Any], PricingRuleSnapshot] = {}
    for snap in snapshots:
        for alias in snap.aliases:
            desired[(snap.provider, alias, snap.effective_from)] = snap

    alias_inserted = 0
    alias_removed = 0
    for key, snap in desired.items():
        if key in alias_by_key:
            existing = alias_by_key[key]
            existing.canonical_model = snap.canonical_model
            existing.effective_to = snap.effective_to
            continue
        rule_row = row_by_key.get(snap.unique_key)
        session.add(
            PricingModelAlias(
                id=uuid.uuid4(),
                rule_uuid=getattr(rule_row, "id", None),
                provider=snap.provider,
                alias=key[1],
                canonical_model=snap.canonical_model,
                effective_from=snap.effective_from,
                effective_to=snap.effective_to,
                created_at=datetime.utcnow(),
            )
        )
        alias_inserted += 1

    managed_providers = (
        restrict_to_providers
        if restrict_to_providers is not None
        else {s.provider for s in snapshots}
    )
    for key, alias_row in alias_by_key.items():
        if key in desired:
            continue
        if alias_row.provider not in managed_providers:
            # 今回の取り込み対象外プロバイダの alias には触らない
            continue
        await session.delete(alias_row)
        alias_removed += 1

    return {
        "inserted": inserted,
        "updated": updated,
        "unchanged": unchanged,
        "closed": closed,
        "alias_inserted": alias_inserted,
        "alias_removed": alias_removed,
        "deactivated": deactivated,
        "diff": diff,
    }


async def record_catalog_state(
    session,
    *,
    source_key: str,
    catalog_version: Optional[str],
    rule_count: int,
    status: str,
    error: Optional[str] = None,
    payload_digest: Optional[str] = None,
    success: bool = False,
) -> None:
    """`pricing_catalog_state` を更新する。"""
    from sqlalchemy import select

    from ...models.ecc_models import PricingCatalogState

    now = datetime.utcnow()
    row = (
        await session.execute(
            select(PricingCatalogState).where(
                PricingCatalogState.source_key == source_key
            )
        )
    ).scalars().first()
    if row is None:
        row = PricingCatalogState(source_key=source_key, rule_count=0)
        session.add(row)
    row.last_attempt_at = now
    row.last_status = status
    row.last_error = error
    if success:
        row.last_success_at = now
        row.catalog_version = catalog_version
        row.rule_count = rule_count
        if payload_digest:
            row.payload_digest = payload_digest
    row.updated_at = now


async def sync_catalog_to_db(
    session_factory=None,
    *,
    path: "str | Path | None" = None,
    source_key: str = "catalog_file",
) -> dict:
    """`config/pricing_catalog.json` を DB へ冪等に同期する。"""
    result = {
        "catalog_version": None,
        "inserted": 0,
        "updated": 0,
        "unchanged": 0,
        "closed": 0,
        "alias_inserted": 0,
        "alias_removed": 0,
        "status": "ok",
        "error": None,
    }
    try:
        document = load_catalog_file(path)
    except Exception as exc:  # カタログが壊れているときは既存料金を触らない
        logger.exception("料金カタログの読み込みに失敗しました")
        result["status"] = "error"
        result["error"] = str(exc)
        return result

    result["catalog_version"] = document.catalog_version

    if session_factory is None:
        from ...memory.database import get_db_session

        session_factory = get_db_session

    # openrouter の実レートは updater が Models API から入れるため、
    # カタログファイル（`*` プレースホルダのみ）で無効化・alias 削除をしてはいけない。
    from .providers import PROVIDER_REPORTED_PROVIDERS

    managed_providers = {
        rule.provider for rule in document.rules
    } - set(PROVIDER_REPORTED_PROVIDERS)

    try:
        async with await session_factory() as session:
            applied = await apply_snapshots(
                session, document.rules, restrict_to_providers=managed_providers
            )
            await record_catalog_state(
                session,
                source_key=source_key,
                catalog_version=document.catalog_version,
                rule_count=document.rule_count,
                status="ok",
                payload_digest=document.digest(),
                success=True,
            )
            await session.commit()
        for key in ("inserted", "updated", "unchanged", "closed", "alias_inserted", "alias_removed"):
            result[key] = applied[key]
    except Exception as exc:
        logger.exception("料金カタログの DB 同期に失敗しました")
        result["status"] = "error"
        result["error"] = str(exc)
        return result

    try:
        from .engine import get_pricing_engine

        get_pricing_engine().invalidate()
    except Exception:  # pragma: no cover - キャッシュ破棄の失敗は致命的でない
        logger.warning("料金エンジンのキャッシュ破棄に失敗しました", exc_info=True)

    return result
