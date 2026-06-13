export interface Snippet {
  prefix: string;
  body: string;
  description?: string;
}

export async function getSnippets(): Promise<Snippet[]> {
  try {
    const res = await fetch("/api/users/me/settings", {
      credentials: "include",
    });
    if (!res.ok) return [];
    const data = await res.json();
    return data.settings?.snippets || [];
  } catch {
    return [];
  }
}

export async function saveSnippets(snippets: Snippet[]): Promise<void> {
  await fetch("/api/users/me/settings", {
    method: "PATCH",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ snippets }),
  });
}
