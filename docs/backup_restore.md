# AoiTalk disaster-recovery backup / restore

This procedure corresponds to:

- AoiTalk baseline `744c8474e9c70a59c4509fbf9d5e5ef17d4e64f9`
- BackupOrchestrator baseline `18c3bd832a19a7ab5397e118e9c01a86a12285e9`

The tracked source contract is `backup/aoitalk-backup.yaml`. It contains path
resolution rules only, never credential values.

## Protected state

- PostgreSQL logical database dump.
- `AOITALK_WORKSPACES_DIR` / `workspaces` including managed Docs, Project, User,
  App, App Instance, and App Artifact storage.
- AoiTalk `data` root, notably character voice assets/captures.
- generated media.
- skill recordings.
- MioTTS references and presets.
- Irodori references.
- ComfyUI user workflows.

The path-resolving sources use AoiTalk's environment/DB configuration where the
pinned implementation supports it. The manifest records the resolved source
path and resolution origin.

## Explicitly not protected

- `.aoitalk-local-llm` runtime binaries/model weights.
- virtual environments and dependency trees.
- frontend/mobile build output.
- caches, logs, temp/test artifacts.
- plaintext secret stores, `.env`, credentials/tokens, keys/certs.

Those are either reconstructable or must be re-provisioned securely.

**Critical:** if restored PostgreSQL contains field-encrypted values and you
need to decrypt them, re-provision the same `AOITALK_FIELD_CRYPTO_KEY_B64` from
an external secret store. The backup intentionally does not upload that key.

## Consistency boundary

Backup/restore is offline with respect to AoiTalk, not PostgreSQL:

1. Stop AoiTalk, frontend writers, app workers/jobs, and any other process that
   can mutate AoiTalk DB/files.
2. Keep PostgreSQL running.
3. Run the BackupOrchestrator command with `--offline-confirmed`.
4. Restart AoiTalk only after success.

This ensures DB and filesystem are stable across the snapshot without claiming
an online cross-resource transaction that AoiTalk does not currently expose.

## Private Hugging Face repository

Configure a private dataset repository:

```powershell
$env:AOITALK_BACKUP_HF_REPO = "ACCOUNT/PRIVATE_BACKUP_REPO"
$env:AOITALK_BACKUP_HF_ACCOUNT = "BACKUP"
$env:HF_TOKEN_BACKUP = "hf_..."
$env:AOITALK_BACKUP_PROFILE = "desktop-main"  # optional
```

The command refuses a public repository.

## Create a backup

From the BackupOrchestrator checkout:

```powershell
python main.py aoitalk backup `
  --aoitalk-root C:\path\to\41_AoiTalk `
  --offline-confirmed
```

The result JSON is the immutable complete snapshot manifest. It includes Git
SHAs/dirty flags, contract hash, DB metadata, filesystem source paths/counts,
and artifact hashes.

## Verify/list from remote only

Local `.backup_state` is disposable. After deleting it, these commands still
use remote manifests:

```powershell
python main.py aoitalk list `
  --aoitalk-root C:\path\to\41_AoiTalk

python main.py aoitalk verify `
  --aoitalk-root C:\path\to\41_AoiTalk `
  --snapshot-id <SNAPSHOT_ID>
```

`LATEST.json` is not trusted for recovery; remote complete manifests are
enumerated directly.

## Restore after total drive/OS loss

1. Install PostgreSQL and put `pg_dump`, `pg_restore`, `psql`, `dropdb`, and
   `createdb` on `PATH`.
2. Clone AoiTalk and BackupOrchestrator. Prefer the source SHAs recorded in the
   selected snapshot.
3. Re-provision excluded secrets and provider credentials externally.
4. Configure the target `POSTGRES_*` and filesystem environment variables.
5. Stop AoiTalk; keep PostgreSQL running.
6. `aoitalk list` and `aoitalk verify` the selected snapshot.
7. Restore:

```powershell
python main.py aoitalk restore `
  --aoitalk-root C:\path\to\41_AoiTalk `
  --snapshot-id <SNAPSHOT_ID> `
  --offline-confirmed `
  --recreate-database `
  --overwrite-files
```

`--overwrite-files` is needed only when target directories are non-empty.
Existing roots are preserved as `<name>.pre-restore-<snapshot>`.

If the application PostgreSQL role cannot drop/create databases, provide
restore-only admin credentials:

```powershell
$env:POSTGRES_ADMIN_USER = "postgres"
$env:POSTGRES_ADMIN_PASSWORD = "..."
```

They are passed to PostgreSQL through the child process environment, not command
arguments or snapshot metadata.

## Relocating DB-configured filesystem paths

If a snapshot source was resolved from a DB config value, restore fails closed
unless the destination is made explicit. This prevents blindly recreating an
old drive path.

For Irodori and ComfyUI use backup-specific relocation variables:

```powershell
$env:AOITALK_BACKUP_IRODORI_REFS_DIR = "E:\AoiTalk\irodori_refs"
$env:AOITALK_BACKUP_COMFYUI_WORKFLOWS_DIR = "E:\AoiTalk\comfyui_workflows"
```

After DB restore, BackupOrchestrator rewrites the corresponding DB config leaf
to the new target.

For sources that AoiTalk itself supports by environment variable, set the real
AoiTalk override, e.g.:

```powershell
$env:AOITALK_SKILL_RECORDINGS_DIR = "E:\AoiTalk\skill_recordings"
$env:AOITALK_MIOTTS_REFS_DIR = "E:\AoiTalk\miotts_refs"
$env:AOITALK_MIOTTS_PRESETS_DIR = "E:\AoiTalk\miotts_presets"
```

## Retention

```powershell
python main.py aoitalk prune `
  --aoitalk-root C:\path\to\41_AoiTalk `
  --keep 7
```

Prune withdraws the completion marker first. If artifact deletion later fails,
the remainder is an ignored orphan rather than a broken complete snapshot.
