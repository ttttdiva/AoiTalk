/**
 * Shared Docs blank-paragraph contract for the mobile repository/editor.
 *
 * An empty title is valid user content only when it is an ordinary `node`
 * carrying the explicit `doc_block` paragraph marker.  Keeping these helpers
 * independent from the repository module also lets UI tests mock docsRepo
 * without losing the normalization predicate.
 */

/** Return true only for the canonical persisted blank paragraph shape. */
export function isExplicitBlankParagraph(
  title: string | null | undefined,
  bodyJson: unknown,
  nodeType: string | null | undefined,
): boolean {
  if (title !== "" || nodeType !== "node") return false;
  if (!bodyJson || typeof bodyJson !== "object" || Array.isArray(bodyJson)) {
    return false;
  }
  const body = bodyJson as Record<string, unknown>;
  return body.format === "doc_block"
    && body.block_type === "paragraph"
    && body.blank === true;
}

/** Return true for an editable multiline markdown/code Docs block body. */
export function isEditableDocsBlockBody(bodyJson: unknown): boolean {
  if (!bodyJson || typeof bodyJson !== "object" || Array.isArray(bodyJson)) {
    return false;
  }
  const body = bodyJson as Record<string, unknown>;
  return body.format === "doc_block"
    && (body.block_type === "markdown" || body.block_type === "code");
}

/** Build the canonical body envelope for a persisted empty paragraph. */
export function blankParagraphBodyJson(existingBodyJson: unknown): Record<string, unknown> {
  const existing = existingBodyJson
    && typeof existingBodyJson === "object"
    && !Array.isArray(existingBodyJson)
    ? existingBodyJson as Record<string, unknown>
    : {};
  return {
    ...existing,
    format: "doc_block",
    block_type: "paragraph",
    blank: true,
  };
}

/** Remove only the explicit blank marker when a paragraph receives text. */
export function clearBlankParagraphMarker(existingBodyJson: unknown): Record<string, unknown> {
  const existing = existingBodyJson
    && typeof existingBodyJson === "object"
    && !Array.isArray(existingBodyJson)
    ? existingBodyJson as Record<string, unknown>
    : {};
  const next = { ...existing };
  delete next.blank;
  return next;
}
