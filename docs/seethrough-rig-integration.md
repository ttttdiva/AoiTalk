# AoiTalk × See-through Rig

## 使い方

Webでは「設定 → キャラクター → 対象キャラクターの編集 → 外見」を開く。
「モデルパッケージを取り込む」で87から書き出したZIPを選び、候補の「プレビュー」で見た目を確認してから「確認して使用」を押す。
取り込んだだけでは使用中モデルは変わらない。「前のモデルに戻す」で以前の使用版へ戻せる。

制作を行わず完成モデルを使うだけなら「See-through Rigでアバターを制作する」はOFFのままでよい。
プレビューの「声と口パクを確認」は既存の音声プレビュー経路を使用する。表情と表示範囲は有効なbindingから得たcapabilitiesに従う。

Nativeではサーバーへログインし、「設定 → キャラクター」で対象を選択する。
画面下のアニメーション設定でZIP取込、候補プレビュー、使用切替を行う。
実音声プレビューは会話と同じ `/api/mobile-voice/speech` と既存expo-avプレイヤーを使い、画面離脱・中止時に再生を終了する。
WebViewは使用せず、共有runtimeをNative GL canvasへ接続する。

## 責務と依存

87は原画からの制作、編集、リグ、変形、物理、表情、共有再生runtimeの正本。
41は制作依頼と進捗、所有者境界、完成ZIPの検証・実体保存、候補・使用版管理、音声と会話イベント、Web/Native表示を担当する。
87形式から旧AoiRigへの変換は行わない。

取り込んだモデルの通常再生は41のgenerated media storageと同梱runtimeだけで行う。
87 checkout、87のPython、See-through推論環境、編集HTTPサーバーは不要。
モデルZIP内のJavaScript/HTMLや任意プログラムは実行しない。
ABI/schema、hash、所有者、path traversal、symlink、サイズ・個数・深さ、画像・mesh・有限値などの検証を通したimmutable copyだけを配信する。

## 登録履歴と保存容量

履歴はキャラクターと所有者の組ごとに最大256 revisionまで保持する。旧上限32から拡張した。
失敗・中止を含む全履歴を残し、新規取込と制作開始は最大revision + 1を採番する。
欠番を再利用したり、過去の候補・原画・モデル実体を削除して空きを作ったりしない。
使用版は独立したポインタで、取込や制作開始によって自動変更しない。
既存DBの書換えやmigrationは不要で、v32の次はv33として登録できる。

上限到達時の新規取込・制作開始はHTTP 429と上限件数を含むエラーを返す。
同一パッケージの再取込は保存済み実体を再検証して同じ候補を返し、履歴枠を消費しない。
保存済みモデルのプレビュー・使用切替・既存ジョブの再開には新しい履歴枠を必要としない。
モデル保存容量512MiB、原画保存容量256MiB、生成キュー8件の既存上限は変更しない。
容量超過は引き続きHTTP 413で拒否する。履歴拡張はWeb/Native共通APIに適用する。
一覧の直近8件と使用版の表示範囲は保存履歴数とは別で、今回変更しない。

## 制作連携

制作設定をONにし、既存の87を検出するか明示パスを指定して「接続を確認」を実行する。
無効な明示パスを指定したときは別cloneへ勝手に切り替えない。
既存の外部管理checkoutは再利用し、無断でpull/reset/cleanしない。
管理対象のインストールと依存準備・更新はbootstrap契約を通し、dirty checkoutの更新を拒否する。
推論環境の場所は環境変数・設定から解決し、PC固有のドライブを製品コードへ固定しない。

制作ジョブは87のproject-create/status/run/resume/cancel/exportを呼び出す。
完成しても自動で使用版へ切り替えず、候補として取込・検証する。
承認済み制作素材をproject-adoptで再利用した実行と、新しい推論実行を区別して記録する。

## runtime配布の更新

`resources/seethrough-rig/runtime` は生成物。直接編集しない。
修正は87の正本に適用してテストし、commit済みの完全なGit SHAを指定して生成する。

```text
87/.venv/Scripts/python.exe scripts/package_runtime.py --output <staging-directory> --source-ref <full-committed-sha>
```

生成されたruntime-provenance.jsonの全ファイルのサイズとSHA-256を確認し、生成物を41へ配置する。
`resources/seethrough-rig/release.json` も同じ固定revisionへ更新する。
通常のWeb/Native/Docker buildが隣接87やそのworking treeを参照してはいけない。
Mobile buildは `scripts/stage_mobile_runtime.py` で同梱配布物をstageする。

現在のhost修正では、同じ表情IDの反復入力がblink-before-swapを毎フレームやり直さないようにした。
Web/Nativeはプレビューと返答リアクションから有効な表情を一つだけ解決し、正本runtimeへ渡す。
87の承認済み過去版motion/deformation/physicsを41独自に変更しない。

## 目の連続変形の互換性

共有runtime 0.4.0（ABI 1）は、従来のruntime 0.1.0 / 0.2.0 / 0.3.0モデルも再生する。
連続変形は実験的なopt-in機能として追加し、標準presetや既存の使用版へ自動適用しない。
外観の承認は、契約・計算・取込の検証とは別にモデルごとに行う。

輪郭変形のみの `continuous.version: 1` は `runtime.version: "0.2.0"` と
`runtime.requires: ["continuous-eyes-v1"]` を必須にする。
登録済みキー画像を使う `continuous.version: 2` は `runtime.version: "0.3.0"` と
`runtime.requires: ["continuous-eyes-v2"]` を必須にする。
動く線画ガイドまたは睫毛の下の補助被覆を使う `continuous.version: 3` は
`runtime.version: "0.4.0"` と `runtime.requires: ["continuous-eyes-v3"]` を必須にする。
左右でversionが違う場合も、最も高いfeatureを一つだけ宣言する。
旧プレイヤーが新しい頂点・画像束縛を無視して誤表示しないよう、既存のversion検査でも拒否される契約にする。
過去版 `core_ref` との併用、未知のfeature、宣言と実際のgeometryの不一致は受け付けない。
従来のモデルを自動変換せず、使用版・履歴・原画は変更しない。

version 2 / 3のringは、全輪郭キーと同名の `eyeRig.artKeys` を持つ。各キーはatlas番号と
頂点数に一致するUV配列だけを持ち、openのatlas/UVは描画要素本体とFloat32変換後に完全一致させる。
version 1、mask、iris、whiteにはartKeysを許可せず、登録済みringを他要素のclip先にも使わない。
補間済みの一つのring mesh上で登録画像を
premultiplied alphaで混合する処理は87だけが担当し、41に別の描画・変形処理を追加しない。

version 3は `strokeGuides` または `underLashCoverage` の一方以上を必要とする。
ガイドは全輪郭キーに対応する上列・下列・四隅の点で、X座標はキー間で固定する。
ringは開口輪郭・動く線画境界・固定外周の三境界を持ち、全キーで三角形の反転と潰れを拒否する。
version 3はFloat32変換後の厳密な頂点対応と閉眼を要求し、正規の向きを持つ三角形で重なりも検出する。
キー間の元座標の補間には87と同じ丸め誤差の上限計算を適用する。頭部・camera変換後の実描画確認は別途必要とする。
補助被覆の `marginPx` は元のcanvas座標で0より大きく2以下とし、角と閉眼で消える。
開口maskは共有し、補助領域は実際の登録済み睫毛alphaの支持範囲に限る。
この補間・被覆の描画処理も87の配布物が担当する。

41の取込境界では、対応する輪郭の有限値・頂点数・順序・閉眼時の閉合、全周リングと共有maskの
topology、Float32変換後の頂点束縛とUV、共通の頭部・呼吸属性を検証する。settingsを合成した後も再検証する。
`EyeJitome` の新しい束縛には連続変形のopt-inが必要。
WebとNativeは同じ固定配布物とversion/feature契約を使用する。

制作元は `https://github.com/ttttdiva/87_see-though_rig.git` の完全一致だけを信頼する。
旧定数 `ttttdiva/seethrough-rig.git` とrelease.jsonの不一致を修正し、別owner・別repoは引き続き拒否する。
制作CLIのidentity version 0.1.0 / protocol version 1は、再生runtime versionとは別契約のまま維持する。

## 互換性と検証の区別

既存旧AoiRigモデル、DB、原画、使用中モデルは保存する。新規制作経路は87へ送る。
旧制作実装の物理削除は、新しい制作経路の実確認が済むまで別の移行作業として扱う。
旧再生互換コードと旧制作コードは同一視しない。

型検証、テスト、Web build、APK buildが成功しても、Native実モデル再生の実確認に代えない。
Webでの実取込・候補・使用切替・再読込・旧版復帰、実音声、Native端末、配布とCIは個別に記録する。
静止画や別demoの表示だけを、製品描画経路の同条件比較と呼ばない。
承認済み琴葉葵の比較profileはWind=0で、headYawは非対応として扱う。
会話用のconversation-v1 profileでは、87の書き出し時に風・瞬き・呼吸・待機・視線・物理の再生設定をZIPへ含められる。41での会話調整は対応する現行engineだけに適用し、ZIPと使用版を変更せず所有者別に保存する。比較profileは風なしで固定し、会話調整とは別modeにする。過去版core_refは固定のまま保持し、会話modeを選んでも設定変更や現行engineへの切替を行わない。

Mobile公開は `docs/mobile-auto-update-standard.md` に従い、未公開のversionと検証済みAPKを使用する。
同一公開versionの上書きは行わず、Releaseとlatest.jsonを一致させる。


## モデル別の会話再生設定API

正本の設定schemaは87の `web/js/runtime/playback-settings.schema.json`。41は同梱runtime内の
同じJSONからPython検証とOpenAPI型を構築する。フィールド・既定値・範囲・条件付きの
最小/最大関係を別々に定義せず、非有限数、型の変換、未知のversion/fieldを拒否する。
保存されたmodel/settingsのblockには `version: 1` が必要で、manifestのblockは完全な設定を持つ。
解決順は共通既定値 → 旧profile → model.playbackSettings → settings.playbackSettings →
manifest.playbackSettings → 保存override。新しいblockを持たないZIPも旧profileから復元する。

共通パスは `/api/characters/manage/{character_id}/rig/builds/{build_id}/playback-settings`。
Webは既存Python proxy、Nativeは認証済みFastAPIを使用する。

- GETはdefaults、overrides、effective、manifest_sha256、revision、mode、editableを返す
- PATCHは `{manifest_sha256, expected_revision, settings}` を受け、部分設定を保存overrideへmergeする
- DELETEは `{manifest_sha256, expected_revision}` を受け、overrideを消してモデル既定値へ戻す
- queryのmode省略（`model`）はZIPのprofileを保持する。比較モデルから会話調整へ進む場合は
  `mode=conversation-v1` を明示する。`comparison-v1` と過去版engineへの書込は409になる
- revisionは0から増加し、reset後も保持する。古いrevisionまたは異なるmanifest hashの保存は409。
  再読込して再確認し、古いdraftを無断で上書きしない
- 所有者・character・buildが違えば404。型・範囲・merged intervalの違反は422。
  DB commit失敗を保存成功として返さない。応答は `Cache-Control: private, no-store`

`character_rig_playback_settings` はbuild UUIDを主キーにし、所有者境界はbuildのrigから継承する。
immutable ZIP実体、manifest、build.configへoverrideを書き込まない。
既存の `package_sha256` はZIP全体ではなくmanifest.jsonのSHA-256であり、APIでは誤解を避け
`manifest_sha256` と呼ぶ。読出し・保存時にmanifestと参照payloadのhashを検証する。
キャッシュはサーバー/認証ユーザー/character/build/modeを分離し、保存成功後に同じモデルの
会話・プレビューを無効化して再取得する。未保存draftは会話へ流さない。

migration `20261001_0001` は設定専用テーブルを追加するだけで、既存候補・原画・使用版を変更しない。
