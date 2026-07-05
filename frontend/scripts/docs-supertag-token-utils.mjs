export function deriveTitleFromBodyText(bodyText) {
  const firstLine = String(bodyText ?? "")
    .replace(/\r\n?/g, "\n")
    .split("\n")
    .map((line) => line.trim())
    .find(Boolean);
  return (firstLine ?? "").slice(0, 500);
}

export function stripAssignedSupertagTokens(bodyText, tagNames) {
  const tagSet = new Set(
    Array.from(tagNames ?? [])
      .filter((name) => typeof name === "string")
      .map((name) => name.trim().toLowerCase())
      .filter(Boolean),
  );
  if (tagSet.size === 0) {
    return { text: String(bodyText ?? ""), removed: 0 };
  }

  let removed = 0;
  const lines = String(bodyText ?? "")
    .replace(/\r\n?/g, "\n")
    .split("\n")
    .map((line) => {
      const next = line.replace(/(^|[\s([{])#([\p{L}\p{N}_-]{1,80})(?=$|[\s)\]},.!?:;])/giu, (match, prefix, name) => {
        if (!tagSet.has(String(name).toLowerCase())) return match;
        removed += 1;
        return /\S/.test(prefix) ? prefix : "";
      });
      return next
        .replace(/[ \t]{2,}/g, " ")
        .replace(/[ \t]+([,.;:!?])/g, "$1")
        .trim();
    });

  return {
    text: lines.join("\n").replace(/\n{3,}/g, "\n\n").trim(),
    removed,
  };
}
