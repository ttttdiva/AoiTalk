import { getTokenSnapshot } from "../../lib/auth";
import { docsRepo } from "../../repositories/docs";
import type { DocsNode } from "../../types/api";

export type DocBlockType = "markdown" | "code";
export type DocBlock = {
  type: DocBlockType;
  label: string;
  content: string;
  clipIngest?: Record<string, unknown>;
};

export type DocsEditorIdleOptions = {
  pollMs?: number;
  maxPolls?: number;
};

function scheduleEditorIdleTimer(
  callback: () => void,
  delayMs: number,
): ReturnType<typeof setTimeout> {
  const timer = setTimeout(callback, delayMs);
  (timer as unknown as { unref?: () => void }).unref?.();
  return timer;
}

/**
 * Wait for already-started editor work to become idle without starting a new
 * save/flush. A false result keeps the caller's guard fail-closed.
 */
export async function waitForDocsEditorIdle(
  waitForStoreIdle: () => Promise<boolean>,
  hasOutlinePending: () => boolean,
  options: DocsEditorIdleOptions = {},
): Promise<boolean> {
  const pollMs = Math.max(1, Math.floor(options.pollMs ?? 100));
  const maxPolls = Math.max(0, Math.floor(options.maxPolls ?? 300));
  const storeWaitMs = pollMs * (maxPolls + 1);
  const waitForStoreIdleBounded = () => new Promise<boolean>((resolve) => {
    let settled = false;
    const timer = scheduleEditorIdleTimer(() => {
      settled = true;
      resolve(false);
    }, storeWaitMs);
    void waitForStoreIdle().then(
      (idle) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve(idle);
      },
      () => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve(false);
      },
    );
  });
  // Let already-registered unmount cleanup microtasks enqueue their existing
  // dispose flights before the first idle probe.
  await Promise.resolve();
  for (let poll = 0; poll <= maxPolls; poll += 1) {
    if (!(await waitForStoreIdleBounded())) return false;
    if (!hasOutlinePending()) return true;
    if (poll === maxPolls) return false;
    await new Promise<void>((resolve) => {
      scheduleEditorIdleTimer(resolve, pollMs);
    });
  }
  return false;
}

/** Only the editable typed-block contract is a displayed body. */
export function readDocBlock(node: Pick<DocsNode, "body_json" | "title">): DocBlock | null {
  const body = node.body_json;
  if (!body || body.format !== "doc_block"
    || (body.block_type !== "markdown" && body.block_type !== "code")
    || typeof body.content !== "string") return null;
  return {
    type: body.block_type,
    label: typeof body.label === "string" && body.label.trim() ? body.label : node.title || "本文",
    content: body.content.replace(/\r\n?/g, "\n"),
    clipIngest: body.clip_ingest && typeof body.clip_ingest === "object" && !Array.isArray(body.clip_ingest)
      ? body.clip_ingest as Record<string, unknown> : undefined,
  };
}

// Serialize UI title/body read-modify-write units for the same node. The
// repository remains the authority for persistence, outbox and authorization.
const editChains = new Map<string, Promise<unknown>>();
export function serializeDocsNodeEdit<T>(nodeId: string, edit: () => Promise<T>): Promise<T> {
  const previous = editChains.get(nodeId) ?? Promise.resolve();
  const next = previous.catch(() => undefined).then(edit);
  editChains.set(nodeId, next);
  void next.finally(() => {
    if (editChains.get(nodeId) === next) editChains.delete(nodeId);
  }).catch(() => undefined);
  return next;
}

const readOnlyMessage = "編集権限が変わったため保存できません。入力した本文はコピーできます。";
const conflictMessage = "別の本文が保存されています。入力内容は保持しました。本文をコピーしてから最新の内容を確認してください。";

/** A screen-owned draft survives FlatList unmounts and participates in navigation saves. */
export class DocBlockDraft {
  node: DocsNode;
  draft: string;
  error: string | null = null;
  saving = false;
  editRequest = 0;
  requestEdit() { this.editRequest += 1; this.notify(); }
  private baseContent: string;
  private readOnly = false;
  private released = false;
  private abandoned = false;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private flight: Promise<void> | null = null;
  private authAtEdit: ReturnType<typeof getTokenSnapshot> | null = null;
  private listeners = new Set<() => void>();
  private savedListeners = new Set<(node: DocsNode) => void>();

  constructor(node: DocsNode, private readonly scopeWritable = () => true) {
    this.node = node;
    this.draft = this.baseContent = readDocBlock(node)?.content ?? "";
  }
  get dirty() { return this.draft !== this.baseContent; }
  get pending() { return this.dirty || this.saving; }
  get canEdit() {
    return !this.abandoned && this.scopeWritable() && !this.readOnly && this.node.access !== "read"
      && this.node.read_only !== true && !this.node.archived_at;
  }
  subscribe(listener: () => void) {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  }
  subscribeSaved(listener: (node: DocsNode) => void) {
    this.savedListeners.add(listener);
    return () => { this.savedListeners.delete(listener); };
  }
  private notify() { this.listeners.forEach((listener) => listener()); }
  owns(node: DocsNode) {
    return node.id === this.node.id && node.workspace_id === this.node.workspace_id
      && node.project_id === this.node.project_id;
  }
  abandon(notify = true) {
    this.abandoned = true;
    this.released = true;
    this.cancelTimer();
    this.savedListeners.clear();
    this.error = readOnlyMessage;
    if (notify) this.notify();
  }
  update(node: DocsNode, readOnly: boolean) {
    if (this.released) return;
    if (!this.owns(node)) { this.abandon(); return; }
    const before = this.draft;
    const couldEdit = this.canEdit;
    this.node = node;
    this.readOnly = readOnly;
    if (!this.pending) {
      this.draft = this.baseContent = readDocBlock(node)?.content ?? "";
      this.authAtEdit = null;
      this.error = null;
    }
    if (!this.canEdit && this.dirty) {
      this.cancelTimer();
      this.error = readOnlyMessage;
    }
    if (before !== this.draft || couldEdit !== this.canEdit) this.notify();
  }
  change(content: string) {
    if (this.released || !this.canEdit) return;
    if (!this.pending) this.authAtEdit = null;
    if (!this.authAtEdit) this.authAtEdit = getTokenSnapshot();
    // Attach a rejection handler immediately; flush still reports the failure.
    void this.authAtEdit.catch(() => undefined);
    this.draft = content.replace(/\r\n?/g, "\n");
    this.error = null;
    this.cancelTimer();
    if (!this.pending) this.authAtEdit = null;
    this.notify();
    if (!this.dirty) return;
    this.timer = setTimeout(() => { void this.flush().catch(() => undefined); }, 450);
  }
  revert() {
    if (this.saving || this.released) return;
    this.cancelTimer();
    this.draft = this.baseContent = readDocBlock(this.node)?.content ?? this.baseContent;
    this.authAtEdit = null;
    this.error = null;
    this.notify();
  }
  dispose(): Promise<void> {
    this.released = true;
    this.cancelTimer();
    this.listeners.clear();
    this.savedListeners.clear();
    // Navigation normally flushed already. Preserve same-account edits on
    // other unmounts, while the save-time auth/ACL checks reject actor changes.
    return this.flush();
  }
  /** Existing flight only; this accessor never starts a flush. */
  get inFlight(): Promise<void> | null { return this.flight; }
  private cancelTimer() {
    if (this.timer) clearTimeout(this.timer);
    this.timer = undefined;
  }
  flush(): Promise<void> {
    this.cancelTimer();
    if (this.flight) return this.flight;
    if (!this.dirty) { this.authAtEdit = null; this.error = null; return Promise.resolve(); }
    this.saving = true;
    this.notify();
    const flight = (async () => {
      try {
        while (this.dirty) {
          await serializeDocsNodeEdit(this.node.id, async () => {
            if (!this.canEdit) throw new Error(readOnlyMessage);
            const originalAuth = await this.authAtEdit;
            const currentAuth = await getTokenSnapshot();
            if (!originalAuth || currentAuth.token !== originalAuth.token
              || currentAuth.revision !== originalAuth.revision) throw new Error(readOnlyMessage);
            const projectionAtRead = this.node;
            const current = await docsRepo.getNode(this.node.id);
            if (!this.canEdit || !current || current.access === "read" || current.read_only
              || current.archived_at || current.workspace_id !== this.node.workspace_id
              || current.project_id !== this.node.project_id) throw new Error(readOnlyMessage);
            const block = readDocBlock(current);
            if (!block) throw new Error("本文の形式が変わったため保存できません。入力内容は保持しています。");
            const content = this.draft;
            if (block.content !== this.baseContent && block.content !== content) {
              this.node = current;
              throw new Error(conflictMessage);
            }
            const checkAuth = await getTokenSnapshot();
            if (!this.canEdit || checkAuth.token !== originalAuth.token
              || checkAuth.revision !== originalAuth.revision) throw new Error(readOnlyMessage);
            const updated = block.content === content ? current : await docsRepo.updateNode(current.id, {
              bodyJson: { ...current.body_json, content },
            }, {
              auth: originalAuth, workspaceId: current.workspace_id, projectId: current.project_id,
            });
            this.baseContent = content;
            if (!this.owns(updated)) { this.abandon(); return; }
            // A write acknowledgement must not restore an older permission
            // projection that arrived before an ACL/auth refresh.
            const mayPublish = this.canEdit;
            const afterAuth = await getTokenSnapshot();
            this.node = this.node === projectionAtRead ? updated
              : { ...updated, ...this.node, body_json: updated.body_json, updated_at: updated.updated_at };
            this.error = null;
            if (mayPublish && this.canEdit && afterAuth.token === originalAuth.token
              && afterAuth.revision === originalAuth.revision) {
              this.savedListeners.forEach((listener) => listener(this.node));
            }
            this.notify();
          });
        }
        this.authAtEdit = null;
      } catch (error) {
        this.error = error instanceof Error ? error.message : "本文の保存に失敗しました。再試行してください。";
        throw error;
      } finally {
        this.saving = false;
        this.flight = null;
        this.notify();
      }
    })();
    this.flight = flight;
    return flight;
  }
}

export class DocBlockDraftStore {
  private drafts = new Map<string, DocBlockDraft>();
  private subscriptions = new Map<string, () => void>();
  private listeners = new Set<() => void>();
  private pendingIds = new Set<string>();
  private disposeFlights = new Set<Promise<void>>();
  private disposeFailed = false;
  private disposed = false;
  private owners = 0;
  retain() {
    this.owners += 1;
    return () => {
      this.owners -= 1;
      queueMicrotask(() => { if (this.owners === 0) this.dispose(); });
    };
  }
  subscribe(listener: () => void) {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  }
  constructor(private readonly canWrite = () => true) {}
  get(node: DocsNode): DocBlockDraft {
    let draft = this.drafts.get(node.id);
    if (draft && !draft.owns(node)) {
      const wasPending = this.pending;
      this.subscriptions.get(node.id)?.();
      this.subscriptions.delete(node.id);
      this.pendingIds.delete(node.id);
      this.drafts.delete(node.id);
      draft.abandon(false);
      this.trackDisposeFlight(draft.dispose());
      draft = undefined;
      if (wasPending !== this.pending) {
        // get() is also used while a row renders; notify its screen after commit.
        queueMicrotask(() => this.listeners.forEach((listener) => listener()));
      }
    }
    if (!draft) {
      draft = new DocBlockDraft(node, this.canWrite);
      this.drafts.set(node.id, draft);
      const storedDraft = draft;
      this.subscriptions.set(node.id, storedDraft.subscribe(() => {
        const wasPending = this.pendingIds.size > 0;
        if (storedDraft.pending) this.pendingIds.add(node.id);
        else this.pendingIds.delete(node.id);
        if (wasPending !== (this.pendingIds.size > 0)) {
          this.listeners.forEach((listener) => listener());
        }
      }));
    }
    return draft;
  }
  get pending() {
    return this.pendingIds.size > 0
      || (this.disposed && (this.disposeFlights.size > 0 || this.disposeFailed));
  }
  private trackDisposeFlight(flight: Promise<void>): void {
    this.disposeFlights.add(flight);
    void flight.then(
      () => {
        this.disposeFlights.delete(flight);
      },
      () => {
        this.disposeFlights.delete(flight);
        this.disposeFailed = true;
      },
    );
  }
  async waitForIdle(): Promise<boolean> {
    while (true) {
      if (this.disposeFailed) return false;
      const flights = [
        ...this.disposeFlights,
        ...[...this.drafts.values()]
          .map((draft) => draft.inFlight)
          .filter((flight): flight is Promise<void> => Boolean(flight)),
      ];
      if (flights.length > 0) {
        await Promise.allSettled(flights);
        continue;
      }
      if (this.disposeFailed) return false;
      if ([...this.drafts.values()].some((draft) => draft.pending)) return false;
      return true;
    }
  }
  dispose(): Promise<boolean> {
    this.disposed = true;
    this.listeners.clear();
    this.subscriptions.forEach((unsubscribe) => unsubscribe());
    this.subscriptions.clear();
    this.drafts.forEach((draft) => this.trackDisposeFlight(draft.dispose()));
    this.drafts.clear();
    this.pendingIds.clear();
    return this.waitForIdle();
  }
  async flush(nodeId?: string): Promise<void> {
    if (nodeId) await this.drafts.get(nodeId)?.flush();
    else await Promise.all([...this.pendingIds].map((id) => this.drafts.get(id)!.flush()));
  }
}
