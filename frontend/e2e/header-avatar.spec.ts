import { expect, test } from "@playwright/test";
import { normalizeBasePath } from "../src/lib/base-path";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

// Run against an artifact built with the same public prefix. In particular,
// NEXT_PUBLIC_BASE_PATH=/at covers the profile-image 404 regression.
const basePath = normalizeBasePath(
  process.env.AOITALK_PUBLIC_BASE_PATH || process.env.NEXT_PUBLIC_BASE_PATH,
);
const avatarPath = "/api/users/header-avatar-fixture/avatar";
const png = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a5f8AAAAASUVORK5CYII=",
  "base64",
);

test("ヘッダー画像はbasePathと更新後のversionを保持して再読込できる", async ({ page }) => {
  await addAuthCookie(page);
  await mockAuthenticatedApis(page);
  let avatarUrl = `${avatarPath}?v=first%20image.png`;
  const imageRequests: string[] = [];
  let uploadCount = 0;

  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const pathname = basePath && url.pathname.startsWith(`${basePath}/`)
      ? url.pathname.slice(basePath.length)
      : url.pathname;
    if (pathname === "/api/auth/status") {
      await route.fulfill({ json: {
        authenticated: true,
        user: { id: "header-avatar-fixture", username: "aoi", display_name: "Aoi", role: "user", avatar_url: avatarUrl },
      } });
      return;
    }
    if (pathname === avatarPath) {
      imageRequests.push(`${url.pathname}${url.search}`);
      // Do not repair an unprefixed image request in the fixture: that is
      // exactly the broken browser request this test needs to catch.
      await route.fulfill(url.pathname === `${basePath}${avatarPath}`
        ? { status: 200, contentType: "image/png", body: png }
        : { status: 404, body: "Wrong public mount" });
      return;
    }
    if (pathname === "/api/users/me/avatar" && route.request().method() === "POST") {
      uploadCount += 1;
      // Exercise an already-prefixed response as well as the initial bare
      // API path. This is a mocked upload, never a real account change.
      avatarUrl = `${basePath}${avatarPath}?v=second%20image.png`;
      await route.fulfill({ json: { avatar_url: avatarUrl } });
      return;
    }
    // The shared API fixture is root-mounted. Adapt its unrelated endpoints
    // after the avatar assertions above, without changing browser image URLs.
    url.pathname = pathname;
    await route.fallback({ url: url.toString() });
  });

  await page.goto(`${basePath}/chat`);
  const menu = page.getByRole("button", { name: "ユーザーメニューを開く" });
  const image = menu.getByRole("img", { name: "Aoiのアイコン" });
  await expect(image).toHaveAttribute("src", `${basePath}${avatarPath}?v=first%20image.png`);
  await expect.poll(() => image.evaluate((node) => (node as HTMLImageElement).naturalWidth)).toBeGreaterThan(0);
  await expect(menu.locator('[data-slot="avatar-fallback"]')).toHaveCount(0);

  await page.locator('header input[type="file"]').setInputFiles({
    name: "avatar.png", mimeType: "image/png", buffer: png,
  });
  await expect(image).toHaveAttribute("src", `${basePath}${avatarPath}?v=second%20image.png`);
  await expect.poll(() => image.evaluate((node) => (node as HTMLImageElement).naturalWidth)).toBeGreaterThan(0);
  expect(uploadCount).toBe(1);

  await page.reload();
  await expect(image).toHaveAttribute("src", `${basePath}${avatarPath}?v=second%20image.png`);
  await expect.poll(() => image.evaluate((node) => (node as HTMLImageElement).naturalWidth)).toBeGreaterThan(0);
  await expect(menu.locator('[data-slot="avatar-fallback"]')).toHaveCount(0);
  expect(imageRequests).toContain(`${basePath}${avatarPath}?v=first%20image.png`);
  expect(imageRequests).toContain(`${basePath}${avatarPath}?v=second%20image.png`);
  expect(imageRequests.every((url) => url.startsWith(`${basePath}${avatarPath}?`))).toBe(true);
});
