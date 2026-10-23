# llama.cpp runtime の独立導入と切替

runtime はモデル保存先と分離し、既存の `executable` 設定で選ぶ。
全アプリから41のcacheを参照する運用を増やさず、外部の版別directoryへ導入する。
この手順はGGUFを取得・移動・削除せず、41のDB設定やPATHを変更しない。

## b11541 候補の準備

2026-10-10の実測候補は[公式 b11541](https://github.com/ggml-org/llama.cpp/releases/tag/b11541)、
commit `f2918cabbffe8abf8e2a90c1c089085fa116c2cf`、Windows Vulkan x64。
現用WinGet版b10437を保持した別配置でHemmingwayのロード・合成応答を確認した。
context1024、MTP ON、`--no-warmup` でready15.66秒、初token8.33秒だった。
この短い確認は通常context32768、tools/reasoning、長文やMTP速度の受入ではない。

uncensoredは `--cpu-moe --load-mode mmap`、context1024で4分以内にreadyへ達しなかった。
停止要求後もしばらくPID/listenerが残り、18:39:40 UTCに消滅・閉鎖を確認した。
**uncensoredの実ロード・生成は未受入**であり、runtimeのbuild条件を満たすだけでは成功扱いしない。
実測結果と生ログは `artifacts/runtime/strata-b11541-20261010-smoke-index.json` に保持する。

親レビュー後に、旧配置とは別の外部rootへruntimeだけを導入する。
保存先例のdirectoryは既存環境に合わせてoperatorが確定する。

```powershell
venv\Scripts\python.exe scripts\install_llama_cpp_runtime.py --runtime-root C:\path\to\runtimes\llama-cpp-b11541-vulkan --release b11541 --backend vulkan --model hemmingway-1
```

このCLIは既存 `ManagedLocalRuntimeManager` のasset URL allowlist、SHA-256、archive traversal防止、
version/minimum build検証と原子的な配置を使う。公式の指定tagからVulkan assetを選び、
CUDA assetやlatestへ黙って切り替えない。配置するexeのbuildは指定releaseと一致する必要がある。
Hemmingwayにはb10964以上、uncensoredにはb10660以上が必要で、b10437では両方を起動拒否する。

runtime rootは明示的なrepository外の絶対pathを指定する。同じreleaseの正しい配置は再利用し、
別releaseやmarkerのない既存配置は上書きせず、別rootを要求する。PATHや環境変数にある他のruntimeは
独立導入の成否に使わない。モデルdownload・起動は行わず、結果は `inference_verified=false` を返す。

## 既存設定で選択し、元へ戻す

CLIが返した `executable` の絶対pathを、既存の
`openai_compatible_local.llama_cpp.executable` または起動processの `LLAMA_CPP_EXECUTABLE` に指定する。
別名 `LLAMA_SERVER_EXE` も既存経路で使える。切替前の値と実際のexe/versionを記録し、
旧runtime本体を保持する。実行中の設定・環境を書き換えただけで反映完了とは扱わない。

statusと起動処理は共通の選択関数で、非空の `LLAMA_CPP_EXECUTABLE`、
非空の `LLAMA_SERVER_EXE`、persisted `executable` の順にoperator指定を選ぶ。
管理起動で明示指定がなければ既存のmarker付きmanaged runtime、PATHの探索を使う。
環境変数のexeが存在しない／不適合ならstatusもその指定をinvalidとして示し、
別のexeを正常な配置として報告しない。

persisted exeが欠損または不適合の場合は、statusと起動処理が同じ回復resolverを使う。
対象profileのmarker・build・commitを検証したmanaged currentを優先し、次に既存の
marker付きlegacy配置を検証する。回復できた場合、statusのexe/version/buildと準備状態は
その最終候補を表し、persisted設定値は変更しない。回復先がなければinvalidとして示し、
persisted exeの欠損をPATH上の別exeで正常扱いしない。PATH探索はoperator指定がない場合に使う。

内部statusの `runtime_executable_scope=launch_candidate` は次回起動の候補・配置の検証結果で、
稼働中PIDの証明ではない。owned processのsnapshotは `owned_process` とPIDを付け、
実際に起動した最終planのexeを保持する。設定や環境変数を変更しても、そのPIDのexeは変わらない。
旧process記録は明確な `Popen.args` だけを参照し、取得できなければexe不明のままにする。
公開APIはexe path・PID・内部scopeを出さず、既存 `server.running/ready` の実process判定を使う。

```powershell
# 以下は新しく起動する所有processだけに渡す例。実際のpathはCLI出力を使う。
$env:LLAMA_CPP_EXECUTABLE = 'C:\path\to\runtimes\llama-cpp-b11541-vulkan\runtimes\llama_cpp\stock\current\llama-server.exe'
$env:LLAMA_CPP_MODEL_ROOT = '<configured-model-root>'
```

切戻しはactive consumer/generationの終了を待ち、自分が所有するruntimeだけを停止して、
保存した `executable` 設定／process環境へ戻して起動する。新配置も旧配置も削除しない。
明示的な環境変数のruntimeが不適合なら、その原因・modelの要求build・実exeを表示して拒否する。
失敗した `--version` が互換build番号を出しても起動を許可しない。
明示exe指定でもdistributionのrequired commitを検証するため、stock b11541を
PrismML専用Bonsai PQ2_0へ流用しない。Bonsaiには既存のPrismML配置を選ぶ。
既存の古いpersisted pathの回復はmarkerとprofileを検証する既存経路を維持する。

## 外部operatorとconsumerの契約

86など複数アプリで使う場合は、一つの外部operatorがruntimeを所有し、各アプリはAPI consumerになる。
41の起動は不要だが、外部runtime本体・設定・参照するGGUFは必要。41も外部所有として接続し、
41の終了でそのruntimeを停止しない。

- operatorが親によるGPU予約を守り、単一モデルの起動・切替・停止を管理する。
- `--alias hemmingway-1` または `--alias qwen3.8-flash-next-uncensored` を使う。
- llama.cppの運用base URL例は `http://127.0.0.1:8080/v1`。portはoperatorが確定する。
- `/health` のHTTP200/status okと `/v1/models` のserved ID完全一致を確認してから要求を送る。
- `/v1/chat/completions` の `model` にserved IDを渡す。GGUF pathや表示ラベルは渡さない。
- Strata現行設定は `http://127.0.0.1:8083/v1`。readinessにはさらに `loaded=true` が必要。
- consumerは共有runtimeを勝手に開始・停止せず、operatorが利用中のconsumerを調整する。

41管理のprocessには既存consumer/generation leaseを適用し、利用中の停止・model切替を防ぐ。
外部runtimeを41のowned process listへ追加しない。認証値は別途設定し、診断や成果物へ記録しない。
検証専用port18084は運用接続先にしない。

ソース・CPU合成の検証とGPU実送信の受入は分ける。今回の変更準備ではGPUを起動せず、
通常context、tools/reasoning、uncensoredの実ロード受入は73/77終了後の別GPU枠で確認する。
