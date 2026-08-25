export type ProjectPermissionMap = Record<string, unknown>;

export const PROJECT_PERMISSIONS = [
  "read",
  "write",
  "delete",
  "manage_members",
  "manage_settings",
] as const;

export type ProjectPermission = (typeof PROJECT_PERMISSIONS)[number];
export type ProjectMemberRole = "owner" | "admin" | "member" | "viewer";

const DEFAULT_PROJECT_PERMISSIONS: Record<string, ProjectPermissionMap> = {
  owner: {
    read: true,
    write: true,
    delete: true,
    manage_members: true,
    manage_settings: true,
  },
  admin: {
    read: true,
    write: true,
    delete: true,
    manage_members: true,
    manage_settings: true,
  },
  member: {
    read: true,
    write: false,
    delete: false,
    manage_members: false,
    manage_settings: false,
  },
  viewer: {
    read: true,
    write: false,
    delete: false,
    manage_members: false,
    manage_settings: false,
  },
};

export function getDefaultProjectPermissions(role: unknown): ProjectPermissionMap {
  const normalizedRole = typeof role === "string" ? role.trim().toLowerCase() : "";
  const defaults = DEFAULT_PROJECT_PERMISSIONS[normalizedRole];
  if (!defaults) {
    throw new TypeError("Unsupported project member role");
  }
  return { ...defaults };
}

export function normalizeProjectPermissions(value: unknown): ProjectPermissionMap {
  let candidate: unknown = value;
  if (typeof value === "string") {
    try {
      candidate = JSON.parse(value);
    } catch {
      // Invalid persisted permission data is deny-all.
      return {};
    }
  }

  if (
    !candidate ||
    typeof candidate !== "object" ||
    Array.isArray(candidate)
  ) {
    return {};
  }

  // Permission rows are persisted as JSON and may have been written by an
  // older or external service.  Treat any unknown key or non-boolean value as
  // malformed rather than allowing a partially trusted object to grant access.
  for (const key of Reflect.ownKeys(candidate)) {
    if (
      typeof key !== "string" ||
      !(PROJECT_PERMISSIONS as readonly string[]).includes(key) ||
      typeof (candidate as Record<string, unknown>)[key] !== "boolean"
    ) {
      return {};
    }
  }

  return Object.fromEntries(
    Object.entries(candidate as Record<string, unknown>),
  );
}

export function hasProjectPermission(
  value: unknown,
  permission: ProjectPermission,
): boolean {
  return normalizeProjectPermissions(value)[permission] === true;
}

export function hasEffectiveProjectPermission(input: {
  userId: string;
  userRole?: string | null;
  projectOwnerId: string;
  memberPermissions: unknown;
  permission: ProjectPermission;
}): boolean {
  if (input.userRole === "admin" || input.userId === input.projectOwnerId) {
    return true;
  }
  return hasProjectPermission(input.memberPermissions, input.permission);
}
