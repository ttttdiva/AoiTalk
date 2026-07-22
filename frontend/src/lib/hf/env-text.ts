function parseEnvLine(line: string): { key: string; value: string } | null {
  const match = line.match(/^\s*([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(.*)\s*$/);
  if (!match) return null;
  let value = match[2] ?? "";
  if (
    value.length >= 2 &&
    ((value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'")))
  ) {
    value = value.slice(1, -1);
  }
  return { key: match[1], value };
}

function serializeEnvValue(value: string): string {
  if (!/[\s#'\\]/.test(value)) return value;
  return JSON.stringify(value);
}

export function updateEnvText(
  source: string,
  updates: Record<string, string>,
): string {
  const newline = source.includes("\r\n") ? "\r\n" : "\n";
  const hadTrailingNewline = source.endsWith("\n");
  const lines = source ? source.split(/\r?\n/) : [];
  if (hadTrailingNewline && lines.at(-1) === "") lines.pop();

  const remaining = new Map(
    Object.entries(updates).map(([key, value]) => [key.toUpperCase(), { key, value }]),
  );
  const next = lines.map((line) => {
    const parsed = parseEnvLine(line);
    if (!parsed) return line;
    const update = remaining.get(parsed.key.toUpperCase());
    if (!update) return line;
    remaining.delete(parsed.key.toUpperCase());
    return `${update.key}=${serializeEnvValue(update.value)}`;
  });

  if (remaining.size > 0 && next.length > 0 && next.at(-1)?.trim()) next.push("");
  for (const { key, value } of remaining.values()) {
    next.push(`${key}=${serializeEnvValue(value)}`);
  }
  return next.join(newline) + newline;
}
