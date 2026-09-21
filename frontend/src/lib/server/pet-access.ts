import { getSession } from "@/lib/auth";
import { fetchPythonApi } from "@/lib/server/python-api-proxy";
import { PetRequestError } from "../pets/pet-server-contract";
import { assertPetCapability } from "./pet-http";

/** Session, profile and authoritative runtime capability gate every asset operation. */
export async function authorizePetAccess(): Promise<void> {
  const user = await getSession();
  if (!user) throw new PetRequestError("認証が必要です。", 401);
  const profiles = [process.env.AOITALK_PROFILE, process.env.AIVTUBER_ENV];
  if (profiles.some((value) => value?.trim().toLowerCase() === "enterprise")) {
    throw new PetRequestError("このプロファイルではペットを利用できません。", 403);
  }
  let response: Response;
  try {
    response = await fetchPythonApi("/api/runtime/features", {
      method: "GET", user: { id: user.id, username: user.username, authoritySource: "web_session" },
      signal: AbortSignal.timeout(5000),
    });
  } catch { throw new PetRequestError("ペットの利用可否をサーバーに確認できません。", 503); }
  if (!response.ok) throw new PetRequestError("ペットの利用可否をサーバーに確認できません。", 503);
  assertPetCapability(profiles, await response.json());
}
