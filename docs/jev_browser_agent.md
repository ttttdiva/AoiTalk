# 既存EdgeのBrowser AgentとJev判断

一般Browser Agentは [ポータブルPC Bridge](pc_bridge.md) で選択したPCの既存Edgeを操作する。サーバーPC固定のローカルPythonホスト方式は廃止した。

`browser_agent(goal, start_url?, tab_id?, device_id?, completion_text?, completion_url?)` は、接続中PCをユーザー設定から解決し、そのPCのEdge拡張へDOM観測と操作を送る。Cookieや保存パスワードをコピーせず、普段のログイン済みセッションを使う。選択PCが未接続なら停止し、隔離ブラウザやサーバーPCへfallbackしない。

Jevは現在の候補からの選択を担当し、キー未設定・無効・timeout・rate-limit・低confidence等では現在の会話LLMへ切り替わる。ページ・タブを初期化する処理はない。同じ判断層をWindows Computer UseのUI Automation候補にも使用する。

ブラウザ専用サイトallowlist、一般操作確認dialog、リクエスト横取り、結果を固定するreplay cacheは設けない。内部QA BrowserとChatGPT Directorは別機能として維持する。

設定の `browser_agent` は enabled / jev_enabled / max_steps / timeout_seconds / jev_model。Jevキーはサーバーの既存 `.env` の `JEV_API_KEY` を使用し、Bridgeの接続設定やEdge拡張へ渡さない。

インストール、通信、操作先選択、exeのビルド、検証結果・未検証範囲は [PC Bridgeガイド](pc_bridge.md) を正本とする。
