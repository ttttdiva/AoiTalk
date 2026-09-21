import type {
  ExplorerBookmark,
  ExplorerBookmarkScope,
} from "@/lib/explorer-api";
import { isExplorerBookmarkFolder } from "@/lib/explorer-bookmark-tree";

/**
 * The durable owner of a Files bookmark.  This intentionally mirrors the
 * explicit API scope rather than deriving an endpoint from the target path.
 */
export type ExplorerBookmarkOwner =
  { scope: "personal" } | { scope: "shared"; spaceId: string };

export type ExplorerBookmarkOwnerKey = "personal:" | `shared:${string}`;

/** A bookmark with endpoint provenance attached by the caller that fetched it. */
export type OwnedExplorerBookmark = ExplorerBookmark & {
  owner: ExplorerBookmarkOwner;
};

export type BookmarkOwnerLike =
  | ExplorerBookmarkOwner
  | ExplorerBookmarkScope
  | { type: "personal" }
  | { type: "shared"; spaceId: string };

const PERSONAL_OWNER: ExplorerBookmarkOwner = { scope: "personal" };

function cleanSpaceId(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const id = value.trim();
  return id || null;
}

function ownerFromUnknown(value: unknown): ExplorerBookmarkOwner | null {
  if (!value || typeof value !== "object") return null;
  const candidate = value as Record<string, unknown>;
  const scope = candidate.scope ?? candidate.type;
  if (scope === "personal") return { ...PERSONAL_OWNER };
  if (scope !== "shared") return null;
  const spaceId = cleanSpaceId(candidate.spaceId ?? candidate.space_id);
  return spaceId ? { scope: "shared", spaceId } : null;
}

/** Normalize an API scope to a fresh owner value. */
export function bookmarkOwnerFromScope(
  scope?: ExplorerBookmarkScope | BookmarkOwnerLike | null,
): ExplorerBookmarkOwner {
  if (scope == null) return { ...PERSONAL_OWNER };
  const owner = ownerFromUnknown(scope);
  if (!owner) {
    throw new TypeError("Invalid bookmark owner scope");
  }
  return owner;
}

/** Return the stable key used by cache/request scope identities. */
export function bookmarkOwnerKey(
  owner?: ExplorerBookmarkOwner | BookmarkOwnerLike | null,
): ExplorerBookmarkOwnerKey {
  const normalized = bookmarkOwnerFromScope(owner);
  return normalized.scope === "shared"
    ? `shared:${normalized.spaceId}`
    : "personal:";
}

/** Parse a scope identity such as `shared:<space id>` or `personal:`. */
export function bookmarkOwnerFromKey(
  key: string | ExplorerBookmarkOwner | BookmarkOwnerLike | null | undefined,
): ExplorerBookmarkOwner | null {
  if (typeof key !== "string") {
    if (key == null) return null;
    return ownerFromUnknown(key);
  }
  const value = key.trim();
  if (value === "personal" || value === "personal:") {
    return { ...PERSONAL_OWNER };
  }
  if (!value.toLowerCase().startsWith("shared:")) return null;
  const spaceId = cleanSpaceId(value.slice("shared:".length));
  return spaceId ? { scope: "shared", spaceId } : null;
}

/** Descriptive alias for callers handling a cached scope identity. */
export const bookmarkOwnerFromScopeKey = bookmarkOwnerFromKey;

/**
 * Resolve a row's owner without ever guessing a Space from `_projects/...`.
 * A fetched personal/shared collection can pass `fallback` for old API rows
 * which predate the `space_id` projection.
 */
export function bookmarkOwnerFromItem(
  item: ExplorerBookmark & {
    owner?: unknown;
    space_id?: string | null;
  },
  fallback?: ExplorerBookmarkOwner | BookmarkOwnerLike | null,
): ExplorerBookmarkOwner {
  const explicit = ownerFromUnknown(item.owner);
  if (explicit) return explicit;
  if (
    Object.prototype.hasOwnProperty.call(item, "user_id") &&
    cleanSpaceId((item as ExplorerBookmark & { user_id?: unknown }).user_id)
  ) {
    return { ...PERSONAL_OWNER };
  }
  // A server-projected `space_id: null` is an explicit personal owner.  Only
  // truly legacy rows that omit the field may inherit the fetched collection's
  // fallback owner.
  if (Object.prototype.hasOwnProperty.call(item, "space_id")) {
    const spaceId = cleanSpaceId(item.space_id);
    return spaceId ? { scope: "shared", spaceId } : { ...PERSONAL_OWNER };
  }
  if (fallback != null) return bookmarkOwnerFromScope(fallback);
  return { ...PERSONAL_OWNER };
}

/** Attach immutable endpoint provenance to a bookmark projection. */
export function withBookmarkOwner(
  item: ExplorerBookmark,
  owner: ExplorerBookmarkOwner | BookmarkOwnerLike,
): OwnedExplorerBookmark {
  return { ...item, owner: bookmarkOwnerFromScope(owner) };
}

/** Alias kept descriptive for sidebar callers. */
export const attachBookmarkOwner = withBookmarkOwner;

/** Compare owners by scope and (for shared rows) exact Space identity. */
export function sameBookmarkOwner(
  left:
    | ExplorerBookmarkOwner
    | BookmarkOwnerLike
    | ExplorerBookmark
    | null
    | undefined,
  right:
    | ExplorerBookmarkOwner
    | BookmarkOwnerLike
    | ExplorerBookmark
    | null
    | undefined,
): boolean {
  const isItem = (value: unknown): value is ExplorerBookmark =>
    Boolean(value && typeof value === "object" && "path" in value);
  const leftOwner = isItem(left)
    ? bookmarkOwnerFromItem(left as ExplorerBookmark & { owner?: unknown })
    : ownerFromUnknown(left);
  const rightOwner = isItem(right)
    ? bookmarkOwnerFromItem(right as ExplorerBookmark & { owner?: unknown })
    : ownerFromUnknown(right);
  if (!leftOwner || !rightOwner || leftOwner.scope !== rightOwner.scope) {
    return false;
  }
  if (leftOwner.scope === "personal") return true;
  return (
    rightOwner.scope === "shared" && leftOwner.spaceId === rightOwner.spaceId
  );
}

/**
 * Check a parent relation without crossing endpoint ownership boundaries.
 * This is deliberately pure; the backend remains authoritative for writes.
 */
export function isSameOwnerBookmarkParent(
  child: ExplorerBookmark & { owner?: unknown; space_id?: string | null },
  parent: ExplorerBookmark & { owner?: unknown; space_id?: string | null },
  fallbackOwner?: ExplorerBookmarkOwner | BookmarkOwnerLike | null,
): boolean {
  return Boolean(
    child.id &&
    parent.id &&
    child.parent_id === parent.id &&
    sameBookmarkOwner(
      bookmarkOwnerFromItem(child, fallbackOwner),
      bookmarkOwnerFromItem(parent, fallbackOwner),
    ),
  );
}

/** Alias suitable for move/drop guards. */
export const hasSameOwnerParent = isSameOwnerBookmarkParent;
export const isBookmarkParentSameOwner = isSameOwnerBookmarkParent;

function normalizePath(path: string): string {
  return path.replace(/\\/g, "/");
}

function trimVirtualPath(path: string): string {
  return normalizePath(path).replace(/^\/+|\/+$/g, "");
}

/** Absolute local/UNC path detection, without resolving or touching disk. */
function isAbsolutePathText(path: string): boolean {
  return /^[A-Za-z]:[\\/]/.test(path) || /^[/\\]{1,2}/.test(path);
}

export function isExplorerAbsolutePath(path: unknown): path is string {
  return typeof path === "string" && isAbsolutePathText(path);
}

export const isAbsoluteExplorerPath = isExplorerAbsolutePath;

export type CanonicalProjectBookmarkPath = {
  projectId: string;
  relativePath: string;
};

/** Parse only the canonical `_projects/project_<id>` namespace. */
export function parseCanonicalProjectBookmarkPath(
  path: unknown,
): CanonicalProjectBookmarkPath | null {
  if (typeof path !== "string" || isAbsolutePathText(path)) return null;
  const normalized = trimVirtualPath(path);
  const parts = normalized.split("/");
  if (parts.length < 2 || parts[0].toLowerCase() !== "_projects") return null;
  const segment = parts[1];
  if (
    !segment ||
    segment.length <= "project_".length ||
    segment.slice(0, "project_".length).toLowerCase() !== "project_"
  ) {
    return null;
  }
  const projectId = segment.slice("project_".length).trim();
  if (!projectId || projectId.includes("/") || projectId.includes("\\"))
    return null;
  return {
    projectId,
    relativePath: parts.slice(2).join("/"),
  };
}

function parseRecordTableProjectId(path: unknown): string | null {
  if (typeof path !== "string") return null;
  const raw = path;
  if (isAbsolutePathText(raw)) return null;
  const match = raw.trim().match(/^aoitalk-record-table:([^:]+):[^:]+$/i);
  return match?.[1]?.trim() || null;
}

function toIdSet(values: Iterable<string> | undefined): Set<string> {
  if (!values) return new Set();
  const ids = new Set<string>();
  for (const value of values) {
    if (typeof value === "string" && value.trim()) ids.add(value.trim());
  }
  return ids;
}

export type ExplorerBookmarkTargetContext = {
  /** Current persistence scope; target ownership is still classified independently. */
  owner?: ExplorerBookmarkOwner | BookmarkOwnerLike | null;
  scope?: ExplorerBookmarkOwner | BookmarkOwnerLike | null;
  bookmarkScope?: ExplorerBookmarkOwner | BookmarkOwnerLike | null;
  currentOwner?: ExplorerBookmarkOwner | BookmarkOwnerLike | null;
  selectedSpaceId?: string | null;
  spaceId?: string | null;
  selectedProjectId?: string | null;
  currentProjectId?: string | null;
  filerTab?: "workspace" | "user" | "hf" | "hydrus" | string;
  scopeRoot?: string | null;
  spaceProjectIds?: Iterable<string>;
  sameSpaceProjectIds?: Iterable<string>;
  projectIds?: Iterable<string>;
};

type NormalizedTargetContext = {
  owner: ExplorerBookmarkOwner;
  spaceId: string | null;
  selectedProjectId: string | null;
  projectIds: Set<string>;
};

function ownerFromContextValue(
  value: ExplorerBookmarkTargetContext | ExplorerBookmarkProjectionContext,
): ExplorerBookmarkOwner {
  const candidate = value as Record<string, unknown>;
  const explicit =
    candidate.owner ?? candidate.bookmarkScope ?? candidate.currentOwner;
  if (explicit != null)
    return bookmarkOwnerFromScope(explicit as BookmarkOwnerLike);
  // Accept both `{ scope: { scope: "shared", spaceId } }` and the practical
  // shorthand `{ scope: "shared", spaceId }` used by lightweight callers.
  if (candidate.scope && typeof candidate.scope === "object") {
    return bookmarkOwnerFromScope(candidate.scope as BookmarkOwnerLike);
  }
  if (candidate.scope === "personal") return { ...PERSONAL_OWNER };
  if (candidate.scope === "shared") {
    const spaceId = cleanSpaceId(candidate.spaceId);
    if (!spaceId) throw new TypeError("Invalid bookmark owner scope");
    return bookmarkOwnerFromScope({
      scope: "shared",
      spaceId,
    });
  }
  return { ...PERSONAL_OWNER };
}

function normalizeTargetContext(
  context?:
    | ExplorerBookmarkTargetContext
    | ExplorerBookmarkOwner
    | BookmarkOwnerLike
    | null,
): NormalizedTargetContext {
  const candidate = context as Record<string, unknown> | null | undefined;
  const hasContextFields = Boolean(
    candidate &&
    ("selectedSpaceId" in candidate ||
      "spaceProjectIds" in candidate ||
      "sameSpaceProjectIds" in candidate ||
      "projectIds" in candidate ||
      "selectedProjectId" in candidate ||
      "currentProjectId" in candidate ||
      "owner" in candidate ||
      "bookmarkScope" in candidate ||
      "currentOwner" in candidate),
  );
  const directOwner = Boolean(
    candidate &&
    !hasContextFields &&
    (candidate.scope === "personal" ||
      candidate.type === "personal" ||
      (candidate.scope === "shared" && "spaceId" in candidate) ||
      (candidate.type === "shared" && "spaceId" in candidate)),
  );
  const value =
    context && typeof context === "object" && !directOwner
      ? (context as ExplorerBookmarkTargetContext)
      : { owner: context as ExplorerBookmarkOwner | null | undefined };
  const owner = ownerFromContextValue(value);
  const ownerSpace = owner.scope === "shared" ? owner.spaceId : null;
  const spaceId =
    cleanSpaceId(value.selectedSpaceId ?? value.spaceId) ?? ownerSpace;
  const selectedProjectId = cleanSpaceId(
    value.selectedProjectId ?? value.currentProjectId,
  );
  const ids = new Set<string>();
  for (const source of [
    value.spaceProjectIds,
    value.sameSpaceProjectIds,
    value.projectIds,
  ]) {
    for (const id of toIdSet(source)) ids.add(id);
  }
  return { owner, spaceId, selectedProjectId, projectIds: ids };
}

export type ExplorerBookmarkTargetClassification =
  | {
      kind: "personal";
      status: "personal";
      owner: { scope: "personal" };
      path: string;
      reason: "absolute" | "user" | "provider";
    }
  | {
      kind: "shared";
      status: "shared";
      owner: { scope: "shared"; spaceId: string };
      path: string;
      projectId: string;
      reason: "same-space-project";
    }
  | {
      kind: "rejected";
      status: "rejected";
      owner: null;
      path: string;
      reason: "empty" | "cross-space" | "ambiguous" | "unsupported";
      projectId?: string;
    };

/**
 * Classify a target before selecting an API endpoint.  Filesystem
 * authorization is intentionally not performed here; the backend validates
 * the actual target.  The helper only prevents a target from being persisted
 * into the wrong durable collection.
 */
export function classifyBookmarkTarget(
  path: unknown,
  context?:
    | ExplorerBookmarkTargetContext
    | ExplorerBookmarkOwner
    | BookmarkOwnerLike
    | null,
): ExplorerBookmarkTargetClassification {
  const raw = typeof path === "string" ? path : "";
  const trimmed = raw.trim();
  if (!trimmed) {
    return {
      kind: "rejected",
      status: "rejected",
      owner: null,
      path: raw,
      reason: "empty",
    };
  }
  if (isExplorerBookmarkFolder({ name: "", path: trimmed })) {
    return {
      kind: "rejected",
      status: "rejected",
      owner: null,
      path: raw,
      reason: "unsupported",
    };
  }
  if (isExplorerAbsolutePath(trimmed)) {
    return {
      kind: "personal",
      status: "personal",
      owner: { scope: "personal" },
      path: raw,
      reason: "absolute",
    };
  }

  const normalized = trimVirtualPath(trimmed);
  const targetContext = normalizeTargetContext(context);
  const project = parseCanonicalProjectBookmarkPath(trimmed);
  const recordProjectId = parseRecordTableProjectId(trimmed);
  const projectId = project?.projectId ?? recordProjectId;
  if (projectId) {
    const projectKnown =
      targetContext.projectIds.size > 0
        ? targetContext.projectIds.has(projectId)
        : targetContext.selectedProjectId === projectId;
    if (!targetContext.spaceId) {
      return {
        kind: "rejected",
        status: "rejected",
        owner: null,
        path: raw,
        reason: "ambiguous",
        projectId,
      };
    }
    if (!projectKnown) {
      return {
        kind: "rejected",
        status: "rejected",
        owner: null,
        path: raw,
        reason: targetContext.projectIds.size > 0 ? "cross-space" : "ambiguous",
        projectId,
      };
    }
    return {
      kind: "shared",
      status: "shared",
      owner: { scope: "shared", spaceId: targetContext.spaceId },
      path: raw,
      projectId,
      reason: "same-space-project",
    };
  }

  if (
    normalized === "hydrus" ||
    normalized.toLowerCase().startsWith("hydrus/") ||
    normalized.toLowerCase().startsWith("hydrus|")
  ) {
    return {
      kind: "rejected",
      status: "rejected",
      owner: null,
      path: raw,
      reason: "unsupported",
    };
  }
  if (normalized.toLowerCase().startsWith("remote://")) {
    return {
      kind: "rejected",
      status: "rejected",
      owner: null,
      path: raw,
      reason: "unsupported",
    };
  }
  if (normalized === "hf" || normalized.toLowerCase().startsWith("hf|")) {
    return {
      kind: "personal",
      status: "personal",
      owner: { scope: "personal" },
      path: raw,
      reason: "provider",
    };
  }
  if (
    normalized.toLowerCase() === "_users" ||
    normalized.toLowerCase().startsWith("_users/")
  ) {
    return {
      kind: "personal",
      status: "personal",
      owner: { scope: "personal" },
      path: raw,
      reason: "user",
    };
  }

  return {
    kind: "rejected",
    status: "rejected",
    owner: null,
    path: raw,
    reason: "ambiguous",
  };
}

export const classifyExplorerBookmarkTarget = classifyBookmarkTarget;

type ProjectionOwnerItem = ExplorerBookmark & {
  owner?: unknown;
  space_id?: string | null;
};

export type ExplorerBookmarkProjectionContext =
  ExplorerBookmarkTargetContext & {
    scopeRoot?: string | null;
    filerTab?: "workspace" | "user" | "hf" | "hydrus" | string;
    includePersonalAbsolute?: boolean;
    includeProviderPersonal?: boolean;
    includeEmptyFolders?: boolean;
  };

function pathWithinRoot(path: string, root: string): boolean {
  if (!path || !root) return false;
  const normalizedPath = normalizePath(path).replace(/\/+$/g, "");
  const normalizedRoot = normalizePath(root).replace(/\/+$/g, "");
  return (
    normalizedPath === normalizedRoot ||
    normalizedPath.startsWith(`${normalizedRoot}/`)
  );
}

function directPersonalProjectionEligible(
  item: ProjectionOwnerItem,
  context: ExplorerBookmarkProjectionContext,
): boolean {
  const path = item.path?.trim() ?? "";
  const classification = classifyBookmarkTarget(path, context);
  if (classification.kind !== "personal") return false;
  if (classification.reason === "absolute") {
    // Absolute targets are local Files entries.  They may be projected into
    // Workspace/User views (including while a shared Project is selected), but
    // never into provider-specific virtual trees such as HF.
    return context.includePersonalAbsolute !== false &&
      (!context.filerTab || context.filerTab === "workspace" || context.filerTab === "user");
  }
  if (classification.reason === "provider") {
    return (
      context.includeProviderPersonal === true || context.filerTab === "hf"
    );
  }
  if (classification.reason === "user") {
    if (context.filerTab && context.filerTab !== "user") return false;
    return context.scopeRoot ? pathWithinRoot(path, context.scopeRoot) : true;
  }
  return false;
}

function directSharedProjectionEligible(
  item: ProjectionOwnerItem,
  owner: ExplorerBookmarkOwner,
  context: ExplorerBookmarkProjectionContext,
): boolean {
  if (owner.scope !== "shared") return false;
  const itemOwner = bookmarkOwnerFromItem(item, owner);
  if (!sameBookmarkOwner(itemOwner, owner)) return false;
  const classification = classifyBookmarkTarget(item.path, {
    ...context,
    owner,
  });
  return (
    classification.kind === "shared" &&
    sameBookmarkOwner(classification.owner, owner)
  );
}

/**
 * Return a scope-safe sidebar projection.  Personal absolute bookmarks are
 * intentionally retained while a shared Project is active; User/HF/remote
 * rows are not.  Ancestor folders are added only when their owner matches the
 * descendant owner, preventing cross-collection `parent_id` relationships.
 */
export function projectExplorerBookmarks(
  bookmarks: readonly ProjectionOwnerItem[],
  context:
    | ExplorerBookmarkProjectionContext
    | ExplorerBookmarkOwner
    | BookmarkOwnerLike = {},
): ExplorerBookmark[] {
  const candidate = context as Record<string, unknown> | null | undefined;
  const directOwner = Boolean(
    candidate &&
    (candidate.scope === "personal" ||
      candidate.type === "personal" ||
      (candidate.scope === "shared" && "spaceId" in candidate) ||
      (candidate.type === "shared" && "spaceId" in candidate)) &&
    !(
      "scopeRoot" in candidate ||
      "filerTab" in candidate ||
      "spaceProjectIds" in candidate ||
      "selectedSpaceId" in candidate ||
      "owner" in candidate ||
      "bookmarkScope" in candidate
    ),
  );
  const options: ExplorerBookmarkProjectionContext =
    context && typeof context === "object" && !directOwner
      ? (context as ExplorerBookmarkProjectionContext)
      : { owner: context as ExplorerBookmarkOwner };
  const activeOwner = ownerFromContextValue(options);
  const directIds = new Set<string>();
  const visible = new Set<ProjectionOwnerItem>();
  for (const item of bookmarks) {
    const itemOwner = bookmarkOwnerFromItem(item, activeOwner);
    if (isExplorerBookmarkFolder(item)) {
      continue;
    }
    const isPersonal = itemOwner.scope === "personal";
    const eligible = isPersonal
      ? activeOwner.scope === "shared"
        ? directPersonalProjectionEligible(item, options)
        : directPersonalProjectionEligible(item, options)
      : directSharedProjectionEligible(item, activeOwner, options);
    if (eligible) {
      visible.add(item);
      if (item.id) directIds.add(item.id);
    }
  }

  const byId = new Map<string, ProjectionOwnerItem>();
  for (const item of bookmarks) if (item.id) byId.set(item.id, item);
  const includedIds = new Set(directIds);
  for (const id of directIds) {
    let child = byId.get(id);
    const childOwner = child ? bookmarkOwnerFromItem(child, activeOwner) : null;
    const visited = new Set<string>();
    while (child?.parent_id && !visited.has(child.parent_id)) {
      visited.add(child.parent_id);
      const parent = byId.get(child.parent_id);
      if (!parent || !isExplorerBookmarkFolder(parent)) break;
      const parentOwner = bookmarkOwnerFromItem(parent, activeOwner);
      if (!childOwner || !sameBookmarkOwner(parentOwner, childOwner)) break;
      includedIds.add(parent.id!);
      child = parent;
    }
  }

  for (const item of bookmarks) {
    if (!isExplorerBookmarkFolder(item) || !item.id) continue;
    if (includedIds.has(item.id)) visible.add(item);
    else if (
      options.includeEmptyFolders &&
      sameBookmarkOwner(bookmarkOwnerFromItem(item, activeOwner), activeOwner)
    ) {
      visible.add(item);
    }
  }
  return bookmarks.filter((item) => visible.has(item));
}

/** Common aliases used by sidebar integrations. */
export const filterExplorerBookmarks = projectExplorerBookmarks;
export const projectBookmarksForSidebar = projectExplorerBookmarks;
export const projectExplorerBookmarkTree = projectExplorerBookmarks;

/** Keep only rows whose endpoint provenance matches `owner`. */
export function filterBookmarksByOwner(
  bookmarks: readonly ProjectionOwnerItem[],
  owner: ExplorerBookmarkOwner | BookmarkOwnerLike,
): ExplorerBookmark[] {
  const normalized = bookmarkOwnerFromScope(owner);
  return bookmarks.filter((item) =>
    sameBookmarkOwner(bookmarkOwnerFromItem(item, normalized), normalized),
  );
}
