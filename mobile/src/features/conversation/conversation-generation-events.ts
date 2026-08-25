import type { WSMessage } from "../../types/api";
import type { GenerationIdentity } from "./generation-reducer";

function recordOf(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function normalizedId(value: unknown): string | null {
  const id = typeof value === "string" ? value.trim() : "";
  return id || null;
}

export function generationTransportId(event: WSMessage): string | null {
  return generationTransportIds(event)[0] ?? null;
}

function generationTransportIds(event: WSMessage): string[] {
  const data = recordOf(event.data);
  return [
    normalizedId(event.agent_run_id) ??
      normalizedId(data.agent_run_id),
    normalizedId(event.request_id) ??
      normalizedId(data.request_id),
    normalizedId(event.client_message_id) ??
      normalizedId(data.client_message_id),
  ].filter((value): value is string => Boolean(value));
}

function sameGeneration(
  left: GenerationIdentity,
  right: GenerationIdentity,
): boolean {
  return (
    left.sessionId === right.sessionId &&
    left.lifecycleId === right.lifecycleId &&
    left.requestId === right.requestId
  );
}

type ActiveGenerationEvent = {
  transportIds: Set<string>;
  generation: GenerationIdentity;
};

/**
 * Associates transport run/request identity with the reducer lifecycle. Late
 * terminal events may still refresh durable data, but can clear React stream
 * state only through a matching GenerationIdentity.
 */
export class ConversationGenerationEventGate {
  private active: ActiveGenerationEvent | null = null;

  bind(event: WSMessage, generation: GenerationIdentity): void {
    this.bindTransportIds(generationTransportIds(event), generation);
  }

  bindTransportId(
    transportId: string | null | undefined,
    generation: GenerationIdentity,
  ): void {
    const normalized = normalizedId(transportId);
    this.bindTransportIds(normalized ? [normalized] : [], generation);
  }

  matchingTerminal(
    event: WSMessage,
    currentGeneration: GenerationIdentity | null,
    options: { allowIdentityless: boolean },
  ): GenerationIdentity | null {
    const active = this.active;
    if (
      !active ||
      !currentGeneration ||
      !sameGeneration(active.generation, currentGeneration)
    ) {
      return null;
    }
    const terminalTransportIds = generationTransportIds(event);
    if (terminalTransportIds.length > 0) {
      return terminalTransportIds.some((id) => active.transportIds.has(id))
        ? { ...active.generation }
        : null;
    }
    if (active.transportIds.size > 0 || !options.allowIdentityless) return null;
    return { ...active.generation };
  }

  complete(expected: GenerationIdentity): void {
    if (this.active && sameGeneration(this.active.generation, expected)) {
      this.active = null;
    }
  }

  reset(): void {
    this.active = null;
  }

  private bindTransportIds(
    transportIds: string[],
    generation: GenerationIdentity,
  ): void {
    if (this.active && sameGeneration(this.active.generation, generation)) {
      for (const id of transportIds) this.active.transportIds.add(id);
      return;
    }
    this.active = {
      transportIds: new Set(transportIds),
      generation: { ...generation },
    };
  }
}
