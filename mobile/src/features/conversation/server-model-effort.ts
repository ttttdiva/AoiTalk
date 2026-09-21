import type { ChatResponseModelOption } from "../../types/api";
import { getDirectReasoningEffortOptions } from "../../lib/mobile-llm";
import { isDirectMobileLlmProvider } from "../../lib/cloud-model-catalog";
import type { ChatResponseTarget } from "./chat-llm-preferences";

/** The selected model's contract, never the server's unrelated global mode. */
export function resolveServerModelEffort(
  target: ChatResponseTarget,
  models: readonly ChatResponseModelOption[],
) {
  const selection = target.kind === "server" ? target.responseModel : undefined;
  const model = target.kind === "server"
    ? models.find((item) => selection
      ? item.provider === selection.provider && item.model === selection.model
      : item.isCurrent)
    : undefined;
  // Existing persisted catalogs may not contain capability metadata. Known
  // direct-provider models have the same public contract on the server route.
  // An explicit [] is authoritative and must never be replaced by a fallback.
  const choices = model?.reasoningEffortOptions ?? (model && isDirectMobileLlmProvider(model.provider)
    ? getDirectReasoningEffortOptions(model.provider, model.model) : []);
  const options = [...new Set(choices.filter((value) => typeof value === "string" && value.length > 0))];
  const preferred = model?.reasoningEffortDefault;
  const defaultValue = preferred && options.includes(preferred) ? preferred
    : options.includes("medium") ? "medium"
    : options.includes("fast") ? "fast"
    : options[0] ?? "";
  const value = selection?.reasoning_effort && options.includes(selection.reasoning_effort)
    ? selection.reasoning_effort : defaultValue;
  return {
    model, options, value,
    kind: model?.reasoningEffortKind ?? (options.includes("fast") ? "response_mode" : "reasoning_effort"),
    labels: Object.fromEntries(options.map((option) => [option, option[0].toUpperCase() + option.slice(1)])),
  };
}

/** Freeze the displayed model/effort into this turn, including the auto target. */
export function snapshotServerModelTarget(
  target: ChatResponseTarget,
  models: readonly ChatResponseModelOption[],
): ChatResponseTarget {
  if (target.kind !== "server") return target;
  const selected = resolveServerModelEffort(target, models);
  const model = selected.model ?? target.responseModel;
  if (!model) return { kind: "server" };
  return { kind: "server", responseModel: {
    provider: model.provider,
    model: model.model,
    ...(selected.value ? { reasoning_effort: selected.value } : {}),
  } };
}
