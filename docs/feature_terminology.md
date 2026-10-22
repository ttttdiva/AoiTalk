# 機能名・identity・実行境界の契約

UI・現行文書・今後の実装では以下の名称を使う。名称整理は保存データや実行権限の移行ではない。履歴文書の当時の表記は変更しない。

## 正式名称

| 名称 | 定義と境界 |
| --- | --- |
| Agent | 仕事を担う永続的なidentity。従来の製品名「AI社員」に対応する既存 `Agent`。認証済みhuman `User`とは別で、identityだけでは実行権限を持たない。 |
| AgentRevision | Agentの職務の固定版。mission / instructions、Execution Team / Profile参照、許可Subagent、capability ceilingやpolicyを持つ。進行中の仕事は正確なrevisionをpinする。 |
| Agent組織 | Agent間の所属・指揮関係と既存Spaceへの所属。上司は `AgentOrganizationProfile.manager_agent_id`。Execution Teamの構成員一覧とは別。 |
| Execution Team | 従来表示名「Agent Team」。Subagent構成とExecution Profileをまとめた実行設定。複数AgentやChatが同じ定義を参照できる。Agent専用の配下組織ではない。内部名 `agent_team` は維持する。 |
| Subagent | Execution Teamの実行役割。Agent identityや所属先ではない。runnerにより指示・tools・model routeの使い方が異なる。 |
| Execution Profile | Execution Teamに属するmodel-routing設定。Default Subagent Routeと個別overrideを持つ。組織や職務版、権限付与とは別。 |
| Character | 会話側のキャラクター定義。外見・声に加え、prompt / model / toolsを用いる会話実行経路がある。Agentの `character_id` はpresentation linkであり、Characterの設定をそのままAgentの職務・権限として継承しない。 |
| Persona（発信人格） | MediaOpsの公開人格・ブランドidentity。UI上の主名はPersona、内部は `Persona` / `media_personas`。Media Personaは会話Characterと区別する文脈上の補足名で、別のidentityではない。関連するProject / owner / assignmentの権限境界を持つ。 |
| Media Character（既存表記） | Media側のslot-freeなPersonaを指す既存UI/API表記。legacy intake slotを持たないPersonaのsubsetであり、すべてのPersonaの別名でも、会話Characterでもない。新しい説明ではPersonaを用い、必要時にMedia側・slot-freeであることを補足する。 |
| AgentWorkItem | 永続的な仕事単位。queue、lease、retry、settlementを管理する。 |
| AgentRun | 1回の実行試行と証拠。WorkItemに複数の試行が紐づく。既存のhuman/conversation Runも維持する。 |
| Agent Harness | 実行を調整する既存の基盤。Agent identity、Execution Team、組織の名称へ置き換えない。 |

## 実行経路を混同しない

- AgentRevisionに基づくOperationsの仕事は、固定AgentRevision → Execution Team → Execution Profile → Subagent → provider / model / effortでrouteを解決する。開始時のRun manifestをretryでも維持する。Mediaの `DefaultAutomationPlanner` など別経路もあり、すべてのOperations処理がAgent identityを経由するわけではない。
- Chatは同じExecution Team / Subagent定義を参照できるが、Operationsの汎用委任とrunnerは異なる。汎用委任はSubagentを主にroute選択に使い、職務指示・委任内容・採用済み成果物を別途構成する。Subagentの指示やtoolsが一式で継承されるとは説明しない。
- 汎用委任のモデル生成はtool-freeで、提供資料の整理・執筆・評価を行う。Web検索、build、test、外部投稿を実施した証拠にはならない。開発仕事の `code_worker` は既存native adapterと検証証拠を用いる別経路。
- 会話Character自体にはprompt / model / toolsの実行経路がある。「Characterは見た目だけ」は誤り。Agent側のpresentation linkが与える意味とは分けて説明する。
- Media Personaは公開人格とscopeのidentity。Persona本文を全Agentのpromptへ自動注入するという共通契約はない。各adapterで実際に渡されるcontextを確認する。
- `Agent.state`（`draft` / `active` / `paused` / `retired`）と `employment_state`（`active` / `on_leave` / `suspended` / `terminated` / `contractor`）は別状態として維持する。名称だけを理由に統合しない。

## 互換性と変更範囲

表示ラベルと説明文を変更し、ID、保存済み名称、DB table / column、API path、operationId、JSON key、enum、設定key、tool名、pending workflow keyを一括置換しない。例: `agent_team` / `agent_team_id`、`employee_delegation` / `employees`、`persona_id` / `character_id` は現行契約を保持する。既存のemployee系ファイル名も改名を必要としない。

会話CharacterとMedia PersonaのIDを同一視しない。AgentのCharacter選択肢と保存先IDの不一致は別の不具合残件であり、この用語契約だけでは解消しない。選択・保存・readbackを確認する挙動修正として別途扱う。

## 実装の照合先

| 確認対象 | 正本・関連テスト |
| --- | --- |
| Agent / revision / 組織 / 二状態 | `src/memory/models/agent_identity.py`、`src/api/agent_identity_routes.py`、`tests/test_agent_identity_foundation.py` |
| Execution Team / Subagent / Profile | `src/services/agent_team_v3.py`、`src/services/agent_team_service.py` |
| Agent routeとmanifest | `src/services/agent_model_routing.py`、`tests/test_agent_model_routing.py` |
| 汎用委任のcontext・tool-free境界 | `src/services/agent_delegation_runtime.py`、`tests/test_agent_delegation_workflow.py` |
| 会話Characterの実行 | `src/services/character_service.py`、`src/llm/specialist_delegate.py`、`tests/test_character_and_agent_team_routing.py` |
| Media Personaとslot-free subset | `src/services/media_operations_service.py` の `list_characters` / `create_character` / `list_personas`、`src/memory/models/media_operations.py` |
| 実行基盤 | `src/agent_harness/orchestrator.py` |

詳細は [Agent domain model](agent_domain_model.md)、[Agent platform](ai_employee_platform.md)、[Agent組織と委任workflow](employee_delegation_workflow.md) を参照する。実装・schema・テストの契約を確認せず、名称から権限やruntime機能を推測しない。
