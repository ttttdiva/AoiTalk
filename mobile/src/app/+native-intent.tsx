import { isGatewayCallbackUrl } from "../constants/config";

/** Keep verified Gateway callbacks out of Expo Router's screen resolver. */
export function redirectSystemPath({ path }: { path: string; initial: boolean }): string {
  if (isGatewayCallbackUrl(path)) {
    return "/(auth)/login";
  }
  return path;
}
