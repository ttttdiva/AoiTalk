package expo.modules.docskeyboard;

import java.util.concurrent.atomic.AtomicInteger;

/** A UI-queue request may show once, unless JS or native lifecycle cancels first. */
public final class KeyboardRequestGate {
  private static final int PENDING = 0;
  private static final int CLAIMED = 1;
  private static final int CANCELLED = 2;
  private final AtomicInteger state = new AtomicInteger(PENDING);

  public boolean isPending() {
    return state.get() == PENDING;
  }

  public boolean claimShow() {
    return state.compareAndSet(PENDING, CLAIMED);
  }

  public void cancel() {
    state.set(CANCELLED);
  }
}
