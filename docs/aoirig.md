# キャラクターモデル

独自AoiRigの生成と再生は削除した。AoiTalkが制作・取り込み・再生するモデル形式は See-through Rig（87）だけである。AoiTalkが担うのはパッケージの取り込み、キャラクターへの割り当て、会話と音声である。

画像とモデルはキャラクターと所有者の組で保存する。共有Characterの `avatar_image_path` は変更しない。実体は generated media root 配下の `aoirig/<rig UUID>/` に置く。
