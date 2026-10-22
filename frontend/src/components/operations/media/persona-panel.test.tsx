// @vitest-environment jsdom

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";

import { MediaPersonaPanel } from "@/components/operations/media/persona-panel";
import { mediaOperationsApi, type MediaCharacterDashboard } from "@/lib/media-operations-api";

vi.mock("@/lib/media-operations-api", async () => {
  const actual = await vi.importActual<
    typeof import("@/lib/media-operations-api")
  >("@/lib/media-operations-api");

  return {
    ...actual,
    mediaOperationsApi: {
      getPersonaIntake: vi.fn(),
      listPersonas: vi.fn(),
      listCharacters: vi.fn(),
      createPersona: vi.fn(),
      createCharacter: vi.fn(),
      getPersona: vi.fn(),
      getCharacter: vi.fn(),
      getCharacterDashboard: vi.fn(),
      getRevenueCostRecords: vi.fn().mockResolvedValue({ items: [], effective: null, has_more: false }),
      createRevenueCostRecord: vi.fn(),
      listCostEntries: vi.fn().mockResolvedValue([]),
      createCostEntry: vi.fn(),
      patchCharacter: vi.fn(),
      updateCharacter: vi.fn(),
      appendPersonaRevision: vi.fn(),
      listPersonaResources: vi.fn(),
      attachPersonaResource: vi.fn(),
    },
  };
});

vi.mock("@/lib/media-operations-setup-api", async () => {
  const actual = await vi.importActual<
    typeof import("@/lib/media-operations-setup-api")
  >("@/lib/media-operations-setup-api");

  return {
    ...actual,
    mediaOperationsSetupApi: {
      importPersonaBulkDraft: vi.fn(),
      getPersonaBulkDraft: vi.fn(),
      correctPersonaBulkDraft: vi.fn(),
      applyPersonaBulkDraft: vi.fn(),
      listPlatformAccounts: vi.fn().mockResolvedValue([]),
      createPlatformAccount: vi.fn(),
      getPlatformAccount: vi.fn(),
      appendPlatformAccountRevision: vi.fn(),
    },
  };
});

function createdDetail() {
  const revision = {
    id: "revision-id",
    persona_id: "persona-id",
    owner_user_id: "owner-id",
    project_id: null,
    version: 1,
    display_name: "test-persona",
    summary: null,
    voice: null,
    audience: null,
    adult_policy: null,
    allowed_subjects: [],
    creative_direction: null,
    default_language: null,
    disclosure_policy: null,
    image_production_policy: {},
    ip_policy: null,
    kpi_objectives: [],
    locale: null,
    monetization_policy: {},
    niche: null,
    positioning: null,
    prohibited_subjects: [],
    public_aliases: [],
    research_policy: {},
    sensitive_policy: null,
    timezone: null,
    video_production_policy: {},
    visual_identity: {},
    platforms: [],
    content_pillars: [],
    content_hash: "b".repeat(64),
    created_by: "owner-id",
    created_at: "2026-09-01T00:00:00",
  };

  return {
    id: "persona-id",
    owner_user_id: "owner-id",
    project_id: null,
    parent_brand_ref: null,
    state: "draft" as const,
    create_hash: "a".repeat(64),
    created_by: "owner-id",
    created_at: "2026-09-01T00:00:00",
    current_revision: revision,
    revisions: [revision],
    revision_history_truncated: false,
  };
}

function characterSummary() {
  const detail = createdDetail();
  return {
    id: detail.id,
    owner_user_id: detail.owner_user_id,
    project_id: detail.project_id,
    parent_brand_ref: detail.parent_brand_ref,
    state: detail.state,
    create_hash: detail.create_hash,
    created_by: detail.created_by,
    created_at: detail.created_at,
    current_revision: detail.current_revision,
  };
}

function emptyDashboard() {
  return {
    character: createdDetail(),
    connected_accounts: [],
    research_candidates: [],
    generation: { recipes: [], runs: [] },
    calendar: [],
    results: { snapshot_count: 0, metrics: {}, last_observed_at: null },
    learning: { count: 0, pending_review_count: 0, items: [] },
  };
}

describe("MediaPersonaPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();

    vi.mocked(mediaOperationsApi.listCharacters).mockResolvedValue([]);
    vi.mocked(mediaOperationsApi.getCharacter).mockResolvedValue(createdDetail());
    vi.mocked(mediaOperationsApi.getCharacterDashboard).mockResolvedValue(emptyDashboard());

    vi.mocked(mediaOperationsApi.createCharacter).mockImplementation(async () => {
      vi.mocked(mediaOperationsApi.listCharacters).mockResolvedValue([characterSummary()]);
      return createdDetail();
    });
  });

  it("distinguishes slot-free Personas from conversation Characters without seeding", async () => {
    render(<MediaPersonaPanel />);

    await waitFor(() => {
      expect(mediaOperationsApi.listCharacters).toHaveBeenCalledTimes(1);
    });

    expect(screen.getByTestId("character-list-card")).toBeInTheDocument();
    expect(screen.getAllByText("Persona（発信人格）")).toHaveLength(2);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Persona（発信人格）の説明" }));
    expect(await screen.findByText(/固定slotに紐づかないもの/)).toHaveTextContent("会話用Characterとは別");
    expect(screen.getByText("0件")).toBeInTheDocument();
    expect(screen.getByTestId("character-create-button")).toHaveTextContent("Personaを追加");
    expect(screen.queryByTestId(/^persona-slot-/u)).not.toBeInTheDocument();

    expect(mediaOperationsApi.createCharacter).not.toHaveBeenCalled();
  });

  it("creates a Character only after explicit form submission", async () => {
    const user = userEvent.setup();

    render(<MediaPersonaPanel />);

    await screen.findByTestId("character-create-button");

    await user.type(
      screen.getByLabelText("表示名", {
        selector: "#media-character-name",
      }),
      "test-persona",
    );

    await user.click(
      screen.getByRole("button", {
        name: "作成",
      }),
    );

    await waitFor(() => {
      expect(mediaOperationsApi.createCharacter).toHaveBeenCalledTimes(1);
    });

    const [payload, idempotencyKey] = vi.mocked(mediaOperationsApi.createCharacter).mock.calls[0];

    expect(payload).toEqual({
      display_name: "test-persona",
      summary: null,
      voice: null,
      audience: null,
      platforms: [],
      content_pillars: [],
    });

    expect(typeof idempotencyKey).toBe("string");
    expect(idempotencyKey.length).toBeGreaterThan(0);
  });

  it("opens and cancels independent cost entry from a Character with no sale", async () => {
    vi.mocked(mediaOperationsApi.listCharacters).mockResolvedValue([characterSummary()]);
    render(<MediaPersonaPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "未配賦費用を開く" }));
    await waitFor(() => expect(mediaOperationsApi.listCostEntries).toHaveBeenCalledWith("persona-id", null));
    expect(await screen.findByRole("form", { name: "未配賦費用フォーム" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "未配賦費用を閉じる" }));
    expect(screen.queryByRole("form", { name: "未配賦費用フォーム" })).not.toBeInTheDocument();
    expect(mediaOperationsApi.createCostEntry).not.toHaveBeenCalled();
  });

  it("renders ACL-scoped metrics and revenue by safe dimensions", async () => {
    const dashboard = {
      ...emptyDashboard(),
      connected_accounts: [
        {
          id: "account-1",
          platform: "youtube",
          account_ref: "acct-ref-1",
          status: "unverified",
          capabilities: { identity: "unverified", publish: "unavailable" },
          adapter_ready: false,
        },
      ],
      metrics: {
        count: 2,
        limit: 100,
        offset: 0,
        has_more: false,
        items: [
          {
            id: "metric-1",
            platform: "youtube",
            platform_account_ref: "acct-ref-1",
            content_variant_ref: "variant-ref-1",
            metrics: { views: 42, likes: 3, gross_revenue: 999999, net_revenue: 99999, refunds: 888, cost: 777 },
          },
          {
            id: "metric-2",
            platform: "youtube",
            platform_account_ref: "acct-ref-1",
            content_variant_ref: "variant-ref-1",
            metrics: { views: 8 },
          },
        ],
      },
      revenue: {
        count: 1,
        limit: 100,
        offset: 0,
        has_more: false,
        items: [
          {
            id: "revenue-1",
            platform: "youtube",
            platform_account_ref: "acct-ref-1",
            content_ref: "content-ref-1",
            currency: "jpy",
            gross_amount: 1200,
            net_amount: 1100,
          },
        ],
      },
      experiments: {
        count: 1,
        limit: 100,
        offset: 0,
        has_more: false,
        items: [
          {
            id: "experiment-1",
            name: "サムネイル比較",
            status: "running",
            results: [],
          },
        ],
      },
    };

    vi.mocked(mediaOperationsApi.listCharacters).mockResolvedValue([characterSummary()]);
    vi.mocked(mediaOperationsApi.getCharacterDashboard).mockResolvedValue(dashboard as MediaCharacterDashboard);

    render(<MediaPersonaPanel />);

    const metrics = await screen.findByTestId("character-dashboard-metrics");
    expect(metrics).toHaveTextContent("YouTube");
    expect(metrics).toHaveTextContent("acct-ref-1");
    expect(metrics).toHaveTextContent("variant-ref-1");
    expect(metrics).toHaveTextContent("views: 50");
    expect(metrics).not.toHaveTextContent("gross_revenue");
    expect(metrics).not.toHaveTextContent("net_revenue");
    expect(metrics).not.toHaveTextContent("refunds");
    expect(metrics).not.toHaveTextContent("cost");

    const revenue = screen.getByTestId("character-dashboard-revenue");
    expect(revenue).toHaveTextContent("JPY");
    expect(revenue).toHaveTextContent("総額 1,200");
    expect(revenue).toHaveTextContent("手取り 1,100");

    await userEvent.click(screen.getByRole("button", { name: "売上 revenue-1 の費用記録を開く" }));
    await screen.findByRole("form", { name: "費用記録フォーム" });
    expect(mediaOperationsApi.getRevenueCostRecords).toHaveBeenCalledWith("revenue-1");
    await userEvent.click(screen.getByRole("button", { name: "閉じる・キャンセル" }));
    expect(screen.queryByRole("form", { name: "費用記録フォーム" })).not.toBeInTheDocument();
    expect(mediaOperationsApi.createRevenueCostRecord).not.toHaveBeenCalled();

    const experiments = screen.getByTestId("character-dashboard-experiments");
    expect(experiments).toHaveTextContent("サムネイル比較");
    expect(experiments).toHaveTextContent("実行中");
    const accounts = screen.getByTestId("character-dashboard-connected-accounts");
    expect(accounts).toHaveTextContent("未検証");
    expect(accounts).toHaveTextContent("publish: 利用不可");
  });

it("separates revenue evidence and refunds, and keeps missing amounts unknown", async () => {
  const base = { platform: "youtube", currency: "USD", gross_amount: 100, net_amount: 90, source: "manual", settlement_status: "estimated", revenue_authority: "manual_estimate", event_type: "sale" };
  const items = [
    { ...base, id: "usd" },
    { ...base, id: "jpy", currency: "JPY" },
    { ...base, id: "settled", settlement_status: "settled", revenue_authority: "manual_settled" },
    { ...base, id: "refund", event_type: "refund", gross_amount: -20, net_amount: -18 },
    { ...base, id: "api", source: "api", settlement_status: "settled", revenue_authority: "provider_settled" },
    { id: "legacy", currency: "EUR", gross_amount: null, net_amount: 0 },
  ];
  vi.mocked(mediaOperationsApi.listCharacters).mockResolvedValue([characterSummary()]);
  vi.mocked(mediaOperationsApi.getCharacterDashboard).mockResolvedValue({ ...emptyDashboard(), revenue: { count: 100, limit: 6, offset: 0, has_more: true, items } } as MediaCharacterDashboard);
  render(<MediaPersonaPanel />);
  const revenue = await screen.findByTestId("character-dashboard-revenue");
  expect(revenue).toHaveTextContent("表示中の6件");
  expect(revenue).toHaveTextContent("利益は算出できません");
  expect(revenue).toHaveTextContent("推計（自己申告）");
  expect(revenue).toHaveTextContent("入金済み（自己申告）");
  expect(revenue).toHaveTextContent("API記録・入金確定");
  expect(revenue).toHaveTextContent("返金");
  expect(revenue).toHaveTextContent("総額 -20 · 手取り -18");
  expect(revenue).toHaveTextContent("総額 不明 · 手取り 0");
  expect(revenue).toHaveTextContent("出所・入金状態不明");
  expect(revenue).not.toHaveTextContent("総額 400");
});

});
