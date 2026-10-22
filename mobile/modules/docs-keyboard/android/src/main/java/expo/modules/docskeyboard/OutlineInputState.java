package expo.modules.docskeyboard;

import java.util.HashMap;
import java.util.Map;

/** UI-thread state shared by the native input and the direct Java regression harness. */
public final class OutlineInputState {
  private long epoch;
  private long generation;
  private long eventCount;
  private long originEpoch;
  private long originGeneration;
  private String text = "";
  private boolean needsFreshConnection;
  private long scope;
  private final Map<RestoreReference, Snapshot> snapshots = new HashMap<>();
  private Snapshot restoredSnapshot;
  private long pendingFocusEpoch = -1;

  public static final class RestoreReference {
    public final long epoch;
    public final long generation;

    public RestoreReference(long epoch, long generation) {
      this.epoch = epoch;
      this.generation = generation;
    }

    @Override public boolean equals(Object other) {
      if (!(other instanceof RestoreReference)) return false;
      RestoreReference reference = (RestoreReference) other;
      return reference.epoch == epoch && reference.generation == generation;
    }

    @Override public int hashCode() {
      return 31 * Long.hashCode(epoch) + Long.hashCode(generation);
    }
  }

  public static final class Snapshot {
    public final long epoch;
    public final long generation;
    public final long eventCount;
    public final String text;
    public final long originEpoch;
    public final long originGeneration;

    private Snapshot(long epoch, long generation, long eventCount, String text,
        long originEpoch, long originGeneration) {
      this.epoch = epoch;
      this.generation = generation;
      this.eventCount = eventCount;
      this.text = text;
      this.originEpoch = originEpoch;
      this.originGeneration = originGeneration;
    }
  }

  public static final class Submission {
    public final Snapshot source;
    public final long nextGeneration;

    private Submission(Snapshot source, long nextGeneration) {
      this.source = source;
      this.nextGeneration = nextGeneration;
    }
  }

  public Snapshot snapshot() {
    return new Snapshot(epoch, generation, eventCount, text, originEpoch, originGeneration);
  }

  public long getScope() {
    return scope;
  }

  public Snapshot restoredSnapshot() {
    return restoredSnapshot;
  }

  /** An IME connection belongs to a manual-edit epoch, not a logical paragraph generation. */
  public boolean acceptsEpoch(long connectionEpoch) {
    return connectionEpoch == epoch;
  }

  public boolean needsFreshConnection() {
    return needsFreshConnection;
  }

  public void connectionRetired() {
    needsFreshConnection = true;
  }

  public void connectionCreated() {
    needsFreshConnection = false;
  }

  public boolean requestFocus(long expectedEpoch) {
    if (expectedEpoch < epoch || expectedEpoch < 0 || expectedEpoch < pendingFocusEpoch) return false;
    pendingFocusEpoch = expectedEpoch;
    return true;
  }

  public long pendingFocusEpoch() {
    return pendingFocusEpoch;
  }

  public boolean focusEligible(boolean active, boolean enabled, boolean attached,
      boolean shown, boolean windowFocused, boolean laidOut) {
    return pendingFocusEpoch == epoch && pendingFocusEpoch >= 0 && active && enabled
      && attached && shown && windowFocused && laidOut;
  }

  public void consumeFocus(long expectedEpoch) {
    if (pendingFocusEpoch == expectedEpoch) pendingFocusEpoch = -1;
  }

  public void cancelFocus(long expectedEpoch) {
    consumeFocus(expectedEpoch);
  }

  public void cancelFocusThroughCurrentEpoch() {
    if (pendingFocusEpoch >= 0 && pendingFocusEpoch <= epoch) pendingFocusEpoch = -1;
  }

  public void cancelAllFocus() {
    pendingFocusEpoch = -1;
  }

  public boolean canApplyControlled(long epoch, long generation, long eventCount, String text) {
    if (epoch < 0 || generation < 0 || eventCount < 0 || text == null || epoch < this.epoch) {
      return false;
    }
    if (epoch == this.epoch
        && (generation != this.generation || eventCount < this.eventCount)) {
      return false;
    }
    return true;
  }

  /** A manual-edit epoch supersedes old input, but never resets the native event counter. */
  public boolean applyControlled(long epoch, long generation, long eventCount, String text) {
    return applyControlled(scope, epoch, generation, eventCount, text, null, 0);
  }

  public boolean applyControlled(long scope, long epoch, long generation, long eventCount,
      String text, RestoreReference[] restoreFrom, long restoreEventCount) {
    if (scope < 0 || restoreEventCount < 0
        || !canApplyControlled(epoch, generation, eventCount, text)) return false;
    boolean newEpoch = epoch > this.epoch;
    boolean scopeChanged = scope != this.scope;
    if (scopeChanged) {
      snapshots.clear();
      // The outgoing scope's current paragraph must never seed the incoming cache.
    }
    Snapshot restored = null;
    if (newEpoch && !scopeChanged && scope > 0 && restoreFrom != null) {
      for (RestoreReference reference : restoreFrom) {
        if (reference == null || reference.epoch < 0 || reference.generation < 0) continue;
        Snapshot candidate = snapshots.get(reference);
        if (candidate != null && candidate.eventCount > restoreEventCount
            && (restored == null || candidate.eventCount > restored.eventCount)) {
          restored = candidate;
        }
      }
    }
    this.scope = scope;
    this.epoch = epoch;
    if (pendingFocusEpoch < epoch) {
      pendingFocusEpoch = -1;
    }
    this.generation = generation;
    this.eventCount = Math.max(this.eventCount, eventCount);
    this.text = restored == null ? text : restored.text;
    if (newEpoch || scopeChanged) {
      originEpoch = restored == null ? epoch : restored.originEpoch;
      originGeneration = restored == null ? generation : restored.originGeneration;
    }
    restoredSnapshot = restored;
    return true;
  }

  public Snapshot change(String text) {
    return change(text, null);
  }

  public Snapshot change(String text, Snapshot origin) {
    if (text == null) throw new IllegalArgumentException("text is required");
    this.text = text;
    eventCount++;
    if (origin != null) {
      originEpoch = origin.originEpoch;
      originGeneration = origin.originGeneration;
    }
    Snapshot changed = snapshot();
    remember(changed);
    return changed;
  }

  /** Capture the full source paragraph before making the next logical row current. */
  public Submission advance(String currentText) {
    if (currentText == null) throw new IllegalArgumentException("text is required");
    text = currentText;
    eventCount++;
    Snapshot source = snapshot();
    remember(source);
    generation++;
    text = "";
    originEpoch = epoch;
    originGeneration = generation;
    return new Submission(source, generation);
  }

  private void remember(Snapshot snapshot) {
    if (scope > 0) snapshots.put(new RestoreReference(snapshot.epoch, snapshot.generation), snapshot);
  }
}
