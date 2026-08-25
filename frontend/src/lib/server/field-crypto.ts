import crypto from "crypto";
import fs from "fs";
import path from "path";
import { spawnSync } from "child_process";

export const ENCRYPTION_PREFIX = "enc:v1:";

const ALG = "aes256gcm";
const KEY_ID = "local";
const NONCE_LEN = 12;
const MAX_LOCAL_KEY_FILE_BYTES = 4096;
const MAX_KEY_PROVIDER_OUTPUT_BYTES = 8192;
const KEY_PROVIDER_TIMEOUT_MS = 10_000;
const ASCII_WHITESPACE = new Set([0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x20]);
const LOCAL_KEY_LINUX_ONLY_ERROR =
  "local field crypto key-file fallback is supported only on Linux";
const KEY_FILE_MODE_ERROR =
  "local field crypto key file permissions must be 0400 or 0600; rotate the key file before retrying";
const SENSITIVE_KEY_RE =
  /(api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|credential|client[_-]?secret|pass)/i;

let cachedKey: Buffer | null = null;

function b64urlEncode(data: Buffer): string {
  return data
    .toString("base64")
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/g, "");
}

function b64urlDecode(value: string): Buffer {
  if (!/^[A-Za-z0-9_-]*$/.test(value) || value.length % 4 === 1) {
    throw new Error("unsupported encrypted field format");
  }
  const padded = value + "=".repeat((4 - (value.length % 4)) % 4);
  const decoded = Buffer.from(padded.replace(/-/g, "+").replace(/_/g, "/"), "base64");
  if (b64urlEncode(decoded) !== value) {
    throw new Error("unsupported encrypted field format");
  }
  return decoded;
}

function findRepoRoot(start = process.cwd()): string {
  let current = path.resolve(start);
  for (;;) {
    if (
      fs.existsSync(path.join(current, "pyproject.toml")) &&
      fs.existsSync(path.join(current, "scripts", "field_crypto_key.ps1"))
    ) {
      return current;
    }
    const parent = path.dirname(current);
    if (parent === current) break;
    current = parent;
  }
  return path.resolve(process.cwd(), "..");
}

function decodeKeyProviderOutput(output: string, provider: string): Buffer {
  if (Buffer.byteLength(output, "utf8") > MAX_KEY_PROVIDER_OUTPUT_BYTES) {
    throw new Error(`${provider} returned an invalid key`);
  }
  const raw = output.trim().split(/\r?\n/).pop()?.trim() || "";
  if (!/^[A-Za-z0-9+/]{43}=$/.test(raw)) {
    throw new Error(`${provider} returned an invalid key`);
  }
  const key = Buffer.from(raw, "base64");
  if (key.length !== 32 || key.toString("base64") !== raw) {
    throw new Error(`${provider} returned an invalid key`);
  }
  return key;
}

function providerFailure(provider: string, status: number | null): Error {
  return typeof status === "number"
    ? new Error(`${provider} failed with exit status ${status}`)
    : new Error(`${provider} failed`);
}

function runKeyCommand(command: string, runner: typeof spawnSync = spawnSync): Buffer {
  const provider = "field crypto key command";
  const result = runner(command, {
    shell: true,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
    maxBuffer: MAX_KEY_PROVIDER_OUTPUT_BYTES,
    timeout: KEY_PROVIDER_TIMEOUT_MS,
    killSignal: "SIGKILL",
  });
  if (result.error || result.status !== 0) {
    throw providerFailure(provider, result.status);
  }
  return decodeKeyProviderOutput(String(result.stdout), provider);
}

function runWindowsDpapiHelper(runner: typeof spawnSync = spawnSync): Buffer {
  const provider = "field crypto DPAPI helper";
  const script = path.join(findRepoRoot(), "scripts", "field_crypto_key.ps1");
  const result = runner(
    "powershell.exe",
    [
      "-NoProfile",
      "-ExecutionPolicy",
      "Bypass",
      "-File",
      script,
      "-Action",
      "GetOrCreateDataKey",
    ],
    {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
      maxBuffer: MAX_KEY_PROVIDER_OUTPUT_BYTES,
      timeout: KEY_PROVIDER_TIMEOUT_MS,
      killSignal: "SIGKILL",
    },
  );
  if (result.error || result.status !== 0) {
    throw providerFailure(provider, result.status);
  }
  return decodeKeyProviderOutput(String(result.stdout), provider);
}

function localKeyFileAllowed(): boolean {
  return /^(1|true|yes)$/i.test(
    process.env.AOITALK_FIELD_CRYPTO_ALLOW_LOCAL_KEY_FILE || "",
  );
}

type CleanupFailure = {
  operation: string;
  code?: string;
};

type ErrorWithCleanupFailures = Error & {
  cleanupFailures?: CleanupFailure[];
};

type PosixParentContext = {
  fd: number;
  path: string;
  dev: number;
  ino: number;
  requiresSafeKey: boolean;
};

function pathInsidePosixParent(parent: PosixParentContext, basename: string): string {
  // Node does not expose openat/linkat. On Linux, /proc/self/fd/<dirfd>/<name>
  // keeps the operation anchored to the already-open directory instead of
  // resolving the parent path again after the identity check.
  if (process.platform !== "linux") {
    throw new Error(LOCAL_KEY_LINUX_ONLY_ERROR);
  }
  return `/proc/self/fd/${parent.fd}/${basename}`;
}

function localKeyPath(keyPath: string, parent: PosixParentContext | undefined): string {
  return parent === undefined
    ? keyPath
    : pathInsidePosixParent(parent, path.basename(keyPath));
}

function localKeyFileFormatError(): Error {
  return new Error("local field crypto key file must contain one valid 32-byte base64 key");
}

function decodeLocalKeyFile(raw: Buffer): Buffer {
  if (raw.length > MAX_LOCAL_KEY_FILE_BYTES) throw localKeyFileFormatError();
  let start = 0;
  let end = raw.length;
  while (start < end && ASCII_WHITESPACE.has(raw[start])) start += 1;
  while (end > start && ASCII_WHITESPACE.has(raw[end - 1])) end -= 1;
  const encodedBytes = raw.subarray(start, end);
  if (encodedBytes.some((byte) => byte > 0x7f)) throw localKeyFileFormatError();
  const encoded = encodedBytes.toString("ascii");
  if (!/^[A-Za-z0-9+/]{43}=$/.test(encoded)) {
    throw localKeyFileFormatError();
  }
  const key = Buffer.from(encoded, "base64");
  if (key.length !== 32 || key.toString("base64") !== encoded) {
    throw localKeyFileFormatError();
  }
  return key;
}

function cleanupFailure(operation: string, error: unknown): CleanupFailure {
  const code = (error as NodeJS.ErrnoException)?.code;
  return typeof code === "string" ? { operation, code } : { operation };
}

function attachCleanupFailures(primaryError: unknown, failures: CleanupFailure[]): void {
  if (failures.length === 0) return;
  if (primaryError instanceof Error) {
    const target = primaryError as ErrorWithCleanupFailures;
    Object.defineProperty(target, "cleanupFailures", {
      configurable: true,
      enumerable: true,
      value: [...(target.cleanupFailures ?? []), ...failures],
    });
    return;
  }
  process.emitWarning("local field crypto cleanup also failed", {
    code: "AOITALK_FIELD_CRYPTO_CLEANUP_FAILED",
    detail: JSON.stringify(failures),
  });
}

function sameIdentity(left: fs.Stats, right: fs.Stats): boolean {
  return left.dev === right.dev && left.ino === right.ino;
}

function trustedDirectoryStat(directoryStat: fs.Stats, requiresNonWritable = false): boolean {
  if (!directoryStat.isDirectory()) {
    throw new Error("local field crypto key parent path must contain only directories");
  }
  const effectiveUid = process.geteuid!();
  if (directoryStat.uid !== 0 && directoryStat.uid !== effectiveUid) {
    throw new Error(
      "local field crypto key ancestors must be owned by root or the effective user",
    );
  }
  const mode = directoryStat.mode & 0o7777;
  const writable = (mode & 0o022) !== 0;
  const stickyWorldWritable = (mode & 0o1000) !== 0 && (mode & 0o002) !== 0;
  if (writable && (requiresNonWritable || !stickyWorldWritable)) {
    if (requiresNonWritable) {
      throw new Error(
        "sticky world-writable key ancestor requires a trusted non-writable next component",
      );
    }
    throw new Error("local field crypto key ancestors must not be group/world writable");
  }
  return stickyWorldWritable;
}

function ensureLinuxProcFdAvailable(): void {
  if (process.platform !== "linux") throw new Error(LOCAL_KEY_LINUX_ONLY_ERROR);
  if (
    typeof fs.constants.O_NOFOLLOW !== "number" ||
    typeof fs.constants.O_DIRECTORY !== "number" ||
    typeof fs.constants.O_NONBLOCK !== "number" ||
    typeof process.geteuid !== "function"
  ) {
    throw new Error("Linux local field crypto key fallback requires /proc/self/fd support");
  }
  try {
    if (!fs.statSync("/proc/self/fd").isDirectory()) throw new Error("not a directory");
  } catch {
    throw new Error("Linux local field crypto key fallback requires /proc/self/fd support");
  }
}

function closeParentContext(parent: PosixParentContext, primaryError?: unknown): void {
  try {
    fs.closeSync(parent.fd);
  } catch (error) {
    if (primaryError === undefined) throw error;
    attachCleanupFailures(primaryError, [cleanupFailure("parent-directory-fd-close", error)]);
  }
}

function walkPosixParent(parentPath: string, createMissing: boolean): PosixParentContext {
  ensureLinuxProcFdAvailable();
  const root = path.parse(parentPath).root;
  const flags = fs.constants.O_RDONLY | fs.constants.O_DIRECTORY | fs.constants.O_NOFOLLOW;
  let fd = fs.openSync(root, flags);
  try {
    const rootPathStat = fs.lstatSync(root);
    const rootFdStat = fs.fstatSync(fd);
    let procFdStat: fs.Stats;
    try {
      procFdStat = fs.statSync(`/proc/self/fd/${fd}`);
    } catch {
      throw new Error("Linux local field crypto key fallback requires /proc/self/fd support");
    }
    if (!sameIdentity(rootPathStat, rootFdStat) || !sameIdentity(rootFdStat, procFdStat)) {
      throw new Error("local field crypto key ancestor changed during path walk");
    }
    let requiresSafeChild = trustedDirectoryStat(rootFdStat);
    for (const component of parentPath.slice(root.length).split(path.sep).filter(Boolean)) {
      const anchoredComponent = `/proc/self/fd/${fd}/${component}`;
      let componentStat: fs.Stats;
      try {
        componentStat = fs.lstatSync(anchoredComponent);
      } catch (error) {
        if (!createMissing || (error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
        try {
          fs.mkdirSync(anchoredComponent, { mode: 0o700 });
        } catch (mkdirError) {
          if ((mkdirError as NodeJS.ErrnoException).code !== "EEXIST") throw mkdirError;
        }
        componentStat = fs.lstatSync(anchoredComponent);
      }
      if (componentStat.isSymbolicLink()) {
        throw new Error("local field crypto key parent path must not contain symlinks");
      }
      let nextFd: number | undefined;
      try {
        nextFd = fs.openSync(anchoredComponent, flags);
        const openedStat = fs.fstatSync(nextFd);
        if (!sameIdentity(componentStat, openedStat)) {
          throw new Error("local field crypto key ancestor changed during path walk");
        }
        const nextRequiresSafeChild = trustedDirectoryStat(openedStat, requiresSafeChild);
        const oldFd = fd;
        fd = nextFd;
        nextFd = undefined;
        fs.closeSync(oldFd);
        requiresSafeChild = nextRequiresSafeChild;
      } catch (error) {
        if (nextFd !== undefined) {
          try {
            fs.closeSync(nextFd);
          } catch (cleanupError) {
            attachCleanupFailures(error, [cleanupFailure("new-ancestor-fd-close", cleanupError)]);
          }
        }
        throw error;
      }
    }
    const parentStat = fs.fstatSync(fd);
    return {
      fd,
      path: parentPath,
      dev: parentStat.dev,
      ino: parentStat.ino,
      requiresSafeKey: requiresSafeChild,
    };
  } catch (error) {
    try {
      fs.closeSync(fd);
    } catch (cleanupError) {
      attachCleanupFailures(error, [cleanupFailure("ancestor-fd-close", cleanupError)]);
    }
    throw error;
  }
}

function openLocalKeyParent(keyPath: string): {
  keyPath: string;
  parent: PosixParentContext;
} {
  ensureLinuxProcFdAvailable();
  const absoluteKeyPath = path.resolve(keyPath);
  const parentPath = path.dirname(absoluteKeyPath);
  return { keyPath: absoluteKeyPath, parent: walkPosixParent(parentPath, true) };
}

function assertParentIdentity(parent: PosixParentContext): void {
  let current: PosixParentContext;
  try {
    current = walkPosixParent(parent.path, false);
  } catch (error) {
    throw new Error("local field crypto configured path changed during operation", {
      cause: error,
    });
  }
  let primaryError: unknown;
  try {
    if (current.dev !== parent.dev || current.ino !== parent.ino) {
      throw new Error("local field crypto configured path changed during operation");
    }
  } catch (error) {
    primaryError = error;
    throw error;
  } finally {
    closeParentContext(current, primaryError);
  }
}

function validatePosixKeyStat(_fd: number, fileStat: fs.Stats): void {
  if (!fileStat.isFile()) {
    throw new Error("local field crypto key path must be a regular file");
  }
  if (fileStat.uid !== process.geteuid!()) {
    throw new Error("local field crypto key file must be owned by the effective user");
  }
  const mode = fileStat.mode & 0o7777;
  if (mode === 0o400 || mode === 0o600) return;
  throw new Error(KEY_FILE_MODE_ERROR);
}

type LocalKeyRead = {
  key: Buffer;
  stat: fs.Stats;
};

function assertConfiguredKeyIdentity(
  keyPath: string,
  parent: PosixParentContext,
  expectedStat: fs.Stats,
): void {
  let current: PosixParentContext;
  try {
    current = walkPosixParent(parent.path, false);
  } catch (error) {
    throw new Error("local field crypto configured path changed during operation", {
      cause: error,
    });
  }
  let primaryError: unknown;
  try {
    if (current.dev !== parent.dev || current.ino !== parent.ino) {
      throw new Error("local field crypto configured path changed during operation");
    }
    let configuredStat: fs.Stats;
    try {
      configuredStat = fs.lstatSync(localKeyPath(keyPath, current));
    } catch (error) {
      throw new Error("local field crypto configured path changed during operation", {
        cause: error,
      });
    }
    validatePosixKeyStat(-1, configuredStat);
    if (!sameIdentity(configuredStat, expectedStat)) {
      throw new Error("local field crypto configured path changed during operation");
    }
  } catch (error) {
    primaryError = error;
    throw error;
  } finally {
    closeParentContext(current, primaryError);
  }
}

function readLocalKeyFileWithIdentity(
  keyPath: string,
  parent: PosixParentContext,
): LocalKeyRead {
  const flags = fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK;

  let fd: number | undefined;
  let primaryError: unknown;
  try {
    assertParentIdentity(parent);
    fd = fs.openSync(localKeyPath(keyPath, parent), flags);
    const fileStat = fs.fstatSync(fd);
    validatePosixKeyStat(fd, fileStat);
    const raw = Buffer.alloc(MAX_LOCAL_KEY_FILE_BYTES + 1);
    let length = 0;
    while (length <= MAX_LOCAL_KEY_FILE_BYTES) {
      const bytesRead = fs.readSync(fd, raw, length, raw.length - length, null);
      if (bytesRead === 0) break;
      length += bytesRead;
    }
    const key = decodeLocalKeyFile(raw.subarray(0, length));
    assertConfiguredKeyIdentity(keyPath, parent, fileStat);
    return { key, stat: fileStat };
  } catch (error) {
    primaryError = error;
    throw error;
  } finally {
    if (fd !== undefined) {
      try {
        fs.closeSync(fd);
      } catch (error) {
        if (primaryError === undefined) throw error;
        attachCleanupFailures(primaryError, [cleanupFailure("key-fd-close", error)]);
      }
    }
  }
}

function readLocalKeyFile(keyPath: string, parent: PosixParentContext): Buffer {
  return readLocalKeyFileWithIdentity(keyPath, parent).key;
}

function fsyncParentDirectory(parent: PosixParentContext): void {
  fs.fsyncSync(parent.fd);
}

function cleanupOwnedPublishedKey(
  keyPath: string,
  parent: PosixParentContext,
  expectedStat: fs.Stats,
  primaryError: unknown,
): void {
  try {
    const configuredStat = fs.lstatSync(localKeyPath(keyPath, parent));
    if (sameIdentity(configuredStat, expectedStat)) {
      fs.unlinkSync(localKeyPath(keyPath, parent));
      fs.fsyncSync(parent.fd);
    }
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") {
      attachCleanupFailures(primaryError, [cleanupFailure("owned-published-key-cleanup", error)]);
    }
  }
}

function runLocalKeyFile(): Buffer {
  if (process.platform !== "linux") {
    throw new Error(LOCAL_KEY_LINUX_ONLY_ERROR);
  }
  ensureLinuxProcFdAvailable();
  if (!localKeyFileAllowed()) {
    throw new Error(
      "No field crypto key provider is configured. Set AOITALK_FIELD_CRYPTO_KEY_COMMAND to a keyring/KMS command, or explicitly allow the local key-file fallback.",
    );
  }

  const configuredKeyPath =
    process.env.AOITALK_FIELD_CRYPTO_LOCAL_KEY_FILE ||
    path.join(
      process.env.HOME || process.cwd(),
      ".config",
      "aoitalk",
      "field-crypto.key",
    );
  const { keyPath, parent } = openLocalKeyParent(configuredKeyPath);
  let primaryError: unknown;
  try {
    try {
      return readLocalKeyFile(keyPath, parent);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    }

    const key = crypto.randomBytes(32);
    const temporaryName = `.${path.basename(keyPath)}.${process.pid}.${crypto.randomBytes(8).toString("hex")}.tmp`;
    const temporaryPath = pathInsidePosixParent(parent, temporaryName);
    let temporaryCreated = false;
    let result: Buffer | undefined;
    let expectedStat: fs.Stats | undefined;
    let publishedOwnKey = false;
    let publicationError: unknown;
    try {
      let fd: number | undefined;
      let fdPrimaryError: unknown;
      try {
        assertParentIdentity(parent);
        fd = fs.openSync(temporaryPath, "wx", 0o600);
        temporaryCreated = true;
        fs.fchmodSync(fd, 0o600);
        fs.writeFileSync(fd, key.toString("base64"), { encoding: "ascii" });
        fs.fsyncSync(fd);
        expectedStat = fs.fstatSync(fd);
        if ((expectedStat.mode & 0o7777) !== 0o600) {
          throw new Error("local field crypto temporary key file must use mode 0600");
        }
      } catch (error) {
        fdPrimaryError = error;
        throw error;
      } finally {
        if (fd !== undefined) {
          try {
            fs.closeSync(fd);
          } catch (error) {
            if (fdPrimaryError === undefined) throw error;
            attachCleanupFailures(fdPrimaryError, [cleanupFailure("temporary-fd-close", error)]);
          }
        }
      }

      try {
        assertParentIdentity(parent);
        fs.linkSync(temporaryPath, localKeyPath(keyPath, parent));
        result = key;
        publishedOwnKey = true;
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code === "EEXIST") {
          const winner = readLocalKeyFileWithIdentity(keyPath, parent);
          result = winner.key;
          expectedStat = winner.stat;
        } else {
          throw new Error(
            "local field crypto key publication requires hard-link support",
            { cause: error },
          );
        }
      }
      if (expectedStat === undefined) {
        throw new Error("local field crypto key initialization failed");
      }
      assertConfiguredKeyIdentity(keyPath, parent, expectedStat);
    } catch (error) {
      publicationError = error;
    }

    const cleanupFailures: CleanupFailure[] = [];
    let firstCleanupError: unknown;
    if (temporaryCreated) {
      try {
        fs.unlinkSync(temporaryPath);
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "ENOENT") {
          firstCleanupError ??= error;
          cleanupFailures.push(cleanupFailure("temporary-unlink", error));
        }
      }
      try {
        fsyncParentDirectory(parent);
      } catch (error) {
        firstCleanupError ??= error;
        cleanupFailures.push(cleanupFailure("parent-directory-fsync", error));
      }
    }

    if (
      publicationError === undefined &&
      cleanupFailures.length === 0 &&
      expectedStat !== undefined
    ) {
      try {
        assertConfiguredKeyIdentity(keyPath, parent, expectedStat);
      } catch (error) {
        publicationError = error;
      }
    }

    if (publicationError !== undefined) {
      if (publishedOwnKey && expectedStat !== undefined) {
        cleanupOwnedPublishedKey(keyPath, parent, expectedStat, publicationError);
      }
      attachCleanupFailures(publicationError, cleanupFailures);
      throw publicationError;
    }
    if (cleanupFailures.length > 0) {
      const cleanupError = new Error("local field crypto temporary key file cleanup failed", {
        cause: firstCleanupError,
      });
      attachCleanupFailures(cleanupError, cleanupFailures.slice(1));
      throw cleanupError;
    }
    if (result === undefined) {
      throw new Error("local field crypto key initialization failed");
    }
    return result;
  } catch (error) {
    primaryError = error;
    throw error;
  } finally {
    closeParentContext(parent, primaryError);
  }
}

export const __fieldCryptoTestHooks = {
  decodeLocalKeyFile,
  runKeyCommand,
  runWindowsDpapiHelper,
  validatePosixKeyStat,
  walkPosixParent,
  resetCachedKey: () => {
    cachedKey = null;
  },
  runLocalKeyFile,
};

function getDataKey(): Buffer {
  if (cachedKey) return cachedKey;

  const keyCommand = process.env.AOITALK_FIELD_CRYPTO_KEY_COMMAND;
  const envKey = process.env.AOITALK_FIELD_CRYPTO_KEY_B64;
  if (keyCommand) {
    // A production keyring/KMS provider wins over the optional test-only B64
    // compatibility secret that may be mounted by Compose.
    cachedKey = runKeyCommand(keyCommand);
  } else if (envKey) {
    if (!/^(1|true|yes)$/i.test(process.env.AOITALK_FIELD_CRYPTO_ALLOW_ENV_KEY || "")) {
      throw new Error("AOITALK_FIELD_CRYPTO_KEY_B64 is set but env keys are disabled");
    }
    cachedKey = Buffer.from(envKey, "base64");
  } else if (process.platform === "win32") {
    cachedKey = runWindowsDpapiHelper();
  } else if (process.platform === "linux" && localKeyFileAllowed()) {
    cachedKey = runLocalKeyFile();
  } else {
    throw new Error(
      process.platform === "linux"
        ? "No field crypto key provider is configured. Set AOITALK_FIELD_CRYPTO_KEY_COMMAND to a keyring/KMS command, or explicitly allow the Linux local key-file fallback."
        : "No field crypto key provider is configured; AOITALK_FIELD_CRYPTO_KEY_COMMAND is required on this platform.",
    );
  }

  if (cachedKey.length !== 32) {
    throw new Error("field crypto data key must be 32 bytes");
  }
  return cachedKey;
}

function aadBytes(aad?: string): Buffer {
  return Buffer.from(aad || "aoitalk:field:v1", "utf8");
}

export function isEncryptedValue(value: unknown): value is string {
  return typeof value === "string" && value.startsWith(ENCRYPTION_PREFIX);
}

export function encryptText(value: string, aad?: string): string;
export function encryptText(value: null | undefined, aad?: string): null | undefined;
export function encryptText(
  value: string | null | undefined,
  aad?: string,
): string | null | undefined;
export function encryptText(value: string | null | undefined, aad?: string): string | null | undefined {
  if (value == null || value === "" || isEncryptedValue(value)) return value;
  const nonce = crypto.randomBytes(NONCE_LEN);
  const cipher = crypto.createCipheriv("aes-256-gcm", getDataKey(), nonce);
  cipher.setAAD(aadBytes(aad));
  const ciphertext = Buffer.concat([cipher.update(value, "utf8"), cipher.final()]);
  const tag = cipher.getAuthTag();
  return [
    "enc",
    "v1",
    ALG,
    KEY_ID,
    b64urlEncode(nonce),
    b64urlEncode(Buffer.concat([ciphertext, tag])),
  ].join(":");
}

export function decryptTextIfNeeded(value: string, aad?: string): string;
export function decryptTextIfNeeded(value: null | undefined, aad?: string): null | undefined;
export function decryptTextIfNeeded(
  value: string | null | undefined,
  aad?: string,
): string | null | undefined;
export function decryptTextIfNeeded(
  value: string | null | undefined,
  aad?: string,
): string | null | undefined {
  if (value == null || value === "" || !isEncryptedValue(value)) return value;
  const parts = value.split(":");
  if (
    parts.length !== 6 ||
    parts[0] !== "enc" ||
    parts[1] !== "v1" ||
    parts[2] !== ALG ||
    parts[3] !== KEY_ID
  ) {
    throw new Error("unsupported encrypted field format");
  }
  const nonce = b64urlDecode(parts[4]);
  const sealed = b64urlDecode(parts[5]);
  if (nonce.length !== NONCE_LEN || sealed.length < 16) {
    throw new Error("unsupported encrypted field format");
  }
  const ciphertext = sealed.subarray(0, sealed.length - 16);
  const tag = sealed.subarray(sealed.length - 16);
  const decipher = crypto.createDecipheriv("aes-256-gcm", getDataKey(), nonce, {
    authTagLength: 16,
  });
  decipher.setAAD(aadBytes(aad));
  decipher.setAuthTag(tag);
  return Buffer.concat([decipher.update(ciphertext), decipher.final()]).toString("utf8");
}

export function encryptJsonValue<T>(value: T, aad?: string): T | string {
  if (value == null || isEncryptedValue(value)) return value;
  if (
    (Array.isArray(value) && value.length === 0) ||
    (typeof value === "object" && Object.keys(value as Record<string, unknown>).length === 0)
  ) {
    return value;
  }
  return encryptText(JSON.stringify(value), aad);
}

export function decryptJsonValueIfNeeded<T>(value: T, aad?: string): T {
  if (!isEncryptedValue(value)) return value;
  return JSON.parse(decryptTextIfNeeded(value, aad)) as T;
}

function isSensitiveKey(pathValue: string): boolean {
  return SENSITIVE_KEY_RE.test(pathValue);
}

export function encryptJsonSecretLeaves<T>(value: T, aadPrefix = "json"): T {
  function walk(node: unknown, keyPath: string): unknown {
    if (Array.isArray(node)) return node.map((v, idx) => walk(v, `${keyPath}[${idx}]`));
    if (node && typeof node === "object") {
      return Object.fromEntries(
        Object.entries(node).map(([k, v]) => [k, walk(v, keyPath ? `${keyPath}.${k}` : k)]),
      );
    }
    if (typeof node === "string" && node && isSensitiveKey(keyPath)) {
      return encryptText(node, `${aadPrefix}:${keyPath}`);
    }
    return node;
  }
  return walk(value, "") as T;
}

export function decryptJsonSecretLeaves<T>(value: T, aadPrefix = "json"): T {
  function walk(node: unknown, keyPath: string): unknown {
    if (Array.isArray(node)) return node.map((v, idx) => walk(v, `${keyPath}[${idx}]`));
    if (node && typeof node === "object") {
      return Object.fromEntries(
        Object.entries(node).map(([k, v]) => [k, walk(v, keyPath ? `${keyPath}.${k}` : k)]),
      );
    }
    if (typeof node === "string" && isEncryptedValue(node)) {
      return decryptTextIfNeeded(node, `${aadPrefix}:${keyPath}`);
    }
    return node;
  }
  return walk(value, "") as T;
}
