import { useCallback, useReducer, useRef, type SetStateAction } from "react";
import type { ConversationMessage } from "../../types/api";

export function conversationMessageIdentity(message: ConversationMessage): string {
  return message.role === "user" && typeof message.metadata?.client_message_id === "string"
    ? message.metadata.client_message_id : message.id;
}

export type SubmissionTimelineState = {
  messages: ConversationMessage[];
  outgoing: Record<string, ConversationMessage>;
};
type SubmissionTimelineAction =
  | { type: "submit"; message: ConversationMessage }
  | { type: "promote"; from: string; to: string }
  | { type: "update"; sessionId: string; value: SetStateAction<ConversationMessage[]> };

/** A delayed SQLite/REST snapshot cannot erase a message already accepted by the UI. */
export function submissionTimelineReducer(
  state: SubmissionTimelineState,
  action: SubmissionTimelineAction,
): SubmissionTimelineState {
  if (action.type === "promote") {
    const promote = (message: ConversationMessage) => message.session_id === action.from
      ? { ...message, session_id: action.to } : message;
    return {
      messages: state.messages.map(promote),
      outgoing: Object.fromEntries(Object.entries(state.outgoing).map(([id, message]) => [id, promote(message)])),
    };
  }
  if (action.type === "submit") {
    const outgoing = { ...state.outgoing, [action.message.id]: action.message };
    return submissionTimelineReducer({ ...state, outgoing }, {
      type: "update", sessionId: action.message.session_id,
      value: (messages) => [...messages.filter((message) => conversationMessageIdentity(message) !== action.message.id), action.message],
    });
  }
  const incoming = typeof action.value === "function" ? action.value(state.messages) : action.value;
  const outgoing = { ...state.outgoing };
  const acknowledged = new Set(incoming.filter((message) => message.role === "user" && !message.metadata?.local_only)
    .map(conversationMessageIdentity));
  // A server receipt may arrive together with its still-cached local row.
  const messages = incoming.filter((message) => !(message.metadata?.local_only && acknowledged.has(message.id)));
  for (const [id, pending] of Object.entries(outgoing)) {
    if (pending.session_id !== action.sessionId) continue;
    if (acknowledged.has(id)) { delete outgoing[id]; continue; }
    const current = messages.find((message) => message.id === id);
    if (current) outgoing[id] = current;
    else messages.push(pending);
  }
  return { messages, outgoing };
}

export function useSubmissionTimeline(sessionId: string) {
  const sessionRef = useRef(sessionId);
  sessionRef.current = sessionId;
  const [state, dispatch] = useReducer(submissionTimelineReducer, { messages: [], outgoing: {} });
  const setMessages = useCallback((value: SetStateAction<ConversationMessage[]>) => {
    dispatch({ type: "update", sessionId: sessionRef.current, value });
  }, []);
  const submitMessage = useCallback((message: ConversationMessage) => dispatch({ type: "submit", message }), []);
  const promoteMessages = useCallback((from: string, to: string) => dispatch({ type: "promote", from, to }), []);
  return { messages: state.messages, setMessages, submitMessage, promoteMessages };
}

/** Distinguishes a retained failed bubble from a request rejected before acceptance. */
export class RetainedSubmissionError extends Error {
  readonly retainedInTimeline = true;
}

/** Serialize handoffs, not UI acceptance. A new input cancels a Direct HTTP reply. */
export class ConversationSubmissionQueue {
  private tail: Promise<void> = Promise.resolve();
  private direct: AbortController | null = null;
  private flights = new Map<string, Promise<void>>();

  enqueue(id: string, deliver: (signal: AbortSignal) => Promise<void>): Promise<void> {
    const existing = this.flights.get(id);
    if (existing) return existing;
    this.direct?.abort();
    const controller = new AbortController();
    this.direct = controller;
    const flight = this.tail.then(() => deliver(controller.signal));
    this.flights.set(id, flight);
    this.tail = flight.catch(() => undefined);
    void flight.catch(() => { this.flights.delete(id); });
    return flight;
  }

  cancelDirect(): void { this.direct?.abort(); }
}
