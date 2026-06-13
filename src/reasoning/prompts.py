"""Prompt templates used by the reasoning subsystem."""

COMPLEXITY_EVALUATION_PROMPT = """
Evaluate the complexity of the following user request.

User input: {user_input}

Available tools:
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

Available tools:
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

Available tools:
{available_tools_with_descriptions}

Selection rules:
- Use `search_memory`, `knowledge_search`, or web search for information lookup.
- Use `project_management_assistant` for task/TODO work, project/case information, project DB facts/documents, WBS checks, and follow-up planning.
- Use `spotify_assistant` for Spotify work.
- Use `filesystem_assistant` for local file work.
- Use `utility_assistant` for time, weather, or calculation requests.
- Use `media_assistant` for image generation or YouTube/NicoNico playback.
- Use `skills_assistant` when an installed skill should handle the task.

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
Available tools: {available_tools}

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
