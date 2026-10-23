# Strata runtime

Strataは既存の `openai_compatible_local` transportのruntime profileとして利用する。
チャット・streaming・tool calling・reasoningは既存clientを共用し、別LLM provider/clientを増やさない。
Agent委譲（正準 prefix `/api/agent-tasks`）でStrataを推論に使う場合のpin済みclientも `OpenAICompatibleLocalClient` であり、native `AgentTurnRunner` ではない。委譲実行のturn loopは local_agent_tasks.md が、共有brokerのlease契約は shared_local_runtime.md が扱い、このprofile契約が会話・tool状態の共有や複数会話 parking の有効化を行うことはない。

## 対応環境と固定source

- Windows 10/11、Linux x86-64。NVIDIA専用GPU、または公式対応AMD Radeon（HIP）が必要
- 統合GPUは公式非対応。Strix Halo等の統合GPU端末ではStrataを導入・起動しない
- 公式release: [Strata v0.1.41](https://github.com/Niko1221/Strata/releases/tag/v0.1.41)（2026-10-08）
- 固定source: [`fb58e0dbc8399662c0e47c76578c6e878b14f6cf`](https://github.com/Niko1221/Strata/tree/fb58e0dbc8399662c0e47c76578c6e878b14f6cf)。2026-10-09に公式tagのSHA、archive、`CMakeLists.txt`のversionと`setup.py`の`MIN_ENGINE=(0,1,41)`を確認
- 管理engineは **0.1.41に固定**。minimumを満たす別版へ自動追従しない
- [公式INSTALL](https://github.com/Niko1221/Strata/blob/fb58e0dbc8399662c0e47c76578c6e878b14f6cf/docs/INSTALL.md)はWindows/Linux両方のAMD HIPに対応。AMDのCPU visionはLinuxのみで、Windows AMDは画像なし

source・engineの固定値の正本は `src/llm/strata_manifest.py`。
旧source pin `99f3dbd0b21d1401b3769e0c0d963913607f380b` とminimum engine 0.1.38は旧実装の情報であり、実マシンで稼働したengine版の証拠ではない。
この更新のcommit/pushは配置準備の完了であり、実機のengine交換・モデル起動・本番反映は別工程として扱う。

| 検証値 | v0.1.41 |
| --- | --- |
| source archive SHA-256 | `0e1d7e8fbb50eebfdb28b60dcff029bfa371215958d59605825746713a0ef034` |
| 展開tree SHA-256 | `4430385813d593db27fe4e7dd40979c2e0053065cf7b8214bc56285e71c2e006` |
| Windows正規化tree SHA-256 | `dc7b46f300f78bee4c0bdfca55836d4d3f0efe19983dce06607237fd8fb5b832` |
| source file count | 2,422 |
| 展開 / 正規化byte count | 38,603,272 / 38,603,031 |

tree digestはPOSIX相対pathでsortし、各fileのpath、NUL、十進byte長、NUL、本文、NULを連結してSHA-256を計算する。
Windows正規化は `.bat/.cmd/.ps1/.sh` のCRLF/CR→LFだけに限定する。両形式とも件数・byte countまで照合する。
2026-10-09の再検証では、codeload archive（17,353,269 bytes）を未展開のままtar memberから直接読み出してraw値を再現した。
同じSHAをno-checkoutでcloneし `git ls-tree -r --long` と `git cat-file --batch` で読んだGit blobは2,422 files / 38,603,031 bytesで、正規化treeのhashと完全一致する。
差241 bytesは `.bat` 7 filesのCRLFだけ（SETUP:3、START-HERE:52、UPDATE:80、benchの3本:5/7/7、tools/hip/build_windows:87）。
全fileが改行正規化後のblobと一致し、それ以外の差はなかった。archive/raw tree checksumは手元の展開・改行変換から作った値ではない。
公式 [.gitattributes](https://github.com/Niko1221/Strata/blob/fb58e0dbc8399662c0e47c76578c6e878b14f6cf/.gitattributes) は `*.bat text eol=crlf` / `*.sh text eol=lf` を指定する。
`core.autocrlf=false, core.eol=lf` を明示したclean checkoutでもbatはCRLFとなり、raw/正規化の両値と一致した（`git status --porcelain` は空、`git ls-files --eol` はbatについて `i/lf w/crlf attr/text eol=crlf`）。
管理archiveのmarkerはrevision・release・engine版・archive/tree checksum・件数・容量・source file一覧を持つ。
setupが生成するvenv・engine・configをsource一覧へ混ぜず、再試行・起動時にも元のsource fileを照合する。
markerを持たないgit checkoutは固定HEAD、clean状態、追跡fileのtree digestのすべてを確認する。
管理導入の標準経路はchecksum済みarchiveとする。任意のWindows git checkoutは `core.autocrlf=true` によりpy/cpp/mdまでCRLFとなるため、cleanでも現行のscript専用正規化では受け付けない。
その場合はtree不一致と改行条件を具体的に診断する。既存checkoutを変換せず、archiveを使うか、別folderへ
`git -c core.autocrlf=false -c core.eol=lf clone --depth 1 --branch v0.1.41 https://github.com/Niko1221/Strata.git <new-root>` でLF checkoutを準備する。
通常のbat=CRLF/sh=LFは公式.gitattributesに従い維持する。

モデルは公式Qwen3.8-Flash-NextのQ2_0、IQ2_XS、IQ3_XXS、IQ3_Sを候補表示する。
served IDは `qwen3.8-flash-next-iq2_xs` 等。既存llama.cppの `qwen3.8-flash-next` とは区別する。
モデルURLやshard一覧はAoiTalkへ複製せず、固定sourceの公式setupに委譲する。

## Personal: 明示的な初回setup

1. Settings → Base ModelでOpenAI互換localの「Strata / …」を選ぶ
2. [ストレージ運用規約](local_llm_storage_policy.md)に従い、repository外の大容量データ保存先を指定する
3. Context（既定32768）、Vision（OFF/CPU/GPU）、port（既定8080）を選ぶ
4. 初回setupのソフトウェア導入・大容量download確認をチェックし「初回セットアップ / 再試行」を押す
5. runtime/model準備後に既存のモデル切替を通して起動する。healthと正確なmodel IDの両方でreadyを判定する

source folderが空なら、管理runtime領域へ固定revisionの小さなsource archiveを取得する。
既存folderを指定する場合は同じrevisionのclean checkoutが必要。既存の別revisionや変更済みsourceは上書きしない。
公式 `START-HERE.bat` / `setup.sh` に `--setup --no-start --yes`、選択設定、
`--prebuilt https://github.com/Niko1221/Strata/releases/download/v0.1.41/` を渡す。
子processの `STRATA_PREBUILT_URL` も同じURLへ固定し、最新releaseへのfallbackを防ぐ。
prepare完了時とmanaged起動直前に `BUILD.json` のversionと実行fileの `--version` の双方が0.1.41であることを確認する。
不足・別版・実version不明なら起動を拒否する。通常の候補一覧・readiness pollではversion probeを繰り返さない。
LinuxでPython/venvや管理者権限が必要な場合など、非対話実行できない依存導入は失敗として報告する。
必要なOS前提条件は公式INSTALLを参照し、整えた後に同じ操作で再試行する。

通常のアプリ起動・chat・ready確認はinstallerを呼ばない。
準備済みの `serve/server.py --engine strata --config … --host 127.0.0.1 --port 8080` を実行する。
text-onlyの管理runtimeは公式 `--lazy` を付け、モデルの読込前にHTTP受付を開始する。Vision有効時は既存の初期化を維持する。
公式server経路を使い、ブラウザ自動表示の `--open` は省略する。
Context/Vision等は公式setupが生成したconfigを利用する。

### 容量・offline・再試行

- 選択GGUFは約66.4〜83.6GB、MTPは追加約6GB、Visionは追加約1GB
- Q2_0＋AVX-512ではexpert再配置用に追加約40GB。Linux HIPのROCmは追加約10GB
- 展開・download staging、Python依存などにも空き領域が必要。単に80GBあれば十分とは扱わない
- 既存GGUFは全shardのあるfolderを指定し、公式 `--gguf-dir` へ渡す
- `--gguf-dir` は完全offlineスイッチではない。未準備のMTP・tokenizer・runtime・vision・依存がある場合は公式setupが取得する
- 完全offlineでは対応ホストで準備したsource/venvまたはPython依存、engine、GGUF、tokenizer、MTP、必要なvision artifactと生成configを配置し、configの絶対パスを整える。配置後の起動・readiness自体はdownloadしない
- setup失敗時は `logs/models/strata-setup.log`、起動失敗時は `logs/models/strata.log` を確認する。部分downloadを勝手に消さず、同じ保存先で再試行する

setup実行中は画面でphaseを確認できる。OSごとのdownload/compile詳細はログへ記録する。
setupは共有インストールロックで直列化し、同じmodelの重複実行は既存taskへまとめる。
source/venv/binary/GGUF/MTP/visionをGitへ含めない。既存 `.aoitalk-local-llm` ignoreとrepository外のモデル領域を使う。

### 実行用SSDへの既存artifact再配置

prefillではPLE（n-gram）tableをランダムに読む。既定の `--ple-io direct` はSSD向けのため、
実行に使うGGUF（PLEを含む）・pack・MTPは高速SSDを推奨する。保管先に十分な容量があっても、
HDD上のままでは起動やprompt読込みが遅くなり得る。保存先の変更はストレージ運用規約と承認範囲に従い、
物理SSD・空き容量・現在の実効パスを先に確認する。junctionでpath guardを迂回しない。

- `data_dir` の変更だけでは既存の実行パスは切り替わらない。固定sourceの公式setupは別driveのデータを自動コピーせず、旧保存先のGGUFやpackを再利用する場合がある
- 既存artifactを再利用する場合は、全GGUF shardを元の名前のまま同じfolderへ、prepared packとMTPを各folder単位でコピーする。転送先volume上でfile count・byte size・SHA-256を照合し、元ファイルは切替検証まで残す
- 実際の `--config` が指す `strata-<quant>.json` の `args` にある `--native`、`--ple-gguf`、`--pack`、`--mtp` と、`tokenizer` をコピー先へ合わせる。その他のartifact path（vision、expert profile等）も確認する。`model_name`・engine/source revisionは変えない
- AoiTalkはprepare完了後とmanaged起動直前に、設定済みの`data_dir`/`gguf_dir`と同じ相対構造で存在するartifactだけをJSONへ原子的に再配線する。コピー先に不足があれば旧volumeを使い続けず、起動を失敗させて再コピー・hash照合を要求する。DBのStrata設定が恒久的な正本であり、生成JSONだけを編集して切替完了とは扱わない
- 既存の生成JSONにある`--prefill`、`--vram-reserve-mib`、`--expert-cache`は、path/config保存時にStrata設定へ取り込み、公式setup後にも再適用する。現行成功値を明示的に変更しない限り、公式setupの既定値（prefill auto、reserve 700 MiB）へ戻さない
- 明示済みの `--ple-io` もsetup前に記録して再適用する。公式setupが条件判定で `ram` を選んでも現用SSDの `direct` を保持し、重複flagを作らない。旧configに未指定ならAoiTalkから追加・上書きせず、公式setupの新しい判定に従う
- 通常起動は準備済みJSONを読むため、正しく配置したartifactの切替にsetupや再downloadは不要。`--gguf-dir` だけで完全offlineになるわけではない
- JSON変更の反映には管理中のStrata Python serverの再起動が必要。native engineの自動再起動は記憶済みの引数を再利用する。処理中の要求と所有者を確認し、通常の停止・起動経路で切り替え、重複serverを起動しない
- 切替後はprocess引数・engine logの実パス、health/model ID、実リクエストを確認する。コピー・検証・起動が失敗したら元ファイルを残し、旧configへ戻せる状態を保つ

根拠: [固定sourceのデータ再利用](https://github.com/Niko1221/Strata/blob/fb58e0dbc8399662c0e47c76578c6e878b14f6cf/setup.py#L3509-L3640)、
[生成config](https://github.com/Niko1221/Strata/blob/fb58e0dbc8399662c0e47c76578c6e878b14f6cf/setup.py#L5452-L5460)、
[PLE読込み](https://github.com/Niko1221/Strata/blob/fb58e0dbc8399662c0e47c76578c6e878b14f6cf/src/prefill/prefill.cpp)、
[serverの再起動](https://github.com/Niko1221/Strata/blob/fb58e0dbc8399662c0e47c76578c6e878b14f6cf/serve/server.py)。

### VRAM余裕とrequest watchdog

固定sourceは全ロード後の空きVRAMが256 MiB未満なら警告する。通常は `LOW: requests may stall` とreserveの提案値を記録するが、
小容量GPU向けにreserveを自動縮小した場合は、他のGPU利用を減らすかcontextを下げるよう案内する。
これは不足リスクの警告であり、OOMや停止原因を確定するものではない。実効 `args` と他のGPU利用を確認する。
`--expert-cache auto` なら、生成JSONの `args` の `--vram-reserve-mib` を調整してexpert cacheを小さくできる。
未指定ならflagと値を追加し、指定済みなら既存値を変更する。重複flagを追加しない。
公式例の `2048` は他アプリとGPUを共有する際の余裕を増やす値で、速度とのtrade-offがあり、stallの解消を保証しない。
固定数のexpert cacheではreserve変更だけに頼らず、実効cache設定も確認する。AoiTalkの既定設定は自動変更しない。

`issue #29` はrequestの進行が一定時間止まったときのwatchdog表示であり、元issueと同じ原因とは限らない。
固定sourceの進行watchdogの既定は60秒。0.1.41ではfile tierのI/O進行がある間は `STRATA_WATCHDOG_IO_S`（既定はその10倍）まで待つ。
調査のために `STRATA_WATCHDOG_S=0` で無効化したり、失敗requestを自動で繰り返したりしない。
engine version、直前の要求時刻、完全なstall report（stage、pool、verify、memory、2秒後の値）と実効引数を保存し、
生成処理の停止と初回ロード待ちを分けて調べる。thread dumpは公開前に内容と共有範囲を確認する。
0.1.41はWindowsでも `--ple-io ram` を受け付けるが、RAM量とVirtualLockの権限を確認する必要がある。既存のSSD/`--ple-io direct`設定は自動変更しない。

根拠: [公式VRAM reserve手順](https://github.com/Niko1221/Strata/blob/fb58e0dbc8399662c0e47c76578c6e878b14f6cf/docs/INSTALL.md#L254-L257)、
[LOW判定](https://github.com/Niko1221/Strata/blob/fb58e0dbc8399662c0e47c76578c6e878b14f6cf/src/program/generate.cpp#L7987-L8025)、
[watchdog](https://github.com/Niko1221/Strata/blob/fb58e0dbc8399662c0e47c76578c6e878b14f6cf/src/program/generate.cpp#L8064-L8105)、
[Windowsを含むPLEのRAM読込み](https://github.com/Niko1221/Strata/blob/fb58e0dbc8399662c0e47c76578c6e878b14f6cf/src/program/generate.cpp#L2843-L2846)。

### v0.1.41の既定変更と反映・切戻し

- CPU prefill支援: 単一NVIDIA GPU・batchなし・1,024 token未満のchunkで既定有効。回答の末尾bitが変わり得る。既存 `env.STRATA_PREFILL_CPU_SHARE` を維持し、旧回答との一致を比較する場合だけoperatorが `"0"` を明示する。AoiTalkは未設定時に無効値を追加しない
- batch groups: layer splitの `--batch` は各GPU stageへgroupを分配するautoが既定。既存の `--batch`、`--batch-groups`、`--batch-mtp` をsetup後も維持する。未指定のbatchを勝手に有効にしない。従来の1 group比較は `--batch-groups 1` を使う
- server stall watchdog: `STRATA_ENGINE_STALL_S` は既定90秒で、無出力かつCPU・disk・GPUがidleのengineを終了する。動作中の遅い処理と区別し、`psutil` が必要。この変数はserver processの環境で指定する（configの `env` はnative engine向けなのでserverの既定値は変えない）。既存のprocess環境と、native watchdog/CPU shareを含むconfigの `env` を保持する
- WindowsのRAM不足時のfile tier改善は上流の評価結果。現在のSSD、context32768、prefill256、reserve2048、expert cache autoを保持して実機で再評価し、速度向上や既存stallの解消を単体テストから推定しない

公式setupは手動 `args` 全体をmergeしない（[carry_over](https://github.com/Niko1221/Strata/blob/fb58e0dbc8399662c0e47c76578c6e878b14f6cf/setup.py#L3658-L3663)）。
例外は別名で実在する `--expert-profile` と未生成の `--expert-profile-save`（[carry_profile_args](https://github.com/Niko1221/Strata/blob/fb58e0dbc8399662c0e47c76578c6e878b14f6cf/setup.py#L3695-L3715)）。
PLEはdisk/RAM条件で `ram` が追加される（[setup判定](https://github.com/Niko1221/Strata/blob/fb58e0dbc8399662c0e47c76578c6e878b14f6cf/setup.py#L5365-L5373)）ため、AoiTalkの明示値保持が必要となる。
上記の保持対象以外の手動engine引数は公式setupが再生成するので、引数をすべて保持すると扱わず、反映時に旧新configと `.bak` を比較する。

一次情報: [公式releaseの既定変更](https://github.com/Niko1221/Strata/releases/tag/v0.1.41)、
[CPU share](https://github.com/Niko1221/Strata/blob/fb58e0dbc8399662c0e47c76578c6e878b14f6cf/src/prefill/prefill.cpp#L157-L176)、
[server stall条件](https://github.com/Niko1221/Strata/blob/fb58e0dbc8399662c0e47c76578c6e878b14f6cf/serve/server.py#L233-L246)。

実機の配置は、既存Strata軽量化担当の終了と親の差分レビュー・反映調整の後に実施する。
モデル削除・再取得を前提とせず、次の順で切り替える。

1. 現在のsource_root、DBのStrata設定、生成configと `env/args`、engineの `BUILD.json` と実 `--version`、所有process/active leaseを記録する。現用configとruntimeを保持し、GGUF/pack/MTP/tokenizerを削除しない
2. 新しい固定revision専用runtime folderを準備する。旧folderへ `UPDATE.bat` / `update.sh` を実行しない。公式UPDATEはmainへgit pullするため、この管理更新の経路に使わない。指定SHAのclean checkoutまたはAoiTalkのchecksum済みarchiveを使う
3. 新sourceへ旧モデルconfigをコピーし、同じdata_dir/gguf_dirを指定した明示的prepareを行う。旧source_rootは残す。公式setupで新しいexe/cwd/profile pathを生成し、既存env、batch設定、prefill256/reserve2048/expert cache auto、明示 `--ple-io direct` を引き継いだことを比較する。他の手動engine引数の差も確認し、自動探索した別installの設定だけに依存しない
4. sourceのみの確認には `venv/Scripts/python.exe scripts/verify_strata_runtime.py --source-root <new-root>`、配置後のartifactと実engine確認には同コマンドへ `--config <new-root>/strata-iq3_s.json` を追加する。Linuxの検証用Pythonは `venv/bin/python`。sourceのみの成功はengine実稼働版の証拠にしない
5. active generationの完了を待ち、AoiTalk所有serverを通常経路で停止する。DBのsource_rootだけを新folderへ切り替え、通常経路で起動する。外部serverを終了しない。healthと正確なmodel ID、load、短文/長文のchat・streaming・tool calling・reasoning、watchdogログを確認する
6. 同じモデル・context・performance設定でcold/load時間、短いprompt（512/1K）、通常prompt、decodeを測る。単一NVIDIAではCPU share on/offを比較し、既存batch構成がある場合だけgroups auto/1を比較する。I/Oが動く遅い処理をhangとして終了しないこと、失敗requestを自動再送しないことを確認する

切戻しはactive leaseを確認して新serverを通常停止し、DBのsource_root/config設定とAoiTalkコードを同じ旧snapshotへ戻して再起動する。
新コードは旧版engineを拒否するため、source_rootだけ戻す操作では切戻し完了にならない。
親のmain統合前ならこのbranchを適用せず現用を継続する。統合後のコード切戻しはこの更新commitだけをrevertし、並行するMobile/記憶UI/軽量化commitをresetしない。
旧runtimeを保持する切替では `--rollback-engine` は不要。in-placeのengine rollbackだけで異なるsource/engineを混在させない。

## 起動・停止・接続

既定APIは `http://127.0.0.1:8080/v1`。
`GET /health` の `service=strata`, `status=ok`, `loaded=true` と、`GET /v1/models` の選択model ID完全一致を確認する。
healthだけ、開いたportだけ、別モデルだけではreadyにしない。既存API key設定がある場合は同じ設定でprobeする。

「準備済み」はartifactの準備状態であり、モデルの読込完了とは別に表示する。通常のモデル一覧はstatic/LKG設定を使い、
model discoveryや全artifactのファイル検査を行わない。明示的な候補更新だけが詳細検証を要求する。
同じ設定の一覧取得は実行中の処理を共有し、ページ取消によってworkerを増殖させない。設定変更後は以前の処理に合流しない。
管理中のモデル読込の状態取得も、起動前に検証したowned processの情報から軽量に投影し、現在のロード境界のphaseと経過秒を表示する。
以前のsetupやロードのログ、外部operatorのログは現在の進捗として表示しない。Strataのstdout/stderrは安定したログファイルへ直接接続し、
バックエンド再起動で親consoleのPIPEが閉じても、残存wrapperのログ出力を切断しない。

text-onlyの初回generationは、公式の `POST /load` を同一processで一度だけ実行し、並行要求は同じ準備処理を待つ。
wrapper起動とモデル準備で既存のreadiness期限を共有し、モデルのready確認後に回答要求を送る。回答用SDKのtimeoutは延長しない。
準備処理はgeneration leaseで停止・再起動から保護し、失敗を自動再送で隠さない。失敗した準備の再実行には明示的なruntime再起動が必要となる。

モデル停止・native再起動ではRAM上のexpert arenaも解放される。次回は再読込が必要で、OSのfile cacheや保存ドライブによって時間が変わる。
IQ3の既存実測では39.97GiBの読込に1065秒、初回AoiTalk会話に404.047秒を要した。会話中のnative処理401.358秒の大部分は入力処理であり、
通常会話の不要なreview/continuationとツール定義を削減する。これらの変更だけでHDD上のモデル読込が高速になるとは扱わない。

Strataでの会話タイトルは、既存のuser本文由来の短いタイトルへフォールバックする。タイトル用の追加推論は送らない。
タイトルの20秒timeoutでは同期native処理を停止できず、通常回答と共用するruntimeの後続処理を占有するためである。
他providerのタイトル生成と、保存済みの手動タイトルの保護は維持する。

### 2026-10-09のSSD切替確認

利用者の明示承認によりStrataのdata/GGUF保存先を `E:\AI\models\Cold_E\llm\strata` へ切り替えた。
一般のllama.cpp rootはF、固定runtimeとexpert profileはDのまま。66ファイル・85,223,481,732 bytesをコピーし、全SHA-256の一致を確認した。
IQ3、context32768、prefill256、reserve2048、expert cache autoを維持した通常WebUIの初回要求は成功し、run実行は150.97秒だった。
native起動からready観測は約131秒、arena読込は39.97GiB/0.44GiB/s、3475-tokenの入力処理8.410秒・decode0.234秒。
背景タイトル要求は0件、別のmemory/utility要求1件を含む。読込中の状態取得・候補一覧は200で、進捗表示とready後のpoll停止を確認した。
停止時はRAMのarenaが解放されるため次回再読込が必要であり、この測定を瞬時のcold startや常に同じ時間の保証とは扱わない。

2026-10-09時点では、Fの移設元削除は利用者承認後も自動承認レビューの拒否で未実施で、Eが現用、Fと旧ごみ箱内の同一shard参照は残存していた（この記述は当日時点の履歴で、現在の保存先ではない）。
初期化中のContext表示0、旧モデルのxhighからFastへの変更時の過渡的な警告、空会話を送信前に再読込した際の待機状態は未修正として分離する。

### 2026-10-22時点の現用保存先

稼働中のStrata engine（port 8083、`strata-iq3_xxs.json`）の引数は、packとGGUF shardをいずれも `C:\path\to\models\Hot\llm\strata-iq3_xxs`（`packs\iq3_xxs`、`models\IQ3_XXS`）から読んでいることを確認した。
`E:\AI\models\Cold_E\llm\strata` は存在せず、上記2026-10-09のE現用は現在の状態ではない。E側の重複は削除（ごみ箱へ移動）済みと親作業で確認している。
`<configured-model-root>\strata` には `models` / `mtp` / `packs` が残っているが、稼働中processはこれを参照していない。保存先の整理・移動は [ローカルLLMストレージ運用規約](local_llm_storage_policy.md) に従い、この文書では行わない。

Personalの画面で起動・停止できる。AoiTalkが起動して追跡しているStrata processだけ停止する。
応答生成中は停止を拒否し、外部processがportを使用中なら勝手に終了させない。
既存のgeneration leaseと通常のアプリ終了処理も共用する。

「Personalで起動・停止を管理する」をOFFにしてLAN endpointを保存するとoperator所有になる。
同じモデル名でもAoiTalkからsetup/start/stopせず、構成済みのURLを保持する。
Native Mobileは既存共有model catalog/engine APIを通してStrata表示・選択を受け取る。
電話自体へGPU runtimeを導入する機能ではない。

## Enterprise

Enterpriseは常にoperator-owned。既知Strata profileや古いauto_start設定が残っていてもAoiTalkは導入・起動・停止しない。
`deploy/enterprise/compose.external.yml` は `LLAMA_CPP_AUTO_START=false` を維持し `STRATA_AUTO_START=false` も明示する。
`AOITALK_EXTERNAL_BASE_URL=http://<future-nvidia-host>:8080/v1` と実際のserved model IDを設定し、既存external backendとして接続する。
既存の認証・秘密情報設定は変更しない。LAN公開・API key・firewallはoperatorの管理範囲とする。

## Prefix / KV cache

AoiTalkは送出copyを安定させ、Strata engineのRAM checkpoint(root=system+tools、ターン開始、16Kごと)を再利用させる。
engineの再利用はtoken・画像の完全一致で判定する。これは性能上の一致判定であり、認可境界ではない。
ユーザー・session・agentの分離はAoiTalk側のscope(session/user/agent、リクエスト単位のclient)が担い、cache一致を根拠にしない。

- 履歴の窓は直近N件の単純スライドではなく、user turn単位のblock境界から始める。状態を持たない関数で、既存の件数・文字数の上限(入力予算)はそのまま守る。
- 固定agent task(`AgentTaskInvocationService` → `build_agent_chat_turn_client` → `OpenAICompatibleLocalClient` → `_handle_tool_calls`)を含むtool loopの各roundは、
  `_tool_loop_completion` で送出copyを作る。古いtool結果の切詰めlevelはscope(session・user・model・loop冒頭)ごとに記憶し、縮める方向にだけ進める。
  入力予算を超えたときだけ回収し、次のpassまで同じprefixを送る。
- 回収先は実装上の選択として、context windowの65%と入力予算の80%の小さい方にしている(数値要件ではない)。既存の入力予算の意味は変えていない。
- `--prompt-cache*` と `--conversation-cache-*` はStrata config(`args`)側の設定で、setup再生成後も保持する。AoiTalkの設定画面や既定値は変更しない。
  `--conversation-cache-mib 8192 --conversation-cache-slots 4` はhost RAMを最大8GiB使うため、運用者が明示して追加する(現時点で本番configには未適用)。

Windowsでの確認範囲: 既存logでroot checkpoint(3520 token)と同一loop内の大きな再利用を確認済み。会話cacheとdisk sessionは
上流docがLinux/CUDAでのみ検証済みとしており、このWindows構成の実機結果は未確認。Windowsでの動作は実測してから有効化する。

## 検証の範囲

v0.1.41準備では、公式tag checkoutとchecksum済みarchiveのsource検証、Windows改行の正規化、marker/件数/容量の改変拒否、
setupのdownload固定と設定保持、実engine版不一致の拒否を確認する。検証CLIはsourceのみの場合に実engine版を未観測として返す。

小さなfixtureとmock HTTPでprofile、URL、Windows/Linux command、setup確認、artifact状態、health/models、Enterprise無起動、外部process保護を検証する。
UI単体テストでは明示確認、設定変更時の確認解除、重複操作防止、外部切替、Enterprise操作非表示、停止を確認する。
実GPUによるsetup、巨大モデル取得、実推論性能、Windows/Linux実機の導入完了をこれらの単体テストの結果から推定しない。
