# Web版 ChatGPT Director モード

AoiTalk の Director モードは、Web版 ChatGPT を統括役（Director）、AoiTalk のツール付き agent を実行役（Operator）として使う経路です。ChatGPT の非公開 API、認証 token、Cookie を読み取って呼び出す方式ではなく、専用 browser profile を Playwright で操作します。

## 実装の正本

UI 文言や状態名は変わり得るため、詳細な selector / browser automation は現在の `ChatGPTWebProvider` と設定 UI を正本とします。この文書では利用手順と security boundary を説明します。

## セットアップ

AoiTalk の venv に project dependencies が入り、Playwright Chromium が利用できることを確認します。

```powershell
.\venv\Scripts\python.exe -m pip install -e .
.\venv\Scripts\python.exe -m playwright install chromium
```

通常は `setup.bat` / [setup_guide.md](setup_guide.md) で作った venv を使います。個人 PC 固有の絶対 checkout path を設定例へ固定しません。

## 初回ログイン

1. AoiTalk の設定画面で LLM / Agent Team の ChatGPT 接続設定を開く。
2. ChatGPT 設定 browser を開く。
3. 専用 browser profile 上で利用者自身が ChatGPT へログインする。
4. ChatGPT 側で利用したい model / mode を選ぶ。
5. 設定 browser を閉じる。
6. AoiTalk の接続テストを実行する。

AoiTalk は CAPTCHA、bot challenge、account restriction、login confirmation を自動回避しません。確認が必要な状態では処理を止め、専用設定 browser で利用者が正規の操作を完了します。

設定 browser と Director runtime は同じ profile を共有するため、同時使用しません。

## Director を使う

設定画面で orchestration mode を `director` にし、必要な timeout / turn limit 等を保存します。`standard` に戻すと通常の AoiTalk main agent が直接応答します。

Director は AoiTalk session ごとに ChatGPT conversation URL を保持・再利用します。保存 URL を再利用できない場合の recovery は provider 実装を正本とし、古い DOM selector や画面文言をこの文書へ固定しません。

## Failure boundary

次の状態では無理に続行しません。状態を「人間による確認が必要」と
「Web UI の自動操作に失敗」に分け、エラーメッセージに含まれる単語だけで
人間操作を要求する判定は行いません。

- **人間による確認が必要 (`ChatGPTWebNeedsHumanError`)**
  - 未ログイン / login expired
  - CAPTCHA、bot challenge、account restriction など、人間の確認が必要な page
  - Web Director は `director.needs_human` と既存の人間向け案内を記録し、外部 CLI は exit code `2` を返します。
- **Web UI 自動操作エラー (`ChatGPTWebUIInteractionError`)**
  - composer、送信/添付ボタン、file chooser、selector、Playwright 操作などの失敗
  - エラー文に「ログイン」「人間確認」が含まれていても、人間必須とは再分類しません。
    Web Director では通常の失敗として扱い、外部 CLI は exit code `1`、
    `check-login` API は `503` を返します。
- profile が別 process に使用されている
- timeout / turn limit

会話履歴のレート制限など、既知の blocking modal は provider が対象の dialog を
検出して「了解」等の dismiss ボタンを自動で押し、送信を継続します。自動で閉じられない、
または安全に対象を特定できない場合は `ChatGPTWebUIInteractionError` として停止し、
人間ログイン要求には変換しません。`needs_human` は明示的な
`ChatGPTWebNeedsHumanError` に限った状態名です。

## セキュリティ

- 通常の `OPENAI_API_KEY` を browser へ返す方式ではありません。
- 専用 profile には login state が保存されるため、共有 folder へ置かないでください。
- attachment を送る経路は、現在の workspace containment / user/project permission を通します。
- private API や Cookie 値を prompt/log へ露出させないでください。

### Director Browser と QA Browser

Director 用の persistent profile/process は親 Controller 専用です。Agent
Team worker は Director provider、profile、ChatGPT origin、MCP 接続を取得
できません。WebUI の確認が必要な場合は、親が
`src/security/qa_browser_transport.py` の `launch_playwright_qa_transport`
で別 profile を起動し、`QABrowserRegistry` から opt-in
`ui_qa_worker` に opaque capability facade だけを渡します。QA lane は
親が登録した HTTP(S) origin 以外、`chatgpt.com`、`file://`、未許可の
localhost、scope 外 upload/download を拒否します。worker に raw page、
Playwright context、profile path、credential を渡してはいけません。

QA 操作は navigate / action / snapshot / bounded wait / upload / download
へ分割し、各呼び出しの timeout と run lifecycle を適用します。外部調査
や Director への相談は QA worker ではなく親 Controller の ChatGPT Web
conversation に戻します。

QA laneをproduction Director runへ有効化する場合は、親設定で
`agent_operator.qa_browser_enabled=true` と
`agent_operator.qa_allowed_origins`（明示したHTTP(S) originの配列）を指定
します。対象repositoryは別途 `agent_operator.repository_root` または
`AOITALK_OPERATOR_REPOSITORY_ROOT` で固定します。未設定時にProjectの
`workspace_root`やモデル本文から推測してscopeを作ることはありません。

## 外部 CLI

AoiTalk UI の Director mode とは別に、外部 Operator から provider を利用する CLI があります。

- `scripts/chatgpt_web_director.py`
- `scripts/chatgpt_web_director.ps1`

checkout 固有の `D:\...` を文書に埋め込まず、repository root から実行します。

```powershell
.\venv\Scripts\python.exe .\scripts\chatgpt_web_director.py status
.\scripts\chatgpt_web_director.ps1 status
.\scripts\chatgpt_web_director.ps1 send --session $env:GROK_SESSION_ID --file msg.txt
.\scripts\chatgpt_web_director.ps1 new --session $env:GROK_SESSION_ID
```

別 working directory から呼ぶ必要がある場合は、script 自身の root 解決または `AOITALK_ROOT` を使います。

主な command:

- `status`: login / profile / Playwright 状態を確認
- `send`: message を送って確定 reply を待つ
- `new`: 保存 conversation URL を破棄
- `send --new`: 新規 conversation で送信

exit code や JSON field は CLI 実装を正本とします。

## 手動 smoke

専用 profile で login 済みかつ設定 browser を閉じた状態で:

```powershell
.\venv\Scripts\python.exe tests\manual\chatgpt_web_smoke.py
```

WebUI behavior を変更する実装作業では、この smoke だけで独立 AI browser QA を代替しません。[ai_webui_qa.md](ai_webui_qa.md) と `AGENTS.md` の gate に従います。
