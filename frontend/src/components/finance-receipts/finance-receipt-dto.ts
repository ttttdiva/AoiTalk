/** Display-only helpers; wire contracts come from the generated OpenAPI types. */
export function maskReference(value: string): string {
  const trimmed = value.trim();
  if (!trimmed) return "…";
  return trimmed.length <= 4 ? "•".repeat(trimmed.length) : `…${trimmed.slice(-4)}`;
}

export function maskDigest(value: string): string {
  return /^[0-9a-f]{64}$/.test(value) ? `${value.slice(0, 8)}…${value.slice(-4)}` : "…";
}

export function maskByteSize(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

/** Raw integer minor-unit display; no currency exponent or conversion is inferred. */
export function displayMinorUnitsRaw(amountMinor: number, currency: string): string {
  return `${amountMinor} ${currency}（最小単位）`;
}
