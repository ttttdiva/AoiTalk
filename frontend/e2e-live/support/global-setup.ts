import { request, type FullConfig } from "@playwright/test";

import { assertLiveVerificationStorageIsolated } from "../../e2e/support/auth";

/**
 * Live specs may authenticate and persist rows.  Verify the actual Next and
 * FastAPI database connections before any test fixture or user login runs.
 * An old/shared server without the read-only probe therefore fails closed.
 */
export default async function globalSetup(config: FullConfig): Promise<void> {
  const baseURL = config.projects[0]?.use.baseURL;
  if (typeof baseURL !== "string" || !baseURL.trim()) {
    throw new Error("live verification requires a configured baseURL");
  }
  const context = await request.newContext({ baseURL });
  try {
    await assertLiveVerificationStorageIsolated(context, {
      requireEphemeralDatabase: true,
    });
  } finally {
    await context.dispose();
  }
}
