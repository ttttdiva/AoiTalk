const { getDefaultConfig } = require('expo/metro-config');

const path = require('node:path');
const config = getDefaultConfig(__dirname);
// Local canonical runtime ships with 41; Metro must watch it outside the app root.
config.watchFolders = [
  ...(config.watchFolders || []),
  path.resolve(__dirname, '../resources/seethrough-rig/runtime'),
  path.resolve(__dirname, '../resources/chat-background'),
];

if (!config.resolver.assetExts.includes('wasm')) {
  config.resolver.assetExts.push('wasm');
}

module.exports = config;
