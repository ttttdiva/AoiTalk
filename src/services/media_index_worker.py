"""Durable media indexing after authorized source writes, without public IPC."""

from __future__ import annotations

import asyncio
from collections import Counter
from itertools import islice
import logging
from pathlib import Path
import sqlite3
import time
import uuid

from .media_index_maintenance import prepare_lifecycle_schema

log = logging.getLogger(__name__)

# Canonical removal work must yield neither to normal attachments (0) nor to
# ordinary/heavy Files jobs (10/20). Lease ownership and backoff still apply.
PRIORITY_CLEANUP = -10
MAX_ENQUEUE_BATCH = 100


class MediaIndexQueue:
    def __init__(self, path="data/search/media-index-queue.sqlite"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS media_jobs (
                actor TEXT, kind TEXT, source_id TEXT, generation INTEGER DEFAULT 1,
                state TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0,
                due REAL DEFAULT 0, token TEXT, error TEXT,
                PRIMARY KEY(actor,kind,source_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS media_reconcile_cursors (
                kind TEXT PRIMARY KEY,cursor TEXT,cycle INTEGER DEFAULT 1,
                scanned INTEGER DEFAULT 0,changed INTEGER DEFAULT 0,
                completed INTEGER DEFAULT 0,last_scan REAL DEFAULT 0,
                last_completed REAL,error TEXT,due REAL DEFAULT 0)""")
            db.execute("""CREATE TABLE IF NOT EXISTS media_reconcile_sources (
                kind TEXT,source_id TEXT,actor TEXT,version TEXT,cycle INTEGER,
                state TEXT DEFAULT 'queued',PRIMARY KEY(kind,source_id))""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(media_jobs)")}
            if "priority" not in columns:
                # Lower runs first. Files jobs yield to conversation/Docs
                # attachments; heavy audio/video yield to small files.
                db.execute("ALTER TABLE media_jobs ADD COLUMN priority INTEGER DEFAULT 0")
            prepare_lifecycle_schema(db)

    def connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def enqueue(self, actor, kind, source_id, *, priority=None):
        self.enqueue_many([(actor, kind, source_id, priority)])

    def enqueue_many(self, items):
        """Commit at most 100 (actor, kind, source_id, priority) notices together.

        Bound consumption before opening SQLite, validate the entire page, and
        reuse the single-job UPSERT so running tokens/generations stay intact.
        Callers chunk larger inventories and yield between pages.
        """
        page = list(islice(items, MAX_ENQUEUE_BATCH + 1))
        if len(page) > MAX_ENQUEUE_BATCH:
            raise ValueError("Media enqueue batch must be bounded to 100 notices")
        normalized = []
        for actor, kind, source_id, priority in page:
            if kind == "context_memory":
                from .scoped_memory_service import _canonical_actor_id
                actor = _canonical_actor_id(actor)
            else:
                actor = str(uuid.UUID(str(actor)))
            priority = self._default_priority(kind) if priority is None else int(priority)
            normalized.append((actor, kind, str(source_id), priority))
        if not normalized:
            return 0
        with self.connect() as db:
            for args in normalized:
                self._enqueue(db, *args)
        return len(normalized)

    @staticmethod
    def _default_priority(kind):
        return 10 if kind == "workspace_file" else 0

    @classmethod
    def _enqueue(cls, db, actor, kind, source_id, priority=None):
        priority = cls._default_priority(kind) if priority is None else int(priority)
        db.execute(
                """INSERT INTO media_jobs(actor,kind,source_id,priority) VALUES(?,?,?,?)
                ON CONFLICT(actor,kind,source_id) DO UPDATE SET
                generation=generation+1,state=CASE WHEN state='running' THEN state ELSE 'pending' END,
                attempts=0,due=CASE WHEN state='running' THEN due ELSE 0 END,error=NULL,
                priority=CASE WHEN state IN ('pending','running') AND priority=?
                    THEN priority ELSE excluded.priority END""",
                (actor, kind, str(source_id), priority, PRIORITY_CLEANUP),
        )

    def retry_failed(self, kind, source_ids):
        """Manual retry of terminal (attempt-capped) jobs for authorized ids."""
        source_ids = [str(value) for value in source_ids]
        changed = 0
        with self.connect() as db:
            for start in range(0, len(source_ids), 500):
                batch = source_ids[start:start + 500]
                changed += db.execute(
                    "UPDATE media_jobs SET state='pending',attempts=0,due=0,error=NULL WHERE kind=? AND state='failed'"
                    " AND source_id IN (" + ",".join("?" for _ in batch) + ")", (kind, *batch)).rowcount
        return changed

    def reconciliation_checkpoint(self, kinds):
        """Rotate bounded scans even when indexing is continuously busy."""
        with self.connect() as db:
            db.executemany("INSERT OR IGNORE INTO media_reconcile_cursors(kind) VALUES(?)",
                           [(kind,) for kind in kinds])
            row = db.execute("""SELECT kind,cursor,cycle FROM media_reconcile_cursors
                WHERE due<=? ORDER BY last_scan,kind LIMIT 1""", (time.time(),)).fetchone()
            return row

    def reconcile_batch(self, checkpoint, items, next_cursor, *, complete, batch_size=100,
                        cycle_seconds=60):
        """Enqueue changes and advance the cursor in one SQLite transaction.

        Canonical DB commits do not need to cross a database transaction boundary:
        a crash before this commit repeats the page; a crash after it leaves jobs.
        Deletion cleanup is also bounded and resumes at the terminal cursor.
        """
        kind, cursor, cycle = checkpoint
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT cursor,cycle FROM media_reconcile_cursors WHERE kind=?",
                                 (kind,)).fetchone()
            if current != (cursor, cycle):
                return False
            changed = 0
            for source_id, actor, version, *extra in items:
                priority = extra[0] if extra else None
                previous = db.execute("SELECT actor,version,state FROM media_reconcile_sources WHERE kind=? AND source_id=?",
                                      (kind, source_id)).fetchone()
                if previous is None or previous != (actor, version, 'queued' if actor else 'unassigned'):
                    if previous and previous[0] and previous[0] != actor:
                        self._enqueue(db, previous[0], kind, source_id, priority)
                    if actor:
                        self._enqueue(db, actor, kind, source_id, priority)
                    changed += 1
                db.execute("""INSERT INTO media_reconcile_sources VALUES(?,?,?,?,?,?)
                    ON CONFLICT(kind,source_id) DO UPDATE SET actor=excluded.actor,
                    version=excluded.version,cycle=excluded.cycle,state=excluded.state""",
                           (kind, source_id, actor, version, cycle, 'queued' if actor else 'unassigned'))
            if complete:
                removed = db.execute("""SELECT source_id,actor FROM media_reconcile_sources
                    WHERE kind=? AND cycle<? AND state!='removed' LIMIT ?""",
                                     (kind, cycle, batch_size)).fetchall()
                for source_id, actor in removed:
                    if actor:
                        self._enqueue(db, actor, kind, source_id, PRIORITY_CLEANUP)
                    db.execute("UPDATE media_reconcile_sources SET state='removed' WHERE kind=? AND source_id=?",
                               (kind, source_id))
                changed += len(removed)
                # One additional page after exactly B deletions is harmless and
                # avoids an unbounded count/scan on a heavily deleted inventory.
                complete = len(removed) < batch_size
            db.execute("""UPDATE media_reconcile_cursors SET cursor=?,cycle=?,
                scanned=scanned+?,changed=changed+?,completed=completed+?,last_scan=?,
                last_completed=CASE WHEN ? THEN ? ELSE last_completed END,error=NULL,due=?
                WHERE kind=?""",
                       (None if complete else next_cursor, cycle + int(complete), len(items), changed,
                        int(complete), now, int(complete), now, now + cycle_seconds if complete else 0, kind))
            return True

    def reconciliation_failed(self, checkpoint):
        with self.connect() as db:
            db.execute("""UPDATE media_reconcile_cursors SET error='inventory_scan_failed',
                last_scan=?,due=? WHERE kind=? AND cycle=? AND cursor IS ?""",
                       (time.time(), time.time() + 30, checkpoint[0], checkpoint[2], checkpoint[1]))

    def reconciliation_status(self, actor):
        """Internal locators for live scope filtering; never return this raw to users."""
        with self.connect() as db:
            return [{"kind": kind, "source_id": source_id, "state": state,
                     "last_scan": last_scan, "last_completed": last_completed,
                     "error": error}
                    for kind, source_id, state, last_scan, last_completed, error in db.execute(
                        """SELECT s.kind,s.source_id,s.state,c.last_scan,c.last_completed,c.error
                        FROM media_reconcile_sources s JOIN media_reconcile_cursors c USING(kind)
                        WHERE s.actor=? LIMIT 100000""", (str(actor),))]

    def claim(self, kinds=None):
        if kinds is not None and not kinds:
            return None
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            kind_filter = " AND kind IN (" + ",".join("?" for _ in kinds) + ")" if kinds is not None else ""
            # Within each priority, recover expired leases, then due retries,
            # before fresh due=0 jobs. The WHERE clause keeps backoff in force.
            row = db.execute(
                """SELECT actor,kind,source_id,generation,attempts FROM media_jobs
                WHERE (state='pending' OR state='running') AND due<=?
                """ + kind_filter + " ORDER BY coalesce(priority,0),CASE WHEN state='running' THEN 0 WHEN attempts>0 AND due>0 THEN 1 ELSE 2 END,due,actor,kind,source_id LIMIT 1",
                (now, *(kinds or ())),
            ).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            db.execute(
                """UPDATE media_jobs SET state='running',token=?,due=?,attempts=attempts+1
                WHERE actor=? AND kind=? AND source_id=?""",
                (token, now + 900, *row[:3]),
            )
            return (*row, token)

    def finish(self, job, result, *, max_attempts=None):
        actor, kind, source_id, generation, attempts, token = job
        state = "done" if result.get("status") in {"ready", "excluded"} else "pending"
        # ``attempts`` is the count before this claim incremented it.
        if state == "pending" and max_attempts is not None and attempts + 1 >= max_attempts:
            # Files jobs stop after a bounded number of attempts; a content
            # change (new generation) or a manual retry re-queues them.
            state = "failed"
        # Retry runtime/model failures; checkpoints resume already verified units.
        due = (
            time.time() + min(3600, 15 * 2 ** min(attempts, 8))
            if state == "pending"
            else 0
        )
        with self.connect() as db:
            db.execute(
                """UPDATE media_jobs SET state=CASE WHEN generation=? THEN ? ELSE 'pending' END,
                due=CASE WHEN generation=? THEN ? ELSE 0 END,token=NULL,error=?
                WHERE actor=? AND kind=? AND source_id=? AND token=?""",
                (
                    generation,
                    state,
                    generation,
                    due,
                    result.get("reason"),
                    actor,
                    kind,
                    source_id,
                    token,
                ),
            )

    def renew(self, job):
        """Keep a long audio/video job leased only while this worker owns it."""
        actor, kind, source_id, _, _, token = job
        with self.connect() as db:
            return (
                db.execute(
                    """UPDATE media_jobs SET due=?
                WHERE actor=? AND kind=? AND source_id=? AND token=? AND state='running'""",
                    (time.time() + 900, actor, kind, source_id, token),
                ).rowcount
                == 1
            )

    def status(self, actor):
        with self.connect() as db:
            return dict(
                db.execute(
                    "SELECT state,count(*) FROM media_jobs WHERE actor=? GROUP BY state",
                    (str(actor),),
                ).fetchall()
            )

    async def scoped_status(self, resolver):
        """Safe user progress for API and AI tools, freshly authorized per scope."""
        from ..rag.index_io import run_index_io
        from .search_sources import SourceRef

        def snapshots():
            with self.connect() as db:
                jobs = db.execute('''SELECT kind,source_id,state,error FROM media_jobs
                    WHERE actor=? LIMIT 100001''', (str(resolver.scope.user_id),)).fetchall()
                reconcile = db.execute('''SELECT s.kind,s.source_id,s.state,c.error
                    FROM media_reconcile_sources s JOIN media_reconcile_cursors c USING(kind)
                    WHERE s.actor=? LIMIT 100001''', (str(resolver.scope.user_id),)).fetchall()
                failed = db.execute("SELECT 1 FROM media_reconcile_cursors WHERE error IS NOT NULL LIMIT 1").fetchone()
            if len(jobs) > 100000 or len(reconcile) > 100000:
                raise RuntimeError('progress_source_limit_exceeded')
            return jobs, reconcile, bool(failed)

        jobs, reconcile, failed_inventory = await run_index_io(snapshots)
        allowed = set(await resolver.allowed_refs([SourceRef(kind, source_id) for kind, source_id, *_ in
                                                  [*jobs, *reconcile]], require_local_original=False))
        if not allowed and failed_inventory:
            raise RuntimeError('inventory_scan_unavailable')
        counts = Counter(state for kind, source_id, state, _ in jobs if f'{kind}:{source_id}' in allowed
                         and state in {'pending', 'running', 'done', 'failed', 'ready', 'excluded'})
        retrying = sum(state == 'pending' and error is not None for kind, source_id, state, error in jobs
                       if f'{kind}:{source_id}' in allowed)
        if retrying:
            counts['retrying'] = retrying
        reconciliation = Counter(state for kind, source_id, state, _ in reconcile if f'{kind}:{source_id}' in allowed
                                 and state in {'queued', 'unassigned', 'removed'})
        failed = sum(error == 'inventory_scan_failed' for kind, source_id, _, error in reconcile
                     if f'{kind}:{source_id}' in allowed)
        reconciliation['failed'] = failed
        warnings = []
        if counts['retrying']:
            warnings.append('index_retry_pending')
        if failed:
            warnings.append('inventory_scan_failed')
        return {'indexing': dict(counts), 'reconciliation': dict(reconciliation), 'warnings': warnings}

    def reconciliation_overview(self):
        """Operator-only recovery health, including unassigned source exclusions."""
        with self.connect() as db:
            return self._reconciliation_overview(db)

    @classmethod
    def read_reconciliation_overview(cls, path="data/search/media-index-queue.sqlite"):
        """Operator inspection never creates or upgrades live queue metadata."""
        source = Path(path)
        if not source.is_file():
            return {'status': 'not_created', 'cursors': [], 'unassigned': 0, 'unassigned_reason': None}
        with sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True, timeout=30) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'media_reconcile_cursors', 'media_reconcile_sources'}.issubset(tables):
                return {'status': 'not_initialized', 'cursors': [], 'unassigned': 0, 'unassigned_reason': None}
            return cls._reconciliation_overview(db)

    @staticmethod
    def _reconciliation_overview(db):
        cursors = [dict(zip(('kind', 'cycle', 'scanned', 'changed', 'completed', 'last_scan',
                                 'last_completed', 'failure', 'retry_due'), row)) for row in db.execute(
                '''SELECT kind,cycle,scanned,changed,completed,last_scan,last_completed,error,due
                   FROM media_reconcile_cursors ORDER BY kind''')]
        exclusions = db.execute("SELECT count(*) FROM media_reconcile_sources WHERE state='unassigned'").fetchone()[0]
        return {'cursors': cursors, 'unassigned': exclusions,
                'unassigned_reason': 'registered_source_without_indexing_actor' if exclusions else None}


def _memory_search_enabled():
    try:
        from ..config import Config

        memory = Config().get_memory_config()
        return bool(memory.get("enabled", True) and memory.get("enable_search", True))
    except Exception:
        return False


def _files_index_settings():
    try:
        from .workspace_file_index import load_files_index_settings

        return load_files_index_settings()
    except Exception:
        return {"enabled": False}


def _enabled_kinds():
    from ..knowledge.index_service import _knowledge_search_enabled
    from .media_index_reconciliation import KINDS

    memory = _memory_search_enabled()
    knowledge = _knowledge_search_enabled()
    files = bool(_files_index_settings().get("enabled"))
    return tuple(
        kind
        for kind in KINDS
        if (kind != "knowledge_document" or knowledge)
        and (kind not in {"conversation_message", "context_memory"} or memory)
        and (kind != "workspace_file" or files)
    )


def notify_media_source(actor, kind, source_id, *, priority=None):
    """Call after a durable write; indexing failures never roll back originals."""
    try:
        MediaIndexQueue().enqueue(actor, kind, source_id, priority=priority)
    except Exception:
        log.warning("Unable to queue media source indexing", exc_info=True)


def notify_media_sources(items):
    """After-commit bounded page; canonical reconciliation recovers failures.

    Each notice is (actor, kind, source_id, priority), with priority=None allowed.
    The caller must submit at most MAX_ENQUEUE_BATCH notices per call.
    """
    try:
        return MediaIndexQueue().enqueue_many(items)
    except Exception:
        log.warning("Unable to queue media source indexing batch", exc_info=True)
        return 0


def queue_media_after_commit(session, actor, kind, source_id):
    """Never publish an uncommitted Docs/Knowledge attachment to the worker."""
    from sqlalchemy import event

    sync_session = session.sync_session
    transaction = (
        sync_session.get_nested_transaction() or sync_session.get_transaction()
    )
    if transaction is None:
        transaction = sync_session.begin()
    pending = sync_session.info.setdefault("eg2_media_notifications", {})
    pending.setdefault(transaction, set()).add((str(actor), kind, str(source_id)))
    if sync_session.info.get("eg2_media_notification_hooks"):
        return
    sync_session.info["eg2_media_notification_hooks"] = True

    def committed(current):
        transaction = current.get_nested_transaction() or current.get_transaction()
        pending = current.info.get("eg2_media_notifications", {})
        if transaction is not None and transaction.nested:
            # A committed savepoint remains provisional until its parent commits.
            notices = pending.pop(transaction, set())
            if notices:
                pending.setdefault(transaction.parent, set()).update(notices)
            return
        notices = pending.get(transaction, set())
        current.info.pop("eg2_media_notifications", None)
        for args in notices:
            notify_media_source(*args)

    def rolled_back(current):
        transaction = current.get_nested_transaction() or current.get_transaction()
        if transaction is not None and transaction.nested:
            # Rolling back optional work must preserve the parent's durable work.
            current.info.get("eg2_media_notifications", {}).pop(transaction, None)
        else:
            current.info.pop("eg2_media_notifications", None)

    def ended(current, transaction):
        if transaction.parent is None:
            # Session.close also discards a transaction without after_rollback.
            current.info.pop("eg2_media_notifications", None)

    event.listen(sync_session, "after_commit", committed)
    event.listen(sync_session, "after_rollback", rolled_back)
    event.listen(sync_session, "after_transaction_end", ended)


class MediaIndexWorker:
    def __init__(self, session_factory, *, queue=None, service=None, poll_seconds=2,
                 reconcile_seconds=2):
        self.session_factory = session_factory
        self.queue = queue or MediaIndexQueue()
        self.service = service
        self.poll_seconds = poll_seconds
        self.task = None
        self.reconcile_task = None
        self.reconcile_seconds = reconcile_seconds
        # Extra Files-only lanes (``search.files_index.max_concurrency`` - 1).
        # Model inference stays serialized by the single EmbeddingGemma 2 IPC
        # worker; these lanes overlap hashing/decoding/Office conversion.
        self.files_tasks = []

    async def start(self):
        install_managed_asset_notifications()
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(
                self.run(), name="embeddinggemma2-media-index"
            )
        if self.reconcile_task is None or self.reconcile_task.done():
            self.reconcile_task = asyncio.create_task(
                self.reconcile_loop(), name="embeddinggemma2-media-reconciliation"
            )
        from .workspace_file_index import MAX_CONCURRENCY
        self.files_tasks = [task for task in self.files_tasks if not task.done()]
        while len(self.files_tasks) < MAX_CONCURRENCY - 1:
            lane = len(self.files_tasks) + 1
            self.files_tasks.append(asyncio.create_task(
                self.files_lane(lane), name=f"embeddinggemma2-files-index-{lane}"))

    async def stop(self):
        tasks = [task for task in (self.task, self.reconcile_task, *self.files_tasks) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.task = self.reconcile_task = None
        self.files_tasks = []

    async def files_lane(self, lane):
        """Additional Files-only worker active while lane < max_concurrency."""
        while True:
            settings = _files_index_settings()
            active = bool(settings.get("enabled")) and lane < int(settings.get("max_concurrency") or 1)
            if active:
                try:
                    if await self.run_once(kinds=("workspace_file",)):
                        continue
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.warning("Files index lane iteration failed", exc_info=True)
            await asyncio.sleep(self.poll_seconds)

    async def reconcile_loop(self):
        while True:
            kinds = _enabled_kinds()
            if kinds:
                try:
                    await self.reconcile_once(kinds=kinds)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.warning("Media reconciliation queue unavailable", exc_info=True)
            await asyncio.sleep(self.reconcile_seconds)

    async def run(self):
        while True:
            kinds = _enabled_kinds()
            if kinds:
                try:
                    if await self.run_once(kinds=kinds):
                        continue
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.warning("Media index worker iteration failed", exc_info=True)
            await asyncio.sleep(self.poll_seconds)

    async def reconcile_once(self, *, kinds=None):
        from .media_index_reconciliation import KINDS, canonical_media_page

        checkpoint = await asyncio.to_thread(self.queue.reconciliation_checkpoint, KINDS if kinds is None else kinds)
        if checkpoint is None:
            return False
        session = None
        try:
            session = await self.session_factory()
            items, cursor, complete = await canonical_media_page(session, checkpoint)
            return await asyncio.to_thread(self.queue.reconcile_batch, checkpoint, items, cursor,
                                           complete=complete)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.to_thread(self.queue.reconciliation_failed, checkpoint)
            log.warning("Canonical media inventory reconciliation failed", exc_info=True)
            return False
        finally:
            if session is not None:
                await session.close()

    async def run_once(self, *, kinds=None):
        from ..rag.media_index import get_media_index_service
        from .search_sources import SearchSourceResolver, SearchScope, SourceRef

        job = await asyncio.to_thread(self.queue.claim, kinds)
        if job is None:
            return False
        actor, kind, source_id, *_ = job
        session = None

        async def heartbeat():
            while True:
                await asyncio.sleep(60)
                if not await asyncio.to_thread(self.queue.renew, job):
                    return

        lease_task = asyncio.create_task(heartbeat())
        try:
            session = await self.session_factory()
            if kind == "context_memory":
                from ..memory.context_memory_index import get_context_memory_index
                result = await get_context_memory_index().sync_source(actor, source_id)
            elif kind == "conversation_message":
                resolver = SearchSourceResolver(session, SearchScope(uuid.UUID(actor)))
                from ..memory.cross_session_memory import get_cross_session_memory

                result = await get_cross_session_memory().index_current_message(resolver, SourceRef(kind, source_id))
            else:
                resolver = SearchSourceResolver(session, SearchScope(uuid.UUID(actor)))
                result = await (self.service or get_media_index_service()).sync_source(resolver, SourceRef(kind, source_id))
        except asyncio.CancelledError:
            await asyncio.to_thread(
                self.queue.finish,
                job,
                {"status": "failed", "reason": "cancelled; retry resumes units"},
            )
            raise
        except Exception as exc:
            result = {"status": "failed", "reason": str(exc)}
        finally:
            lease_task.cancel()
            try:
                await lease_task
            except asyncio.CancelledError:
                pass
            if session is not None:
                await session.close()
        max_attempts = None
        if kind == "workspace_file":
            max_attempts = int(_files_index_settings().get("max_failed_attempts") or 5)
        await asyncio.to_thread(self.queue.finish, job, result, max_attempts=max_attempts)
        return True


_managed_asset_notifications_installed = False


def install_managed_asset_notifications():
    """Observe formal byte registration without changing Operations writers."""
    global _managed_asset_notifications_installed
    if _managed_asset_notifications_installed:
        return
    from sqlalchemy import event
    from sqlalchemy.orm import Session
    from types import SimpleNamespace
    from ..memory.models import MediaAssetRevision

    def flushed(session, _):
        for row in session.new:
            if isinstance(row, MediaAssetRevision):
                queue_media_after_commit(
                    SimpleNamespace(sync_session=session),
                    row.owner_user_id,
                    "generation_output",
                    row.generation_output_id,
                )

    event.listen(Session, "after_flush", flushed)
    _managed_asset_notifications_installed = True
