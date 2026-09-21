import { authorizePetAccess } from "@/lib/server/pet-access";
import { createPetHandlers } from "@/lib/server/pet-http";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";
const handlers = createPetHandlers(authorizePetAccess);
export const GET = handlers.GET;
export const PUT = handlers.PUT;
export const DELETE = handlers.DELETE;
