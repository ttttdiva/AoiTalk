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

  it("renders an unlimited Character list without creating seeded Personas", async () => {
    render(<MediaPersonaPanel />);

    await waitFor(() => {
      expect(mediaOperationsApi.listCharacters).toHaveBeenCalledTimes(1);
    });

    expect(screen.getByTestId("character-list-card")).toBeInTheDocument();
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
            metrics: { views: 42, likes: 3 },
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

    const revenue = screen.getByTestId("character-dashboard-revenue");
    expect(revenue).toHaveTextContent("JPY");
    expect(revenue).toHaveTextContent("Gross 1,200");
    expect(revenue).toHaveTextContent("Net 1,100");

    const experiments = screen.getByTestId("character-dashboard-experiments");
    expect(experiments).toHaveTextContent("サムネイル比較");
    expect(experiments).toHaveTextContent("実行中");
    const accounts = screen.getByTestId("character-dashboard-connected-accounts");
    expect(accounts).toHaveTextContent("未検証");
    expect(accounts).toHaveTextContent("publish: 利用不可");
  });
});
