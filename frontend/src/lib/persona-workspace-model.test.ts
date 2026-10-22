import { describe, expect, it } from "vitest";
import type { MediaPersona } from "./media-operations-api";
import type { ContentVariant } from "./media-operations-content-api";
import type { PlatformAccount } from "./media-operations-setup-api";
import { accountInScope, personaScope, safeReceiptUrl, variantInScope, workspaceHref } from "./persona-workspace-model";

const scope = { personaId: "p", personaRevisionId: "pr", projectId: "project", ownerUserId: "owner", displayName: "UN//REAL" };
const account = { id: "a", persona_id: "p", project_id: "project", owner_user_id: "owner", platform: "x", current_revision: { id: "ar", platform_account_id: "a", project_id: "project", owner_user_id: "owner" } } as PlatformAccount;
const variant = { id: "v", content_item_id: "i", owner_user_id: "owner", project_id: "project", platform: "x", status: "draft", current_revision: { id: "r", owner_user_id: "owner", project_id: "project", content_variant_id: "v", content_item_id: "i", persona_revision_id: "pr", platform_account_id: "a", platform: "x", version: 1, content_hash: "c".repeat(64), content_item_hash: "a".repeat(64), persona_revision_hash: "b".repeat(64), payload: { type: "x_post", text: "保存済み本文" }, generation_output_refs: [], source_evidence: [] } } satisfies ContentVariant;

describe("persona workspace ownership boundary", () => {
  it("requires the persona and immutable revision to share identity, owner and Project", () => {
    const persona = { id: "p", project_id: "project", owner_user_id: "owner", current_revision: { id: "pr", persona_id: "p", project_id: "project", owner_user_id: "owner", display_name: "UN//REAL" } } as MediaPersona;
    expect(personaScope(persona)).toEqual(scope);
    expect(personaScope({ ...persona, project_id: null })).toBeNull();
    expect(personaScope({ ...persona, current_revision: { ...persona.current_revision, owner_user_id: "other" } })).toBeNull();
    expect(personaScope({ ...persona, current_revision: { ...persona.current_revision, persona_id: "other" } })).toBeNull();
  });
  it("rejects cross-persona account rows and mismatched account revisions", () => {
    expect(accountInScope(account, scope)).toBe(true);
    for (const field of ["persona_id", "project_id", "owner_user_id"]) expect(accountInScope({ ...account, [field]: "other" }, scope)).toBe(false);
    expect(accountInScope({ ...account, current_revision: { ...account.current_revision, platform_account_id: "other" } }, scope)).toBe(false);
  });
  it("rejects an old persona revision and mixed variant/revision/account responses", () => {
    expect(variantInScope(variant, scope, [account])).toBe(true);
    for (const field of ["persona_revision_id", "owner_user_id", "project_id", "content_variant_id", "content_item_id", "platform_account_id", "platform"]) {
      expect(variantInScope({ ...variant, current_revision: { ...variant.current_revision!, [field]: "other" } }, scope, [account])).toBe(false);
    }
    expect(variantInScope(variant, scope, [])).toBe(false);
    expect(variantInScope({ ...variant, owner_user_id: "other" }, scope, [account])).toBe(false);
  });
  it("retains opaque persona IDs and only links real platform receipt hosts", () => {
    expect(workspaceHref("name/with?chars", "publish")).toBe("/operations?tab=studio&persona=name%2Fwith%3Fchars&stage=publish");
    expect(safeReceiptUrl("https://x.com/fixture/status/1", "x")).toBe("https://x.com/fixture/status/1");
    expect(safeReceiptUrl("https://www.youtube.com/watch?v=1", "youtube")).toBeTruthy();
    for (const url of ["javascript:alert(1)", "https://x.com.evil.test/status/1", "https://secret@x.com/status/1", "http://x.com/status/1", "https://youtu.be/1"]) expect(safeReceiptUrl(url, "x")).toBeNull();
  });
});
