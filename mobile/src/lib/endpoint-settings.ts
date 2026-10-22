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

let endpointSettingsWriteQueue: Promise<void> = Promise.resolve();

/** Read both legacy-compatible records through the same public settings type. */
export async function readEndpointSettings(): Promise<EndpointSettings> {
  await endpointSettingsWriteQueue;
  const [storedApiUrl, routing] = await Promise.all([
    getApiUrl(),
    getNetworkEndpointRoutingConfig(),
  ]);
  return {
    apiUrl: storedApiUrl || DEFAULT_API_URL,
    routing,
  };
}

export const getEndpointSettings = readEndpointSettings;

/**
 * Persist the basic endpoint and network routing without exposing a new
 * server/routing combination during a partial write.
 */
export async function saveEndpointSettings(
  settings: EndpointSettings,
): Promise<void> {
  const write = endpointSettingsWriteQueue.then(async () => {
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

    try {
      if (!basicWriteNeeded && previousFingerprint === nextFingerprint) {
        // A route-only edit remains one atomic routing-schema write.
        await saveNetworkEndpointRoutingConfig(normalizedRouting);
        return;
      }

      if (previousFingerprint !== nextFingerprint) {
        // A new durable server must never be paired with the old active route.
        // Disable the old route before changing the identity, then publish the
        // requested route as one final schema write.
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
  });
  endpointSettingsWriteQueue = write.catch(() => undefined);
  return write;
}
