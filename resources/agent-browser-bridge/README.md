# Portable PC Bridge helper source checkpoint

初回接続診断はdaemonの外側のprivacy境界で固定6値へ変換する。
内側で先にerrorをcodeへ置き換える重複処理を削除し、実際のlocalhost
WebSocket拒否をdispatchまで通す回帰テストで診断の保持を確認した。
CLIの初期launch失敗も許可された診断値とexecutedだけを保持し、任意の
data・URL・target ID・例外本文を出力しない。自動再接続は追加しない。

agent-browser v0.38.2の固定公式sourceへpatchを適用して利用する。stock binaryで代用せず、上流tree・Cargo cache・browser profile・実行ファイルはここへ置かない。

## 背景GUIからの入力修正（source checkpoint）

strict fill/typeは入力時に限り、選択済み親pageのCDP sessionへ1回
`Page.bringToFront`を送り、refのnode/document/frame/meaningを再確認する。
`Document.hasFocus`とnative deep focus検査は維持し、OS共通キー入力や別tabへの
fallbackは使わない。最初のclear/insert前にnative focus不成立を確認できた場合は
`FOCUS_CHANGED` / `executed:false`とし、接続は保持、操作refは失効する。
focusイベントによるアプリ側処理まで未実行と保証するものではない。
CDP/transport失敗、clear後・途中入力後のfocus driftは従来通りunknownで停止し、
自動再送や自動再接続はしない。次の操作には新snapshotが必要。

この修正はhelper再buildが必要だった。旧candidate artifactは変更せず、focus版の
実測helper hash/manifestを新しい配布候補へ固定する。単一EXE再buildと、
Bridge GUI前面からの実Edge入力確認を通すまでは、新focus版を配布済み修正と扱わない。

## 状態

通常のbrowser操作とCrowdWorksはextension不要のagent_browserへ、media_prepareと素材転送はmanaged_profileへ固定してルーティングする。Bridge.exeが両用途の入口で、失敗時に別backendへfallbackしない。固定helper bundleがない候補はbrowser対応済みと扱わない。旧candidateの検証はそのartifactだけの証跡として保持し、現在の用途別ルーティング・GUI・実Studio・新しい単一EXEの成功へ流用しない。

## 再現手順

source checkpointは初回strict接続の任意診断field `data.connectionDiagnostic`
を追加した。既存の`unavailable`と`executed`は変えず、WebSocket、
`Target.setDiscoverTargets`、`Target.getTargets`のtimeout/errorを固定6値で
区別する。例外本文やURLは返さない。Windows MSVCの新helperはRust回帰184件PASS・9件ignoredとrelease buildを確認し、
実測bytes/SHA-256へ正式pinを更新した。実行可能な固定診断対応と、
実Edgeの受入・単一EXE配布の完了は区別する。hash照合を緩めて候補を実行しない。

1. upstream.jsonの公式archiveを取得し、sizeとSHA-256を照合する。作業領域はrepo配下.localを使う
2. Windowsのautocrlfが有効なcheckoutでは、bridge.patchをgit blobから取り出し、upstream.jsonのhashを照合する。展開rootを独立した一時git repositoryにし、core.autocrlf=falseを設定してから git apply --check と git apply を実行する。親repository内の未追跡subdirectoryでそのままapplyしない。resulting-files.jsonの全38ファイルのbytes/hashを照合する
3. Rust 1.99.0とWindows MSVC環境のcliディレクトリで cargo build --locked --release --target x86_64-pc-windows-msvc -j1 を実行する
4. agent-browser.exeの実測bytes/SHA-256と、--version --jsonのversion/protocol/capabilitiesを確認する
5. bundleにexe、manifest.json、build-manifest.json、NOTICE-bridge-modifications.txt、licenses原文を置く
6. venvのPythonで scripts/build_pc_bridge.py --agent-browser-bundle <bundle> --no-install を実行。--self-testと--verify-browser-helperの両方を通し、単一EXEの別hashも記録する

manifest.jsonはschema:1、platform:"win32-x64"、固定upstream_commit、bridge_protocol:1、contract.jsonと一致するdaemon_stamp/capabilities、helper:{filename:"agent-browser.exe",version:"0.38.2",size_bytes:実測,sha256:実測}を持つ。bridge.4の固定診断対応Windows helperは20,263,424 bytes、SHA-256 `1b239a673a53c2d8890cd8d1e6d45540dab1fe5e2e525b40fed85b3e45fad0cb`で、製品のpackaged default・配布gate・実機受入入口はこのhashを固定照合する。test/build injectionの合成helperは明示的な非配布fixtureに限る。旧hashやstock helperを流用しない。API keyや接続tokenはbundleに含めない。 production候補ではbuild_manifest:{filename:"build-manifest.json",size_bytes:76166,sha256:"630a5d99ca852c5e9d087c8071b93adb8e24d6fbec32880b911349a8a3157614"}を必須とし、同梱する来歴原文も固定照合する。

### 2026-10-10の入力復元確認

再ビルドのsource基点は `bb8b67c35c568d0fed74c992faa3c3a4e0363497`。この基点に2026-10-10のselector修正を適用し、現在のpatch/resulting-filesへ記録した。公式archive、固定patch、適用後38ファイル、Cargo.lock（SHA-256 `f1f1980cf6802f2fe1f6543645c91779f95531156690a452f0342da35e4aa92f`）を照合し、Rust 1.99.0（`b940084d7eb6a299eb4bfeb8e34901bc051e7ac4`）とMSVC 14.50.35717で `cargo +1.99.0 build --locked --offline --release --target x86_64-pc-windows-msvc -j1` が成功した。

再ビルド候補は20,263,424 bytes、SHA-256 `1b239a673a53c2d8890cd8d1e6d45540dab1fe5e2e525b40fed85b3e45fad0cb`。実binaryのversion 0.38.2、protocol 1、10 capabilitiesは契約に一致する。親の選択に従い、この候補を新しい固定pinとして採用した。旧binaryの復元成功とは扱わない。build-manifest.jsonには全38 source hash、Windows対象245依存のchecksum、Rust/MSVC/linkerの版と実体hash、release設定、生成dashboard placeholderの原文と作成/更新時刻、PE timestampを記録する。後から照合したtoolchainの実体hashと、ビルド時に保存できていない完全なprocess環境・linker argvを区別する。修正後ビルドではSOURCE_DATE_EPOCH=1791590400を明示し、生成placeholderの作成/更新時刻も保存した。rust-embed deterministic-timestamps featureは無効で、byte単位の再現性は保証していない。

旧記録の `<private-artifacts>/pc-bridge/cce3bc745-focus-reviewed-20261006/AoiTalk-PC-Bridge.exe` はこの端末に存在せず、開発repoのActions artifactは0件、開発・公開repoのrelease assetにもhelper/notice bundleは見つからなかった。取得できる正本はupstream.jsonの公式source archiveとこのcheckpointであり、今回のpin変更と新しい単体EXE候補を親レビューへ渡す。main統合・製品置換・公開配布は親レビュー後に行う。実GUI操作・実Studio確認は未実施。

notice入力はgit blobの原文を使う。Windows対象245 packagesのgraphとcache archive checksum、Rust aggregateの427原文hashを再照合した。Python依存とPyInstallerの公式PyPI wheel 11件のhash、26原文、axe-core 4.12.1の公式NPM integrity・同梱payloadを確認した。保存済みaxe-core第三者noticeは公式原文にある2か所の行末spaceが欠けており、公式原文を別途保存した。この入力復元直後の時点では、新しいPlaywright/Nodeを含むfrozen inventoryとMicrosoft runtimeの対応原文は未確認だった。

その後、selector修正版の実CArchive/PYZ inventory、全49 native payloadのhashと公式Python/PyPI/Microsoft原本、固定helper・来歴の同梱bytes、EXEから書き出した原文bundleの全61ファイルを照合した。dependency-notices-status.jsonのproduct_components_and_packagingは、この新候補のtechnical確認結果をcompleteとして記録している。法的release clearanceと親レビューは未完了で、candidate notice manifestのreview_statusはpending_parent_release_reviewのまま。実GUI用途切替と実Studioも未確認であり、technical確認のPASSには含めない。

## 安全境界と実機QA

- 既存device_idとbrowser/computer/GUI共通lockを使う
- 最初の明示接続を保持してmetadata discoveryから明示target選択へ進む。0targetsは未選択で止まり、別tabへfallbackしない
- GUI再描画はcached診断のみ。run終了はref/epochを失効し、idleなWSS transportの再接続があっても承認済みCDPを保持する。timeout/unknownまたはidentity fault後はCDPへ自動再接続せず、操作を再送しない
- QAは明示合成tabのみ。既存利用者tabの本文や入力値を収集しない
- 初回実機QAは接続attempt1回・少数actionに限定し、許可再表示またはunknownで即停止する。許可UI自動承認・security設定変更はしない
- CDP check-and-actの原子性は保証しない。editable値除外は任意page proseの一般的DLPではない
- 終了は所有Job/helperだけを停止し、既存Edge/profile/登録tokenを維持する

## 配布前の残件

固定Cargo.lockのWindows対象license/notice/source通知、実Edge/CDP/JEV/ref/actionの受入、Browser経路のWebUI、同一WS寿命、公開手順を確認する。通常browserにextension/NativeMessaging経路を復活させない。source push、正式候補build、公開配布、runtime適用の完了を区別する。

## bridge.4の追加契約

- CrowdWorks観測はGUI明示選択の同じtargetだけを使う。shared lockと単一run_idを保ち、各URLへ1回navigateし、finally end_runでrefを失効する
- 1回のnative AX/DOM captureを無害化してmain/light-DOMだけを返す。pierced DOM取得には子frame/shadow metadataが含まれ得るが、出力では除外する
- 候補labelとcard contextが別案件に混ざる場合、不完全なAX/DOM、範囲超過、文書変更は明示失敗。切捨てや空一覧として成功させない。実サイト互換性は未確認
- Windows policyはraw観測に限定し、実効禁止やEdge未起動を断定しない
- bundleには固定licenses/RUST-DEPENDENCIES-NOTICES.txtも必要。原文不足は0で、dependency-notices-status.jsonのtechnical input reviewは完了している。zune/combined-termとlegal clearanceは別の残件として保持する

VM対象統合783PASS・7環境skip。helper専用20+1、bridge56+15、strict28+2、snapshot28、exact-target12、通常clippy/fmtがPASS。既知host EPERM fixtureは除外し、環境変数を使う既存testの並列競合はserialで検証した。all-targets clippyは未変更の既存test警告が残る。これは全repo/実機受入の成功ではない。

2026-10-10の実Windows合成QAで前候補 `472f500df33b8679a751e7a7c558fb796fed8b11107295d958a72a7aa0b461a1` のselector snapshotがoption refを重複renderすると判明した。仮想AX祖先も調べてrootを決めるsource修正を適用し、上記の新pinへ再ビルドした。Bridgeの重複ref拒否は緩和していない。今回の生成placeholder・SOURCE_DATE_EPOCH・明示build環境はbuild前に保存した。全OS環境とlinker argvの未記録は来歴にも明記する。

今回の対象Python検証771PASS・3環境skip、Rust snapshot30PASS/bridge60PASS、frozen合成WSS実操作14段階PASS。単体EXE SHA-256は `1d048f67980cb4c247e14ebb621141f725c2996ec9ef185ba60770e5e7123a8b`。61ファイルの原文ZIPをEXEから書き出し、入力bytesと一致した。既存のログイン済みStudioタブがなく、native GUI操作のtool surfaceも利用できないため、その二つの実確認はPASSに含めない。

## 2026-10-10 notice整備のreview候補

上記のselector修正候補は保存済みの履歴である。current pinは全38 custom sourceに変更表示を付けた再ビルドである。runtimeの許可pinも変わるため、新runtime・helper・bundle/build-manifest・source/noticeの同時配布が必要になる。JSONは `$comment` metadataだけを追加し、元のfield値を保持する。helperは20,263,424 bytes / SHA-256 `83cacd4103400c1a1ae73c9a1fc0148d9afa1c9c65f4ea7f6e131018ac6d7217`、build-manifestは76,101 bytes / SHA-256 `3a92c1662308f9f4d7269bf52763e59cb5e33245cebb6cacee82812368c663ff`。source/patch/resulting-filesの新hashを同梱する。

実crateのlicense/COPYING/NOTICE/PATENTS/COPYRIGHT/AUTHORS名を再走査し、既存aggregateの427原本に補足6原本を追加した。rav1e PATENTSのCP1252、各COPYRIGHTのCRLFをGit/packagingで変換しない。全433原本の収集完了は、再配布権やAOMの条件・特許面を包括的に承認する意味ではない。

Zuneの5版はZlibへ統一する案で、親の選択確定は未記録。Microsoftの具体的3DLLは公式原本と同一だが、配布主体の適用licenseと再配布権は未確認。notice候補manifestと配布gateはこの未確認を維持する。新規規約への同意や有料購入は行っていない。実GUI/Studioと正式配布も別の未完了条件である。

通常のproduction bundle検証では新helper `83cacd41…` とbuild-manifest `3a92c166…` の両pinを要求し、旧 `1b239a67…` / `630a5d99…` を拒否する。既存frozen Bridgeは自身の内蔵pinを維持し、新runtimeへ更新済みとは扱わない。新候補は再頒布権／release review未確認のreview用である。

Rust aggregateは原本2,241,101 bytes / `ef228f39…` をGit、helper同梱、notice exportで完全保持する。改行変換済み2,241,023 bytes / `2c064b48…` は3原本のSHAが不一致になるため同梱を拒否する。

## Redacted historical provenance（source読者向けの参考情報）

`build-manifest.redacted-historical-provenance.json` は、private開発repoの `build-manifest.json`（76,101 bytes / SHA-256 `3a92c166…`）から、個人のuser名を含むtoolchain path 2件（`toolchain.assets[0].path` / `[1].path`）と開発checkoutのpath 1件（`build_environment.CARGO_TARGET_DIR`）だけを伏せ字にした写し（76,125 bytes / SHA-256 `d1d68246…`）である。それ以外のJSON値（helper、toolchain assetのhash、generated inputsのhash、source baseのcommit等）は原本と同一で、新しいbuildの証跡ではない。

- これはproduction bundleの成果物ではない。runtimeとbuildは、明示的に指定された信頼済みexternal bundleの原本 `build-manifest.json`（SHA-256 `3a92c166…`）だけを検証し、この写しを読まず、検証にも使わない。
- 公開sourceには原本の `build-manifest.json` を含めない。bundleに入れる原本は上記の手順どおり別途用意する。
- 新しいpinや第二のtrust対象を追加するものではない。
