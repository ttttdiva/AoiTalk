/** Native-mobile verification helpers for a durable AoiTalk server identity. */

import {
  getServerIdentityPin,
  getServerIdentityPinForFingerprint,
  saveServerIdentityPin,
  sha256Hex,
  type ServerIdentityPin,
} from "./auth";

export const SERVER_IDENTITY_PROTOCOL_VERSION = 2 as const;
export const SERVER_IDENTITY_ALGORITHM = "Ed25519" as const;
export const SERVER_IDENTITY_DOMAIN = "AoiTalk-Mobile-Server-Identity/v2\u0000";
export const SERVER_IDENTITY_CHALLENGE_PATH = "/api/mobile/server-identity/challenge";
export const SERVER_IDENTITY_PROBE_HEADER = "X-AoiTalk-SDK-Gateway";
export const SERVER_IDENTITY_PROBE_HEADER_VALUE = "identity-probe-v1";

export type ServerIdentityProof = {
  protocol_version: number;
  algorithm: string;
  server_id: string;
  public_key: string;
  nonce: string;
  target_namespace: string;
  signature: string;
};
export type Ed25519Verifier = (
  publicKey: Uint8Array,
  signature: Uint8Array,
  message: Uint8Array,
) => boolean | Promise<boolean>;

export class ServerIdentityVerificationUnavailable extends Error {
  constructor(message = "Ed25519署名検証を利用できません") {
    super(message);
    this.name = "ServerIdentityVerificationUnavailable";
  }
}

/** One-shot challenge nonce guard. Replaying a signed response is rejected. */
export class ServerIdentityNonceReplayGuard {
  private readonly consumed = new Set<string>();

  constructor(private readonly maxEntries = 256) {}

  consume(nonce: string): boolean {
    if (this.consumed.has(nonce)) return false;
    this.consumed.add(nonce);
    while (this.consumed.size > this.maxEntries) {
      const oldest = this.consumed.values().next().value;
      if (typeof oldest !== "string") break;
      this.consumed.delete(oldest);
    }
    return true;
  }
}

const defaultNonceReplayGuard = new ServerIdentityNonceReplayGuard();

function decodeBase64Url(value: unknown, expectedLength: number, field: string): Uint8Array {
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]+$/.test(value)) {
    throw new Error(`invalid server identity ${field}`);
  }
  const padded = value + "=".repeat((4 - (value.length % 4)) % 4);
  let binary: string;
  try {
    if (typeof globalThis.atob !== "function") throw new Error("atob unavailable");
    binary = globalThis.atob(padded.replace(/-/g, "+").replace(/_/g, "/"));
  } catch {
    throw new Error(`invalid server identity ${field}`);
  }
  if (binary.length !== expectedLength) throw new Error(`invalid server identity ${field}`);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

function encodeUtf8(value: string): Uint8Array {
  if (typeof TextEncoder !== "undefined") return new TextEncoder().encode(value);
  // Expo/Hermes supports TextEncoder; this branch keeps unit-test shims small.
  return Uint8Array.from(unescape(encodeURIComponent(value)), (character) => character.charCodeAt(0));
}

export function normalizeServerIdentityTargetNamespace(value: string): string {
  const raw = value.trim();
  if (!raw || raw.includes("\\")) throw new Error("invalid server identity target namespace");
  let parsed: URL;
  try {
    parsed = new URL(raw);
  } catch {
    throw new Error("invalid server identity target namespace");
  }
  if (
    !["http:", "https:"].includes(parsed.protocol.toLowerCase())
    || !parsed.hostname
    || parsed.username
    || parsed.password
    || parsed.search
    || parsed.hash
  ) throw new Error("invalid server identity target namespace");
  for (const segment of (parsed.pathname || "/").split("/")) {
    let decoded: string;
    try {
      decoded = decodeURIComponent(segment);
    } catch {
      throw new Error("invalid server identity target namespace");
    }
    if (decoded === "." || decoded === ".." || decoded.includes("/") || decoded.includes("\\")) {
      throw new Error("invalid server identity target namespace");
    }
  }
  const protocol = parsed.protocol.toLowerCase();
  const host = parsed.hostname.toLowerCase();
  const defaultPort = protocol === "https:" ? "443" : "80";
  const port = parsed.port || defaultPort;
  const authority = port === defaultPort ? host : `${host}:${port}`;
  const pathname = (parsed.pathname || "/").replace(/\/+/g, "/").replace(/\/+$/, "") || "/";
  return `${protocol}//${authority}${pathname === "/" ? "" : pathname}`;
}

export function identityChallengeMessage(
  serverId: string,
  nonce: string,
  targetNamespace: string,
): Uint8Array {
  const normalizedId = serverId.trim().toLowerCase();
  if (!/^[0-9a-f]{64}$/.test(normalizedId)) throw new Error("invalid server identity");
  if (!/^[A-Za-z0-9._~-]{16,256}$/.test(nonce)) throw new Error("invalid server identity nonce");
  const normalizedTarget = normalizeServerIdentityTargetNamespace(targetNamespace);
  return encodeUtf8(`${SERVER_IDENTITY_DOMAIN}${normalizedId}\u0000${nonce}\u0000${normalizedTarget}`);
}

export function parseServerIdentityProof(value: unknown): ServerIdentityProof {
  if (!value || typeof value !== "object") throw new Error("invalid server identity proof");
  const proof = value as Partial<ServerIdentityProof>;
  if (
    proof.protocol_version !== SERVER_IDENTITY_PROTOCOL_VERSION
    || proof.algorithm !== SERVER_IDENTITY_ALGORITHM
    || typeof proof.server_id !== "string"
    || !/^[0-9a-f]{64}$/i.test(proof.server_id)
    || typeof proof.public_key !== "string"
    || typeof proof.signature !== "string"
    || typeof proof.nonce !== "string"
    || typeof proof.target_namespace !== "string"
  ) throw new Error("invalid server identity proof");
  const publicKey = decodeBase64Url(proof.public_key, 32, "public_key");
  decodeBase64Url(proof.signature, 64, "signature");
  if (sha256Hex(publicKey) !== proof.server_id.toLowerCase()) {
    throw new Error("invalid server identity server_id");
  }
  if (!/^[A-Za-z0-9._~-]{16,256}$/.test(proof.nonce)) {
    throw new Error("invalid server identity nonce");
  }
  const targetNamespace = normalizeServerIdentityTargetNamespace(proof.target_namespace);
  return {
    protocol_version: proof.protocol_version,
    algorithm: proof.algorithm,
    server_id: proof.server_id.toLowerCase(),
    public_key: proof.public_key,
    nonce: proof.nonce,
    target_namespace: targetNamespace,
    signature: proof.signature,
  };
}

/** Use platform WebCrypto where available; native builds may inject noble. */
export async function verifyEd25519WithWebCrypto(
  publicKey: Uint8Array,
  signature: Uint8Array,
  message: Uint8Array,
): Promise<boolean> {
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) throw new ServerIdentityVerificationUnavailable();
  const publicKeyBuffer = Uint8Array.from(publicKey).buffer as ArrayBuffer;
  const signatureBuffer = Uint8Array.from(signature).buffer as ArrayBuffer;
  const messageBuffer = Uint8Array.from(message).buffer as ArrayBuffer;
  try {
    const key = await subtle.importKey(
      "raw",
      publicKeyBuffer,
      { name: "Ed25519" },
      false,
      ["verify"],
    );
    return await subtle.verify(
      { name: "Ed25519" },
      key,
      signatureBuffer,
      messageBuffer,
    );
  } catch {
    throw new ServerIdentityVerificationUnavailable();
  }
}

export async function verifyServerIdentityProof(
  value: unknown,
  expectedNonce: string,
  expectedTargetNamespace: string,
  verifier: Ed25519Verifier = verifyEd25519WithWebCrypto,
  replayGuard: ServerIdentityNonceReplayGuard = defaultNonceReplayGuard,
): Promise<ServerIdentityProof> {
  const proof = parseServerIdentityProof(value);
  if (proof.nonce !== expectedNonce) throw new Error("server identity nonce mismatch");
  if (proof.target_namespace !== normalizeServerIdentityTargetNamespace(expectedTargetNamespace)) {
    throw new Error("server identity target namespace mismatch");
  }
  const publicKey = decodeBase64Url(proof.public_key, 32, "public_key");
  const signature = decodeBase64Url(proof.signature, 64, "signature");
  const message = identityChallengeMessage(
    proof.server_id,
    proof.nonce,
    proof.target_namespace,
  );
  const valid = await verifier(publicKey, signature, message);
  if (!valid) throw new Error("server identity signature mismatch");
  if (!replayGuard.consume(proof.nonce)) throw new Error("server identity nonce replay");
  return proof;
}

export async function pinVerifiedServerIdentity(
  proof: ServerIdentityProof,
  serverFingerprint: string,
): Promise<void> {
  await saveServerIdentityPin({
    serverId: proof.server_id,
    publicKey: proof.public_key,
    fingerprints: [serverFingerprint],
  });
}

export async function getPinnedServerIdentity(
  serverFingerprint: string,
): Promise<ServerIdentityPin | null> {
  return getServerIdentityPinForFingerprint(serverFingerprint);
}

export async function getPinnedServerIdentityById(
  serverId: string,
): Promise<ServerIdentityPin | null> {
  return getServerIdentityPin(serverId);
}

export function proofMatchesServerIdentityPin(
  proof: Pick<ServerIdentityProof, "server_id" | "public_key">,
  pin: Pick<ServerIdentityPin, "serverId" | "publicKey"> | null,
): boolean {
  return Boolean(
    pin
    && pin.serverId === proof.server_id.toLowerCase()
    && pin.publicKey === proof.public_key,
  );
}

export {
  decodeBase64Url,
};
