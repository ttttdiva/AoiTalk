# Agent task triage design

Updated: 2026-05-15

## Goal

When new tasks are created in AoiTalk, the AoiTalk agent should proactively
organize them into an executable form: intent, unknowns, investigation needs,
implementation unit, and Codex handoff summary.

This is separate from the already-completed Codex task-reading flow.

## Metadata

Store triage state in `tasks.task_metadata`:

```json
{
  "agent_triage_status": "pending",
  "agent_triage_summary": "",
  "agent_triage_questions": [],
  "agent_triage_checked_at": null,
  "agent_triage_run_id": null,
  "agent_triage_error": null
}
```

Allowed statuses:

- `pending`
- `in_progress`
- `needs_user`
- `ready`
- `done`
- `failed`

## Target selection

Include:

- active tasks with no `agent_triage_status`
- active tasks with `agent_triage_status=pending`
- user-created tasks

Exclude:

- closed/cancelled/deleted/archived tasks
- tasks with `task_metadata.agent_triage_disabled=true`
- tasks already triaged in the same run

## Worker flow

1. Lock a small batch by setting `agent_triage_status=in_progress`.
2. Build task context: project, title, description, source, comments, tags.
3. Ask the project-management agent to produce:
   - intent
   - target surface: web / mobile / backend / docs / unknown
   - category: bug / feature / refactor / investigation / release / maintenance
   - uncertainty
   - user questions
   - next executable unit
   - Codex handoff summary
4. Write the output back to `task_metadata`.
5. Add or update one triage comment. Do not create duplicate comments.
6. On failure, set `agent_triage_status=failed` and call automatic failure
   recording with `operation=agent_task_triage`.

## UI

Task list:

- `要確認` when status is `needs_user`
- `整理済み` when status is `ready`
- `失敗` when status is `failed`

Task detail:

- Show triage summary and questions.
- Provide `新規タスクを整理` / `再整理` action.
- Keep the existing `Run with agent` button.

## Heartbeat

The heartbeat should run project-level batches and avoid duplicate comments by
using the stored `agent_triage_run_id` and a deterministic triage comment marker.

## Dependency

This feature should call the automatic failure recorder introduced for
`auto_failure` feedback so failed worker runs are visible in Settings >
Feedback.
