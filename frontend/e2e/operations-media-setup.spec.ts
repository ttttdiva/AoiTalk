import {
  expect,
  test,
} from "@playwright/test";

import {
  addAuthCookie,
  mockAuthenticatedApis,
} from "./support/auth";


function unknownFact() {
  return {
    state: "unknown",
    value: null,
    evidence: null,
  };
}

function emptySlots() {
  return Array.from(
    {
      length: 9,
    },
    (_, index) => ({
      slot: index + 1,
      persona_id: null,
      persona: null,
    }),
  );
}

test.describe(
  "MediaOps WS2 setup",
  () => {
    test(
      "Persona-first workspace exposes bulk review and secret-free PlatformAccount management without changing legacy tabs",
      async ({
        page,
      }) => {
        await addAuthCookie(
          page,
        );
        await mockAuthenticatedApis(
          page,
        );

        await page.route(
          "**/api/python-proxy/operations/media/persona-intake*",
          async (route) => {
            await route.fulfill({
              status: 200,
              json: {
                project_id: null,
                slots:
                  emptySlots(),
              },
            });
          },
        );

        await page.route(
          "**/api/python-proxy/operations/media/platform-accounts*",
          async (route) => {
            await route.fulfill({
              status: 200,
              json: [],
            });
          },
        );

        await page.route(
          "**/api/python-proxy/operations/connections*",
          async (route) => {
            await route.fulfill({
              status: 200,
              json: [],
            });
          },
        );

        await page.goto(
          "/operations",
        );

        await expect(
          page.getByTestId(
            "persona-bulk-draft-panel",
          ),
        ).toBeVisible();

        await expect(
          page.getByTestId(
            "platform-account-panel",
          ),
        ).toBeVisible();

        await expect(
          page.getByText(
            "password / token / credential refを入力する欄はありません。",
          ),
        ).toBeVisible();

        await page.goto(
          "/operations?tab=connections",
        );

        await expect(
          page.getByTestId(
            "operations-connections-panel",
          ),
        ).toBeVisible();

        await expect(
          page.getByTestId(
            "persona-bulk-draft-panel",
          ),
        ).toHaveCount(0);

        await page.goto(
          "/operations",
        );

        const typedDraft = {
          slots: Array.from(
            {
              length: 9,
            },
            (_, index) => ({
              slot:
                index + 1,
              display_name: {
                state:
                  "explicit",
                value:
                  `test-${index + 1}`,
                evidence: null,
              },
              summary:
                index === 0
                  ? {
                      state:
                        "inferred",
                      value:
                        "inferred",
                      evidence:
                        "basis",
                    }
                  : unknownFact(),
              voice:
                unknownFact(),
              audience:
                unknownFact(),
              platforms:
                unknownFact(),
              content_pillars:
                unknownFact(),
            }),
          ),
        };

        await page.route(
          "**/api/python-proxy/operations/media/persona-drafts",
          async (route) => {
            await route.fulfill({
              status: 200,
              json: {
                id:
                  "draft-id",
                owner_user_id:
                  "owner-id",
                project_id:
                  null,
                source_hash:
                  "a".repeat(
                    64,
                  ),
                draft_hash:
                  "b".repeat(
                    64,
                  ),
                version: 1,
                status:
                  "draft",
                applyable:
                  false,
                issues: [
                  {
                    code:
                      "inferred_fact_requires_correction",
                    slot: 1,
                    field:
                      "summary",
                    message:
                      "inferred facts cannot be applied",
                  },
                ],
                slots:
                  typedDraft.slots,
                applied_at:
                  null,
                created_by:
                  "owner-id",
                created_at:
                  "2026-09-01T00:00:00",
                updated_at:
                  "2026-09-01T00:00:00",
              },
            });
          },
        );

        await page
          .getByLabel(
            "9人Persona draft JSON",
          )
          .fill(
            JSON.stringify(
              typedDraft,
            ),
          );

        await page
          .getByRole(
            "button",
            {
              name:
                "Preview",
            },
          )
          .click();

        await expect(
          page.getByTestId(
            "bulk-draft-inferred-count",
          ),
        ).toContainText(
          "inferred 1",
        );

        await expect(
          page.getByRole(
            "button",
            {
              name:
                "Atomic Apply",
            },
          ),
        ).toBeDisabled();
      },
    );
  },
);
