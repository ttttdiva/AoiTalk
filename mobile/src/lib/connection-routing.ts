import NetInfo, {
  type NetInfoState,
  type NetInfoStateType,
} from "@react-native-community/netinfo";
import * as SecureStore from "expo-secure-store";
import { PermissionsAndroid, Platform } from "react-native";
import { STORAGE_KEYS } from "../constants/config";
import {
  normalizeApiUrl,
  requireConfiguredApiUrl,
} from "./api-url";

export interface NetworkEndpointRoutingConfig {
  enabled: boolean;
  wifiSsid: string;
  wifiApiUrl: string;
  cellularApiUrl: string;
}

export interface CurrentNetworkInfo {
  type: NetInfoStateType | "unknown";
  ssid: string | null;
  /** NetInfo isConnected; null means the connectivity state is unknown. */
  connected: boolean | null;
}

/** A read-only probe supplied by api-client without creating a module cycle. */
export type NetworkEndpointProbe = (url: string) => Promise<boolean>;

export interface NetworkEndpointResolution {
  /** Ordered, de-duplicated endpoints for the current network. */
  candidates: string[];
  /** Stable key for the network that produced this resolution. */
  networkKey: string;
  /** Durable routing revision captured while resolving the candidates. */
  routingRevision: number;
}

export interface ResolveApiUrlOptions {
  /** Probe candidates before selecting one when there is more than one. */
  probe?: NetworkEndpointProbe;
  /** How long a successful or failed probe may be reused. */
  healthTtlMs?: number;
}

/** No configured route answered its read-only AoiTalk health probe. */
export class ApiEndpointUnavailableError extends Error {
  readonly candidates: readonly string[];
  readonly networkKey: string;

  constructor(candidates: readonly string[], networkKey: string) {
    super("No configured AoiTalk API endpoint is reachable");
    this.name = "ApiEndpointUnavailableError";
    this.candidates = [...candidates];
    this.networkKey = networkKey;
  }
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
let routingRevision = 0;

/** Short-lived route health cache; failures must never pin the app offline. */
export const NETWORK_ENDPOINT_HEALTH_TTL_MS = 30_000;
interface RouteHealthCacheEntry {
  reachable: boolean;
  checkedAt: number;
}
const routeHealthCache = new Map<string, RouteHealthCacheEntry>();
let networkGeneration = 0;
let lastNetworkIdentity: string | null = null;

function normalizeUrl(value: string): string {
  return normalizeApiUrl(value);
}

function normalizeSsid(value: string | null | undefined): string {
  return (value ?? "").trim();
}

/**
 * Normalize settings before persistence. Disabled routes are intentionally
 * opaque so an invalid legacy value can be retained until the user enables
 * routing and explicitly repairs it.
 */
export function normalizeNetworkEndpointRoutingConfig(
  config: NetworkEndpointRoutingConfig,
): NetworkEndpointRoutingConfig {
  return {
    enabled: config.enabled,
    wifiSsid: config.wifiSsid.trim(),
    wifiApiUrl: config.enabled ? normalizeUrl(config.wifiApiUrl) : config.wifiApiUrl,
    cellularApiUrl: config.enabled
      ? normalizeUrl(config.cellularApiUrl)
      : config.cellularApiUrl,
  };
}

/** Validate only route values that can become active. */
export function validateNetworkEndpointRoutingConfig(
  config: NetworkEndpointRoutingConfig,
): NetworkEndpointRoutingConfig {
  const normalized = normalizeNetworkEndpointRoutingConfig(config);
  if (!normalized.enabled) return normalized;

  return {
    ...normalized,
    wifiApiUrl: normalized.wifiApiUrl
      ? requireConfiguredApiUrl(normalized.wifiApiUrl)
      : "",
    cellularApiUrl: normalized.cellularApiUrl
      ? requireConfiguredApiUrl(normalized.cellularApiUrl)
      : "",
  };
}

/** Revision of durable routing state and its in-memory cache. */
export function getNetworkEndpointRoutingRevision(): number {
  return routingRevision;
}

export function configureNetworkEndpointRouting(): void {
  if (configuredNetInfo) return;
  configuredNetInfo = true;
  NetInfo.configure({ shouldFetchWiFiSSID: true });
}

async function requestWifiSsidPermission(): Promise<boolean> {
  if (Platform.OS !== "android") return true;
  try {
    const status = await PermissionsAndroid.request(
      PermissionsAndroid.PERMISSIONS.ACCESS_FINE_LOCATION,
    );
    return status === PermissionsAndroid.RESULTS.GRANTED;
  } catch {
    // SSID will be unavailable; callers fall back to the non-Wi-Fi endpoint.
    return false;
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

  for (;;) {
    const revisionAtStart = routingRevision;
    const raw = await SecureStore.getItemAsync(
      STORAGE_KEYS.NETWORK_ENDPOINT_ROUTING,
    );
    if (routingRevision !== revisionAtStart) {
      if (cachedConfig) return cachedConfig;
      continue;
    }

    const parsed = parseConfig(raw);
    if (routingRevision !== revisionAtStart) {
      if (cachedConfig) return cachedConfig;
      continue;
    }

    cachedConfig = parsed;
    return parsed;
  }
}

export async function saveNetworkEndpointRoutingConfig(
  config: NetworkEndpointRoutingConfig,
): Promise<void> {
  const normalizedConfig = validateNetworkEndpointRoutingConfig(config);
  await SecureStore.setItemAsync(
    STORAGE_KEYS.NETWORK_ENDPOINT_ROUTING,
    JSON.stringify(normalizedConfig),
  );
  routingRevision += 1;
  cachedConfig = normalizedConfig;
  routeHealthCache.clear();
}

export function clearNetworkEndpointRoutingCache(): void {
  routingRevision += 1;
  cachedConfig = null;
  routeHealthCache.clear();
}

function networkFromState(state: NetInfoState): CurrentNetworkInfo {
  const details = state.details as { ssid?: string | null } | null;
  return {
    type: state.type ?? "unknown",
    ssid: normalizeSsid(details?.ssid) || null,
    connected: typeof state.isConnected === "boolean" ? state.isConnected : null,
  };
}

export async function getCurrentNetworkInfo(): Promise<CurrentNetworkInfo> {
  configureNetworkEndpointRouting();
  const canReadSsid = await requestWifiSsidPermission();
  const network = networkFromState(await NetInfo.fetch());
  return canReadSsid ? network : { ...network, ssid: null };
}

function networkKey(network: CurrentNetworkInfo): string {
  return `${networkGeneration}:${networkIdentity(network)}`;
}

function networkIdentity(network: CurrentNetworkInfo): string {
  const connected =
    network.connected === null
      ? "unknown"
      : network.connected
        ? "connected"
        : "disconnected";
  return `${connected}:${network.type}:${normalizeSsid(network.ssid)}`;
}

/**
 * NetInfo can report the same SSID after a link has gone down and come back.
 * Treat every observable path transition as a new generation so an old
 * negative health result cannot survive a disconnect/reconnect cycle.
 */
function observeNetworkState(network: CurrentNetworkInfo): void {
  const identity = networkIdentity(network);
  if (lastNetworkIdentity === null) {
    lastNetworkIdentity = identity;
    return;
  }
  if (lastNetworkIdentity === identity) return;
  lastNetworkIdentity = identity;
  networkGeneration += 1;
  routeHealthCache.clear();
}

function uniqueCandidates(candidates: string[]): string[] {
  const seen = new Set<string>();
  return candidates.filter((candidate) => {
    if (seen.has(candidate)) return false;
    seen.add(candidate);
    return true;
  });
}

/**
 * Resolve ordered route candidates without probing them.  A configured
 * public/cellular route is authoritative when present; the basic endpoint is
 * used only when that route is not configured.
 */
export async function getNetworkEndpointResolution(
  fallbackApiUrl: string,
): Promise<NetworkEndpointResolution> {
  const fallback = normalizeUrl(fallbackApiUrl);

  for (;;) {
    const revisionAtStart = routingRevision;
    const config = await getNetworkEndpointRoutingConfig();
    if (routingRevision !== revisionAtStart) continue;

    if (!config.enabled) {
      return {
        candidates: [fallback],
        networkKey: "disabled",
        routingRevision: revisionAtStart,
      };
    }

    let network: CurrentNetworkInfo;
    try {
      network = await getCurrentNetworkInfo();
    } catch {
      // NetInfo/permission failures cannot block the public/basic endpoint.
      network = { type: "unknown", ssid: null, connected: null };
    }
    if (routingRevision !== revisionAtStart) continue;

    observeNetworkState(network);

    if (network.connected === false) {
      return {
        candidates: [],
        networkKey: networkKey(network),
        routingRevision: revisionAtStart,
      };
    }

    const wifiSsid = normalizeSsid(config.wifiSsid);
    const currentSsid = normalizeSsid(network.ssid);
    const matchingWifi =
      network.connected === true &&
      network.type === "wifi" &&
      Boolean(wifiSsid) &&
      wifiSsid === currentSsid;
    const publicOrBasic = config.cellularApiUrl
      ? normalizeUrl(config.cellularApiUrl)
      : fallback;
    const candidates = matchingWifi && config.wifiApiUrl
      ? [normalizeUrl(config.wifiApiUrl), publicOrBasic]
      : [publicOrBasic];

    return {
      candidates: uniqueCandidates(candidates),
      networkKey: networkKey(network),
      routingRevision: revisionAtStart,
    };
  }
}

function routeHealthCacheKey(
  resolution: NetworkEndpointResolution,
  candidate: string,
): string {
  return `${resolution.routingRevision}:${resolution.networkKey}:${candidate}`;
}

export async function resolveApiUrlForCurrentNetwork(
  fallbackApiUrl: string,
  options: ResolveApiUrlOptions = {},
): Promise<string> {
  for (;;) {
    const resolution = await getNetworkEndpointResolution(fallbackApiUrl);
    const candidates = resolution.candidates;

    if (candidates.length === 0) {
      throw new ApiEndpointUnavailableError(candidates, resolution.networkKey);
    }

    // Routing off, a non-matching network, and a single configured endpoint
    // retain the old no-preflight behavior.
    if (!options.probe || candidates.length <= 1) {
      return candidates[0] ?? "";
    }

    let resolutionChanged = false;
    for (const candidate of candidates) {
      if (routingRevision !== resolution.routingRevision) {
        resolutionChanged = true;
        break;
      }

      const cacheKey = routeHealthCacheKey(resolution, candidate);
      const now = Date.now();
      const ttl = options.healthTtlMs ?? NETWORK_ENDPOINT_HEALTH_TTL_MS;
      const cached = routeHealthCache.get(cacheKey);
      let reachable: boolean;

      if (cached && now - cached.checkedAt <= ttl) {
        reachable = cached.reachable;
      } else {
        try {
          reachable = await options.probe(candidate);
        } catch {
          // Invalid routes and probe failures are treated as unavailable so a
          // configured public route can still rescue a dead LAN route.
          reachable = false;
        }
        if (routingRevision !== resolution.routingRevision) {
          resolutionChanged = true;
          break;
        }
        routeHealthCache.set(cacheKey, {
          reachable,
          checkedAt: Date.now(),
        });
      }

      if (reachable) return candidate;
    }

    if (resolutionChanged) continue;
    throw new ApiEndpointUnavailableError(candidates, resolution.networkKey);
  }
}
