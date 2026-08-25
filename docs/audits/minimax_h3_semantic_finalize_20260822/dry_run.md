# MiniMax H3 semantic finalization audit
- mode: `dry_run`
- status: `dry_run_ready`
- root: `4a3c2921-1a3a-4242-aab3-74b5794e9d7f`
- generated_at: `2026-08-22T11:01:52.441515+00:00`
- historical audit: preserved unchanged
- original topic title recovered: false

## Rename mapping
- G01: `71019796-9c90-4f1b-9686-68c7f47cfdb5` ClipIngest repair — revision 1fce9f89-cdcf-42b7-8d4f-d62fe2acc7a3 -> Kijai MiniMax H3 / ComfyUIディスカッション
- G02: `e67bfbf2-d3e4-4025-b953-4d1e6044cf1a` ClipIngest repair — revision 78d06fed-f051-47bd-a49e-f0d06c5aa8a9 -> MiniMax H3 EZ/Turbo RTXアップスケール・LTX Refineワークフロー
- G03: `2ba28918-e937-426b-85ec-7d256d190e48` ClipIngest repair — revision a5ac0653-5785-449d-83d7-0c49e00a403b -> H3LT X2 Riding POV I2Vモデル
- G04: `dcc72c6b-d026-4851-8084-19b8d095272a` ClipIngest repair — revision 98a56fc6-2ed9-40d0-b7e8-e5bf653cebba -> MiniMax H3公式リポジトリ／prompt-writingスキル
- G06: `8b5e5e3d-52b7-489d-ad29-762b94a220e3` ClipIngest repair — revision fc932396-c394-40ea-b96d-459400042de5 -> ClipProj-MiniMax-H3埋め込みアダプタ
- G07: `6ce7dbe4-adef-4cb8-a140-7efa8edbfb08` ClipIngest repair — revision af36de1f-d43e-45d0-a897-276d085271e5 -> 10eros Max INT8 Ref2VAモデル
- G08: `5db7f4c2-e7a6-4e3a-972a-c38d4b8b4575` ClipIngest repair — revision e2c973ad-d805-44cc-87d4-b67853320179 -> MiniMax H3 Latent Upscaler配布
- G09: `ed402bdb-385d-43c0-be1b-69555f40ae5f` ClipIngest repair — revision 12c506e1-ef0d-473a-b6b5-dc35e71985c2 -> DaSiWa MiniMax H3 continue-from-clipワークフロー
- G10: `6cc990c1-686f-4903-af9d-8d4baea2fe47` ClipIngest repair — revision 9d43c342-2f36-42fd-a38b-c507e40e94b0 -> MiniMax H3キャラクターLoRA学習（AI Toolkit）
- G11: `94b6330a-3073-40a4-a6d3-aed49b9a15e8` ClipIngest repair — revision d5e78d90-5e1a-4a76-a0eb-9a01c9c77868 -> ComfyUI-H3-Multishotリポジトリ

## Proposed topics
- G05: `329e8fa1-4811-5ddc-bce3-ef1045f829be` MiniMax H3キャラクター入れ替えテスト sort=24.0
- G12: `7f65079a-61c7-5a32-a890-bff52adb89ba` DaSiWaワークフロー補助リソース（Spectrum／Motion Context／Latent Upscaler） sort=46.0
- G13: `0cdec5fa-4a2c-5ce6-bce3-0c3e1d56a13c` MiniMax H3参照素材プロンプト構文（未整理） sort=23.5

## Reparent mapping
- G05: `9c54a88b-a874-4e7a-a5a6-e9e9ed30acfe` -> `329e8fa1-4811-5ddc-bce3-ef1045f829be` (pre-sort=24.0)
- G05: `cd245f05-32a6-48d8-8346-7d28bfc4cec0` -> `329e8fa1-4811-5ddc-bce3-ef1045f829be` (pre-sort=25.0)
- G05: `e58f5e1c-d72c-4ba3-8b8c-b89fc3ac63b0` -> `329e8fa1-4811-5ddc-bce3-ef1045f829be` (pre-sort=26.0)
- G05: `97337e1e-6b05-4fba-8961-091423722703` -> `329e8fa1-4811-5ddc-bce3-ef1045f829be` (pre-sort=27.0)
- G12: `d5336184-da99-4dc5-a6da-d78c463703d4` -> `7f65079a-61c7-5a32-a890-bff52adb89ba` (pre-sort=46.0)
- G12: `bb461cb4-18fc-4dd4-996d-8d1154e081c7` -> `7f65079a-61c7-5a32-a890-bff52adb89ba` (pre-sort=47.0)
- G12: `815e2dc4-bc71-401a-a959-f6da347c5a01` -> `7f65079a-61c7-5a32-a890-bff52adb89ba` (pre-sort=48.0)
- G12: `eb8a6065-b8f4-4c71-b911-3596bf486bfb` -> `7f65079a-61c7-5a32-a890-bff52adb89ba` (pre-sort=49.0)
- G12: `db291b46-5220-4281-8ead-f0cae79c4540` -> `7f65079a-61c7-5a32-a890-bff52adb89ba` (pre-sort=50.0)

## Group outcomes
- G05: one human-readable topic; four children reparented; Reddit empty_body provenance retained
- G12: one umbrella topic; five children reparented; supplemental wrappers retained
- G13: new unresolved topic; two input hashes moved losslessly
- G14: no independent topic; audit-only fold into G13

## Preconditions
- root_id/library_id/project_id/root_page_id exact
- all source revision IDs exist and remain attached to root
- all selected IDs exist exactly once
- all wrapper IDs exist and retain selected-child ancestry
- body_text/body_json/title/URL wrapper/ancestry snapshots compare exactly before lock and after lock
- G13 hashes and metrics recompute exactly
- transaction rollback on any failed precondition

## Counts
{"attachments": 0, "edges": 0, "nodes": 208, "placements": 0, "revisions": 367}
