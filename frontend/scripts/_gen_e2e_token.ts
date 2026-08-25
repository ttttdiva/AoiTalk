import { createSessionToken, ensureE2EUser } from "../e2e/support/auth";

async function main() {
  await ensureE2EUser();
  const token = await createSessionToken();
  console.log(token);
}

void main();
