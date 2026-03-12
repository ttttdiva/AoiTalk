"""推論モードで使用するプロンプトテンプレート"""

# 複雑度評価用プロンプト
COMPLEXITY_EVALUATION_PROMPT = """
以下のユーザー入力の複雑度を評価してください。

ユーザー入力: {user_input}

利用可能なツール:
{available_tools}

以下の観点で評価し、JSON形式で回答してください:
1. multi_tool_score (0.0-1.0): 複数のツールが必要か
2. dependency_score (0.0-1.0): タスク間に依存関係があるか
3. conditional_score (0.0-1.0): 条件分岐が必要か
4. transformation_score (0.0-1.0): データの変換・加工が必要か
5. reasoning (string): 評価の理由

回答例:
{{
    "multi_tool_score": 0.8,
    "dependency_score": 0.6,
    "conditional_score": 0.2,
    "transformation_score": 0.4,
    "reasoning": "メモリ検索とClickUp MCP登録の2つのツールが必要で、検索結果を登録に使用する依存関係がある"
}}
"""

# タスク分解用プロンプト
TASK_DECOMPOSITION_PROMPT = """
以下のユーザー入力を実行可能なステップに分解してください。

ユーザー入力: {user_input}

利用可能なツール:
{available_tools}

現在のコンテキスト:
{context}

以下の形式でステップを定義してください:
1. 各ステップは単一の明確なアクションであること
2. ステップ間の依存関係を明確にすること
3. 必要なツールを特定すること

JSON形式で回答してください:
{{
    "steps": [
        {{
            "id": "step_1",
            "description": "ステップの説明",
            "tool_requirements": ["必要なツール名"],
            "dependencies": [],
            "expected_output": "期待される出力の説明"
        }},
        ...
    ]
}}
"""

# ツール選択用プロンプト
TOOL_SELECTION_PROMPT = """
以下のタスクに最適なツールを選択してください。

タスク説明: {task_description}
タスクタイプ: {task_type}

利用可能なツール:
{available_tools_with_descriptions}

選択基準:
1. タスクの目的に最も適したツールを選ぶ
2. 複数のツールが必要な場合はすべて列挙する
3. ツールの説明を参考に、実際の機能を理解して選択する

以下の点を考慮してください:
- 「調べる」「検索する」場合:
  - 過去の会話や記憶を探す → search_memory
  - 技術文書や知識を探す → search_rag  
  - 最新情報やイベント、一般的な情報 → WebSearch
- 「音楽」「再生」「Spotify」関連 → spotify_assistant
- 「タスク」「予定」「ClickUp」関連 → use_mcp_tool (server_name='clickup')
- 「ローカルファイル」「編集」「リファクタリング」操作 → execute_file_operation (OS操作ツール)
- 「天気」「天候」情報 → get_weather

JSON形式で回答してください:
{{
    "selected_tools": ["ツール名1", "ツール名2"],
    "reasoning": "選択理由の説明"
}}
"""

# ステップ実行用プロンプト
STEP_EXECUTION_PROMPT = """
以下のステップを実行してください。

ステップ: {step_description}
必要なツール: {required_tools}

前のステップの結果:
{previous_results}

共有コンテキスト:
{shared_context}

このステップの目的を達成するために、適切なツールを使用して実行してください。
"""

# 最終応答生成用プロンプト
RESPONSE_GENERATION_PROMPT = """
以下の実行結果を基に、ユーザーへの最終的な応答を生成してください。

元のユーザー入力: {user_input}

実行計画:
{execution_plan}

実行結果:
{execution_results}

以下の点に注意して応答を生成してください:
1. 元の質問に対する明確な回答を含めること
2. 重要な結果を強調すること
3. エラーがあった場合は適切に説明すること
4. 自然で分かりやすい日本語で応答すること
"""

# エラーリカバリー用プロンプト
ERROR_RECOVERY_PROMPT = """
以下のステップでエラーが発生しました。代替案を提案してください。

失敗したステップ: {failed_step}
エラー内容: {error_message}
利用可能なツール: {available_tools}

代替案を以下の形式で提案してください:
{{
    "alternative_approach": "代替アプローチの説明",
    "new_steps": [
        {{
            "description": "新しいステップの説明",
            "tool_requirements": ["必要なツール"]
        }}
    ],
    "explanation": "なぜこの代替案が有効か"
}}
"""

# 進捗表示用テンプレート
PROGRESS_TEMPLATES = {
    "analyzing": "🤔 タスクを分析中...",
    "planning": "📋 実行計画を作成中...",
    "plan_display": """📋 実行計画:
{plan_steps}""",
    "executing": "🔄 実行中... ({current}/{total})",
    "step_complete": "✅ {step_description}",
    "step_failed": "❌ {step_description}: {error}",
    "complete": "✨ 完了: {summary}",
    "partial_complete": "⚠️ 部分的に完了: {summary}"
}


def format_plan_steps(steps):
    """実行計画のステップをフォーマット"""
    formatted = []
    for i, step in enumerate(steps, 1):
        deps = f" (依存: {', '.join(step.dependencies)})" if step.dependencies else ""
        formatted.append(f"  {i}. {step.description}{deps}")
    return "\n".join(formatted)


def format_execution_results(results):
    """実行結果をフォーマット"""
    formatted = []
    for step_id, result in results.items():
        if result.success:
            formatted.append(f"- {step_id}: ✅ 成功")
        else:
            formatted.append(f"- {step_id}: ❌ 失敗 ({result.error})")
    return "\n".join(formatted)