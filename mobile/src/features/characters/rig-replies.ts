export type RigReplyMessage = {
  id: string;
  role: string;
  content: string;
  is_active_branch?: boolean;
  metadata?: Record<string, unknown> | null;
};

/** Host conversation events, not a second animation/physics implementation. */
export class RigReplyTracker {
  private scope = '';
  private initialized = false;
  private seen = new Set<string>();

  reset() {
    this.scope = '';
    this.initialized = false;
    this.seen.clear();
  }

  observe(scope: string, messages: readonly RigReplyMessage[], ready: boolean, busy: boolean): string | null {
    if (scope !== this.scope || !ready) {
      this.reset();
      this.scope = scope;
    }
    if (!ready) return null;
    if (!this.initialized) {
      this.seen = new Set(messages.map(message => message.id));
      this.initialized = true;
      return null; // Opening a page or loading history is not a new reply.
    }
    if (busy) return null; // Do not react to a partially streamed answer.
    let reply: string | null = null;
    for (const message of messages) {
      if (this.seen.has(message.id) || message.role !== 'assistant' || message.is_active_branch === false) continue;
      const metadata = message.metadata ?? {};
      if (!message.content.trim() || metadata.error || metadata.cancelled || metadata.interrupted
        || ['failed', 'cancelled', 'error'].includes(String(metadata.status))) continue;
      reply = message.id;
    }
    this.seen = new Set(messages.map(message => message.id));
    return reply;
  }
}
