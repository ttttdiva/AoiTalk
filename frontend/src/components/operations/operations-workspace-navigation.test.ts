import { describe, expect, it } from "vitest";
import { operationsSectionFromQuery } from "./operations-workspace-navigation";

describe("Operations entry compatibility", () => {
  it("opens persona production on bare entry and preserves saved graph links", () => {
    expect(operationsSectionFromQuery(new URLSearchParams())).toBe("studio");
    for (const key of ["graph_node", "graph_work", "execution_focus", "execution_node", "work_view"]) {
      expect(operationsSectionFromQuery(new URLSearchParams({ [key]: "fixture" }))).toBe("overview");
    }
    expect(operationsSectionFromQuery(new URLSearchParams("tab=studio&graph_work=fixture"))).toBe("studio");
    for (const tab of ["overview", "agents", "work", "activity", "connections", "opportunities", "actions", "personas"] as const) {
      expect(operationsSectionFromQuery(new URLSearchParams({ tab }))).toBe(tab);
    }
  });
});
