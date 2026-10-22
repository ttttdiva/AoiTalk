# Personal通常チャットのMCP OFF

2026-10-15、本人が「通常チャットではAoiTalk登録MCPの注入をOFFにし、CLI自身のMCP設定を尊重する」仕様を承認。CLI側で有効なMCPは利用可能になることを説明した上で承認を受けた。新規UIスイッチは追加しない。

変更は共通CLI capability層。Personalかつ通常チャット、Agent execution/scope/role/tool/dialogue/workspace contextがなく、全ツール禁止でもない場合だけ、mcp_enabled=Falseによるprovider全MCP禁止を適用しない。AoiTalkのMCP注入は空のまま。Enterprise、AgentRunScope、Agent Team、明示的native MCP deny、Filesystem/Search OFF、tools OFFの制限を維持。

実ソースの新規回帰16件PASS。独立レビューで同じ16件を再実行しPASS。既存モデル8件PASS、対象lint PASS。Agent Teamの実際のrun/run_asyncのrole設定とthread context伝播を正本で確認し、旧contract fixtureにroleを追加した。

## 2026-10-16: VM契約検証の完了

現行main `c38f0be8dcea7fe443ff4ffd54ebb8197e270e5f` の対象ソースを照合し、前回不足していたruntime registry等の依存ソースをVMへ補完した。既存venvに軽量Python依存を追加し、大型モデル・音声・GPU依存は導入していない。productionコードへの追加変更はない。

- `tests/test_runtime_feature_gate_contract.py`: 96件PASS（既存93件とAntigravity回帰3件）。モジュールの全ケースを実行し、skip/deselectなし。
- `tests/test_cli_mcp_integration_scope.py`: 16件PASS。上記と合同で112件PASS。
- 対象capability実装と上記2テストのRuff、Python compile検証: PASS。
- 新規3件は独立レビューでもPASS、指摘なし。

追加回帰は `mcp_enabled=False / "false" / 0` で「今何時？」を実際の `CLILLMClient` preflightとAntigravity wrapperへ渡し、provider実行境界だけをmockする。AoiTalk登録serverと古いfallback serverが注入されず、native MCP禁止引数を追加せずに実行へ進み、設定も変更されないことを確認する。AoiTalk側の `call_mcp_tool` は引き続き無効である。

合同実行では一時HOME/CODEX_HOMEを使用し、Python socket接続を禁止した。接続試行は0件。契約に含まれるCodexの3ケースは `mcp list --json` による構文解析だけで、モデル生成・MCP server起動・ツール実行はしない。警告は既存 `google.generativeai` 非推奨の1件。

この結果は対象contractと静的検証の完了を示す。VMは部分ソースと軽量依存のため、全backend suiteや任意機能の統合確認を実施したことにはならない。実Antigravity CLIのログイン・モデル応答、稼働中AoiTalkへの反映、Web/Androidからの実際の通常チャット受入は未実施。MCP設定やCLI固有の保存設定を変更せず、Core担当の実行ループにも変更していない。
