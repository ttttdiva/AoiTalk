/**
 * Native protected HTTP boundary.
 *
 * React Native's global fetch may ignore `redirect: "manual"` on some
 * platforms. Expo's fetch implementation uses the native networking stack
 * with manual redirect support, so every API/Gateway request goes through this
 * wrapper. The Expo implementation is required for protected traffic; tests
 * replace this module with an explicit mock instead of falling back to a
 * platform global.
 */

import { fetch as expoFetch } from "expo/fetch";

export class ProtectedFetchRedirectError extends Error {
  readonly status: number | null;

  constructor(status: number | null = null) {
    super("認証付き通信のリダイレクトは許可されていません");
    this.name = "ProtectedFetchRedirectError";
    this.status = status;
  }
}
type FetchLike = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

export async function protectedFetch(
  input: RequestInfo | URL,
  init: RequestInit = {},
): Promise<Response> {
  const response = await (expoFetch as unknown as FetchLike)(input, {
    ...init,
    credentials: "omit",
    redirect: "manual",
  });
  if (
    response.type === "opaqueredirect" ||
    response.redirected ||
    (response.status >= 300 && response.status < 400)
  ) {
    throw new ProtectedFetchRedirectError(response.status);
  }
  return response;
}
/** Clear the cached implementation after a test/runtime adapter change. */
export function clearProtectedFetchCache(): void {
  // Kept as a test/runtime boundary for callers that previously selected a
  // fetch adapter. Expo owns the native implementation now.
}
