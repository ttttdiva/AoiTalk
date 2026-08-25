import { deactivateE2EUser } from "./auth";

export default async function globalTeardown() {
  await deactivateE2EUser();
}
