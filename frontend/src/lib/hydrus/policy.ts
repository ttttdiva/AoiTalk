/**
 * Compatibility re-export for personal Hydrus callers and tests.
 *
 * Enterprise handoffs exclude the Hydrus feature namespace.  Keep the
 * implementation in the neutral server namespace so shared Hugging Face
 * storage can retain its endpoint-policy dependency without pulling the
 * excluded tree into the sanitized artifact.
 */
export * from "../server/hydrus-policy";
