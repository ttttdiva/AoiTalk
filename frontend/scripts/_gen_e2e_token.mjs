import { createSessionToken, ensureE2EUser } from "../e2e/support/auth.ts";

await ensureE2EUser();
const token = await createSessionToken();
console.log(token);
