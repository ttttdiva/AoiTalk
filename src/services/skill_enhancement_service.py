"""スキル拡張管理サービス

カテゴリ・プリセット・チェーン・インポート/エクスポートなど、
スキルシステムの拡張機能を提供する。

DB モデル: SkillCategory, SkillPreset, SkillChain (src/models/ecc_models.py)
スキル定義: SkillDefinition (src/skills/models.py)
レジストリ: SkillRegistry (src/skills/registry.py)
ローダー: loader.py (config/skills/*.yaml の読み書き)
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select, delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.database import get_db_session
from ..models.ecc_models import SkillCategory, SkillChain, SkillPreset
from ..skills.loader import SKILLS_DIR, save_skill_to_yaml
from ..skills.models import SkillDefinition, SkillTriggerMode
from ..skills.registry import get_skill_registry, register_skill

logger = logging.getLogger(__name__)


class SkillEnhancementService:
    """スキルシステムの拡張管理サービス

    カテゴリ CRUD、プリセットライブラリ管理、チェーン実行、
    スキルのインポート/エクスポートを非同期で提供する。
    """

    # ────────────────────────────────────────────
    # カテゴリ CRUD
    # ────────────────────────────────────────────

    async def list_categories(self) -> List[Dict[str, Any]]:
        """全スキルカテゴリを取得する。

        Returns:
            カテゴリ辞書のリスト（sort_order, name 昇順）。
        """
        async with await get_db_session() as session:  # type: AsyncSession
            stmt = select(SkillCategory).order_by(
                SkillCategory.sort_order, SkillCategory.name
            )
            result = await session.execute(stmt)
            categories = result.scalars().all()
            logger.debug("スキルカテゴリを取得: %d 件", len(categories))
            return [c.to_dict() for c in categories]

    async def create_category(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """スキルカテゴリを新規作成する。

        Args:
            data: カテゴリ定義。name, display_name は必須。

        Returns:
            作成されたカテゴリの辞書表現。

        Raises:
            ValueError: 必須項目が不足、または名前が重複している場合。
        """
        name = data.get("name", "").strip()
        display_name = data.get("display_name", "").strip()
        if not name or not display_name:
            raise ValueError("name と display_name は必須です")

        async with await get_db_session() as session:  # type: AsyncSession
            # 重複チェック
            existing = (
                await session.execute(
                    select(SkillCategory).where(SkillCategory.name == name)
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise ValueError(f"カテゴリ名 '{name}' は既に使用されています")

            category = SkillCategory(
                id=uuid.uuid4(),
                name=name,
                display_name=display_name,
                description=data.get("description", ""),
                icon=data.get("icon", ""),
                color=data.get("color", ""),
                sort_order=data.get("sort_order", 0),
            )
            session.add(category)
            await session.commit()
            await session.refresh(category)

            logger.info("スキルカテゴリを作成しました: %s", name)
            return category.to_dict()

    async def delete_category(self, cat_id: str) -> bool:
        """スキルカテゴリを削除する。

        Args:
            cat_id: カテゴリの UUID 文字列。

        Returns:
            削除に成功した場合 True。

        Raises:
            ValueError: UUID が無効、またはカテゴリが見つからない場合。
        """
        uid = _parse_uuid_strict(cat_id)

        async with await get_db_session() as session:  # type: AsyncSession
            category = await session.get(SkillCategory, uid)
            if category is None:
                raise ValueError(f"カテゴリが見つかりません: {cat_id}")

            cat_name = category.name
            await session.execute(
                sa_delete(SkillCategory).where(SkillCategory.id == uid)
            )
            await session.commit()

            logger.info("スキルカテゴリを削除しました: %s (%s)", cat_name, uid)
            return True

    # ────────────────────────────────────────────
    # プリセットライブラリ
    # ────────────────────────────────────────────

    async def list_presets(self, category: Optional[str] = None) -> List[Dict[str, Any]]:
        """プリセットスキル一覧を取得する。

        Args:
            category: カテゴリ名でフィルタ（None の場合は全件）。

        Returns:
            プリセット辞書のリスト。
        """
        async with await get_db_session() as session:  # type: AsyncSession
            stmt = select(SkillPreset).order_by(SkillPreset.name)
            if category:
                stmt = stmt.where(SkillPreset.category == category)

            result = await session.execute(stmt)
            presets = result.scalars().all()
            logger.debug("プリセット一覧を取得: %d 件", len(presets))
            return [p.to_dict() for p in presets]

    async def install_preset(self, preset_id: str) -> Dict[str, Any]:
        """プリセットをインストールする。

        プリセット定義を config/skills/ に YAML として書き出し、
        SkillRegistry に登録する。install_count をインクリメントする。

        Args:
            preset_id: プリセットの UUID 文字列。

        Returns:
            インストールされたスキルの辞書表現。

        Raises:
            ValueError: UUID が無効、またはプリセットが見つからない場合。
        """
        uid = _parse_uuid_strict(preset_id)

        async with await get_db_session() as session:  # type: AsyncSession
            preset = await session.get(SkillPreset, uid)
            if preset is None:
                raise ValueError(f"プリセットが見つかりません: {preset_id}")

            # SkillDefinition に変換
            try:
                trigger_mode = SkillTriggerMode(preset.trigger_mode)
            except ValueError:
                trigger_mode = SkillTriggerMode.BOTH

            skill = SkillDefinition(
                name=preset.name,
                description=preset.description or "",
                prompt_template=preset.prompt_template,
                trigger_mode=trigger_mode,
                aliases=preset.aliases or [],
                bound_tools=preset.bound_tools or [],
                examples=preset.examples or [],
                tags=preset.tags or [],
                parameters=preset.parameters or {},
            )

            # YAML として保存
            saved = save_skill_to_yaml(skill)
            if not saved:
                raise RuntimeError(f"スキル '{preset.name}' の YAML 保存に失敗しました")

            # レジストリに登録
            register_skill(skill)

            # install_count をインクリメント
            preset.install_count = (preset.install_count or 0) + 1
            await session.commit()

            logger.info(
                "プリセットをインストールしました: %s (install_count=%d)",
                preset.name,
                preset.install_count,
            )
            return skill.to_dict()

    # ────────────────────────────────────────────
    # チェーン CRUD
    # ────────────────────────────────────────────

    async def create_chain(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """スキルチェーンを新規作成する。

        Args:
            data: チェーン定義。name, display_name, steps は必須。

        Returns:
            作成されたチェーンの辞書表現。

        Raises:
            ValueError: 必須項目が不足、または名前が重複している場合。
        """
        name = data.get("name", "").strip()
        display_name = data.get("display_name", "").strip()
        steps = data.get("steps")
        if not name or not display_name:
            raise ValueError("name と display_name は必須です")
        if not steps or not isinstance(steps, list):
            raise ValueError("steps は空でないリストである必要があります")

        async with await get_db_session() as session:  # type: AsyncSession
            existing = (
                await session.execute(
                    select(SkillChain).where(SkillChain.name == name)
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise ValueError(f"チェーン名 '{name}' は既に使用されています")

            chain = SkillChain(
                id=uuid.uuid4(),
                name=name,
                display_name=display_name,
                description=data.get("description", ""),
                steps=steps,
                created_by=_parse_uuid(data.get("created_by")),
            )
            session.add(chain)
            await session.commit()
            await session.refresh(chain)

            logger.info("スキルチェーンを作成しました: %s", name)
            return chain.to_dict()

    async def list_chains(self) -> List[Dict[str, Any]]:
        """全スキルチェーンを取得する。

        Returns:
            チェーン辞書のリスト。
        """
        async with await get_db_session() as session:  # type: AsyncSession
            stmt = select(SkillChain).order_by(SkillChain.name)
            result = await session.execute(stmt)
            chains = result.scalars().all()
            logger.debug("スキルチェーン一覧を取得: %d 件", len(chains))
            return [c.to_dict() for c in chains]

    async def update_chain(self, chain_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """スキルチェーンを更新する。

        Args:
            chain_id: チェーンの UUID 文字列。
            data: 更新するフィールドの辞書。

        Returns:
            更新後のチェーン辞書表現。

        Raises:
            ValueError: UUID が無効、またはチェーンが見つからない場合。
        """
        uid = _parse_uuid_strict(chain_id)

        async with await get_db_session() as session:  # type: AsyncSession
            chain = await session.get(SkillChain, uid)
            if chain is None:
                raise ValueError(f"チェーンが見つかりません: {chain_id}")

            # 名前変更時の重複チェック
            new_name = data.get("name")
            if new_name and new_name != chain.name:
                dup = (
                    await session.execute(
                        select(SkillChain).where(SkillChain.name == new_name)
                    )
                ).scalar_one_or_none()
                if dup is not None:
                    raise ValueError(f"チェーン名 '{new_name}' は既に使用されています")

            updatable = {"name", "display_name", "description", "steps"}
            for key in updatable:
                if key in data:
                    setattr(chain, key, data[key])

            chain.updated_at = datetime.utcnow()
            await session.commit()
            await session.refresh(chain)

            logger.info("スキルチェーンを更新しました: %s (%s)", chain.name, uid)
            return chain.to_dict()

    async def delete_chain(self, chain_id: str) -> bool:
        """スキルチェーンを削除する。

        Args:
            chain_id: チェーンの UUID 文字列。

        Returns:
            削除に成功した場合 True。

        Raises:
            ValueError: UUID が無効、またはチェーンが見つからない場合。
        """
        uid = _parse_uuid_strict(chain_id)

        async with await get_db_session() as session:  # type: AsyncSession
            chain = await session.get(SkillChain, uid)
            if chain is None:
                raise ValueError(f"チェーンが見つかりません: {chain_id}")

            chain_name = chain.name
            await session.execute(
                sa_delete(SkillChain).where(SkillChain.id == uid)
            )
            await session.commit()

            logger.info("スキルチェーンを削除しました: %s (%s)", chain_name, uid)
            return True

    # ────────────────────────────────────────────
    # チェーン実行
    # ────────────────────────────────────────────

    async def execute_chain(
        self,
        chain_id: str,
        initial_input: Optional[str] = None,
    ) -> Dict[str, Any]:
        """スキルチェーンを順次実行する。

        各ステップの出力を次のステップの入力として渡す。
        ステップの on_error が "skip" の場合はエラーを無視して続行、
        "abort"（デフォルト）の場合はチェーンを中断する。

        Args:
            chain_id: チェーンの UUID 文字列。
            initial_input: 最初のステップへの入力テキスト。

        Returns:
            実行結果を含む辞書:
                - chain_name: チェーン名
                - steps_executed: 実行済みステップ数
                - steps_total: 総ステップ数
                - final_output: 最終出力
                - step_results: 各ステップの結果リスト
                - success: 全ステップが成功したか
        """
        uid = _parse_uuid_strict(chain_id)

        async with await get_db_session() as session:  # type: AsyncSession
            chain = await session.get(SkillChain, uid)
            if chain is None:
                raise ValueError(f"チェーンが見つかりません: {chain_id}")

        steps = chain.steps or []
        registry = get_skill_registry()

        current_input = initial_input or ""
        step_results: List[Dict[str, Any]] = []
        success = True

        for i, step in enumerate(steps):
            skill_name = step.get("skill_name", "")
            on_error = step.get("on_error", "abort")
            input_mapping = step.get("input_mapping", {})

            skill = registry.get(skill_name) or registry.get_by_alias(skill_name)
            if skill is None:
                error_msg = f"ステップ {i + 1}: スキル '{skill_name}' が見つかりません"
                logger.error(error_msg)
                step_results.append({
                    "step": i + 1,
                    "skill_name": skill_name,
                    "status": "error",
                    "error": error_msg,
                })
                if on_error == "skip":
                    continue
                success = False
                break

            # 入力マッピングの解決
            render_kwargs = {}
            for param_key, mapping_value in input_mapping.items():
                if isinstance(mapping_value, str) and mapping_value == "$prev.output":
                    render_kwargs[param_key] = current_input
                else:
                    render_kwargs[param_key] = mapping_value

            try:
                rendered = skill.render_prompt(current_input, **render_kwargs)
                step_results.append({
                    "step": i + 1,
                    "skill_name": skill_name,
                    "status": "completed",
                    "output": rendered,
                })
                current_input = rendered
                logger.debug(
                    "チェーン '%s' ステップ %d/%d (%s) を実行しました",
                    chain.name, i + 1, len(steps), skill_name,
                )
            except Exception as e:
                error_msg = f"ステップ {i + 1} ({skill_name}) の実行に失敗: {e}"
                logger.error(error_msg)
                step_results.append({
                    "step": i + 1,
                    "skill_name": skill_name,
                    "status": "error",
                    "error": str(e),
                })
                if on_error == "skip":
                    continue
                success = False
                break

        result = {
            "chain_name": chain.name,
            "steps_executed": len(step_results),
            "steps_total": len(steps),
            "final_output": current_input,
            "step_results": step_results,
            "success": success,
        }
        logger.info(
            "チェーン '%s' の実行完了: %d/%d ステップ, success=%s",
            chain.name, len(step_results), len(steps), success,
        )
        return result

    # ────────────────────────────────────────────
    # インポート / エクスポート
    # ────────────────────────────────────────────

    async def export_skills(self) -> List[Dict[str, Any]]:
        """レジストリ内の全スキルをエクスポート用辞書リストとして返す。

        Returns:
            各スキルの to_dict() 出力のリスト。
        """
        registry = get_skill_registry()
        skills = registry.get_all()
        exported = [s.to_dict() for s in skills]
        logger.info("スキルをエクスポートしました: %d 件", len(exported))
        return exported

    async def import_skills(self, data: List[Dict[str, Any]]) -> Dict[str, Any]:
        """スキル定義のリストをインポートする。

        各定義を config/skills/ に YAML として保存し、SkillRegistry に登録する。
        既存の同名スキルは上書きされる。

        Args:
            data: スキル定義の辞書リスト。各辞書には最低限 name と prompt_template が必要。

        Returns:
            インポート結果（imported, skipped, errors の各カウント）。
        """
        imported = 0
        skipped = 0
        errors = 0

        for item in data:
            name = item.get("name", "").strip()
            prompt_template = item.get("prompt_template", "").strip()
            if not name or not prompt_template:
                logger.warning("スキル定義をスキップ（name または prompt_template が空）: %s", item)
                skipped += 1
                continue

            try:
                trigger_str = item.get("trigger_mode", "both")
                try:
                    trigger_mode = SkillTriggerMode(trigger_str)
                except ValueError:
                    trigger_mode = SkillTriggerMode.BOTH

                skill = SkillDefinition(
                    name=name,
                    description=item.get("description", ""),
                    prompt_template=prompt_template,
                    trigger_mode=trigger_mode,
                    aliases=item.get("aliases", []),
                    bound_tools=item.get("bound_tools", []),
                    examples=item.get("examples", []),
                    tags=item.get("tags", []),
                    parameters=item.get("parameters", {}),
                )

                saved = save_skill_to_yaml(skill)
                if not saved:
                    logger.error("スキル '%s' の YAML 保存に失敗しました", name)
                    errors += 1
                    continue

                register_skill(skill)
                imported += 1
            except Exception as e:
                logger.error("スキル '%s' のインポートに失敗: %s", name, e)
                errors += 1

        result = {
            "imported": imported,
            "skipped": skipped,
            "errors": errors,
            "total": len(data),
        }
        logger.info(
            "スキルインポート完了: imported=%d, skipped=%d, errors=%d (total=%d)",
            imported, skipped, errors, len(data),
        )
        return result

    # ────────────────────────────────────────────
    # スキル一覧（カテゴリ情報付き）
    # ────────────────────────────────────────────

    async def list_skills_enriched(self) -> List[Dict[str, Any]]:
        """レジストリ内の全スキルにカテゴリ情報と利用統計を付与して返す。

        各スキルの tags 内のカテゴリ名と DB 上の SkillCategory を突合し、
        カテゴリの display_name, icon, color を付加する。
        また、対応する SkillPreset が存在すれば install_count も付加する。

        Returns:
            拡張スキル情報の辞書リスト。
        """
        registry = get_skill_registry()
        skills = registry.get_all()

        # DB からカテゴリとプリセットを一括取得
        async with await get_db_session() as session:  # type: AsyncSession
            cat_result = await session.execute(select(SkillCategory))
            categories = {c.name: c.to_dict() for c in cat_result.scalars().all()}

            preset_result = await session.execute(select(SkillPreset))
            presets = {p.name: p.to_dict() for p in preset_result.scalars().all()}

        enriched: List[Dict[str, Any]] = []
        for skill in skills:
            entry = skill.to_dict()

            # カテゴリ情報の付加（tags の最初のマッチを使用）
            matched_category = None
            for tag in skill.tags:
                if tag in categories:
                    matched_category = categories[tag]
                    break
            entry["category_info"] = matched_category

            # プリセット由来の利用統計
            preset = presets.get(skill.name)
            entry["install_count"] = preset["install_count"] if preset else 0

            enriched.append(entry)

        logger.debug("拡張スキル一覧を取得: %d 件", len(enriched))
        return enriched


# ────────────────────────────────────────────
# ユーティリティ
# ────────────────────────────────────────────

def _parse_uuid(value: Any) -> Optional[uuid.UUID]:
    """値を UUID に変換する。None や無効値は None を返す。"""
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError):
        return None


def _parse_uuid_strict(value: str) -> uuid.UUID:
    """値を UUID に変換する。無効な場合は ValueError を送出する。"""
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError):
        raise ValueError(f"無効なUUID形式です: {value}")
