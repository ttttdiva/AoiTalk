import { describe, expect, it } from "vitest";
import type { PersonaWorkspaceScope } from "./persona-workspace-model";
import {
  emptyRecipeDraft,
  parseRegistrationAttempt,
  recipeRegistration,
  recipeRegistrationMatches,
  registrationScopeKey,
  workspaceRegistration,
  workspaceRegistrationMatches,
} from "./media-generation-registration";

const scope: PersonaWorkspaceScope = {
  ownerUserId: "owner",
  projectId: "project",
  personaId: "persona",
  personaRevisionId: "revision",
  displayName: "Fixture",
};
const key = "media-registration:11111111-1111-4111-8111-111111111111";
const workspace = () =>
  workspaceRegistration(
    scope,
    "https://studio.example.invalid/",
    "wsp_fixture01",
    "prj_fixture01",
  );
const recipe = () =>
  recipeRegistration(scope, {
    ...emptyRecipeDraft(),
    name: "Fixture",
    prompt: "Original illustration",
    modelId: "ims_fixture01",
  });

describe("Generation registration closed snapshots", () => {
  it("pins authority and canonicalizes a credential-free origin", () => {
    expect(workspace()).toMatchObject({
      project_id: scope.projectId,
      provider: "comfyui_workbench",
      base_url: "https://studio.example.invalid",
    });
    for (const origin of [
      "https://u:secret@studio.example.invalid",
      "https://studio.example.invalid/path",
      "https://studio.example.invalid?token=x",
      "file:///tmp",
      "https://studio.example.invalid/#x",
    ]) {
      expect(() =>
        workspaceRegistration(scope, origin, "wsp_fixture01", ""),
      ).toThrow();
    }
    expect(() =>
      workspaceRegistration(
        scope,
        "https://studio.example.invalid",
        "wsp_short",
        "",
      ),
    ).toThrow();
  });

  it("validates bounds and requires the video model contract", () => {
    expect(recipe()).toMatchObject({
      persona_id: scope.personaId,
      persona_revision_id: scope.personaRevisionId,
      project_id: scope.projectId,
      cost_policy: { metered: false, requires_metered_ack: false },
      human_review_policy: { required: true },
    });
    for (const invalid of [
      { candidateCount: "21" },
      { width: "1.5" },
      { height: "16385" },
      { aspectRatio: "0:1" },
      { prompt: "x".repeat(8001) },
      { recipeType: "video" },
      { recipeType: "video", modelId: "ims_fixture01" },
      {
        references: Array.from({ length: 21 }, (_, n) => `ast_${n}`).join(" "),
      },
    ]) {
      expect(() =>
        recipeRegistration(scope, {
          ...emptyRecipeDraft(),
          name: "Fixture",
          prompt: "Original",
          ...invalid,
        }),
      ).toThrow();
    }
    const video = recipeRegistration(scope, {
      ...emptyRecipeDraft(),
      name: "Fixture",
      prompt: "Original",
      recipeType: "video",
      modelId: "wsl_fixture01",
      duration: "6",
      frames: "144",
      storyboard: "scene one\nscene two",
    });
    expect(video).toMatchObject({
      duration_seconds: 6,
      frame_count: 144,
      storyboard: ["scene one", "scene two"],
    });
  });

  it("restores only exact, validated requests in the same authority tuple", () => {
    expect(parseRegistrationAttempt(null, scope)).toBeNull();
    for (const [kind, body] of [
      ["workspace", workspace()],
      ["recipe", recipe()],
    ] as const) {
      const attempt = {
        version: 1,
        scopeKey: registrationScopeKey(scope),
        key,
        kind,
        body,
      };
      expect(parseRegistrationAttempt(JSON.stringify(attempt), scope)).toEqual(
        attempt,
      );
      for (const other of [
        { ...attempt, key: "arbitrary" },
        { ...attempt, body: { ...body, owner_user_id: "foreign" } },
        { ...attempt, body: { ...body, project_id: "foreign" } },
        { ...attempt, extra: true },
      ]) {
        expect(() =>
          parseRegistrationAttempt(JSON.stringify(other), scope),
        ).toThrow();
      }
      expect(() =>
        parseRegistrationAttempt(JSON.stringify(attempt), {
          ...scope,
          personaRevisionId: "next",
        }),
      ).toThrow();
    }
  });

  it("rejects foreign and altered persisted receipts instead of reporting success", () => {
    const body = workspace();
    const receipt = {
      ...body,
      id: "row",
      owner_user_id: scope.ownerUserId,
      config_hash: "a".repeat(64),
      status: "configured",
      base_url: body.base_url,
      external_workspace_id: body.external_workspace_id,
      provider: "comfyui_workbench",
    };
    expect(workspaceRegistrationMatches(receipt, scope, body)).toBe(true);
    expect(
      workspaceRegistrationMatches(
        { ...receipt, owner_user_id: "foreign" },
        scope,
        body,
      ),
    ).toBe(false);
    expect(
      workspaceRegistrationMatches(
        { ...receipt, config_hash: "invalid" },
        scope,
        body,
      ),
    ).toBe(false);
    const input = recipe();
    const saved = {
      ...input,
      id: "recipe-row",
      owner_user_id: scope.ownerUserId,
      current_revision: {
        ...input,
        id: "recipe-revision",
        version: 1,
        creative_recipe_id: "recipe-row",
        owner_user_id: scope.ownerUserId,
        content_hash: "b".repeat(64),
      },
    };
    expect(recipeRegistrationMatches(saved, scope, input)).toBe(true);
    expect(
      recipeRegistrationMatches(
        {
          ...saved,
          current_revision: {
            ...saved.current_revision,
            persona_id: "foreign",
          },
        },
        scope,
        input,
      ),
    ).toBe(false);
    expect(
      recipeRegistrationMatches(
        {
          ...saved,
          current_revision: {
            ...saved.current_revision,
            prompt_template: "Changed",
          },
        },
        scope,
        input,
      ),
    ).toBe(false);
  });
});
