/**
 * 単独の改行を Markdown のhard breakへ変換する。
 *
 * ユーザーが書いた本文やタスクコメントは改行がそのまま意味を持つのに、
 * Markdownの既定では単独改行が段落内で連結されて1行になってしまう。
 * fenced code block の中は変換しない。
 */
export function withHardLineBreaks(markdown: string): string {
  const lines = String(markdown ?? "").split("\n");
  let inFence = false;
  return lines
    .map((line, index) => {
      if (/^\s*(```|~~~)/.test(line)) {
        inFence = !inFence;
        return line;
      }
      if (inFence) return line;
      const next = lines[index + 1];
      if (next === undefined || next.trim() === "") return line;
      if (line.trim() === "") return line;
      if (/(\s{2,}|\\)$/.test(line)) return line;
      return `${line}  `;
    })
    .join("\n");
}
