import { DEFAULT_API_URL } from "../constants/config";
import { getApiUrl, saveApiUrl } from "./auth";
import {
  clearApiUrlCache,
  normalizeApiServerFingerprint,
} from "./api-client";
import {
  getNetworkEndpointRoutingConfig,
  saveNetworkEndpointRoutingConfig,
  validateNetworkEndpointRoutingConfig,
  type NetworkEndpointRoutingConfig,
} from "./connection-routing";
import { normalizeApiUrl, requireConfiguredApiUrl } from "./api-url";

export interface EndpointSettings {
  apiUrl: string;
  routing: NetworkEndpointRoutingConfig;
}

/**
 * Persist the basic endpoint and network routing without exposing a new
 * server/routing combination during a partial write.
 */
export async function saveEndpointSettings(
  settings: EndpointSettings,
): Promise<void> {
  try {
    const normalizedApiUrl = requireConfiguredApiUrl(settings.apiUrl);
    const normalizedRouting = validateNetworkEndpointRoutingConfig(
      settings.routing,
    );
    const [storedApiUrl, previousRouting] = await Promise.all([
      getApiUrl(),
      getNetworkEndpointRoutingConfig(),
    ]);
    const previousFingerprint = normalizeApiServerFingerprint(
      storedApiUrl || DEFAULT_API_URL,
    );
    const nextFingerprint = normalizeApiServerFingerprint(normalizedApiUrl);
    const basicWriteNeeded =
      !storedApiUrl || normalizeApiUrl(storedApiUrl) !== normalizedApiUrl;

    if (!basicWriteNeeded && previousFingerprint === nextFingerprint) {
      // A route-only edit must remain exactly one routing write.
      await saveNetworkEndpointRoutingConfig(normalizedRouting);
      return;
    }

    if (previousFingerprint !== nextFingerprint) {
      // A new durable server must never be paired with the old active route.
      await saveNetworkEndpointRoutingConfig({
        ...previousRouting,
        enabled: false,
      });
      clearApiUrlCache();
    }

    if (basicWriteNeeded) await saveApiUrl(normalizedApiUrl);
    await saveNetworkEndpointRoutingConfig(normalizedRouting);
  } finally {
    clearApiUrlCache();
  }
}
