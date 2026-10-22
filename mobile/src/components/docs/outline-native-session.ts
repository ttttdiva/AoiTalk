export type NativeOutlineReference = { epoch: number; generation: number };

export type NativeOutlineValue = {
  epoch: number;
  generation: number;
  eventCount: number;
  text: string;
  restoreFrom?: NativeOutlineReference[];
  restoreEventCount?: number;
  originEpoch?: number;
  originGeneration?: number;
};

export type NativeOutlineSubmit = NativeOutlineValue & {
  nextGeneration: number;
  focused: boolean;
};

export type OutlineNativeSessionCallbacks = {
  change: (nodeId: string, text: string, value: NativeOutlineValue, active: boolean) => void;
  submit: (nodeId: string, text: string, active: boolean, event: NativeOutlineSubmit) => string | null;
  activate: (nodeId: string, value: NativeOutlineValue) => void;
};

type BoundRow = {
  nodeId: string;
  value: NativeOutlineValue;
  sealed: boolean;
  receivedNative: boolean;
};

type Session = {
  currentGeneration: number;
  rows: Map<number, BoundRow>;
  changes: Map<number, NativeOutlineValue>;
  submissions: Map<number, NativeOutlineSubmit>;
};

function validValue(value: NativeOutlineValue): boolean {
  return Number.isSafeInteger(value.epoch) && value.epoch >= 0
    && Number.isSafeInteger(value.generation) && value.generation >= 0
    && Number.isSafeInteger(value.eventCount) && value.eventCount >= 0
    && typeof value.text === "string";
}

function copyValue(value: NativeOutlineValue): NativeOutlineValue {
  return {
    ...value,
    ...(value.restoreFrom ? { restoreFrom: value.restoreFrom.map((reference) => ({ ...reference })) } : {}),
  };
}

/**
 * Binds native input generations to permanent row IDs without waiting for storage.
 * A new manual-edit epoch changes the active row, while late events from earlier
 * epochs can still save their original rows. No text interpretation happens here.
 */
export class OutlineNativeSession {
  private nextEpoch = 0;
  private activeEpoch: number | null = null;
  private sessions = new Map<number, Session>();
  private acceptedRows = new Map<string, { eventCount: number; epoch: number }>();
  private rowReferences = new Map<string, NativeOutlineReference[]>();

  constructor(private readonly callbacks: OutlineNativeSessionCallbacks) {}

  begin(nodeId: string, text: string): NativeOutlineValue {
    const references = this.rowReferences.get(nodeId);
    const value: NativeOutlineValue = {
      epoch: ++this.nextEpoch, generation: 0, eventCount: 0, text,
      ...(references?.length ? {
        restoreFrom: references.map((reference) => ({ ...reference })),
        restoreEventCount: this.acceptedRows.get(nodeId)?.eventCount ?? 0,
      } : {}),
    };
    this.sessions.set(value.epoch, {
      currentGeneration: 0,
      rows: new Map([[0, { nodeId, value, sealed: false, receivedNative: false }]]),
      changes: new Map(),
      submissions: new Map(),
    });
    this.rememberRow(nodeId, value);
    this.activeEpoch = value.epoch;
    return copyValue(value);
  }

  get currentNodeId(): string | null {
    return this.currentRow?.nodeId ?? null;
  }

  get currentValue(): NativeOutlineValue | null {
    const value = this.currentRow?.value;
    return value ? copyValue(value) : null;
  }

  private get currentRow(): BoundRow | undefined {
    const session = this.activeEpoch === null ? undefined : this.sessions.get(this.activeEpoch);
    return session?.rows.get(session.currentGeneration);
  }

  change(event: NativeOutlineValue): void {
    if (!validValue(event)) return;
    const session = this.sessions.get(event.epoch);
    if (!session) return;
    const row = session.rows.get(event.generation);
    if (row?.sealed) return;
    if (row && event.eventCount <= row.value.eventCount) return;
    const buffered = session.changes.get(event.generation);
    if (buffered && event.eventCount <= buffered.eventCount) return;
    session.changes.set(event.generation, { ...event });
    if (row) {
      this.drain(event.epoch, session);
      this.publish(event.epoch);
    }
  }

  submit(event: NativeOutlineSubmit): void {
    if (!validValue(event) || !Number.isSafeInteger(event.nextGeneration)
      || event.nextGeneration !== event.generation + 1 || typeof event.focused !== "boolean") return;
    const session = this.sessions.get(event.epoch);
    if (!session) return;
    const row = session.rows.get(event.generation);
    if (row?.sealed || (row && event.eventCount < row.value.eventCount)) return;
    // Native advances once per submit. Re-delivery cannot create another row.
    if (session.submissions.has(event.generation)) return;
    session.submissions.set(event.generation, { ...event });
    if (row) {
      this.drain(event.epoch, session);
      this.publish(event.epoch);
    }
  }

  private drain(epoch: number, session: Session): void {
    // Iteration also handles a burst whose later generations arrived before
    // their source submit. Intermediate controlled values are never published.
    while (this.sessions.get(epoch) === session) {
      const generation = session.currentGeneration;
      const row = session.rows.get(generation);
      if (!row || row.sealed) return;
      const change = session.changes.get(generation);
      if (change) {
        session.changes.delete(generation);
        if (change.eventCount > row.value.eventCount) {
          row.value = change;
          row.receivedNative = true;
          this.saveSnapshot(row.nodeId, change);
          if (this.sessions.get(epoch) !== session) return;
        }
      }
      const submit = session.submissions.get(generation);
      if (!submit) return;
      session.submissions.delete(generation);
      if (submit.eventCount < row.value.eventCount) continue;
      const source: NativeOutlineValue = {
        epoch, generation, eventCount: submit.eventCount, text: submit.text,
        ...(submit.originEpoch === undefined ? {} : { originEpoch: submit.originEpoch }),
        ...(submit.originGeneration === undefined ? {} : { originGeneration: submit.originGeneration }),
      };
      row.value = source;
      row.receivedNative = true;
      // A delayed source still advances its own input generation, but cannot
      // overwrite a newer edit accepted after the same row was reopened.
      this.saveSnapshot(row.nodeId, source);
      if (this.sessions.get(epoch) !== session) return;
      row.sealed = true;
      const nextId = this.callbacks.submit(row.nodeId, source.text, epoch === this.activeEpoch, { ...submit });
      if (!nextId || this.sessions.get(epoch) !== session) return;
      const nextValue: NativeOutlineValue = {
        epoch, generation: submit.nextGeneration, eventCount: submit.eventCount, text: "",
      };
      session.rows.set(submit.nextGeneration, {
        nodeId: nextId, value: nextValue, sealed: false, receivedNative: false,
      });
      this.rememberRow(nextId, nextValue);
      session.currentGeneration = submit.nextGeneration;
    }
  }

  private saveSnapshot(nodeId: string, value: NativeOutlineValue): void {
    const accepted = this.acceptedRows.get(nodeId);
    // Native eventCount is monotonic across manual-edit epochs. The epoch
    // breaks equal-counter ties without treating begin's counter=0 as an edit.
    if (accepted && (value.eventCount < accepted.eventCount
      || (value.eventCount === accepted.eventCount && value.epoch <= accepted.epoch))) return;
    this.acceptedRows.set(nodeId, { eventCount: value.eventCount, epoch: value.epoch });
    this.callbacks.change(nodeId, value.text, { ...value }, value.epoch === this.activeEpoch);
    const current = this.currentRow;
    if (current?.nodeId === nodeId && !current.receivedNative
      && (current.value.epoch !== value.epoch || current.value.generation !== value.generation)) {
      // Reopening the row may have used a DB snapshot from before this native
      // event. Update its baseline until a real event from the new edit arrives.
      // Native independently restores from numeric references before accepting
      // the next key, covering the case where JS receives this event too late.
      current.value = {
        ...current.value, eventCount: value.eventCount, text: value.text,
        restoreEventCount: value.eventCount,
      };
      this.callbacks.activate(nodeId, copyValue(current.value));
    }
  }

  private rememberRow(nodeId: string, value: NativeOutlineValue): void {
    const references = this.rowReferences.get(nodeId) ?? [];
    references.push({ epoch: value.epoch, generation: value.generation });
    this.rowReferences.set(nodeId, references);
  }

  private publish(epoch: number): void {
    if (epoch !== this.activeEpoch) return;
    const row = this.currentRow;
    if (row) this.callbacks.activate(row.nodeId, copyValue(row.value));
  }

  drop(epoch: number): void {
    this.sessions.delete(epoch);
    if (this.activeEpoch === epoch) this.activeEpoch = null;
  }

  clear(): void {
    this.sessions.clear();
    this.acceptedRows.clear();
    this.rowReferences.clear();
    this.activeEpoch = null;
    // Never reuse an epoch: native events can outlive a screen/root reset.
  }

  clearSessions(): void { this.clear(); }
}
