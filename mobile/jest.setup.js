// AsyncStorage をテスト全体で公式のインメモリモックに差し替える。
// 個別テストで jest.mock により上書きすることも可能。
jest.mock("@react-native-async-storage/async-storage", () =>
  require("@react-native-async-storage/async-storage/jest/async-storage-mock"),
);
