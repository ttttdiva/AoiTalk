import { describe, expect, it, vi } from "vitest";
import { emptyGraph, type TeamGraph } from "./agent-team-graph-api";
import { boundedGraphRead, classifyGraphReadback } from "./graph-update-recovery";
const base: TeamGraph = { id: "g", title: "base", project_id: null, revision_id: "r", version: 1, content_hash: "h", definition: emptyGraph(), diagnostics: [] };
describe("graph update readback", () => {
  it("rejects sparse arrays even when both have the same holes", () => {
    const definition = { ...emptyGraph(), nodes: new Array(1) };
    expect(classifyGraphReadback(base, { title: "new", project_id: null, definition }, { ...base, title: "new", version: 2, definition })).toBe("conflict");
  });
  it("recognizes null normalization without accepting a different media kind", () => {
    const group = { key: "g", title: "Group", purpose: "", node_keys: [], leader_key: null };
    const intended = { ...emptyGraph(), groups: [{ ...group, execution_kind: null }] };
    const observed = { ...base, title: "new", version: 2, definition: { ...emptyGraph(), groups: [group] } };
    expect(classifyGraphReadback(base, { title: "new", project_id: null, definition: intended }, observed)).toBe("confirmed");
    expect(classifyGraphReadback(base, { title: "new", project_id: null, definition: { ...emptyGraph(), groups: [{ ...group, execution_kind: "media_image" }] } }, observed)).toBe("conflict");
  });
  it("bounds a stalled read", async () => {
    vi.useFakeTimers();
    try {
      const read = boundedGraphRead(() => new Promise(() => {}), 50);
      const assertion = expect(read).rejects.toThrow("タイムアウト");
      await vi.advanceTimersByTimeAsync(50);
      await assertion;
    } finally { vi.useRealTimers(); }
  });
});
