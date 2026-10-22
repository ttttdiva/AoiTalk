const preset = require("jest-expo/jest-preset");

const transform = { ...preset.transform };
const babelKey = "\\.[jt]sx?$";
const babelOptions = Array.isArray(transform[babelKey])
  ? transform[babelKey][1]
  : {};
transform["^.+\\.mjs$"] = ["babel-jest", { ...babelOptions }];

module.exports = {
  ...preset,
  rootDir: __dirname,
  transform,
  setupFiles: [...(preset.setupFiles || []), "<rootDir>/jest.setup.js"],
  testMatch: [
    "**/__tests__/**/*.test.ts",
    "**/__tests__/**/*.test.tsx",
  ],
  modulePaths: ["<rootDir>/node_modules"],
  moduleNameMapper: {
    ...(preset.moduleNameMapper || {}),
    "^@aoi-talk/mobile-gateway-auth$":
      "<rootDir>/vendor/mobile-gateway/index.mjs",
    "^@babel/runtime/(.*)$": "<rootDir>/node_modules/@babel/runtime/$1",
  },
};
