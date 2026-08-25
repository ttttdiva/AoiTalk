import { expect, test, type Page } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

/**
 * 設計書 §13.2 の U13 導線 e2e。
 * 「第1章 → 第2章A」を用意し、分岐の 2 入口・複製元の不変性・分岐スイッチの往復・
 * 通し読みの差分・分割 → 複製の 2 手順を実 UI 操作で検証する。
 *
 * モックのレスポンス形は実 API（`src/api/story_routes.py` / `src/services/story_studio.py`）
 * に合わせる。overview は `{work, graph, current_route}`、graph の links は
 * `from_episode_id` / `to_episode_id`、structure は `{results:[...], ...graph}`、
 * split は `{source, created, links}` を返す。
 */

type MockEpisode = {
  id: string;
  work_id: string;
  title: string;
  plot: string;
  summary: string;
  premise_note: string;
  status: string;
  target_chars: number;
  char_count: number;
  body: string;
  body_etag: string;
  current_rev_no: number;
  map_x: number;
  map_y: number;
  sort_hint: number;
};

type MockLink = {
  id: string;
  work_id: string;
  from_episode_id: string;
  to_episode_id: string;
  choice_label: string | null;
  position: number;
  is_primary: boolean;
};

type StoryMockState = {
  episodes: MockEpisode[];
  links: MockLink[];
  uiState: Record<string, unknown>;
  createBodies: Array<Record<string, unknown>>;
  structureOps: Array<Array<Record<string, unknown>>>;
  splitBodies: Array<Record<string, unknown>>;
  writeBodies: Array<Record<string, unknown>>;
  nextEpisodeNumber: number;
  nextLinkNumber: number;
};

const CHAPTER_1_BODY = "第一章の前半。旅立ちの朝だった。\n第一章の後半。城門をくぐった。";
const CHAPTER_2A_BODY = "第二章Aの本文。王を信じ、城に残った。";
const CHAPTER_2B_BODY = "第二章Bの本文。王を疑い、城を出た。";

function makeEpisode(state: StoryMockState, overrides: Partial<MockEpisode> & { title: string }): MockEpisode {
  const id = `ep-${state.nextEpisodeNumber++}`;
  const body = overrides.body ?? "";
  return {
    id,
    work_id: "work-1",
    plot: "",
    summary: "",
    premise_note: "",
    status: "draft",
    target_chars: 6000,
    map_x: 0,
    map_y: 0,
    sort_hint: state.episodes.length,
    current_rev_no: 1,
    ...overrides,
    body,
    char_count: Array.from(body).length,
    body_etag: `etag-${id}`,
  };
}

function addLink(state: StoryMockState, link: Omit<MockLink, "id" | "work_id">): MockLink {
  const created: MockLink = { id: `link-${state.nextLinkNumber++}`, work_id: "work-1", ...link };
  state.links.push(created);
  return created;
}

function createMockState(): StoryMockState {
  const state: StoryMockState = {
    episodes: [],
    links: [],
    uiState: {},
    createBodies: [],
    structureOps: [],
    splitBodies: [],
    writeBodies: [],
    nextEpisodeNumber: 1,
    nextLinkNumber: 1,
  };
  const first = makeEpisode(state, { title: "第一章", plot: "主人公が旅立つ。", body: CHAPTER_1_BODY, summary: "旅立ち" });
  const secondA = makeEpisode(state, { title: "第二章A", plot: "王を信じる。", body: CHAPTER_2A_BODY });
  state.episodes.push(first, secondA);
  addLink(state, { from_episode_id: first.id, to_episode_id: secondA.id, choice_label: null, position: 0, is_primary: true });
  return state;
}

/** graph の episodes は実 API と同じく本文を含めない（`to_dict(include_body=False)`）。 */
function graphOf(state: StoryMockState) {
  return {
    episodes: state.episodes.map((episode) => ({
      id: episode.id,
      work_id: episode.work_id,
      title: episode.title,
      plot: episode.plot,
      summary: episode.summary,
      premise_note: episode.premise_note,
      status: episode.status,
      target_chars: episode.target_chars,
      char_count: episode.char_count,
      body_etag: episode.body_etag,
      current_rev_no: episode.current_rev_no,
      map_x: episode.map_x,
      map_y: episode.map_y,
      sort_hint: episode.sort_hint,
    })),
    links: state.links,
    start_episode_id: state.episodes[0]?.id ?? null,
  };
}

function overviewOf(state: StoryMockState) {
  const totalChars = state.episodes.reduce((total, episode) => total + episode.char_count, 0);
  const outgoing = new Map<string, number>();
  for (const link of state.links) outgoing.set(link.from_episode_id, (outgoing.get(link.from_episode_id) ?? 0) + 1);
  return {
    work: {
      id: "work-1",
      title: "U13 テスト作品",
      kind: "novel",
      status: "planning",
      synopsis: "分岐導線の E2E 作品",
      plot: "",
      style_guide: "",
      planned_episode_count: 3,
      target_episode_chars: 6000,
      model_override: {},
      resolved_model: "mock-model",
      model_layer: "writing",
      start_episode_id: state.episodes[0]?.id ?? null,
      ui_state: state.uiState,
      episode_count: state.episodes.length,
      total_chars: totalChars,
      notes_count: 0,
      characters_count: 0,
      rulebooks_count: 0,
      branch_count: [...outgoing.values()].filter((count) => count >= 2).length,
      updated_at: "2026-08-02T00:00:00Z",
    },
    graph: graphOf(state),
    current_route: [],
  };
}

async function installStoryApiMock(page: Page, state: StoryMockState) {
  await page.route("**/api/python-proxy/story/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace("/api/python-proxy", "/api");
    const method = request.method();
    const body = (request.postDataJSON() ?? {}) as Record<string, unknown>;
    const episodeById = (id: string) => state.episodes.find((item) => item.id === id);

    if (method === "GET" && path === "/api/story/works/work-1/overview") {
      await route.fulfill({ json: overviewOf(state) });
      return;
    }
    if (method === "PATCH" && path === "/api/story/works/work-1") {
      if (body.ui_state && typeof body.ui_state === "object") state.uiState = body.ui_state as Record<string, unknown>;
      await route.fulfill({ json: overviewOf(state).work });
      return;
    }
    if (method === "GET" && path === "/api/story/works/work-1/graph") {
      await route.fulfill({ json: graphOf(state) });
      return;
    }
    if (method === "GET" && path.endsWith("/revisions")) {
      await route.fulfill({ json: { items: [], limit: 50, offset: 0 } });
      return;
    }
    if (method === "GET" && /^\/api\/story\/episodes\/[^/]+$/.test(path)) {
      const episode = episodeById(path.split("/").at(-1) ?? "");
      await route.fulfill({ status: episode ? 200 : 404, json: episode ?? { detail: "not found" } });
      return;
    }
    if (method === "PUT" && path.endsWith("/body")) {
      const episode = episodeById(path.split("/").at(-2) ?? "");
      if (episode && typeof body.body === "string") {
        episode.body = body.body;
        episode.char_count = Array.from(body.body).length;
        episode.body_etag = `etag-${episode.id}-${episode.char_count}`;
        episode.current_rev_no += 1;
      }
      await route.fulfill({
        json: {
          id: episode?.id ?? "",
          body_etag: episode?.body_etag ?? "",
          char_count: episode?.char_count ?? 0,
          current_rev_no: episode?.current_rev_no ?? 1,
          revision: null,
          pre_revision: null,
        },
      });
      return;
    }
    if (method === "POST" && path === "/api/story/works/work-1/episodes") {
      state.createBodies.push(body);
      const parentId = typeof body.after_episode_id === "string" ? body.after_episode_id : null;
      const episode = makeEpisode(state, {
        title: typeof body.title === "string" && body.title ? body.title : "新しい分岐",
        plot: typeof body.plot === "string" ? body.plot : "",
        body: typeof body.body === "string" ? body.body : "",
        status: typeof body.status === "string" ? body.status : "unwritten",
      });
      state.episodes.push(episode);
      if (parentId) {
        const siblings = state.links.filter((link) => link.from_episode_id === parentId);
        addLink(state, {
          from_episode_id: parentId,
          to_episode_id: episode.id,
          choice_label: typeof body.choice_label === "string" ? body.choice_label : null,
          position: siblings.length,
          is_primary: siblings.length === 0,
        });
      }
      await route.fulfill({ json: episode });
      return;
    }
    if (method === "POST" && path === "/api/story/works/work-1/structure") {
      const ops = (Array.isArray(body.ops) ? body.ops : []) as Array<Record<string, unknown>>;
      state.structureOps.push(ops);
      const results = ops.map((operation) => {
        if (operation.op !== "duplicate_as_branch") return { op: String(operation.op) };
        const sourceId = typeof operation.episode_id === "string" ? operation.episode_id : "";
        const source = episodeById(sourceId);
        if (!source) return { op: "duplicate_as_branch" };
        const clone = makeEpisode(state, {
          title: typeof operation.new_title === "string" && operation.new_title ? operation.new_title : `${source.title}（別パターン）`,
          plot: source.plot,
          body: source.body,
          summary: source.summary,
          premise_note: source.premise_note,
          map_x: source.map_x + 80,
          map_y: source.map_y + 80,
          sort_hint: source.sort_hint + 0.5,
        });
        state.episodes.push(clone);
        const incoming = state.links.filter((link) => link.to_episode_id === source.id);
        const linkIds = incoming.map((parent) =>
          addLink(state, {
            from_episode_id: parent.from_episode_id,
            to_episode_id: clone.id,
            choice_label: typeof operation.choice_label === "string" ? operation.choice_label : null,
            position: parent.position + 0.5,
            is_primary: false,
          }).id,
        );
        return { op: "duplicate_as_branch", episode_id: clone.id, link_ids: linkIds, unplaced: incoming.length === 0 };
      });
      await route.fulfill({ json: { results, ...graphOf(state) } });
      return;
    }
    if (method === "POST" && path.endsWith("/split")) {
      state.splitBodies.push(body);
      const source = episodeById(path.split("/").at(-2) ?? "");
      const offset = typeof body.offset === "number" ? body.offset : 1;
      const sourceBody = source?.body ?? "";
      const created = makeEpisode(state, {
        title: typeof body.new_title === "string" ? body.new_title : "分割後半",
        plot: source?.plot ?? "",
        body: sourceBody.slice(offset),
        summary: source?.summary ?? "",
        premise_note: source?.premise_note ?? "",
      });
      state.episodes.push(created);
      if (source) {
        source.body = sourceBody.slice(0, offset);
        source.char_count = Array.from(source.body).length;
        source.body_etag = `etag-${source.id}-split`;
        // 実 API と同じく、元章の後続リンクは後半章へ付け替える。
        for (const link of state.links.filter((item) => item.from_episode_id === source.id)) link.from_episode_id = created.id;
        addLink(state, { from_episode_id: source.id, to_episode_id: created.id, choice_label: null, position: 0, is_primary: true });
      }
      await route.fulfill({
        json: {
          source: { id: source?.id ?? "", body_etag: source?.body_etag ?? "", char_count: source?.char_count ?? 0, current_rev_no: source?.current_rev_no ?? 1 },
          created: { id: created.id, body_etag: created.body_etag, char_count: created.char_count, current_rev_no: created.current_rev_no },
          links: { created: [], rewired: [] },
        },
      });
      return;
    }
    if (method === "POST" && path === "/api/story/works/work-1/write") {
      state.writeBodies.push(body);
      await route.fulfill({
        json: {
          id: "writing-1",
          work_id: "work-1",
          episode_id: typeof body.episode_id === "string" ? body.episode_id : null,
          conversation_session_id: typeof body.conversation_session_id === "string" ? body.conversation_session_id : null,
        },
      });
      return;
    }
    if (method === "GET" && path.startsWith("/api/story/writing-sessions/by-conversation/")) {
      const last = state.writeBodies.at(-1);
      await route.fulfill({
        json: last
          ? { id: "writing-1", work_id: "work-1", episode_id: last.episode_id ?? null, conversation_session_id: path.split("/").at(-1) }
          : null,
      });
      return;
    }
    await route.fulfill({ json: {} });
  });
}

async function openManuscript(page: Page, state: StoryMockState) {
  await addAuthCookie(page);
  await mockAuthenticatedApis(page);
  await installStoryApiMock(page, state);
  await page.goto("/scenarios/work-1/manuscript?episode=ep-1");
  await expect(page.getByTestId("story-manuscript")).toBeVisible();
  await expect(page.getByTestId("story-episode-row-ep-1")).toBeVisible();
}

async function openEpisodeMenu(page: Page, episodeId: string, item: string) {
  await page.getByTestId(`story-episode-row-${episodeId}`).getByRole("button", { name: "章のメニュー" }).click();
  await page.getByRole("menuitem", { name: item }).click();
}

async function submitBranchDialog(page: Page, title: string, choice: string) {
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("textbox").nth(0).fill(title);
  await dialog.getByRole("textbox").nth(1).fill(choice);
  await dialog.getByRole("button", { name: "作成", exact: true }).click();
  await expect(dialog).toBeHidden();
}

/** ルートバーの分岐スイッチで、指定章の続きを選び直す。 */
async function switchRouteBranch(page: Page, fromTitle: string, optionName: string) {
  await page.getByRole("combobox", { name: `${fromTitle} の分岐` }).click();
  await page.getByRole("option", { name: optionName }).click();
}

function episodeBody(state: StoryMockState, id: string): string {
  return state.episodes.find((episode) => episode.id === id)?.body ?? "";
}

test.describe("Scenario Studio U13 分岐・分割導線", () => {
  test("入口①: 第1章の「続きの分岐を追加」で第2章Bを白紙作成しても第2章Aは変わらない", async ({ page }) => {
    const state = createMockState();
    await openManuscript(page, state);

    await openEpisodeMenu(page, "ep-1", "続きの分岐を追加");
    await submitBranchDialog(page, "第二章B", "王を疑う");

    await expect.poll(() => state.createBodies.length).toBe(1);
    expect(state.createBodies[0]).toMatchObject({ title: "第二章B", choice_label: "王を疑う", after_episode_id: "ep-1" });

    // (b) 複製元ではない第2章Aは一切変更されない。
    expect(episodeBody(state, "ep-2")).toBe(CHAPTER_2A_BODY);
    expect(state.episodes.find((episode) => episode.id === "ep-2")?.title).toBe("第二章A");
    // 白紙で作られるので本文は空。第1章の続きとして繋がる。
    const branch = state.episodes.at(-1);
    expect(branch?.title).toBe("第二章B");
    expect(branch?.body).toBe("");
    expect(state.links.some((link) => link.from_episode_id === "ep-1" && link.to_episode_id === branch?.id)).toBe(true);

    // 第1章から 2 本の続きが生えたので、ルートバーに分岐スイッチが出る。
    await expect(page.getByTestId("story-route-bar").getByRole("combobox", { name: "第一章 の分岐" })).toBeVisible();
  });

  test("入口②: 第2章Aの「複製して分岐にする」→ 分岐スイッチ往復 → 通し読みでルートごとに内容が変わる", async ({ page }) => {
    const state = createMockState();
    await openManuscript(page, state);

    // (a) 入口② — 第2章Aを複製して第2章Bを作る。
    await openEpisodeMenu(page, "ep-2", "複製して分岐にする");
    await submitBranchDialog(page, "第二章B", "王を疑う");

    await expect.poll(() => state.structureOps.length).toBe(1);
    expect(state.structureOps[0][0]).toMatchObject({ op: "duplicate_as_branch", episode_id: "ep-2", choice_label: "王を疑う", new_title: "第二章B" });
    const branchId = state.episodes.at(-1)?.id ?? "";
    expect(branchId).not.toBe("");

    // 複製直後は複製元と同じ本文。複製先だけを書き換える。
    await expect(page.getByRole("textbox", { name: "章タイトル" })).toHaveValue("第二章B");
    const editor = page.getByTestId("story-manuscript-editor").locator(".cm-content");
    await editor.click();
    await page.keyboard.press("ControlOrMeta+a");
    await page.keyboard.type(CHAPTER_2B_BODY);
    await page.keyboard.press("ControlOrMeta+s");
    await expect.poll(() => episodeBody(state, branchId), { timeout: 10000 }).toBe(CHAPTER_2B_BODY);

    // (b) 複製先を書き換えても第2章Aは変わらない。
    expect(episodeBody(state, "ep-2")).toBe(CHAPTER_2A_BODY);

    // (c) 分岐スイッチで 2B へ。
    await switchRouteBranch(page, "第一章", "王を疑う");
    await expect(page.getByTestId(`story-episode-row-${branchId}`)).toBeVisible();
    await expect(page.getByTestId("story-episode-row-ep-2")).toBeHidden();

    // (d) 通し読みは選択中ルートの本文になる。
    await page.goto("/scenarios/work-1/review");
    await expect(page.getByTestId("story-review")).toBeVisible();
    await expect(page.getByTestId("story-review")).toContainText(CHAPTER_2B_BODY);
    await expect(page.getByTestId("story-review")).not.toContainText(CHAPTER_2A_BODY);

    // (c) 2A へ戻す。ラベル未設定の主ルートは接続先の章名が選択肢になる。
    await page.goto("/scenarios/work-1/manuscript?episode=ep-1");
    await expect(page.getByTestId("story-manuscript")).toBeVisible();
    await switchRouteBranch(page, "第一章", "第二章A");
    await expect(page.getByTestId("story-episode-row-ep-2")).toBeVisible();
    await expect(page.getByTestId(`story-episode-row-${branchId}`)).toBeHidden();

    // (d) 同じ通し読み画面がもう一方のルートを出す。
    await page.goto("/scenarios/work-1/review");
    await expect(page.getByTestId("story-review")).toBeVisible();
    await expect(page.getByTestId("story-review")).toContainText(CHAPTER_2A_BODY);
    await expect(page.getByTestId("story-review")).not.toContainText(CHAPTER_2B_BODY);
  });

  test("(e) カーソル位置で章を分割し、後半章を複製して分岐にできる", async ({ page }) => {
    const state = createMockState();
    await openManuscript(page, state);

    // 手順1: 2 行目（「第一章の後半。」）の先頭にカーソルを置いて分割する。
    // 本文エディタは折り返し表示（`EditorView.lineWrapping`）なので、ArrowDown は論理行ではなく
    // 表示行単位で動く。狭い幅では 1 行目が複数行に折り返され、ArrowDown + Home では論理行の
    // 先頭（オフセット 17）ではなく 1 行目の途中に留まってしまう。折り返しに依存せず論理行頭を
    // 指すため、2 行目の行頭を直接クリックする。
    const secondLine = page.getByTestId("story-manuscript-editor").locator(".cm-line").nth(1);
    await secondLine.click({ position: { x: 2, y: 4 } });
    const splitButton = page.getByRole("button", { name: "カーソル位置で章を分割" });
    await expect(splitButton).toBeEnabled();
    await splitButton.click();

    await expect.poll(() => state.splitBodies.length).toBe(1);
    expect(state.splitBodies[0]).toMatchObject({ new_title: "第一章（後半）" });
    // カーソルは 2 行目の先頭にあるので、分割位置は「第一章の後半」の開始オフセットになる。
    expect(state.splitBodies[0].offset).toBe(CHAPTER_1_BODY.indexOf("第一章の後半"));
    const splitId = state.episodes.at(-1)?.id ?? "";
    expect(episodeBody(state, splitId)).toContain("第一章の後半");
    expect(episodeBody(state, "ep-1")).not.toContain("第一章の後半");

    // 手順2: 切り出した後半章を、そのまま複製して分岐にする。
    await expect(page.getByTestId(`story-episode-row-${splitId}`)).toBeVisible();
    await openEpisodeMenu(page, splitId, "複製して分岐にする");
    await submitBranchDialog(page, "第一章（後半・別案）", "城門で引き返す");

    await expect.poll(() => state.structureOps.length).toBe(1);
    expect(state.structureOps[0][0]).toMatchObject({ op: "duplicate_as_branch", episode_id: splitId, choice_label: "城門で引き返す", new_title: "第一章（後半・別案）" });
    // 複製元の後半章は変更されない。
    expect(episodeBody(state, splitId)).toContain("第一章の後半");
    const duplicated = state.episodes.at(-1);
    expect(duplicated?.title).toBe("第一章（後半・別案）");
    expect(duplicated?.id).not.toBe(splitId);
  });
});

test.describe("Scenario Studio 作品シェル", () => {
  test("左レールのバッジは件数があるときだけ出る", async ({ page }) => {
    const state = createMockState();
    await openManuscript(page, state);

    // 章数 2 は出す。設定・資料 / 参加人数 / ルールブック / 分岐点は 0 なので出さない。
    await expect(page.getByTestId("story-nav-badge-manuscript")).toHaveText("2");
    await expect(page.getByTestId("story-nav-badge-settings")).toBeHidden();
    await expect(page.getByTestId("story-nav-badge-cast")).toBeHidden();
    await expect(page.getByTestId("story-nav-badge-rules")).toBeHidden();
    await expect(page.getByTestId("story-nav-badge-map")).toBeHidden();

    // 分岐を 1 つ増やして開き直すと、章数バッジと分岐点バッジが増える。
    await openEpisodeMenu(page, "ep-1", "続きの分岐を追加");
    await submitBranchDialog(page, "第二章B", "王を疑う");
    await page.goto("/scenarios/work-1/manuscript?episode=ep-1");
    await expect(page.getByTestId("story-manuscript")).toBeVisible();
    await expect(page.getByTestId("story-nav-badge-manuscript")).toHaveText("3");
    await expect(page.getByTestId("story-nav-badge-map")).toHaveText("1");
  });

  test("「チャットで執筆」で執筆セッションを作り、チャット側に対象章が出る", async ({ page }) => {
    const state = createMockState();
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);
    await page.route("**/api/python-proxy/characters", async (route) => {
      await route.fulfill({ json: { characters: ["aoi"], current: "aoi" } });
    });
    // シェルが付ける `[執筆]` タイトルのセッションとして復元する（チャット側の執筆判定条件）。
    await page.route("**/api/conversations/session-e2e/resume*", async (route) => {
      await route.fulfill({
        json: {
          session: {
            id: "session-e2e",
            user_id: "user-1",
            title: "[執筆] U13 テスト作品 / 第二章A",
            character_name: "aoi",
            message_count: 0,
            is_active: true,
            is_group_chat: false,
            group_character_names: [],
          },
          messages: [],
        },
      });
    });
    await installStoryApiMock(page, state);
    await page.goto("/scenarios/work-1/manuscript?episode=ep-2");
    await expect(page.getByTestId("story-manuscript")).toBeVisible();

    await page.getByTestId("story-start-chat-writing").click();

    await expect.poll(() => state.writeBodies.length).toBe(1);
    expect(state.writeBodies[0]).toMatchObject({ episode_id: "ep-2", conversation_session_id: "session-e2e" });
    await expect(page).toHaveURL(/\/chat\?s=session-e2e/);

    // §4.12: チャット側パネルに対象エピソード名と「スタジオで開く」リンクが出る。
    await expect(page.getByTestId("story-chat-authoring-workspace")).toBeVisible();
    await expect(page.getByTestId("story-chat-target-episode")).toContainText("第二章A");
    await expect(page.getByTestId("story-chat-open-studio")).toHaveAttribute("href", "/scenarios/work-1/manuscript?episode=ep-2");
  });
});
