import { describe, expect, it } from "vitest";
import type { PlatformAccount, PlatformCredentialDetail } from "./media-operations-setup-api";
import { personaConnectionState } from "./persona-connection-state";
import { isTestFixtureName } from "./operations-test-data";

const account = (overrides: Partial<PlatformAccount>) => ({ id: "a", platform: "x", account_ref: "@XDouphe9619", status: "active", connection_id: "c", ...overrides }) as PlatformAccount;
const credential = (overrides: Partial<PlatformCredentialDetail>) => ({
  connection_type: "oauth", status: "verified", verification_code: "identity_match",
  capabilities: { identity: "available", publish: "unknown", media: "unknown", analytics: "unknown" },
  last_verified_at: "2026-10-29T00:00:00Z", ...overrides,
}) as PlatformCredentialDetail;

describe("personaConnectionState", () => {
  it("counts only a verified OAuth identity as connected", () => {
    expect(personaConnectionState(account({}), credential({}))).toMatchObject({ tone: "ok", label: "本人接続済み", nextAction: null });
    // Today's X slots: an imported cookie package waiting for verification is not a connection.
    const cookie = personaConnectionState(account({}), credential({ connection_type: "cookie_export", status: "verification_pending", verification_code: null, capabilities: { identity: "unknown", publish: "unknown", media: "unknown", analytics: "unknown" }, last_verified_at: null }));
    expect(cookie).toMatchObject({ tone: "action", nextAction: "X で本人接続する" });
    expect(cookie.label).toContain("Cookie");
    expect(personaConnectionState(account({}), credential({ verification_code: "identity_mismatch" })).tone).not.toBe("ok");
    expect(personaConnectionState(account({}), null)).toMatchObject({ label: "未接続", tone: "action" });
    expect(personaConnectionState(account({}), undefined)).toMatchObject({ tone: "unknown", nextAction: null });
    expect(personaConnectionState(account({}), credential({ verification_code: "credential_expired", status: "invalid" }))).toMatchObject({ label: "認証期限切れ", tone: "action" });
  });

  it("walks a YouTube slot through channel ID, activation and 本人接続", () => {
    const placeholder = account({ platform: "youtube", account_ref: "sns-v6:offby1:youtube", status: "paused", connection_id: null });
    expect(personaConnectionState(placeholder, null)).toMatchObject({ label: "channel ID 未登録", nextAction: "YouTube の channel ID を登録する" });
    const registered = { ...placeholder, account_ref: "UC" + "a".repeat(22) } as PlatformAccount;
    expect(personaConnectionState(registered, null)).toMatchObject({ label: "停止中", nextAction: "YouTube 枠を有効化する" });
    expect(personaConnectionState({ ...registered, status: "active" } as PlatformAccount, null)).toMatchObject({ nextAction: "YouTube で本人接続する" });
  });
});

describe("isTestFixtureName", () => {
  it("hides today's QA / acceptance records and keeps every SNS v6 persona", () => {
    for (const name of ["QA Media Vault Character", "QA Operations Acceptance Character 20261010", "QA Operations Saved Content 20261010", "受入確認 20261011・雨の日のクチバシ", "制作経路実生成検証・青いカップ（QA）", "QA CrowdWorks Connection"]) {
      expect(isTestFixtureName(name), name).toBe(true);
    }
    for (const name of ["OFFBY1", "元気が2番！", "よぱにしゃっど！", "鬱戦", "刺されても死なない大きなクチバシ", "OUTDATED™", "どうせ嘘だろうが", "UN//REAL", "UN//REAL 初期制作設計", "CrowdWorks", "@Autoobike1984"]) {
      expect(isTestFixtureName(name), name).toBe(false);
    }
  });
});
