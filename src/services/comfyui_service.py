"""ComfyUI画像生成サービス

ローカルComfyUI (localhost:8188) のREST APIを経由して
ワークフローベースの画像生成を行う。
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import aiohttp

logger = logging.getLogger(__name__)

# 出力ディレクトリ
OUTPUT_DIR = Path("temp/generated_images")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ワークフローディレクトリ
WORKFLOWS_DIR = Path("config/comfyui_workflows")
WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_AUTO_WORKFLOW = WORKFLOWS_DIR / "aoitalk_auto_sdxl.json"
DEFAULT_NEGATIVE_PROMPT = (
    "lowres, bad anatomy, bad hands, extra fingers, missing fingers, "
    "bad face, deformed, cropped, worst quality, low quality, jpeg artifacts, "
    "text, watermark, signature, username, blurry"
)


class ComfyUIError(Exception):
    """ComfyUI操作のエラー"""


class ComfyUIService:
    """ComfyUI REST APIクライアント"""

    def __init__(
        self,
        enabled: bool = True,
        base_url: str = "http://127.0.0.1:8188",
        default_workflow_path: str = str(DEFAULT_AUTO_WORKFLOW),
        timeout_seconds: int = 180,
    ):
        self.enabled = bool(enabled)
        self.base_url = base_url.rstrip("/")
        self.default_workflow_path = default_workflow_path
        self.timeout_seconds = timeout_seconds
        self.client_id = str(uuid.uuid4())
        
        # ワークフローディレクトリの確保
        WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_config(cls, config) -> "ComfyUIService":
        """config.yaml の comfyui セクションから初期化する。"""
        comfyui_conf = config.get("comfyui", {})
        if not comfyui_conf:
            comfyui_conf = {}
        return cls(
            enabled=comfyui_conf.get("enabled", True),
            base_url=comfyui_conf.get("url", "http://127.0.0.1:8188"),
            default_workflow_path=comfyui_conf.get(
                "default_workflow", str(DEFAULT_AUTO_WORKFLOW)
            ),
            timeout_seconds=comfyui_conf.get("timeout_seconds", 120),
        )

    async def is_available(self) -> bool:
        """ComfyUIサーバーに接続可能か確認する。"""
        if not self.enabled:
            return False
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.base_url}/system_stats",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False

    async def list_workflows(self) -> list[dict[str, Any]]:
        """利用可能なワークフローJSONの一覧を取得する。"""
        workflows = []
        for p in WORKFLOWS_DIR.glob("*.json"):
            workflows.append({
                "name": p.name,
                "path": str(p.absolute()),
                "is_default": str(p.absolute()) == str(Path(self.default_workflow_path).absolute()) if self.default_workflow_path else False,
                "mtime": p.stat().st_mtime
            })
        return sorted(workflows, key=lambda x: x["mtime"], reverse=True)

    async def save_workflow(self, name: str, content: str | dict) -> str:
        """ワークフローJSONを保存する。"""
        if not name.endswith(".json"):
            name += ".json"
        
        target_path = WORKFLOWS_DIR / name
        
        if isinstance(content, dict):
            content_str = json.dumps(content, indent=2, ensure_ascii=False)
        else:
            content_str = content
            # JSONバリデーション
            json.loads(content_str)
            
        target_path.write_text(content_str, encoding="utf-8")
        return str(target_path.absolute())

    async def delete_workflow(self, name: str) -> bool:
        """ワークフローJSONを削除する。"""
        if not name.endswith(".json"):
            name += ".json"
        
        target_path = WORKFLOWS_DIR / name
        if target_path.exists():
            target_path.unlink()
            return True
        return False

    async def get_workflow_content(self, name: str) -> dict:
        """ワークフローJSONの内容を取得する。"""
        if not name.endswith(".json"):
            name += ".json"
        
        target_path = WORKFLOWS_DIR / name
        if not target_path.exists():
            raise FileNotFoundError(f"Workflow not found: {name}")
            
        return self._load_workflow(str(target_path))

    def update_config(
        self,
        enabled: bool = None,
        base_url: str = None,
        default_workflow_path: str = None,
    ):
        """設定を更新する。"""
        if enabled is not None:
            self.enabled = bool(enabled)
        if base_url is not None:
            self.base_url = base_url.rstrip("/")
        if default_workflow_path is not None:
            self.default_workflow_path = default_workflow_path

    async def generate_image(
        self,
        positive_prompt: str,
        negative_prompt: str = "",
        workflow_path: Optional[str] = None,
        overrides: Optional[Dict[str, Any]] = None,
    ) -> str:
        """画像を生成し、ローカルファイルパスを返す。

        Args:
            positive_prompt: ポジティブプロンプト（Danbooruタグ形式推奨）
            negative_prompt: ネガティブプロンプト
            workflow_path: ワークフローJSONのパス（Noneならデフォルト使用）
            overrides: 上書き設定 {checkpoint, lora, lora_strength,
                       width, height, steps, cfg, sampler, scheduler, seed}

        Returns:
            生成画像の絶対パス

        Raises:
            ComfyUIError: 生成失敗時
        """
        overrides = overrides or {}

        # ワークフロー読み込み
        wf_path = workflow_path or self.default_workflow_path
        if not wf_path or not Path(wf_path).exists():
            wf_path = await self.ensure_default_workflow(overrides)
        if not wf_path or not Path(wf_path).exists():
            raise ComfyUIError(f"ワークフローファイルが見つかりません: {wf_path}")

        workflow = self._load_workflow(wf_path)

        # APIプロンプト形式に変換
        prompt = self._workflow_to_api_prompt(
            workflow, positive_prompt, negative_prompt, overrides
        )

        # ジョブ投入
        prompt_id = await self._queue_prompt(prompt)
        logger.info("ComfyUI ジョブを投入しました: %s", prompt_id)

        # 完了待ち
        result = await self._wait_for_completion(prompt_id)

        # 画像ダウンロード
        image_path = await self._download_result_image(result)
        logger.info("ComfyUI 画像生成完了: %s", image_path)

        return str(image_path)

    async def ensure_default_workflow(self, overrides: Optional[Dict[str, Any]] = None) -> str:
        """利用可能な実モデルから標準 txt2img ワークフローを作成して返す。"""
        overrides = overrides or {}
        target = Path(self.default_workflow_path) if self.default_workflow_path else DEFAULT_AUTO_WORKFLOW
        if target.exists():
            self.default_workflow_path = str(target)
            return str(target)

        checkpoint = str(overrides.get("checkpoint") or "").strip()
        if not checkpoint:
            checkpoint = await self._select_available_checkpoint()
        sampler = str(overrides.get("sampler") or "euler_ancestral")
        scheduler = str(overrides.get("scheduler") or "normal")
        workflow = self._build_default_txt2img_workflow(
            checkpoint=checkpoint,
            sampler=sampler,
            scheduler=scheduler,
            width=int(overrides.get("width") or 1024),
            height=int(overrides.get("height") or 1024),
            steps=int(overrides.get("steps") or 24),
            cfg=float(overrides.get("cfg") or 6.5),
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(workflow, ensure_ascii=False, indent=2), encoding="utf-8")
        self.default_workflow_path = str(target)
        logger.info("ComfyUI 標準ワークフローを作成しました: %s", target)
        return str(target)

    async def _select_available_checkpoint(self) -> str:
        """ComfyUI に登録されている checkpoint から実在するものを選ぶ。"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.base_url}/object_info/CheckpointLoaderSimple",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        raise ComfyUIError(f"checkpoint 一覧取得失敗: HTTP {resp.status}")
                    info = await resp.json()
        except ComfyUIError:
            raise
        except Exception as e:
            raise ComfyUIError(f"checkpoint 一覧取得失敗: {e}") from e

        choices = (
            info.get("CheckpointLoaderSimple", {})
            .get("input", {})
            .get("required", {})
            .get("ckpt_name", [[]])[0]
        )
        if not choices:
            raise ComfyUIError("ComfyUI に利用可能な checkpoint がありません")

        preferred = [
            "waiNSFWIllustrious_v150.safetensors",
            "pornmaster_proSDXLV8.safetensors",
        ]
        for name in preferred:
            if name in choices:
                return name
        return str(choices[0])

    def _build_default_txt2img_workflow(
        self,
        *,
        checkpoint: str,
        sampler: str,
        scheduler: str,
        width: int,
        height: int,
        steps: int,
        cfg: float,
    ) -> dict:
        """ComfyUI UI JSON 形式の標準 SDXL txt2img ワークフローを作る。"""
        return {
            "last_node_id": 7,
            "last_link_id": 9,
            "nodes": [
                {"id": 1, "type": "CheckpointLoaderSimple", "mode": 0, "inputs": [], "widgets_values": [checkpoint]},
                {"id": 2, "type": "CLIPTextEncode", "mode": 0, "inputs": [{"name": "clip", "link": 2}], "widgets_values": ["positive prompt"]},
                {"id": 3, "type": "CLIPTextEncode", "mode": 0, "inputs": [{"name": "clip", "link": 3}], "widgets_values": [DEFAULT_NEGATIVE_PROMPT]},
                {"id": 4, "type": "EmptyLatentImage", "mode": 0, "inputs": [], "widgets_values": [width, height, 1]},
                {
                    "id": 5,
                    "type": "KSampler",
                    "mode": 0,
                    "inputs": [
                        {"name": "model", "link": 1},
                        {"name": "positive", "link": 4},
                        {"name": "negative", "link": 5},
                        {"name": "latent_image", "link": 6},
                    ],
                    "widgets_values": [0, "randomize", steps, cfg, sampler, scheduler, 1.0],
                },
                {
                    "id": 6,
                    "type": "VAEDecode",
                    "mode": 0,
                    "inputs": [
                        {"name": "samples", "link": 7},
                        {"name": "vae", "link": 8},
                    ],
                    "widgets_values": [],
                },
                {"id": 7, "type": "SaveImage", "mode": 0, "inputs": [{"name": "images", "link": 9}], "widgets_values": ["aoitalk"]},
            ],
            "links": [
                [1, 1, 0, 5, 0, "MODEL"],
                [2, 1, 1, 2, 0, "CLIP"],
                [3, 1, 1, 3, 0, "CLIP"],
                [4, 2, 0, 5, 1, "CONDITIONING"],
                [5, 3, 0, 5, 2, "CONDITIONING"],
                [6, 4, 0, 5, 3, "LATENT"],
                [7, 5, 0, 6, 0, "LATENT"],
                [8, 1, 2, 6, 1, "VAE"],
                [9, 6, 0, 7, 0, "IMAGE"],
            ],
        }

    def _load_workflow(self, path: str) -> dict:
        """ワークフローJSONを読み込む。"""
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _workflow_to_api_prompt(
        self,
        workflow: dict,
        positive_prompt: str,
        negative_prompt: str,
        overrides: dict,
    ) -> dict:
        """ワークフローJSONをComfyUI APIのprompt dictに変換する。

        ワークフローの nodes 配列からノードID→class_type→inputs を構築し、
        プロンプトやパラメータを動的に差し替える。
        """
        nodes = workflow.get("nodes", [])
        links = workflow.get("links", [])

        # リンクテーブル構築: link_id → (from_node_id, from_slot)
        link_map = {}
        for link in links:
            # link format: [link_id, from_node_id, from_slot, to_node_id, to_slot, type]
            link_id, from_node, from_slot = link[0], link[1], link[2]
            link_map[link_id] = (str(from_node), from_slot)

        # ノードマップ構築
        prompt = {}
        node_info = {}  # node_id → {class_type, widgets_values, inputs, mode}

        for node in nodes:
            node_id = str(node["id"])
            class_type = node["type"]
            widgets = node.get("widgets_values", [])
            inputs_list = node.get("inputs", [])
            mode = node.get("mode", 0)

            node_info[node_id] = {
                "class_type": class_type,
                "widgets_values": widgets,
                "inputs": inputs_list,
                "mode": mode,
            }

        # 各ノードをAPIプロンプト形式に変換
        for node_id, info in node_info.items():
            class_type = info["class_type"]
            widgets = info["widgets_values"]
            inputs_raw = info["inputs"]
            mode = info["mode"]

            # mode=4 はバイパス (パススルー)
            # バイパスノードはAPIに含めるが、接続を直結にする必要あり
            # 今回はLoRAのバイパスを考慮して処理

            api_inputs = {}

            # ── ノード種別ごとのinputs構築 ──

            if class_type == "CheckpointLoaderSimple":
                ckpt = overrides.get("checkpoint", widgets[0] if widgets else "")
                api_inputs["ckpt_name"] = ckpt

            elif class_type == "LoraLoader":
                lora_name = overrides.get("lora", widgets[0] if len(widgets) > 0 else "")
                strength_model = overrides.get("lora_strength", widgets[1] if len(widgets) > 1 else 1.0)
                strength_clip = overrides.get("lora_strength", widgets[2] if len(widgets) > 2 else 1.0)
                api_inputs["lora_name"] = lora_name
                api_inputs["strength_model"] = strength_model
                api_inputs["strength_clip"] = strength_clip
                # model/clip 入力はリンクから
                for inp in inputs_raw:
                    if inp["name"] == "model" and inp.get("link") is not None:
                        api_inputs["model"] = list(link_map[inp["link"]])
                    elif inp["name"] == "clip" and inp.get("link") is not None:
                        api_inputs["clip"] = list(link_map[inp["link"]])

            elif class_type == "EmptyLatentImage":
                api_inputs["width"] = overrides.get("width", widgets[0] if len(widgets) > 0 else 1280)
                api_inputs["height"] = overrides.get("height", widgets[1] if len(widgets) > 1 else 1536)
                api_inputs["batch_size"] = widgets[2] if len(widgets) > 2 else 1

            elif class_type == "CLIPTextEncode":
                # positive (ノード6) vs negative (ノード7) の判別
                # outputsのリンク先を確認してKSamplerのpositive/negativeどちらに繋がるか判定
                # 簡易判定: node_id == "6" → positive, "7" → negative
                if self._is_positive_prompt_node(node_id, links):
                    api_inputs["text"] = positive_prompt
                else:
                    api_inputs["text"] = negative_prompt or (widgets[0] if widgets else "")
                # clip入力
                for inp in inputs_raw:
                    if inp["name"] == "clip" and inp.get("link") is not None:
                        api_inputs["clip"] = list(link_map[inp["link"]])

            elif class_type == "KSampler":
                seed = overrides.get("seed", random.randint(0, 2**32 - 1))
                api_inputs["seed"] = seed
                api_inputs["steps"] = overrides.get("steps", widgets[2] if len(widgets) > 2 else 25)
                api_inputs["cfg"] = overrides.get("cfg", widgets[3] if len(widgets) > 3 else 8.0)
                api_inputs["sampler_name"] = overrides.get("sampler", widgets[4] if len(widgets) > 4 else "euler_ancestral")
                api_inputs["scheduler"] = overrides.get("scheduler", widgets[5] if len(widgets) > 5 else "normal")
                api_inputs["denoise"] = widgets[6] if len(widgets) > 6 else 1.0
                # model/positive/negative/latent_image 入力はリンクから
                for inp in inputs_raw:
                    link = inp.get("link")
                    if link is not None and link in link_map:
                        api_inputs[inp["name"]] = list(link_map[link])

            elif class_type == "VAEDecode":
                for inp in inputs_raw:
                    link = inp.get("link")
                    if link is not None and link in link_map:
                        api_inputs[inp["name"]] = list(link_map[link])

            elif class_type == "SaveImage":
                api_inputs["filename_prefix"] = overrides.get("filename_prefix", "aoitalk")
                for inp in inputs_raw:
                    link = inp.get("link")
                    if link is not None and link in link_map:
                        api_inputs[inp["name"]] = list(link_map[link])

            else:
                # 未知のノードタイプ: widgets_values とリンクをそのまま使用
                for inp in inputs_raw:
                    link = inp.get("link")
                    if link is not None and link in link_map:
                        api_inputs[inp["name"]] = list(link_map[link])

            # バイパスモード (mode=4) のノードはスキップ
            # ただしリンク構造を維持するために出力先を直結する必要がある
            if mode == 4:
                continue

            prompt[node_id] = {
                "inputs": api_inputs,
                "class_type": class_type,
            }

        # バイパスされたノードのリンクを修正
        # LoRA がバイパスの場合、CheckpointLoader→KSampler/CLIPTextEncode に直結
        self._fix_bypassed_links(prompt, node_info, link_map, links)

        return prompt

    def _is_positive_prompt_node(self, node_id: str, links: list) -> bool:
        """ノードがKSamplerのpositive入力に繋がっているか判定する。"""
        for link in links:
            # link: [link_id, from_node, from_slot, to_node, to_slot, type]
            if str(link[1]) == node_id and link[4] == 1:  # slot 1 = positive
                return True
        return False

    def _fix_bypassed_links(self, prompt: dict, node_info: dict, link_map: dict, links: list):
        """バイパスされたノードを経由するリンクを直結に修正する。"""
        bypassed_ids = {nid for nid, info in node_info.items() if info["mode"] == 4}
        if not bypassed_ids:
            return

        # バイパスされたノードの入力→出力の対応を構築
        # LoraLoaderの場合: input model(slot0)→output MODEL(slot0), input clip(slot1)→output CLIP(slot1)
        for bp_id in bypassed_ids:
            bp_info = node_info[bp_id]
            bp_inputs = bp_info["inputs"]

            # 入力スロット → ソースノード のマッピング
            input_sources = {}
            for inp in bp_inputs:
                link = inp.get("link")
                if link is not None and link in link_map:
                    # LoraLoader: model(0番目input) → 0番目output, clip(1番目input) → 1番目output
                    input_sources[inp["name"]] = link_map[link]

            # このバイパスノードを参照しているpromptノードのリンクを修正
            for nid, node_data in prompt.items():
                inputs = node_data.get("inputs", {})
                for key, val in list(inputs.items()):
                    if isinstance(val, list) and len(val) == 2 and str(val[0]) == bp_id:
                        output_slot = val[1]
                        # LoraLoader: output slot 0 = model → 入力の model
                        # output slot 1 = clip → 入力の clip
                        if bp_info["class_type"] == "LoraLoader":
                            if output_slot == 0 and "model" in input_sources:
                                inputs[key] = list(input_sources["model"])
                            elif output_slot == 1 and "clip" in input_sources:
                                inputs[key] = list(input_sources["clip"])

    async def _queue_prompt(self, prompt: dict) -> str:
        """ComfyUI にプロンプトをキュー投入する。"""
        payload = {
            "prompt": prompt,
            "client_id": self.client_id,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/prompt",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    raise ComfyUIError(f"プロンプト投入失敗 (HTTP {resp.status}): {error_text}")

                result = await resp.json()
                if "error" in result:
                    raise ComfyUIError(f"プロンプトバリデーションエラー: {result['error']}")

                return result["prompt_id"]

    async def _wait_for_completion(self, prompt_id: str) -> dict:
        """ジョブ完了を待機してhistoryデータを返す。"""
        import asyncio

        start = time.time()
        while time.time() - start < self.timeout_seconds:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.base_url}/history/{prompt_id}",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        await asyncio.sleep(1)
                        continue

                    history = await resp.json()
                    if prompt_id in history:
                        entry = history[prompt_id]
                        status = entry.get("status", {})
                        if status.get("completed"):
                            if status.get("status_str") == "success":
                                return entry
                            else:
                                msgs = status.get("messages", [])
                                raise ComfyUIError(f"生成失敗: {msgs}")

            await asyncio.sleep(1)

        raise ComfyUIError(f"タイムアウト ({self.timeout_seconds}秒)")

    async def _download_result_image(self, history_entry: dict) -> Path:
        """生成結果の画像をダウンロードしてローカルに保存する。"""
        outputs = history_entry.get("outputs", {})

        # SaveImageノードの出力を探す
        for node_id, output in outputs.items():
            images = output.get("images", [])
            if images:
                img_info = images[0]
                filename = img_info["filename"]
                subfolder = img_info.get("subfolder", "")

                params = {"filename": filename, "type": "output"}
                if subfolder:
                    params["subfolder"] = subfolder

                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"{self.base_url}/view",
                        params=params,
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as resp:
                        if resp.status != 200:
                            raise ComfyUIError(f"画像ダウンロード失敗: HTTP {resp.status}")

                        content = await resp.read()
                        ext = Path(filename).suffix or ".png"
                        output_name = f"comfyui_{int(time.time())}_{random.randint(1000, 9999)}{ext}"
                        output_path = OUTPUT_DIR / output_name
                        output_path.write_bytes(content)
                        return output_path.resolve()

        raise ComfyUIError("生成結果に画像が含まれていません")


# ────────────────────────────────────────────
# シングルトンインスタンス管理
# ────────────────────────────────────────────

_instance: Optional[ComfyUIService] = None


def get_comfyui_service(config=None) -> ComfyUIService:
    """ComfyUIServiceのシングルトンを取得する。"""
    global _instance
    if _instance is None:
        if config:
            _instance = ComfyUIService.from_config(config)
        else:
            try:
                from ..config import Config

                _instance = ComfyUIService.from_config(Config())
            except Exception:
                _instance = ComfyUIService()
    elif config:
        comfyui_conf = config.get("comfyui", {}) if hasattr(config, "get") else {}
        _instance.update_config(
            enabled=comfyui_conf.get("enabled", True),
            base_url=comfyui_conf.get("url"),
            default_workflow_path=comfyui_conf.get("default_workflow"),
        )
        if comfyui_conf.get("timeout_seconds"):
            _instance.timeout_seconds = int(comfyui_conf["timeout_seconds"])
    return _instance


async def generate_image(
    prompt: str,
    negative_prompt: str = "",
    workflow_path: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """ComfyUIで画像生成し、呼び出し元が扱いやすいdictで返す。"""
    service = get_comfyui_service()
    if not service.enabled:
        raise ComfyUIError("ComfyUI連携は設定で無効化されています")
    if not await service.is_available():
        raise ComfyUIError(f"ComfyUIサーバーに接続できません: {service.base_url}")

    image_path = await service.generate_image(
        positive_prompt=prompt,
        negative_prompt=negative_prompt,
        workflow_path=workflow_path,
        overrides=overrides or {},
    )
    filename = Path(image_path).name
    return {
        "success": True,
        "image_path": image_path,
        "image_url": f"/api/generated-images/{filename}",
        "filename": filename,
    }
