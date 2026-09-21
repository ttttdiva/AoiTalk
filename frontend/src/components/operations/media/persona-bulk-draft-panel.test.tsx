// @vitest-environment jsdom

import {
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";

import { PersonaBulkDraftPanel } from "@/components/operations/media/persona-bulk-draft-panel";
import { mediaOperationsSetupApi } from "@/lib/media-operations-setup-api";


vi.mock(
  "@/lib/media-operations-setup-api",
  async () => {
    const actual =
      await vi.importActual<
        typeof import("@/lib/media-operations-setup-api")
      >(
        "@/lib/media-operations-setup-api",
      );

    return {
      ...actual,
      mediaOperationsSetupApi: {
        importPersonaBulkDraft:
          vi.fn(),
        getPersonaBulkDraft:
          vi.fn(),
        correctPersonaBulkDraft:
          vi.fn(),
        applyPersonaBulkDraft:
          vi.fn(),
        listPlatformAccounts:
          vi.fn(),
        createPlatformAccount:
          vi.fn(),
        getPlatformAccount:
          vi.fn(),
        appendPlatformAccountRevision:
          vi.fn(),
      },
    };
  },
);

function unknownFact() {
  return {
    state: "unknown",
    value: null,
    evidence: null,
  };
}

function slots() {
  return Array.from(
    {
      length: 9,
    },
    (_, index) => ({
      slot: index + 1,
      display_name: {
        state: "explicit",
        value: `test-${index + 1}`,
        evidence: null,
      },
      summary: unknownFact(),
      voice: unknownFact(),
      audience: unknownFact(),
      platforms: unknownFact(),
      content_pillars:
        unknownFact(),
    }),
  );
}

function preview(
  applyable: boolean,
) {
  return {
    id: "draft-id",
    owner_user_id:
      "owner-id",
    project_id: null,
    source_hash: "a".repeat(64),
    draft_hash: "b".repeat(64),
    version: 1,
    status: "draft",
    applyable,
    issues: applyable
      ? []
      : [
          {
            code: "inferred_fact_requires_correction",
            slot: 1,
            field:
              "summary",
            message:
              "inferred facts cannot be applied",
          },
        ],
    slots: slots(),
    applied_at: null,
    created_by:
      "owner-id",
    created_at:
      "2026-09-01T00:00:00",
    updated_at:
      "2026-09-01T00:00:00",
  };
}

describe(
  "PersonaBulkDraftPanel",
  () => {
    beforeEach(() => {
      vi.clearAllMocks();
    });

    it("shows inferred blocker and disables atomic apply", async () => {
      vi.mocked(
        mediaOperationsSetupApi.importPersonaBulkDraft,
      ).mockResolvedValue(
        preview(false) as never,
      );

      render(
        <PersonaBulkDraftPanel
          onApplied={() => undefined}
        />,
      );

      fireEvent.change(
        screen.getByLabelText(
          "9人Persona draft JSON",
        ),
        {
          target: {
            value:
              JSON.stringify({
                slots: slots(),
              }),
          },
        },
      );

      await userEvent.click(
        screen.getByRole(
          "button",
          {
            name: "Preview",
          },
        ),
      );

      await waitFor(() => {
        expect(
          screen.getByTestId(
            "bulk-draft-inferred-count",
          ),
        ).toHaveTextContent(
          "inferred 1",
        );
      });

      expect(
        screen.getByRole(
          "button",
          {
            name:
              "Atomic Apply",
          },
        ),
      ).toBeDisabled();
    });

    it("applies only after corrected preview is applyable", async () => {
      const blocked =
        preview(false);
      const corrected = {
        ...preview(true),
        version: 2,
      };

      vi.mocked(
        mediaOperationsSetupApi.importPersonaBulkDraft,
      ).mockResolvedValue(
        blocked as never,
      );

      vi.mocked(
        mediaOperationsSetupApi.correctPersonaBulkDraft,
      ).mockResolvedValue(
        corrected as never,
      );

      vi.mocked(
        mediaOperationsSetupApi.applyPersonaBulkDraft,
      ).mockResolvedValue(
        {
          draft_id:
            "draft-id",
          draft_hash:
            corrected.draft_hash,
          applied_at:
            "2026-09-01T01:00:00",
          created_persona_ids:
            [],
          intake: {
            project_id: null,
            slots: [],
          },
        } as never,
      );

      vi.mocked(
        mediaOperationsSetupApi.getPersonaBulkDraft,
      ).mockResolvedValue(
        {
          ...corrected,
          status: "applied",
          applyable: false,
          applied_at:
            "2026-09-01T01:00:00",
        } as never,
      );

      const onApplied =
        vi.fn();

      render(
        <PersonaBulkDraftPanel
          onApplied={onApplied}
        />,
      );

      fireEvent.change(
        screen.getByLabelText(
          "9人Persona draft JSON",
        ),
        {
          target: {
            value:
              JSON.stringify({
                slots: slots(),
              }),
          },
        },
      );

      await userEvent.click(
        screen.getByRole(
          "button",
          {
            name: "Preview",
          },
        ),
      );

      await userEvent.click(
        screen.getByRole(
          "button",
          {
            name:
              "修正を再Preview",
          },
        ),
      );

      await waitFor(() => {
        expect(
          screen.getByRole(
            "button",
            {
              name:
                "Atomic Apply",
            },
          ),
        ).toBeEnabled();
      });

      await userEvent.click(
        screen.getByRole(
          "button",
          {
            name:
              "Atomic Apply",
          },
        ),
      );

      await waitFor(() => {
        expect(
          onApplied,
        ).toHaveBeenCalledTimes(
          1,
        );
      });
    });
  },
);
