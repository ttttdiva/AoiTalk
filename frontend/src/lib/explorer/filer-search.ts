/** ファイラーの名前検索・置換で共有する正規表現ヘルパー。 */

export function escapeFilerSearchPattern(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function globToFilerSearchPattern(value: string): string {
  let source = "";
  for (const character of value) {
    if (character === "*") {
      source += ".*";
    } else if (character === "?") {
      source += ".";
    } else {
      source += escapeFilerSearchPattern(character);
    }
  }
  return `^${source}$`;
}

/**
 * 検索欄の値から、名前検索用の大文字小文字を区別しない正規表現を作る。
 * 通常検索も同じ実装へ寄せることで、検索結果と置換結果の判定を一致させる。
 */
export function createFilerSearchPattern(
  query: string,
  useRegex = false,
): RegExp {
  const trimmed = query.trim();
  if (!trimmed) throw new Error("検索文字列を入力してください");

  try {
    const hasGlob = !useRegex && (trimmed.includes("*") || trimmed.includes("?"));
    return new RegExp(
      useRegex
        ? trimmed
        : hasGlob
          ? globToFilerSearchPattern(trimmed)
          : escapeFilerSearchPattern(trimmed),
      "gi",
    );
  } catch {
    throw new Error("正規表現が無効です");
  }
}

export function filerNameMatches(
  name: string,
  query: string,
  useRegex = false,
): boolean {
  const pattern = createFilerSearchPattern(query, useRegex);
  pattern.lastIndex = 0;
  return pattern.test(name);
}

export function replaceFilerName(
  name: string,
  query: string,
  replacement: string,
  useRegex = false,
): string {
  return name.replace(
    createFilerSearchPattern(query, useRegex),
    replacement,
  );
}
