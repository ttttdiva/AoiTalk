import { ensureE2EUser } from "./auth";

export default async function globalSetup() {
  await ensureE2EUser();
}
