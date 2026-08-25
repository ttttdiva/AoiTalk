/** Count occurrences of one cookie name in the raw request Cookie header. */
export function countCookieOccurrences(
  rawCookieHeader: string | null | undefined,
  cookieName: string,
): number {
  const target = cookieName.trim();
  if (!target || !rawCookieHeader) return 0;

  return rawCookieHeader.split(/[;,]/).reduce((count, part) => {
    const separator = part.indexOf("=");
    if (separator < 0) return count;
    const name = part.slice(0, separator).trim();
    return name === target ? count + 1 : count;
  }, 0);
}
