/**
 * Canonical Docs Library table boundary.
 *
 * The physical table and Drizzle columns are canonical (`docs_libraries`,
 * `docsLibraryId`, `libraryType`).  A few rolling test/sync adapters still
 * expose the historical `knowledgeWorkspaces` export, so the compatibility
 * alias is kept in this single server boundary rather than in route logic.
 */
import { docsLibraries } from "@/db/schema";

export { docsLibraries };
