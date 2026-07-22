export function plainDocsTitle(value: string | null | undefined): string {
  return String(value ?? "")
    .replace(/\[\[user:[^|\]]+\|([^\]]+)\]\]/g, "$1")
    .replace(/\[\[node:[^|\]]+\|([^\]]+)\]\]/g, "$1")
    .replace(/\[\[([^\]|]+)\]\]/g, "$1")
    .replace(/\[([^\]]+)\]\((?:https?:\/\/|mailto:)[^)]+\)/g, "$1")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/==([^=]+)==/g, "$1")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\s+/g, " ")
    .trim();
}
