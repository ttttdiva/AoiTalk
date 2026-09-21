import { authorizePetAccess } from "@/lib/server/pet-access";
import { createPetHandlers } from "@/lib/server/pet-http";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";
export const GET = createPetHandlers(authorizePetAccess).IMAGE;
