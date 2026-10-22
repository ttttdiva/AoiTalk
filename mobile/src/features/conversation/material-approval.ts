/** Decode the existing HumanInteractionManager material approval envelope. */
export function materialApprovalResponse(data: Record<string, unknown>): Record<string, unknown> | null {
  const binding = data.plan_binding;
  const preview = data.material_action_preview;
  if (!binding || typeof binding !== "object" || Array.isArray(binding) ||
      !preview || typeof preview !== "object" || Array.isArray(preview)) return null;
  const fields = binding as Record<string, unknown>;
  const material = preview as Record<string, unknown>;
  const keys = ["plan_id", "revision", "plan_digest", "action_digest", "context_selection_hash",
    "evidence_hash_set_digest", "material_action_preview_digest"];
  if (Object.keys(fields).length !== keys.length || Object.keys(fields).some((key) => !keys.includes(key))) return null;
  if (!Number.isInteger(data.revision) || (data.revision as number) < 1 ||
      data.revision !== data.plan_revision || data.revision !== fields.revision) return null;
  if (keys.filter((key) => key !== "revision").some((key) => typeof fields[key] !== "string" || !fields[key])) return null;
  if (data.plan_digest !== fields.plan_digest || data.action_digest !== fields.action_digest ||
      data.material_action_preview_digest !== fields.material_action_preview_digest) return null;
  if (typeof data.plan_text !== "string" || material.version !== 1 || !Array.isArray(material.actions) ||
      material.actions.length < 1 || material.actions.length > 32 ||
      material.action_count !== material.actions.length) return null;
  if (material.actions.some((item: unknown, index: number) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) return true;
    const action = item as Record<string, unknown>;
    return action.index !== index || typeof action.tool !== "string" || !action.tool ||
      !action.arguments || typeof action.arguments !== "object" || Array.isArray(action.arguments) ||
      !Array.isArray(action.dynamic_fields) || action.dynamic_fields.length !== 0;
  })) return null;
  return { action: "approve", plan_text: data.plan_text, revision: data.revision,
    plan_binding: { ...fields }, action_digest: data.action_digest,
    material_action_preview_digest: data.material_action_preview_digest };
}
