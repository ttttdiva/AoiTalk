// AsyncStorage をテスト全体で公式のインメモリモックに差し替える。
// 個別テストで jest.mock により上書きすることも可能。
jest.mock("@react-native-async-storage/async-storage", () =>
  require("@react-native-async-storage/async-storage/jest/async-storage-mock"),
);

// Native keyboard events use the library-provided test implementation.
jest.mock("react-native-keyboard-controller", () =>
  require("react-native-keyboard-controller/jest"),
);

// Native Expo Fetch is unavailable in the Jest VM. Protected transport tests
// still exercise credentials/redirect options by forwarding to the test
// global fetch; device builds resolve the real `expo/fetch` module.
jest.mock("expo/fetch", () => ({
  fetch: (...args) => global.fetch(...args),
}));

jest.mock("expo-crypto", () => ({
  getRandomBytes: (length) => Uint8Array.from({ length }, (_, index) => index + 1),
  getRandomBytesAsync: async (length) => Uint8Array.from({ length }, (_, index) => index + 1),
  digestStringAsync: async () => "",
  CryptoDigestAlgorithm: { SHA256: "SHA-256" },
  CryptoEncoding: { BASE64: "base64" },
}));
