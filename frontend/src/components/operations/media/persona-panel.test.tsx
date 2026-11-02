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
import { mediaOperationsApi } from "@/lib/media-operations-api";

vi.mock("next/link", () => ({ default: ({ href, children, ...props }: React.ComponentProps<"a">) => <a href={href} {...props}>{children}</a> }));

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

describe("MediaPersonaPanel (one persona's settings)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(mediaOperationsApi.getCharacter).mockResolvedValue(createdDetail());
    vi.mocked(mediaOperationsApi.listPersonaResources).mockResolvedValue([]);
    vi.mocked(mediaOperationsApi.patchCharacter).mockImplementation(async (_id, patch) => {
      const detail = createdDetail();
      return { ...detail, current_revision: { ...detail.current_revision, ...patch, version: 2 } } as never;
    });
  });

  it("opens only the requested persona, without a persona list, create flow or dashboard", async () => {
    render(<MediaPersonaPanel personaId="persona-id" />);
    expect(await screen.findByRole("heading", { name: "test-persona の設定" })).toBeInTheDocument();
    expect(mediaOperationsApi.getCharacter).toHaveBeenCalledWith("persona-id");
    expect(screen.getByRole("link", { name: "人格に戻る" })).toHaveAttribute("href", "/operations?tab=studio&persona=persona-id");
    expect(mediaOperationsApi.listCharacters).not.toHaveBeenCalled();
    expect(mediaOperationsApi.getCharacterDashboard).not.toHaveBeenCalled();
    expect(screen.queryByTestId("character-list-card")).not.toBeInTheDocument();
    expect(screen.queryByTestId("character-create-button")).not.toBeInTheDocument();
    expect(screen.getByTestId("character-resources")).toBeInTheDocument();
  });

  it("saves only the edited fields and can revert unsaved edits", async () => {
    const user = userEvent.setup();
    render(<MediaPersonaPanel personaId="persona-id" />);
    const name = await screen.findByLabelText("表示名", { selector: "#media-character-name" });
    const save = screen.getByRole("button", { name: "設定を保存" });
    expect(save).toBeDisabled();

    await user.clear(name);
    await user.type(name, "draft name");
    await user.click(screen.getByRole("button", { name: "変更を戻す" }));
    expect(name).toHaveValue("test-persona");

    await user.clear(name);
    await user.type(name, "renamed");
    await user.click(screen.getByRole("button", { name: "設定を保存" }));
    await waitFor(() => expect(mediaOperationsApi.patchCharacter).toHaveBeenCalledTimes(1));
    const [id, patch, idempotencyKey] = vi.mocked(mediaOperationsApi.patchCharacter).mock.calls[0];
    expect(id).toBe("persona-id");
    expect(Object.keys(patch).filter(key => !key.startsWith("expected_"))).toEqual(["display_name"]);
    expect(patch).toMatchObject({ display_name: "renamed" });
    expect(typeof idempotencyKey).toBe("string");
    expect(mediaOperationsApi.createCharacter).not.toHaveBeenCalled();
  });
});
