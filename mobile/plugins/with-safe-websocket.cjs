const { withMainApplication } = require("@expo/config-plugins");

const KOTLIN_IMPORTS = [
  "import com.facebook.react.modules.network.CustomClientBuilder",
  "import com.facebook.react.modules.websocket.WebSocketModule",
];

const JAVA_IMPORTS = [
  "import com.facebook.react.modules.network.CustomClientBuilder;",
  "import com.facebook.react.modules.websocket.WebSocketModule;",
];

const HOOK_MARKER = "AoiTalk WebSocket redirect policy";

function addImports(contents, imports) {
  const missing = imports.filter((line) => !contents.includes(line));
  if (missing.length === 0) return contents;

  const packageMatch = contents.match(/^package[^\r\n]*(?:\r?\n)/m);
  const importBlock = `${missing.join("\n")}\n`;
  if (!packageMatch || packageMatch.index === undefined) {
    return `${importBlock}${contents}`;
  }
  const insertAt = packageMatch.index + packageMatch[0].length;
  return `${contents.slice(0, insertAt)}\n${importBlock}${contents.slice(insertAt)}`;
}

function applyKotlin(contents) {
  let next = addImports(contents, KOTLIN_IMPORTS);
  if (next.includes(HOOK_MARKER)) return next;

  const hook = [
    "    // AoiTalk WebSocket redirect policy: reject native handshake redirects.",
    "    WebSocketModule.setCustomClientBuilder(",
    "      CustomClientBuilder { builder ->",
    "        builder.followRedirects(false)",
    "        builder.followSslRedirects(false)",
    "      }",
    "    )",
    "",
  ].join("\n");
  const onCreate = /(override\s+fun\s+onCreate\s*\(\s*\)\s*\{\s*super\.onCreate\s*\(\s*\)\s*\r?\n)/m;
  if (!onCreate.test(next)) {
    throw new Error("with-safe-websocket requires a Kotlin MainApplication onCreate after super.onCreate()");
  }
  next = next.replace(onCreate, (prefix) => `${prefix}${hook}`);
  return next;
}

function applyJava(contents) {
  let next = addImports(contents, JAVA_IMPORTS);
  if (next.includes(HOOK_MARKER)) return next;

  const hook = [
    "    // AoiTalk WebSocket redirect policy: reject native handshake redirects.",
    "    WebSocketModule.setCustomClientBuilder((CustomClientBuilder) builder -> {",
    "      builder.followRedirects(false);",
    "      builder.followSslRedirects(false);",
    "    });",
    "",
  ].join("\n");
  const onCreate = /(public\s+void\s+onCreate\s*\(\s*\)\s*\{\s*super\.onCreate\s*\(\s*\)\s*;\s*\r?\n)/m;
  if (!onCreate.test(next)) {
    throw new Error("with-safe-websocket requires a Java MainApplication onCreate after super.onCreate()");
  }
  next = next.replace(onCreate, (prefix) => `${prefix}${hook}`);
  return next;
}

function applySafeWebSocketPolicy(mainApplication) {
  if (!mainApplication || typeof mainApplication.contents !== "string") {
    throw new Error("with-safe-websocket received an invalid MainApplication file");
  }
  if (mainApplication.language === "kt") {
    return { ...mainApplication, contents: applyKotlin(mainApplication.contents) };
  }
  if (mainApplication.language === "java") {
    return { ...mainApplication, contents: applyJava(mainApplication.contents) };
  }
  throw new Error(`with-safe-websocket does not support MainApplication language: ${mainApplication.language}`);
}

function withSafeWebSocket(config) {
  return withMainApplication(config, (modConfig) => {
    modConfig.modResults = applySafeWebSocketPolicy(modConfig.modResults);
    return modConfig;
  });
}

module.exports = withSafeWebSocket;
module.exports.applySafeWebSocketPolicy = applySafeWebSocketPolicy;
