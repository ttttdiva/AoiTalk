"""ComfyUI画像生成ツール

LLMから呼び出し可能な画像生成ツール。
キャラクターのappearance_tagsとComfyUI設定を使って画像を生成する。
"""

import logging
from typing import Optional, Dict, Any
from .core import tool

logger = logging.getLogger(__name__)


@tool
async def list_comfyui_workflows() -> str:
    """利用可能なComfyUIワークフローの一覧を取得する。

    Returns:
        ワークフロー名の一覧（JSON形式）
    """
    try:
        from ..services.comfyui_service import get_comfyui_service
        service = get_comfyui_service()
        workflows = await service.list_workflows()
        
        import json
        return json.dumps([{"name": w["name"], "is_default": w["is_default"]} for w in workflows], ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("ComfyUIワークフロー一覧取得エラー: %s", e)
        return f"エラー: ワークフロー一覧の取得に失敗しました: {e}"


@tool
async def generate_comfyui_image(
    prompt: str,
    negative_prompt: str = "",
    character_slug: str = "",
    workflow_name: str = "",
    width: Optional[int] = None,
    height: Optional[int] = None,
    steps: Optional[int] = None,
    cfg: Optional[float] = None,
    sampler: str = "",
    scheduler: str = "",
    lora_strength: Optional[float] = None,
    seed: Optional[int] = None,
) -> str:
    """ComfyUIを使ってキャラクター画像を生成する。

    Args:
        prompt: 画像の説明（Danbooruタグ形式推奨、例: "smile, sitting, cafe, eating cake"）
        negative_prompt: 除外したい要素（省略可）
        character_slug: キャラクターのslug名（指定するとキャラの外見タグが自動結合される）
        workflow_name: 使用するワークフロー名（省略可、デフォルトが使用される）
        width: 画像の幅（省略可）
        height: 画像の高さ（省略可）
        steps: サンプリングステップ数（省略可）
        cfg: CFGスケール（省略可）
        sampler: サンプラー名（例: "euler_ancestral", "dpmpp_2m_sde", 省略可）
        scheduler: スケジューラ名（例: "karras", "exponential", 省略可）
        lora_strength: LoRAの強度（0.0〜2.0、省略可）
        seed: ランダムシード（省略可）

    Returns:
        生成画像のパスを含む特殊タグ [GENERATED_IMAGE:<path>]
    """
    try:
        from ..services.comfyui_service import get_comfyui_service, ComfyUIError, WORKFLOWS_DIR

        service = get_comfyui_service()

        # ComfyUI利用可能チェック
        if not await service.is_available():
            return "エラー: ComfyUIサーバーに接続できません。起動状態を確認してください。"

        # ワークフローパスの解決
        workflow_path = None
        if workflow_name:
            if not workflow_name.endswith(".json"):
                workflow_name += ".json"
            potential_path = WORKFLOWS_DIR / workflow_name
            if potential_path.exists():
                workflow_path = str(potential_path.absolute())
            else:
                logger.warning("指定されたワークフローが見つかりません: %s", workflow_name)

        # キャラクター情報の取得
        full_positive = prompt
        full_negative = negative_prompt
        overrides = {}

        # 引数による上書き設定の構築
        if width: overrides["width"] = width
        if height: overrides["height"] = height
        if steps: overrides["steps"] = steps
        if cfg: overrides["cfg"] = cfg
        if sampler: overrides["sampler"] = sampler
        if scheduler: overrides["scheduler"] = scheduler
        if lora_strength is not None: overrides["lora_strength"] = lora_strength
        if seed is not None: overrides["seed"] = seed

        if character_slug:
            try:
                from ..services.character_service import get_character_for_prompt
                char = await get_character_for_prompt(character_slug)
                if char:
                    # キャラクターの外見タグをプロンプトに結合
                    appearance = char.get("appearance_tags", "")
                    if appearance:
                        full_positive = f"{appearance}, {prompt}" if prompt else appearance

                    char_negative = char.get("negative_tags", "")
                    if char_negative:
                        full_negative = f"{char_negative}, {negative_prompt}" if negative_prompt else char_negative

                    # キャラクター固有のComfyUI設定（引数より優先度が低い）
                    comfyui_config = char.get("comfyui_config", {})
                    if comfyui_config:
                        # 引数で指定されていないものだけ上書き
                        for k, v in comfyui_config.items():
                            if k not in overrides:
                                overrides[k] = v

                    logger.info(
                        "キャラクター '%s' のタグを適用: appearance=%d文字",
                        character_slug,
                        len(appearance),
                    )
            except Exception as e:
                logger.warning("キャラクター情報取得失敗 (%s): %s", character_slug, e)

        logger.info(
            "ComfyUI画像生成開始: workflow=%s, positive=%d文字, overrides=%s",
            workflow_name or "default",
            len(full_positive),
            list(overrides.keys()),
        )

        image_path = await service.generate_image(
            positive_prompt=full_positive,
            negative_prompt=full_negative,
            workflow_path=workflow_path,
            overrides=overrides,
        )

        return f"[GENERATED_IMAGE:{image_path}]"

    except ComfyUIError as e:
        logger.error("ComfyUI画像生成エラー: %s", e)
        return f"画像生成エラー: {e}"
    except Exception as e:
        logger.error("ComfyUI画像生成で予期せぬエラー: %s", e, exc_info=True)
        return f"画像生成で予期せぬエラーが発生しました: {e}"
