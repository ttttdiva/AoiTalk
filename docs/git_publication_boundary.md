# Parent-only Git publication boundary

`src/security/git_publication_gate.py` is the narrow GitHub-first boundary for
an AoiTalk agent run.  It is a **preflight contract**, not a Git transport:
the module never runs `commit`, `push`, `reset`, or `clean`.

## Run start

The parent controller captures a `RunSnapshot` before workers are started:

```python
from src.security.git_publication_gate import RunSnapshot

snapshot = RunSnapshot.capture(repository_root, run_id=run_id)
```

The snapshot stores the canonical Git root, repository identity (the `origin`
URL when present), baseline `HEAD`, branch, porcelain status, modified files,
staged files, untracked files, and a worktree digest for baseline paths.  The
baseline is immutable for the lifetime of the run.  Existing user changes are
therefore not mistaken for worker output.

## Worker boundary

Workers do not receive a publication transport.  `worker_publication_decision`
always returns a denial and `assert_worker_publication_denied` raises.  The
runtime tool registry also removes commit/push/reset/clean tools from child
workers.  A worker report can describe changed scope, but it cannot grant
itself authority.

## Parent review and publication preflight

After the Director (or other explicitly configured parent reviewer) approves,
the parent performs a non-mutating gate check:

```python
from src.security.git_publication_gate import GitPublicationGate

gate = GitPublicationGate(snapshot, allowed_paths=worker_changed_paths)
decision = gate.assert_publishable(
    review={"approved": True, "review_id": director_review_id},
    review_ref=director_review_id,
)
```

The decision is structured and serialisable (`decision.as_dict()`).  It
contains the baseline/current heads and status, introduced paths,
pre-existing paths preserved, publishable paths, rejected paths, review
metadata, and the reason for an allow/deny result.

The gate denies when:

* explicit parent review is missing or the caller is not the parent;
* current `HEAD` differs from the run baseline (including a worker commit or
  history rewrite);
* a worker reports commit, push, force-push, reset, clean, branch-delete, or
  rebase activity;
* a baseline user path was changed, removed, or has a different worktree
  digest;
* the current diff or requested changed scope resolves outside the canonical
  repository/run scope (including symlink/junction escapes); or
* no run-introduced path remains to publish.

`allowed_paths`/`changed_scope` should come from the parent’s accepted worker
report.  Passing it is how unrelated files in the same checkout are rejected.
The parent may then invoke its separately audited commit/push transport using
only `decision.publishable_paths`; this module intentionally does not perform
that mutation.

## Safety invariant

Do not use `git add -A` or a whole-repository commit after a successful gate.
Stage exactly the returned publishable paths in the parent lane.  Preserve the
pre-existing paths and re-run the gate if the working tree changes before the
publication command.
