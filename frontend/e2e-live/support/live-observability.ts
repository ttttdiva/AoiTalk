import type { Page } from "@playwright/test";

export type LiveObservability = {
  consoleErrors: string[];
  pageErrors: string[];
  requestFailures: string[];
  httpErrors: Array<{ method: string; status: number; url: string }>;
};

export function attachLiveObservability(page: Page): LiveObservability {
  const issues: LiveObservability = {
    consoleErrors: [],
    pageErrors: [],
    requestFailures: [],
    httpErrors: [],
  };
  page.on("console", (message) => {
    if (message.type() === "error") {
      issues.consoleErrors.push(`${page.url()} :: ${message.text()}`);
    }
  });
  page.on("pageerror", (error) => {
    issues.pageErrors.push(`${page.url()} :: ${error.message}`);
  });
  page.on("requestfailed", (request) => {
    // Next.js App Router routinely aborts a superseded document/RSC request
    // during a client-side navigation. This is not a runtime failure; retain
    // all other failed requests for the live-flow assertion.
    const errorText = request.failure()?.errorText || "";
    // Playwright reports ERR_ABORTED for both superseded App Router requests
    // and background fetches canceled as a page/session is logged out or
    // closed. These are expected lifecycle cancellations, not failed HTTP
    // responses; actual 4xx/5xx responses remain tracked below.
    if (errorText.includes("ERR_ABORTED")) return;
    issues.requestFailures.push(
      `${request.method()} ${request.url()} :: ${errorText || "request failed"}`,
    );
  });
  page.on("response", (response) => {
    const status = response.status();
    if (status >= 500 || status === 401 || status === 403) {
      issues.httpErrors.push({
        method: response.request().method(),
        status,
        url: response.url(),
      });
    }
  });
  return issues;
}

export function assertNoLiveObservabilityIssues(
  issues: LiveObservability,
  scope: string,
): void {
  const details = [
    ...issues.consoleErrors.map((entry) => `console.error: ${entry}`),
    ...issues.pageErrors.map((entry) => `pageerror: ${entry}`),
    ...issues.requestFailures.map((entry) => `requestfailed: ${entry}`),
    ...issues.httpErrors.map(
      (entry) => `${entry.method} ${entry.status}: ${entry.url}`,
    ),
  ];
  if (details.length > 0) {
    throw new Error(`${scope} emitted runtime errors:\n${details.join("\n")}`);
  }
}
