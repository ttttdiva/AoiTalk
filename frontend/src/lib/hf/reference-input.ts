import type { RepoType } from "./client";

export type ParsedHfInput =
  | { kind: "token"; token: string }
  | { kind: "repo"; repoId: string; hintedType?: RepoType };

export function parseHfReferenceInput(raw: string): ParsedHfInput | null {
  const value = raw.trim();
  if (!value) return null;
  if (/^hf_[A-Za-z0-9]+$/.test(value)) return { kind: "token", token: value };

  let repoId = value;
  let hintedType: RepoType | undefined;
  try {
    const url = new URL(value);
    if (url.hostname !== "huggingface.co" && !url.hostname.endsWith(".huggingface.co")) {
      return null;
    }
    const parts = url.pathname.split("/").filter(Boolean);
    if (parts[0] === "datasets") {
      hintedType = "dataset";
      parts.shift();
    } else if (parts[0] === "models") {
      hintedType = "model";
      parts.shift();
    } else if (parts[0] === "spaces") {
      return null;
    }
    repoId = parts.slice(0, 2).join("/");
  } catch {
    // canonical owner/repository input
  }

  const match = repoId.match(/^([A-Za-z0-9][A-Za-z0-9._-]*)\/([A-Za-z0-9][A-Za-z0-9._-]*)$/);
  if (!match) return null;
  return { kind: "repo", repoId: `${match[1]}/${match[2]}`, hintedType };
}
