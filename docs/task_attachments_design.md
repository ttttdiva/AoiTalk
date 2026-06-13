# Task attachments design

Updated: 2026-05-15

## Goal

Allow webui and mobile users to attach images and files to tasks. Files live
under the project workspace, while the database stores the task-to-file
relationship.

## Storage

Recommended path:

```text
workspaces/_projects/project_<project_id>/attachments/tasks/<task_id>/
```

This keeps task attachments separate from `management/`, which is used for WBS,
issues, risks, and request files.

## Data model

Table: `task_attachments`

- `id uuid primary key`
- `task_id uuid not null references tasks(id) on delete cascade`
- `project_id uuid not null references projects(id) on delete cascade`
- `file_path text not null`
- `display_name varchar(255) not null`
- `mime_type varchar(120)`
- `size_bytes integer`
- `kind varchar(16) not null` with values `image | file`
- `created_by uuid references users(id)`
- `created_at timestamp default now()`
- `attachment_metadata json default {}`

Uniqueness recommendation:

- unique `(task_id, file_path)`

## API

- `GET /api/tasks/{taskId}/attachments`
- `POST /api/tasks/{taskId}/attachments`
- `DELETE /api/tasks/{taskId}/attachments/{attachmentId}`

POST multipart fields:

- `file`
- optional `display_name`
- optional `kind`

Server behavior:

1. Load task and verify the current user can read/write the task project.
2. Resolve destination to `attachments/tasks/<taskId>`.
3. Reject blocked executable/script extensions using the same policy as project
   management file upload.
4. Write a unique filename into the destination directory.
5. Insert the DB row with `project_id` from the task, not from client input.

## Web UI

Task detail modal:

- Add an attachments section near comments.
- Support file input and drag/drop.
- Show image thumbnails for `image/*`.
- Show filename, size, delete button, and open/download link.

Task list:

- Show only an attachment count badge.

## Mobile UI

Task detail screen:

- Add `画像を添付` and `ファイルを添付`.
- Use image picker for images and document picker for files.
- Show thumbnails when the attachment is an image.
- Upload online only in the first implementation. Offline pending upload can be
  added later and should integrate with the sync outbox.

## Task deletion

Initial implementation should cascade-delete DB rows through `task_id`, but it
does not need to physically delete files immediately. A later cleanup job can
scan `attachments/tasks/<task_id>` folders with no DB rows and remove or archive
them. This avoids accidental data loss during early rollout.

## Failure recording

Upload failures should call automatic failure recording with:

- source: `webui` or `mobile`
- operation: `task_attachment_upload`
- task_id
- project_id
- error_type / error_message
- input_summary with file name, size, mime type only

No file contents or auth headers should be recorded.
