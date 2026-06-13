import NetInfo, {
  type NetInfoState,
  type NetInfoStateType,
} from "@react-native-community/netinfo";
import * as SecureStore from "expo-secure-store";
import { PermissionsAndroid, Platform } from "react-native";
import { STORAGE_KEYS } from "../constants/config";
import { normalizeApiUrl } from "./api-url";

export interface NetworkEndpointRoutingConfig {
  enabled: boolean;
  wifiSsid: string;
  wifiApiUrl: string;
  cellularApiUrl: string;
}

export interface CurrentNetworkInfo {
  type: NetInfoStateType | "unknown";
  ssid: string | null;
}

export const DEFAULT_NETWORK_ENDPOINT_ROUTING_CONFIG: NetworkEndpointRoutingConfig =
  {
    enabled: false,
    wifiSsid: "",
    wifiApiUrl: "",
    cellularApiUrl: "",
  };

let configuredNetInfo = false;
let cachedConfig: NetworkEndpointRoutingConfig | null = null;

function normalizeUrl(value: string): string {
  return normalizeApiUrl(value);
}

function normalizeSsid(value: string | null | undefined): string {
  return (value ?? "").trim();
}

export function configureNetworkEndpointRouting(): void {
  if (configuredNetInfo) return;
  configuredNetInfo = true;
  NetInfo.configure({ shouldFetchWiFiSSID: true });
}

async function requestWifiSsidPermission(): Promise<void> {
  if (Platform.OS !== "android") return;
  try {
    await PermissionsAndroid.request(
      PermissionsAndroid.PERMISSIONS.ACCESS_FINE_LOCATION,
    );
  } catch {
    // SSID will be unavailable; callers fall back to the non-Wi-Fi endpoint.
  }
}

function parseConfig(raw: string | null): NetworkEndpointRoutingConfig {
  if (!raw) return DEFAULT_NETWORK_ENDPOINT_ROUTING_CONFIG;
  try {
    const parsed = JSON.parse(raw) as Partial<NetworkEndpointRoutingConfig>;
    return {
      enabled: Boolean(parsed.enabled),
      wifiSsid: typeof parsed.wifiSsid === "string" ? parsed.wifiSsid : "",
      wifiApiUrl:
        typeof parsed.wifiApiUrl === "string" ? parsed.wifiApiUrl : "",
      cellularApiUrl:
        typeof parsed.cellularApiUrl === "string" ? parsed.cellularApiUrl : "",
    };
  } catch {
    return DEFAULT_NETWORK_ENDPOINT_ROUTING_CONFIG;
  }
}

export async function getNetworkEndpointRoutingConfig(): Promise<NetworkEndpointRoutingConfig> {
  if (cachedConfig) return cachedConfig;
  cachedConfig = parseConfig(
    await SecureStore.getItemAsync(STORAGE_KEYS.NETWORK_ENDPOINT_ROUTING),
  );
  return cachedConfig;
}

export async function saveNetworkEndpointRoutingConfig(
  config: NetworkEndpointRoutingConfig,
): Promise<void> {
  cachedConfig = {
    enabled: config.enabled,
    wifiSsid: config.wifiSsid.trim(),
    wifiApiUrl: normalizeUrl(config.wifiApiUrl),
    cellularApiUrl: normalizeUrl(config.cellularApiUrl),
  };
  await SecureStore.setItemAsync(
    STORAGE_KEYS.NETWORK_ENDPOINT_ROUTING,
    JSON.stringify(cachedConfig),
  );
}

export function clearNetworkEndpointRoutingCache(): void {
  cachedConfig = null;
}

function networkFromState(state: NetInfoState): CurrentNetworkInfo {
  const details = state.details as { ssid?: string | null } | null;
  return {
    type: state.type ?? "unknown",
    ssid: normalizeSsid(details?.ssid) || null,
  };
}

export async function getCurrentNetworkInfo(): Promise<CurrentNetworkInfo> {
  configureNetworkEndpointRouting();
  await requestWifiSsidPermission();
  return networkFromState(await NetInfo.fetch());
}

export async function resolveApiUrlForCurrentNetwork(
  fallbackApiUrl: string,
): Promise<string> {
  const fallback = normalizeUrl(fallbackApiUrl);
  const config = await getNetworkEndpointRoutingConfig();
  if (!config.enabled) return fallback;

  const network = await getCurrentNetworkInfo();
  const wifiSsid = normalizeSsid(config.wifiSsid);
  const currentSsid = normalizeSsid(network.ssid);

  if (
    network.type === "wifi" &&
    wifiSsid &&
    currentSsid &&
    wifiSsid === currentSsid &&
    config.wifiApiUrl
  ) {
    return normalizeUrl(config.wifiApiUrl);
  }

  return config.cellularApiUrl ? normalizeUrl(config.cellularApiUrl) : fallback;
}
