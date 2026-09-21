const { getAndroidPreset, getIOSPreset } = require('jest-expo/config/getPlatformPreset');

function nativeProject(preset) {
  const { watchPlugins: _watchPlugins, ...projectPreset } = preset;
  const transform = { ...preset.transform };
  const babelKey = Object.keys(transform).find((key) =>
    Array.isArray(transform[key]) && transform[key][0] === 'babel-jest',
  );
  // Expo 54's platform presets replace the default transform and lose its
  // implicit Babel config. Keep the real SDK preset and platform caller.
  const [babelJest, options] = transform[babelKey];
  transform[babelKey] = [babelJest, {
    ...options,
    presets: [require.resolve('babel-preset-expo')],
  }];
  return {
    ...projectPreset,
    rootDir: __dirname,
    transform,
    setupFiles: [...preset.setupFiles, '<rootDir>/jest.setup.js'],
    testMatch: ['**/chat/__tests__/index.native.test.tsx'],
  };
}

module.exports = {
  projects: [nativeProject(getAndroidPreset()), nativeProject(getIOSPreset())],
};
