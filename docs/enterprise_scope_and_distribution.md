# Enterprise scope and distribution

この文書は、AoiTalk Enterprise の**機能範囲**と**配布方式**を分離して判断する正本です。
`README.enterprise.md` は運用runbook、`src/features.py` と
`docs/feature_distribution_matrix.md` はruntime capabilityの正本です。

## 結論

Enterprise は Personal 版の全機能入りではありません。会社サーバー上で、社内の
project / task / schedule / document / file / chat / code-maintenance を扱うための
curated profileです。Personal・趣味・media・voice・外部consumer連携を除外し、
認証・権限・永続化・監査・安全な更新を追加します。

通常の配布物は sanitized-source handoff です。完全なrestricted-network dependency closureは
Enterpriseの普遍要件ではなく、対象環境にair-gap/no-network要件がある場合だけ選ぶ配布variantです。

## 判断根拠

この判断はrepositoryで追跡できる次の履歴に基づきます。別途、顧客契約・受入仕様・RFP等で
これより強い要件が確認された場合は、その一次資料をこの文書へ追加して再判定します。

| 時点 | commit | repository上の意味 |
| --- | --- | --- |
| 2026-01-09 | `e5d9afbc4c5a2b00156db5825234602db17898a3` | Enterprise profileを「コード保守向けの最小機能」として導入。voice/TTS/Discord/crawler/entertainmentを無効化し、code agentを有効化 |
| 2026-06-16 | `22e373dc5c4b0fef1309b6a664397f0b0c94328a` | 社内project/task/schedule/document検索をcompany serverで扱い、個人・趣味機能を除くEnterprise説明を導入 |
| 2026-08-31 | `9b25bdeb429dfac2515d945aee5642269a922c86` | Windowsはsanitized sourceをhandoffし、production buildはtarget Linuxで行う二段階方式を明文化 |
| 2026-09-05 | `0cfb93826bff6a92acfcc0ea231d97a353ee449a` | restricted-network用の`OfflineInputRoot` companionを任意機能として追加 |
| 2026-09-16 | `8f6464838a4869f61e728cb041cf250bc2c7c01e` | complete offline producerを通常経路へ変更。ただし、それ以前のEnterprise要件にair-gapを必須化する一次資料はrepository内で確認できない |
| 2026-09-22 | `76d5e4371c01c2cfb139e0b04a23159e1e943a09` | Windows Docker不在時のWSL2 Docker fallbackをoffline producerへ追加 |
| 2026-09-22 | `aed030ad6cdc47273833f3c4911724b68dbcdbee` | optional offline producerのnpm bundled dependency判定を修正 |

`contracts/product-contract.json` の `offline` はmobile clientのcache / queue / reconnect契約であり、
Enterprise releaseをair-gap配布にする契約ではありません。

## A. Enterpriseに必要

### Product capability

- company server上のworkspace、project、task、status、due date、calendar、notification
- chat、社内document/file管理、検索、reporting
- project-management / filesystem / skills / code-maintenance assistant
- Enterprise profileで明示された`code_agent`
- user authentication、project/workspace scope、role/permission enforcement
- PostgreSQL/Qdrant等のdurable state、schema migration、backup/restore、transactional update/rollback
- secrets管理、HTTPS、監査可能なbuild/runtime verification
- Personal機能をUI表示だけでなくbackend capabilityでもfail closedにすること

### Distribution and operations

- canonical remote `origin/main` のexact commitを固定すること
- launch checkoutの未commit変更を混入させないisolated snapshot
- sanitized source、branding、import closure、secret/personal-data exclusion
- manifest、SHA-256 coverage、source commit/provenance binding
- target Linux上でのproduction image build、startup、HTTPS、persistence、machine verification
- 現在唯一保守されているdeployment pathとしてのtarget-side Docker/Compose
  - Dockerは現行運用実装として必要であり、Enterprise product capabilityそのものではない

## B. Enterpriseから除外すべき

### Personal / consumer capability

- personal voice conversation、TTS/STT、VOICEROID/A.I.VOICE/CeVIO等の個人Windows音声連携
- Discord、Spotify、consumer crawler、event/video crawler
- ComfyUIとの個人画像生成連携
- Personal remote-server/mobile固有機能
- Personal版の全機能を「コードに存在する」という理由だけでEnterpriseへ同期すること

Hugging Faceブラウザ/同期、Hydrusブラウザ、Story/Novel Studio・TRPGは、Enterprise除外対象ではなく
41のcodebaseに存在しない。正本はそれぞれ独立アプリ `60_huggingface-sync`、`57_Hydrus_App`、
`86_Novel-Graph-Studio` であり、Apps画面から外部リンクするだけである。41 PostgreSQLの
`story_*`/`trpg_*` テーブルは `86_Novel-Graph-Studio` の取り込み元として保持するだけで、
41 runtimeからは使用しない。

### Universal release requirementとして不要

- 通常の`Enterprise.bat`でWSL2を起動すること
- 通常releaseごとにDocker producerを起動すること
- 全OCI imageをproducer PCへ大量取得すること
- APT offline closure、Python wheelhouse、npm offline cacheを毎回生成すること
- `--network=none` build proofを全Enterprise配布の完成条件にすること
- source-only/sanitized-source handoffを不完全なEnterprise配布と扱うこと
- standard external backendしか使わない環境でもROCm vLLM、CUDA SGLang、llama.cppを一括取得すること

## C. 要件確認が必要

- 対象顧客・siteでair-gap / restricted-networkが必須か
- offline handoffで必要なinference backend
  - `external`
  - `gemma-vllm` (AMD/ROCm)
  - `sglang-cuda` (NVIDIA/CUDA)
  - `deepseek-llamacpp`
- backend別のOCI closureを生成するか、全互換backendを1 companionへ入れるか
- EnterpriseAssistant PC Bridgeを全配布へ常に含めるか
- Active Directory transportを標準要件にするか
- native employee execution / autonomous agent runtimeを許可するか
- Docker以外のtarget runtimeを保守対象にするか
- optional manual MediaOps / EngagementOpsの具体的範囲

判断保留項目は「実装済みだから必要」と扱いません。対象環境、operator、hardware、network、
security policy、acceptance testを一次資料で確認した後にAまたはBへ移します。

## Offline producerの評価

| 項目 | 通常Enterprise | restricted-network variant | 判定 |
| --- | --- | --- | --- |
| WSL2 | 不要 | Windows Docker不在時の実装fallback | 通常はB、offline実装手段としてC |
| Docker Engine (producer) | 不要 | no-network proof生成に必要 | 通常はB、offlineでは条件付きA |
| Docker/Compose (target) | 現行の正式deployment path | 同じ | 現行運用としてA |
| OCI image取得 | targetが必要pinだけ取得 | selected backendを含むclosureが必要 | 全backend一括はB、backend選択設計はC |
| APT offline closure | 不要 | targetがapt repositoryへ接続できない場合だけ必要 | 条件付きA |
| Python wheels | 不要 | targetがpackage indexへ接続できない場合だけ必要 | 条件付きA |
| npm offline cache | 不要 | targetがnpm registryへ接続できない場合だけ必要 | 条件付きA |
| no-network build | 不要 | air-gap acceptanceとして必要 | 条件付きA |

## 現在の既知の過剰処理

明示的なoffline producerは、handoff manifestの全dependency/build-base pinを収集します。これには
standard defaultの`external` backendで不要なoptional inference imagesも含まれます。

- `rocm/vllm` (`gemma-vllm`)
- `lmsysorg/sglang` (`sglang-cuda`)
- `ghcr.io/ggml-org/llama.cpp` (`deepseek-llamacpp`)

現行validator、updater、target launcherは全pin setを厳密に検証するため、producerだけを部分的に
filterするとcontract不整合になります。今回の変更では互換性のためoptional offline pathを保持し、
通常経路から外します。backend-specific companionへ簡素化するときは、manifest schema、producer、
offline validator、updater、deploy launcher、testsを一体で変更します。

## 現在の配布契約

### 既定

```bat
.\Enterprise.bat
```

- sanitized-source handoffのみ生成
- Docker / WSL2 / OCI大量取得 / offline closure生成なし
- manifestは`offline_build.enabled=false`, `mode=none`
- target serverでpinned dependenciesを取得しproduction buildを実施

### 明示的なrestricted-network variant

```bat
.\Enterprise.bat -Offline
```

- handoffとsource-bound offline companionを生成
- Docker/Buildx、dependency取得、no-network build proofを実行
- 現時点では全supported-backend compatibility closureを収集する

既に検証済みの同一source用closureを再利用するときは、次を使います。

```bat
.\Enterprise.bat -OfflineInputRoot <directory>
```

## 変更gate

将来、Personal側の機能やoffline資材をEnterpriseへ追加するときは、少なくとも次を記録します。

1. 利用者と業務use case
2. capabilityを有効にするEnterprise要件の一次資料
3. data/secret/network/security boundary
4. target hardwareとbackend
5. positive/negative acceptance test
6. package size、download時間、storage、update/rollbackへの影響
7. A/B/C分類の更新

この記録なしに「Personalにある」「実装済み」「全部入りが完全版」という理由で同期しません。
