/** Native Ed25519 and nonce adapters for credential-target identity proofs. */

import { ed25519 } from "@noble/curves/ed25519";
import * as ExpoCrypto from "expo-crypto";
import type { Ed25519Verifier } from "./server-identity";

const BASE64URL = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

function bytesToBase64Url(bytes: Uint8Array): string {
  let output = "";
  for (let index = 0; index < bytes.length; index += 3) {
    const a = bytes[index] ?? 0;
    const b = bytes[index + 1] ?? 0;
    const c = bytes[index + 2] ?? 0;
    output += BASE64URL[a >> 2];
    output += BASE64URL[((a & 3) << 4) | (b >> 4)];
    if (index + 1 < bytes.length) output += BASE64URL[((b & 15) << 2) | (c >> 6)];
    if (index + 2 < bytes.length) output += BASE64URL[c & 63];
  }
  return output;
}

/** ExpoCrypto-backed nonce source; it does not depend on Hermes WebCrypto. */
export function nativeIdentityNonce(): string {
  return bytesToBase64Url(ExpoCrypto.getRandomBytes(24));
}

/** Noble verifier used when Hermes lacks `crypto.subtle` Ed25519 support. */
export const nativeEd25519Verifier: Ed25519Verifier = (
  publicKey,
  signature,
  message,
) => ed25519.verify(signature, message, publicKey);
