// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { readSubmissionMarker, submissionStorageKey, writeSubmissionMarker } from "./media-publication-submission";

const scope = { ownerUserId: "owner", projectId: "project", personaId: "persona", personaRevisionId: "revision", displayName: "UN//REAL" };
const marker = { idempotencyKey: "request-key", revisionId: "revision", contentHash: "c".repeat(64), accountId: "account", publicationId: "publication", state: "unknown" as const };
beforeEach(() => { localStorage.clear(); vi.restoreAllMocks(); });
describe("publication reload guard", () => {
  it("preserves an unknown submission and isolates owner, persona and account identities", () => {
    const key = submissionStorageKey(scope, marker.accountId, marker.contentHash); writeSubmissionMarker(key, marker);
    expect(readSubmissionMarker(key)).toEqual(marker);
    expect(readSubmissionMarker(submissionStorageKey({ ...scope, ownerUserId: "other" }, marker.accountId, marker.contentHash))).toBeNull();
    expect(readSubmissionMarker(submissionStorageKey({ ...scope, personaId: "other" }, marker.accountId, marker.contentHash))).toBeNull();
    expect(readSubmissionMarker(submissionStorageKey(scope, "other", marker.contentHash))).toBeNull();
  });
  it("blocks malformed local records and storage failure instead of creating another intent", () => {
    const key = submissionStorageKey(scope, marker.accountId, marker.contentHash);
    localStorage.setItem(key, '{"state":"unknown"}'); expect(() => readSubmissionMarker(key)).toThrow();
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("storage blocked"); });
    expect(() => writeSubmissionMarker(key, marker)).toThrow("storage blocked");
  });
});
