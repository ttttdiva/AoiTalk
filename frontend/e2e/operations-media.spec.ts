import {
  expect,
  test,
} from "@playwright/test";

import {
  addAuthCookie,
  mockAuthenticatedApis,
} from "./support/auth";


function emptySlots() {
  return Array.from({ length: 9 }, (_, index) => ({
    slot: index + 1,
    persona_id: null,
    persona: null,
  }));
}

test.describe("Operations Media Persona", () => {
  test("bare /operations is Overview and legacy tabs keep their query routes", async ({
    page,
  }) => {
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);

    await page.route(
      "**/api/python-proxy/operations/media/**",
      async (route) => {
        const url = new URL(
          route.request().url(),
        );

        if (
          url.pathname.endsWith(
            "/operations/media/persona-intake",
          )
        ) {
          await route.fulfill({
            status: 200,
            json: {
              project_id: null,
              slots: emptySlots(),
            },
          });
          return;
        }

        if (
          url.pathname.endsWith(
            "/operations/media/personas",
          )
        ) {
          await route.fulfill({
            status: 200,
            json: [],
          });
          return;
        }

        await route.fulfill({
          status: 404,
          json: {
            detail: "not mocked",
          },
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

    await page.goto("/operations");

    await expect(page.getByTestId("operations-command-center")).toBeVisible();
    await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();

    const operationsNavigation =
      page.getByRole("navigation", {
        name: "Operationsワークスペース",
      });

    await expect(
      operationsNavigation.getByRole(
        "link",
        { name: /Personas/u },
      ),
    ).toHaveAttribute(
      "href",
      "/operations?tab=personas",
    );

    await expect(
      operationsNavigation.getByRole(
        "link",
        { name: /Connections/u },
      ),
    ).toHaveAttribute(
      "href",
      "/operations?tab=connections",
    );

    await expect(
      operationsNavigation.getByRole(
        "link",
        { name: /Opportunities/u },
      ),
    ).toHaveAttribute(
      "href",
      "/operations?tab=opportunities",
    );

    await expect(
      operationsNavigation.getByRole(
        "link",
        { name: /Approvals \/ Actions/u },
      ),
    ).toHaveAttribute(
      "href",
      "/operations?tab=actions",
    );

    await page.goto(
      "/operations?tab=connections",
    );

    await expect(page).toHaveURL(
      /\/operations\?tab=connections$/u,
    );

    await expect(
      page.getByTestId(
        "operations-connections-panel",
      ),
    ).toBeVisible();

    await expect(
      page.getByTestId(
        "media-persona-panel",
      ),
    ).toHaveCount(0);
  });
});
