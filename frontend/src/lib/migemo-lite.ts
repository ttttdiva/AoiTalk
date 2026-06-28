export interface MigemoDictionaryEntry {
  key: string;
  values: string[];
}

export interface FilerSearchItem {
  path: string;
  name: string;
}

const ROMAJI_TABLE: Record<string, string> = {
  a: "あ",
  i: "い",
  u: "う",
  e: "え",
  o: "お",
  xa: "ぁ",
  xi: "ぃ",
  xu: "ぅ",
  xe: "ぇ",
  xo: "ぉ",
  la: "ぁ",
  li: "ぃ",
  lu: "ぅ",
  le: "ぇ",
  lo: "ぉ",
  ka: "か",
  ki: "き",
  ku: "く",
  ke: "け",
  ko: "こ",
  kya: "きゃ",
  kyi: "きぃ",
  kyu: "きゅ",
  kye: "きぇ",
  kyo: "きょ",
  ga: "が",
  gi: "ぎ",
  gu: "ぐ",
  ge: "げ",
  go: "ご",
  gya: "ぎゃ",
  gyi: "ぎぃ",
  gyu: "ぎゅ",
  gye: "ぎぇ",
  gyo: "ぎょ",
  sa: "さ",
  si: "し",
  shi: "し",
  su: "す",
  se: "せ",
  so: "そ",
  sya: "しゃ",
  syi: "しぃ",
  syu: "しゅ",
  sye: "しぇ",
  syo: "しょ",
  sha: "しゃ",
  shu: "しゅ",
  she: "しぇ",
  sho: "しょ",
  za: "ざ",
  zi: "じ",
  ji: "じ",
  zu: "ず",
  ze: "ぜ",
  zo: "ぞ",
  zya: "じゃ",
  zyi: "じぃ",
  zyu: "じゅ",
  zye: "じぇ",
  zyo: "じょ",
  ja: "じゃ",
  ju: "じゅ",
  je: "じぇ",
  jo: "じょ",
  ta: "た",
  ti: "ち",
  chi: "ち",
  tu: "つ",
  tsu: "つ",
  te: "て",
  to: "と",
  tya: "ちゃ",
  tyi: "ちぃ",
  tyu: "ちゅ",
  tye: "ちぇ",
  tyo: "ちょ",
  cha: "ちゃ",
  chu: "ちゅ",
  che: "ちぇ",
  cho: "ちょ",
  xtu: "っ",
  xtsu: "っ",
  ltu: "っ",
  ltsu: "っ",
  da: "だ",
  di: "ぢ",
  du: "づ",
  de: "で",
  do: "ど",
  dya: "ぢゃ",
  dyi: "ぢぃ",
  dyu: "ぢゅ",
  dye: "ぢぇ",
  dyo: "ぢょ",
  na: "な",
  ni: "に",
  nu: "ぬ",
  ne: "ね",
  no: "の",
  nya: "にゃ",
  nyi: "にぃ",
  nyu: "にゅ",
  nye: "にぇ",
  nyo: "にょ",
  ha: "は",
  hi: "ひ",
  hu: "ふ",
  fu: "ふ",
  he: "へ",
  ho: "ほ",
  hya: "ひゃ",
  hyi: "ひぃ",
  hyu: "ひゅ",
  hye: "ひぇ",
  hyo: "ひょ",
  fa: "ふぁ",
  fi: "ふぃ",
  fe: "ふぇ",
  fo: "ふぉ",
  fya: "ふゃ",
  fyu: "ふゅ",
  fyo: "ふょ",
  ba: "ば",
  bi: "び",
  bu: "ぶ",
  be: "べ",
  bo: "ぼ",
  bya: "びゃ",
  byi: "びぃ",
  byu: "びゅ",
  bye: "びぇ",
  byo: "びょ",
  pa: "ぱ",
  pi: "ぴ",
  pu: "ぷ",
  pe: "ぺ",
  po: "ぽ",
  pya: "ぴゃ",
  pyi: "ぴぃ",
  pyu: "ぴゅ",
  pye: "ぴぇ",
  pyo: "ぴょ",
  ma: "ま",
  mi: "み",
  mu: "む",
  me: "め",
  mo: "も",
  mya: "みゃ",
  myi: "みぃ",
  myu: "みゅ",
  mye: "みぇ",
  myo: "みょ",
  ya: "や",
  yu: "ゆ",
  yo: "よ",
  xya: "ゃ",
  xyu: "ゅ",
  xyo: "ょ",
  lya: "ゃ",
  lyu: "ゅ",
  lyo: "ょ",
  ra: "ら",
  ri: "り",
  ru: "る",
  re: "れ",
  ro: "ろ",
  rya: "りゃ",
  ryi: "りぃ",
  ryu: "りゅ",
  rye: "りぇ",
  ryo: "りょ",
  wa: "わ",
  wi: "うぃ",
  we: "うぇ",
  wo: "を",
  va: "ゔぁ",
  vi: "ゔぃ",
  vu: "ゔ",
  ve: "ゔぇ",
  vo: "ゔぉ",
  nn: "ん",
};

const CONSONANTS = new Set("bcdfghjklmpqrstvwxyz".split(""));

export function normalizeSearchText(value: string): string {
  return value.normalize("NFKC").toLowerCase();
}

export function hiraganaToKatakana(value: string): string {
  return value.replace(/[\u3041-\u3096]/g, (char) =>
    String.fromCharCode(char.charCodeAt(0) + 0x60),
  );
}

export function katakanaToHiragana(value: string): string {
  return value.replace(/[\u30a1-\u30f6]/g, (char) =>
    String.fromCharCode(char.charCodeAt(0) - 0x60),
  );
}

export function romajiToHiragana(input: string): string {
  const source = normalizeSearchText(input);
  let result = "";
  let i = 0;

  while (i < source.length) {
    const char = source[i];
    const next = source[i + 1];

    if (char === "n" && next === "'") {
      result += "ん";
      i += 2;
      continue;
    }
    if (char === "n" && (!next || !/[aiueoyn]/.test(next))) {
      result += "ん";
      i += 1;
      continue;
    }
    if (next && char === next && char !== "n" && CONSONANTS.has(char)) {
      result += "っ";
      i += 1;
      continue;
    }

    let converted = "";
    let consumed = 0;
    for (const length of [3, 2, 1]) {
      const token = source.slice(i, i + length);
      if (ROMAJI_TABLE[token]) {
        converted = ROMAJI_TABLE[token];
        consumed = length;
        break;
      }
    }

    if (converted) {
      result += converted;
      i += consumed;
    } else {
      result += char;
      i += 1;
    }
  }

  return result;
}

export function buildFallbackMigemoTerms(query: string): string[] {
  const normalized = normalizeSearchText(query.trim());
  if (!normalized) return [];

  const hiragana = romajiToHiragana(normalized);
  const katakana = hiraganaToKatakana(hiragana);
  return uniqueTerms([
    normalized,
    hiragana,
    katakana,
    katakanaToHiragana(normalized),
    hiraganaToKatakana(normalized),
  ]);
}

export function buildMigemoTermsFromEntries(
  query: string,
  entries: MigemoDictionaryEntry[],
  limit = 200,
): string[] {
  const fallbackTerms = buildFallbackMigemoTerms(query);
  const kanaPrefixes = fallbackTerms
    .map((term) => katakanaToHiragana(term))
    .filter((term) => /[\u3041-\u3096]/.test(term));

  const terms: string[] = [...fallbackTerms];
  for (const prefix of kanaPrefixes) {
    for (const entry of entries) {
      if (!entry.key.startsWith(prefix)) continue;
      terms.push(entry.key, hiraganaToKatakana(entry.key), ...entry.values);
      if (terms.length >= limit * 2) break;
    }
    if (terms.length >= limit * 2) break;
  }

  return uniqueTerms(terms).slice(0, limit);
}

export function itemMatchesMigemoTerms(
  item: FilerSearchItem,
  terms: string[],
): boolean {
  const name = normalizeSearchText(item.name);
  const kanaName = katakanaToHiragana(name);
  return terms.some((term) => {
    const normalizedTerm = normalizeSearchText(term);
    if (!normalizedTerm) return false;
    const kanaTerm = katakanaToHiragana(normalizedTerm);
    return name.includes(normalizedTerm) || kanaName.includes(kanaTerm);
  });
}

export function findIncrementalSearchMatch(
  items: FilerSearchItem[],
  activePath: string | null,
  terms: string[],
): FilerSearchItem | null {
  if (items.length === 0 || terms.length === 0) return null;

  const activeIndex = activePath
    ? items.findIndex((item) => item.path === activePath)
    : -1;
  if (activeIndex >= 0 && itemMatchesMigemoTerms(items[activeIndex], terms)) {
    return items[activeIndex];
  }

  const startIndex = activeIndex >= 0 ? activeIndex + 1 : 0;
  for (let offset = 0; offset < items.length; offset += 1) {
    const index = (startIndex + offset) % items.length;
    if (itemMatchesMigemoTerms(items[index], terms)) return items[index];
  }

  return null;
}

function uniqueTerms(terms: string[]): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const term of terms) {
    const normalized = normalizeSearchText(term.trim());
    if (!normalized || seen.has(normalized)) continue;
    seen.add(normalized);
    result.push(term.trim());
  }
  return result;
}
