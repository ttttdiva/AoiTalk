# MiniMax H3 content hierarchy audit
- mode: `apply`
- status: `applied`
- root: `4a3c2921-1a3a-4242-aab3-74b5794e9d7f`
- G14: `no visible topic`
- QA archive: `soft archive only`

## Topic paths
- G01: `MiniMax H3 / 参考・未確認`
- G02: `MiniMax H3 / ワークフロー`
- G03: `MiniMax H3 / モデル`
- G04: `MiniMax H3 / 公式・実装`
- G05: `MiniMax H3 / 参考・未確認`
- G06: `MiniMax H3 / モデル`
- G07: `MiniMax H3 / モデル`
- G08: `MiniMax H3 / 後加工`
- G09: `MiniMax H3 / ワークフロー`
- G10: `MiniMax H3 / LoRA`
- G11: `MiniMax H3 / 公式・実装`
- G12: `MiniMax H3 / 後加工・補助リソース`
- G13: `MiniMax H3 / プロンプト・制作技法`

## Explanations
- G01: KijaiのMiniMax-H3_comfy配布ページと関連discussionをまとめる参照項目。具体的な議論内容は保存済み本文から確定できないため要約しない。
- G02: MiniMax H3向けにRTX UpscaleとLTX Refineを組み合わせるCivitaiワークフロー。EZ/Turbo構成を掲げているが、倍率・速度・画質差は根拠不足なので書かない。
- G03: Riding POV系I2V向けとして保存されたモデル項目。モデル説明とは別に、取得済みの開始用prompt例を再利用可能な原文として保持する。
- G04: MiniMax-AI公式repositoryとh3-prompt-writingスキルの導入情報。保存済みinstall commandを改変せず実行用資料として残す。
- G05: MiniMax H3でのキャラクター入れ替えテストに関するX/Reddit参照。Xは取得履歴があるがsemantic本文が未整理、Redditは本文取得不能のため内容を推測しない。
- G06: Qwen3-VL-4Bの埋め込みを学習済み線形変換で32B互換5120次元へ変換するPoC。保存済み記録ではテキストエンコーダVRAMが約15.7GB→5.2GB、32B固有知識や多言語音声性能の一部喪失が報告されている。
- G07: 10Eros_Max beta2をMiniMax H3のRef2VA向けにINT8 ConvRot化した実験的モデル。video referenceを使う用途を想定した配布物として公開されている。
- G08: MiniMax H3の24-channel latentをlatent spaceのままアップスケールするためのモデル。低解像度生成→latent upscale→refineという流れを想定し、途中のVAE decode/encodeによるround-tripを避ける構成になっている。
- G09: MiniMax H3で既存clipから生成を継続する用途として公開されたDaSiWaのCivitaiワークフロー。保存済み証拠から詳細手順までは復元しない。
- G10: 保存済み記録ではAI Toolkit 0.12.8を使用し、学習画像24枚（既存20＋補助4）、LoRA rank/alphaはいずれも16。これら3 factをそのままtopic直下に置く。
- G11: MiniMax H3で複数shotを連続生成するmulti-shot chain向けのComfyUI repository。複数のshotをつないで扱う実装として公開されている。
- G12: DaSiWaのMiniMax H3 workflowで利用する補助リソース群。Spectrum、Motion Context / Masked AV continuation、LBH Latent Upscalerのnode/modelを、workflowを補助する関連実装としてまとめて管理する。
- G13: SubjectとPicture/Video参照を対応付ける入力例と、「参照画像をkeyframeとして使わない」指定を保存する。構文仕様として公式確認できていないため未整理を維持する。

## Typed SHA-256
- `2a6f5e05771afa5560275f066a4ab6624cf0b8e8d6cc58f16a2abb6a968fc224`
- `7d0c19e87d5d389ca9df5f9397146ab5840cbe2103340e8ce61589dd94ca98d9`
- `99d7e6a4dab022a557436f52f10950ad4a8b1b81e722536531fe05ef70cccbfb`
- `a338b514110b0e4e4975e87d0681b247be648ef7aaafdb2302c9e1f35275e2ec`
- `b4592ec241d852c1d03963011604e7992de5b61696dd54195324519b855c5419`
