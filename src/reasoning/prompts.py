"""Prompt templates used by the reasoning subsystem."""

COMPLEXITY_EVALUATION_PROMPT = """
Evaluate the complexity of the following user request.

User input: {user_input}

利用可能なツール:
{available_tools}

Return JSON with these fields:
1. multi_tool_score (0.0-1.0)
2. dependency_score (0.0-1.0)
3. conditional_score (0.0-1.0)
4. transformation_score (0.0-1.0)
5. reasoning (string)

Example:
{{
    "multi_tool_score": 0.8,
    "dependency_score": 0.6,
    "conditional_score": 0.2,
    "transformation_score": 0.4,
    "reasoning": "The request needs multiple tools and depends on combining their results."
}}
"""


TASK_DECOMPOSITION_PROMPT = """
Break the following user request into concrete execution steps.

User input: {user_input}

利用可能なツール:
{available_tools}

Conversation context:
{context}

Return JSON with this structure:
{{
    "steps": [
        {{
            "id": "step_1",
            "description": "Describe the step",
            "tool_requirements": ["tool_name"],
            "dependencies": [],
            "expected_output": "Describe the output"
        }}
    ]
}}
"""


TOOL_SELECTION_PROMPT = """
Select the most appropriate tools for the task below.

Task description: {task_description}
Task type: {task_type}

利用可能なツール:
{available_tools_with_descriptions}

Selection rules:
- 情報検索には `search_memory` またはWeb検索を使う。
- タスク/TODO、案件情報Docs、内部 WBS.dbtable、
  follow-up planning には `list_project_information`、`list_record_tables`、
  `get_upcoming_wbs_tasks`、`create_record_table`、`append_record_rows`、
  `create_task`、`patch_project_information_doc` などの直toolを使う。
  外部WBS Excelを内部 WBS.dbtable に取り込む時だけ `sync_wbs_tasks` を使う。
- 案件情報Docsを書く前に `list_project_information` で正本を読み、既存見出しを尊重して
  `patch_project_information_doc` の section_heading / operation / change_summary /
  source_refs_json を使う。根拠のない断定は本文ではなく要確認またはQ&A candidateに回す。
- Spotify操作には `spotify_assistant` を使う。
- ローカルファイル作業には `find_workspace_items`、
  `inspect_workspace_tree`、`read_workspace_file`、`view_file`、
  `search_files` などの直filesystem toolを使う。
- 時刻、天気、計算には `utility_assistant` を使う。
- 画像生成やYouTube/NicoNico再生には `media_assistant` を使う。
- インストール済みskillが適切な場合だけ `invoke_skill` を使う。

Return JSON:
{{
    "selected_tools": ["tool_name"],
    "reasoning": "Why these tools were selected"
}}
"""


STEP_EXECUTION_PROMPT = """
Execute the following step with the required tools.

Step: {step_description}
Required tools: {required_tools}

Previous step results:
{previous_results}

Shared context:
{shared_context}
"""


RESPONSE_GENERATION_PROMPT = """
Generate the final user-facing response from the execution results.

Original user input: {user_input}

Execution plan:
{execution_plan}

Execution results:
{execution_results}

Response requirements:
1. Answer the user's request directly.
2. Summarize important results clearly.
3. Mention errors only when relevant.
4. Use concrete facts rather than vague statements.
"""


ERROR_RECOVERY_PROMPT = """
One execution step failed. Propose a recovery plan.

Failed step: {failed_step}
Error: {error_message}
利用可能なツール: {available_tools}

Return JSON:
{{
    "alternative_approach": "Describe the fallback",
    "new_steps": [
        {{
            "description": "Describe the new step",
            "tool_requirements": ["tool_name"]
        }}
    ],
    "explanation": "Why this fallback should work"
}}
"""


PROGRESS_TEMPLATES = {
    "analyzing": "Analyzing the task...",
    "planning": "Building an execution plan...",
    "plan_display": """Execution plan:
{plan_steps}""",
    "executing": "Executing steps... ({current}/{total})",
    "steering_applied": "追加指示を反映します",
    "step_complete": "Completed: {step_description}",
    "step_failed": "Failed: {step_description}: {error}",
    "complete": "Done: {summary}",
    "partial_complete": "Partially complete: {summary}",
}


def format_plan_steps(steps):
    """Format execution plan steps for display."""
    formatted = []
    for i, step in enumerate(steps, 1):
        deps = f" (dependencies: {', '.join(step.dependencies)})" if step.dependencies else ""
        formatted.append(f"  {i}. {step.description}{deps}")
    return "\n".join(formatted)


def format_execution_results(results):
    """Format execution results for display."""
    formatted = []
    for step_id, result in results.items():
        if result.success:
            formatted.append(f"- {step_id}: success")
        else:
            formatted.append(f"- {step_id}: failed ({result.error})")
    return "\n".join(formatted)
