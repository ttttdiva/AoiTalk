import crypto from "crypto";
import fs from "fs";
import path from "path";
import { spawnSync } from "child_process";

export const ENCRYPTION_PREFIX = "enc:v1:";

const ALG = "aes256gcm";
const KEY_ID = "local";
const NONCE_LEN = 12;
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
  const padded = value + "=".repeat((4 - (value.length % 4)) % 4);
  return Buffer.from(padded.replace(/-/g, "+").replace(/_/g, "/"), "base64");
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

function runKeyCommand(command: string): Buffer {
  const result = spawnSync(command, {
    shell: true,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });
  if (result.status !== 0) {
    throw new Error(`field crypto key command failed: ${result.stderr}`);
  }
  const raw = result.stdout.trim().split(/\r?\n/).pop() || "";
  return Buffer.from(raw, "base64");
}

function runWindowsDpapiHelper(): Buffer {
  const script = path.join(findRepoRoot(), "scripts", "field_crypto_key.ps1");
  const result = spawnSync(
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
    { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] },
  );
  if (result.status !== 0) {
    throw new Error(`field crypto DPAPI helper failed: ${result.stderr}`);
  }
  const raw = result.stdout.trim().split(/\r?\n/).pop() || "";
  return Buffer.from(raw, "base64");
}

function getDataKey(): Buffer {
  if (cachedKey) return cachedKey;

  const envKey = process.env.AOITALK_FIELD_CRYPTO_KEY_B64;
  if (envKey) {
    if (!/^(1|true|yes)$/i.test(process.env.AOITALK_FIELD_CRYPTO_ALLOW_ENV_KEY || "")) {
      throw new Error("AOITALK_FIELD_CRYPTO_KEY_B64 is set but env keys are disabled");
    }
    cachedKey = Buffer.from(envKey, "base64");
  } else if (process.env.AOITALK_FIELD_CRYPTO_KEY_COMMAND) {
    cachedKey = runKeyCommand(process.env.AOITALK_FIELD_CRYPTO_KEY_COMMAND);
  } else if (process.platform === "win32") {
    cachedKey = runWindowsDpapiHelper();
  } else {
    throw new Error(
      "No field crypto key provider is configured. Set AOITALK_FIELD_CRYPTO_KEY_COMMAND to a keyring/KMS command.",
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
  if (parts.length !== 6 || parts[2] !== ALG) {
    throw new Error("unsupported encrypted field format");
  }
  const nonce = b64urlDecode(parts[4]);
  const sealed = b64urlDecode(parts[5]);
  const ciphertext = sealed.subarray(0, sealed.length - 16);
  const tag = sealed.subarray(sealed.length - 16);
  const decipher = crypto.createDecipheriv("aes-256-gcm", getDataKey(), nonce);
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
