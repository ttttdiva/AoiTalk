import {
  expect,
  test,
} from "@playwright/test";

import {
  addAuthCookie,
  mockAuthenticatedApis,
} from "./support/auth";


test.describe(
  "MediaOps Research",
  () => {
    test(
      "research is a separate MediaOps tab while Persona and Engagement deep links remain stable",
      async ({
        page,
      }) => {
        await addAuthCookie(page);
        await mockAuthenticatedApis(page);

        await page.route(
          "**/api/python-proxy/operations/media/research-routines*",
          async (route) => {
            await route.fulfill({
              status: 200,
              json: [],
            });
          },
        );

        await page.route(
          "**/api/python-proxy/operations/media/editorial-programs*",
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
          "/operations?tab=research",
        );

        await expect(
          page.getByTestId(
            "media-research-panel",
          ),
        ).toBeVisible();

        const navigation =
          page.getByRole(
            "navigation",
            {
              name:
                "Operationsワークスペース",
            },
          );

        await expect(
          navigation.getByRole(
            "link",
            {
              name: /Personas/u,
            },
          ),
        ).toHaveAttribute(
          "href",
          "/operations?tab=personas",
        );

        await expect(
          navigation.getByRole(
            "link",
            {
              name: /Research/u,
            },
          ),
        ).toHaveAttribute(
          "href",
          "/operations?tab=research",
        );

        await expect(
          navigation.getByRole(
            "link",
            {
              name:
                /Connections/u,
            },
          ),
        ).toHaveAttribute(
          "href",
          "/operations?tab=connections",
        );

        await page.goto(
          "/operations",
        );

        await expect(
          page.getByTestId("operations-command-center"),
        ).toBeVisible();

        await expect(
          page.getByTestId(
            "media-research-panel",
          ),
        ).toHaveCount(0);

        await page.goto(
          "/operations?tab=connections",
        );

        await expect(
          page.getByTestId(
            "operations-connections-panel",
          ),
        ).toBeVisible();
      },
    );
  },
);
