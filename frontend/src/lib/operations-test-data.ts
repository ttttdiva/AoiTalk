/**
 * QA / acceptance fixtures live in the same tables as real operations data
 * (e.g. "QA Media Vault Character", "受入確認 20261011・…"). Operations hides
 * them by default; every list that can contain them offers a "テスト用を表示" toggle.
 */
const TEST_FIXTURE_NAME = /(?:^|[^A-Za-z])QA(?:[^A-Za-z]|$)|受入確認|acceptance|fixture/iu;

export function isTestFixtureName(...names: Array<string | null | undefined>): boolean {
  return names.some(name => typeof name === "string" && TEST_FIXTURE_NAME.test(name));
}

export function withoutTestFixtures<T>(items: readonly T[], names: (item: T) => Array<string | null | undefined>, includeTest = false): T[] {
  return includeTest ? [...items] : items.filter(item => !isTestFixtureName(...names(item)));
}
