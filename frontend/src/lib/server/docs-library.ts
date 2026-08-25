/**
 * Canonical Docs Library boundary.
 *
 * Callers should use this module's `DocsLibrary` naming and `docs_library_id`
 * DTO rather than coupling new code to legacy workspace vocabulary.  The
 * physical Docs table is `docs_libraries`; legacy workspace keys are handled
 * only by the sync boundary, not emitted by web Docs routes.
 */
export {
  ensurePersonalDocsLibrary as ensureDocsLibrary,
  getPersonalDocsLibrary as getDocsLibrary,
  type DocsLibrary,
  type DocsLibraryId,
} from "./project-information-hierarchy";

import type { DocsLibrary } from "./project-information-hierarchy";

export function serializeDocsLibrary(library: DocsLibrary | null | undefined) {
  if (!library) return null;
  return {
    id: library.id,
    library_id: library.id,
    docs_library_id: library.id,
    name: library.name,
    description: library.description,
    owner_user_id: library.ownerUserId,
    library_type: library.libraryType ?? "personal",
    settings: library.settingsJson ?? {},
    created_at: library.createdAt instanceof Date ? library.createdAt.toISOString() : library.createdAt,
    updated_at: library.updatedAt instanceof Date ? library.updatedAt.toISOString() : library.updatedAt,
  };
}
