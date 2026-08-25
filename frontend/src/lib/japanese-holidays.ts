// 日本の国民の祝日を計算で求める。
// 以前は年ごとの日付をハードコードしていたためリスト末尾の年を過ぎるとスキップが
// 無反応になっていた。祝日法の規則（固定日・ハッピーマンデー・春分/秋分・振替休日・
// 国民の休日）をそのまま実装して恒久的に判定できるようにする。
//
// 春分/秋分の近似式の有効範囲に合わせて 1980-2099 年を対象とする。
// 範囲外の年は祝日なしとして扱う（誤った日をスキップするより安全side）。

const SUPPORTED_YEAR_MIN = 1980;
const SUPPORTED_YEAR_MAX = 2099;

/** 指定した月の第 nth 月曜日の日を返す（ハッピーマンデー用）。 */
function nthMondayOfMonth(year: number, month: number, nth: number): number {
  // month は 1-12。Date の getDay() は 0=日曜, 1=月曜。
  const firstWeekday = new Date(year, month - 1, 1).getDay();
  const offsetToMonday = (1 - firstWeekday + 7) % 7;
  return 1 + offsetToMonday + (nth - 1) * 7;
}

/** 春分の日（1980-2099 用の近似式）。 */
function vernalEquinoxDay(year: number): number {
  return Math.floor(
    20.8431 + 0.242194 * (year - 1980) - Math.floor((year - 1980) / 4),
  );
}

/** 秋分の日（1980-2099 用の近似式）。 */
function autumnalEquinoxDay(year: number): number {
  return Math.floor(
    23.2488 + 0.242194 * (year - 1980) - Math.floor((year - 1980) / 4),
  );
}

function dateKey(year: number, month: number, day: number): string {
  return `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
}

/** 祝日法に定められた「国民の祝日」本体（振替休日・国民の休日を含まない）。 */
function buildBaseHolidays(year: number): Map<string, string> {
  const holidays = new Map<string, string>();
  const add = (month: number, day: number, name: string) => {
    holidays.set(dateKey(year, month, day), name);
  };

  add(1, 1, "元日");
  add(1, nthMondayOfMonth(year, 1, 2), "成人の日");
  add(2, 11, "建国記念の日");
  add(2, 23, "天皇誕生日");
  add(3, vernalEquinoxDay(year), "春分の日");
  add(4, 29, "昭和の日");
  add(5, 3, "憲法記念日");
  add(5, 4, "みどりの日");
  add(5, 5, "こどもの日");
  add(7, nthMondayOfMonth(year, 7, 3), "海の日");
  add(8, 11, "山の日");
  add(9, nthMondayOfMonth(year, 9, 3), "敬老の日");
  add(9, autumnalEquinoxDay(year), "秋分の日");
  add(10, nthMondayOfMonth(year, 10, 2), "スポーツの日");
  add(11, 3, "文化の日");
  add(11, 23, "勤労感謝の日");

  return holidays;
}

/**
 * 振替休日と国民の休日を加えた、その年の休日全体を返す。
 * - 振替休日: 祝日が日曜に当たるとき、その後の最初の平日
 * - 国民の休日: 祝日に挟まれた平日（9月の敬老の日と秋分の日の間など）
 */
function buildYearHolidays(year: number): Map<string, string> {
  const holidays = buildBaseHolidays(year);

  // 振替休日。1月1日から順に見て、日曜の祝日の次の平日を休日にする。
  for (let month = 1; month <= 12; month++) {
    const daysInMonth = new Date(year, month, 0).getDate();
    for (let day = 1; day <= daysInMonth; day++) {
      const key = dateKey(year, month, day);
      if (!holidays.has(key)) continue;
      const date = new Date(year, month - 1, day);
      if (date.getDay() !== 0) continue;

      const substitute = new Date(date);
      do {
        substitute.setDate(substitute.getDate() + 1);
      } while (
        holidays.has(
          dateKey(
            substitute.getFullYear(),
            substitute.getMonth() + 1,
            substitute.getDate(),
          ),
        )
      );
      if (substitute.getFullYear() === year) {
        holidays.set(
          dateKey(year, substitute.getMonth() + 1, substitute.getDate()),
          "振替休日",
        );
      }
    }
  }

  // 国民の休日。前後がともに祝日である平日（日曜を除く）を休日にする。
  for (let month = 1; month <= 12; month++) {
    const daysInMonth = new Date(year, month, 0).getDate();
    for (let day = 1; day <= daysInMonth; day++) {
      const key = dateKey(year, month, day);
      if (holidays.has(key)) continue;
      const date = new Date(year, month - 1, day);
      if (date.getDay() === 0) continue;

      const prev = new Date(date);
      prev.setDate(prev.getDate() - 1);
      const next = new Date(date);
      next.setDate(next.getDate() + 1);
      const prevKey = dateKey(
        prev.getFullYear(),
        prev.getMonth() + 1,
        prev.getDate(),
      );
      const nextKey = dateKey(
        next.getFullYear(),
        next.getMonth() + 1,
        next.getDate(),
      );
      if (holidays.has(prevKey) && holidays.has(nextKey)) {
        holidays.set(key, "国民の休日");
      }
    }
  }

  return holidays;
}

const yearCache = new Map<number, Map<string, string>>();

function getYearHolidays(year: number): Map<string, string> {
  let cached = yearCache.get(year);
  if (!cached) {
    cached = buildYearHolidays(year);
    yearCache.set(year, cached);
  }
  return cached;
}

/** 指定日が日本の休日（国民の祝日・振替休日・国民の休日）かどうか。 */
export function isJapaneseHoliday(date: Date): boolean {
  return japaneseHolidayName(date) !== null;
}

/** 指定日の休日名。休日でなければ null。 */
export function japaneseHolidayName(date: Date): string | null {
  const year = date.getFullYear();
  if (year < SUPPORTED_YEAR_MIN || year > SUPPORTED_YEAR_MAX) return null;
  const key = dateKey(year, date.getMonth() + 1, date.getDate());
  return getYearHolidays(year).get(key) ?? null;
}

/** 指定年の休日を YYYY-MM-DD の昇順配列で返す（検証・テスト用）。 */
export function listJapaneseHolidays(year: number): string[] {
  if (year < SUPPORTED_YEAR_MIN || year > SUPPORTED_YEAR_MAX) return [];
  return [...getYearHolidays(year).keys()].sort();
}
