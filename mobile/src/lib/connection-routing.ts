import NetInfo, {
  type NetInfoState,
  type NetInfoStateType,
} from "@react-native-community/netinfo";
import * as SecureStore from "expo-secure-store";
import { PermissionsAndroid, Platform } from "react-native";
import { DEFAULT_PORTAL_URL, STORAGE_KEYS } from "../constants/config";
import {
  normalizeApiUrl,
  requireConfiguredApiUrl,
} from "./api-url";
import { normalizeGatewayServiceUrl, normalizePortalUrl } from "./portal-auth";

export type NetworkConnectionMode = "direct" | "gateway";

export interface NetworkEndpointRoutingConfig {
  /** Legacy records omit this field and are migrated as direct mode. */
  mode?: NetworkConnectionMode;
  /** Independent Gateway portal root; direct cellular URL remains preserved. */
  portalUrl?: string;
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

export interface NetworkRouteSnapshot {
  /** Network generation increments across Wi-Fi/cellular/SSID transitions. */
  networkKey: string;
  routingRevision: number;
}

/** A read-only probe supplied by api-client without creating a module cycle. */
export type NetworkEndpointProbe = (
  url: string,
  timeoutMs?: number,
  signal?: AbortSignal,
) => Promise<boolean | string>;

export interface NetworkEndpointResolution {
  /** Ordered, de-duplicated endpoints for the current network. */
  candidates: string[];
  /** Stable key for the network that produced this resolution. */
  networkKey: string;
  /** Durable routing revision captured while resolving the candidates. */
  routingRevision: number;
  /** Route kind for each candidate, parallel to `candidates`. */
  candidateModes?: NetworkConnectionMode[];
}

export interface ResolveApiUrlOptions {
  /** Probe candidates before selecting one when there is more than one. */
  probe?: NetworkEndpointProbe;
  /** How long a successful probe may be reused. */
  healthTtlMs?: number;
  /** Delay before confirming an all-routes probe failure with read-only probes. */
  failureConfirmationDelayMs?: number;
  /** Selected public Gateway service (`https://host/at`) for gateway mode. */
  gatewayApiUrl?: string;
  /** Abort only this caller; shared route/auth work remains safe to finish. */
  signal?: AbortSignal;
  /** Called after route/network selection and before the first health probe. */
  onResolution?: (resolution: NetworkEndpointResolution) => void;
}

/** No configured route answered its read-only AoiTalk health probe. */
export class ApiEndpointUnavailableError extends Error {
  readonly candidates: readonly string[];
  readonly networkKey: string;

  constructor(candidates: readonly string[], networkKey: string) {
    super("AoiTalkサーバーへ接続できませんでした。通信状態を確認して再試行してください。");
    this.name = "ApiEndpointUnavailableError";
    this.candidates = [...candidates];
    this.networkKey = networkKey;
  }
}

export const DEFAULT_NETWORK_ENDPOINT_ROUTING_CONFIG: NetworkEndpointRoutingConfig =
  {
    mode: "direct",
    portalUrl: DEFAULT_PORTAL_URL,
    enabled: false,
    wifiSsid: "",
    wifiApiUrl: "",
    cellularApiUrl: "",
  };

let configuredNetInfo = false;
let networkSubscription: (() => void) | null = null;
let cachedConfig: NetworkEndpointRoutingConfig | null = null;
let routingRevision = 0;

/** Successful route health may be reused briefly; failed probes are never cached. */
export const NETWORK_ENDPOINT_HEALTH_TTL_MS = 30_000;
export const NETWORK_ENDPOINT_FAILURE_CONFIRMATION_DELAY_MS = 250;
export const NETWORK_DISCONNECT_CONFIRMATION_DELAY_MS = 150;
interface RouteHealthCacheEntry {
  endpoint: string;
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

function delay(ms: number, signal?: AbortSignal): Promise<void> {
  if (signal?.aborted) {
    return Promise.reject(Object.assign(new Error("Routing probe was aborted"), { name: "AbortError" }));
  }
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
      reject(Object.assign(new Error("Routing probe was aborted"), { name: "AbortError" }));
    };
    signal?.addEventListener("abort", onAbort, { once: true });
  });
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
    mode: config.mode === "gateway" ? "gateway" : "direct",
    portalUrl: config.portalUrl?.trim() || DEFAULT_PORTAL_URL,
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

  if (normalized.mode === "gateway") {
    return {
      ...normalized,
      wifiApiUrl: normalized.wifiApiUrl
        ? requireConfiguredApiUrl(normalized.wifiApiUrl)
        : "",
      // `cellularApiUrl` remains a legacy/direct route value. Gateway mode
      // derives its `/at` service from the independent portal root below.
      portalUrl: normalizePortalUrl(normalized.portalUrl || DEFAULT_PORTAL_URL),
    };
  }

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
  const subscribe = (NetInfo as unknown as {
    addEventListener?: (listener: (state: NetInfoState) => void) => () => void;
  }).addEventListener;
  if (typeof subscribe === "function") {
    networkSubscription = subscribe((state) => {
      observeNetworkState(networkFromState(state));
    });
  }
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

function parseConfig(raw: string | null): {
  config: NetworkEndpointRoutingConfig;
  legacyModeMissing: boolean;
} {
  if (!raw) return {
    config: DEFAULT_NETWORK_ENDPOINT_ROUTING_CONFIG,
    legacyModeMissing: false,
  };
  try {
    const parsed = JSON.parse(raw) as Partial<NetworkEndpointRoutingConfig>;
    return {
      config: {
      // Existing settings deliberately have only enabled/wifiSsid/wifiApiUrl/
      // cellularApiUrl. Treating the missing discriminator as direct retains
      // every legacy value while making the new selection explicit.
      mode: parsed.mode === "gateway" ? "gateway" : "direct",
      portalUrl: typeof parsed.portalUrl === "string" && parsed.portalUrl.trim()
        ? parsed.portalUrl
        : DEFAULT_PORTAL_URL,
      enabled: Boolean(parsed.enabled),
      wifiSsid: typeof parsed.wifiSsid === "string" ? parsed.wifiSsid : "",
      wifiApiUrl:
        typeof parsed.wifiApiUrl === "string" ? parsed.wifiApiUrl : "",
      cellularApiUrl:
        typeof parsed.cellularApiUrl === "string" ? parsed.cellularApiUrl : "",
      },
      legacyModeMissing: parsed.mode !== "direct" && parsed.mode !== "gateway",
    };
  } catch {
    return {
      config: DEFAULT_NETWORK_ENDPOINT_ROUTING_CONFIG,
      legacyModeMissing: false,
    };
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

    if (parsed.legacyModeMissing && raw) {
      // Migrate only the discriminator. Existing key, route values and even
      // disabled legacy URL text remain intact until the user edits them.
      let migratedRaw: Record<string, unknown> = {};
      try {
        const candidate = JSON.parse(raw) as unknown;
        if (candidate && typeof candidate === "object" && !Array.isArray(candidate)) {
          migratedRaw = { ...(candidate as Record<string, unknown>) };
        }
      } catch {
        // parseConfig already supplied the safe known-field projection.
      }
      migratedRaw.mode = "direct";
      await SecureStore.setItemAsync(
        STORAGE_KEYS.NETWORK_ENDPOINT_ROUTING,
        JSON.stringify(Object.keys(migratedRaw).length > 1 ? migratedRaw : parsed.config),
      );
      if (routingRevision !== revisionAtStart) {
        if (cachedConfig) return cachedConfig;
        continue;
      }
    }

    cachedConfig = parsed.config;
    return parsed.config;
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

/** Read and record the current path identity for request snapshot checks. */
export async function getCurrentNetworkRouteSnapshot(): Promise<NetworkRouteSnapshot> {
  const network = await getCurrentNetworkInfo();
  observeNetworkState(network);
  return {
    networkKey: networkKey(network),
    routingRevision,
  };
}

/** Return the last path observed by routing or NetInfo without another probe. */
export function getObservedNetworkRouteSnapshot(): NetworkRouteSnapshot | null {
  if (lastNetworkIdentity === null) return null;
  return {
    networkKey: `${networkGeneration}:${lastNetworkIdentity}`,
    routingRevision,
  };
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
 * Resolve ordered route candidates without probing them. Direct mode retains
 * the legacy basic/public behavior; Gateway mode has only the optional
 * SSID-matching LAN candidate and the selected portal `/at` service.
 */
export async function getNetworkEndpointResolution(
  fallbackApiUrl: string,
  options: Pick<ResolveApiUrlOptions, "gatewayApiUrl" | "signal"> = {},
): Promise<NetworkEndpointResolution> {
  const fallback = normalizeUrl(fallbackApiUrl);

  for (;;) {
    if (options.signal?.aborted) {
      throw Object.assign(new Error("Routing resolution was aborted"), { name: "AbortError" });
    }
    const revisionAtStart = routingRevision;
    const config = await getNetworkEndpointRoutingConfig();
    if (routingRevision !== revisionAtStart) continue;

    const mode = config.mode ?? "direct";

    if (!config.enabled && mode === "direct") {
      return {
        candidates: [fallback],
        networkKey: "disabled",
        routingRevision: revisionAtStart,
        candidateModes: ["direct"],
      };
    }

    if (!config.enabled && mode === "gateway") {
      const gatewayApiUrl = normalizeGatewayServiceUrl(
        options.gatewayApiUrl ?? `${config.portalUrl || DEFAULT_PORTAL_URL}/at`,
      );
      return {
      candidates: [gatewayApiUrl],
        networkKey: "gateway-disabled",
        routingRevision: revisionAtStart,
        candidateModes: ["gateway"],
      };
    }

    let network: CurrentNetworkInfo;
    try {
      network = await getCurrentNetworkInfo();
    } catch {
      // NetInfo/permission failures cannot block the selected internet route.
      network = { type: "unknown", ssid: null, connected: null };
    }
    if (routingRevision !== revisionAtStart) continue;

    observeNetworkState(network);

    if (network.connected === false) {
      // NetInfo can briefly emit a disconnected snapshot while Android is
      // switching/validating an otherwise healthy Wi-Fi path. Confirm once
      // before turning that transient sample into "no route candidates".
      await delay(NETWORK_DISCONNECT_CONFIRMATION_DELAY_MS, options.signal);
      if (routingRevision !== revisionAtStart) continue;
      try {
        const confirmed = await getCurrentNetworkInfo();
        if (routingRevision !== revisionAtStart) continue;
        observeNetworkState(confirmed);
        network = confirmed;
      } catch {
        // Keep the original disconnected sample only when confirmation itself
        // is unavailable; callers will surface a real no-path state below.
      }
      if (network.connected === false) {
        return {
          candidates: [],
          networkKey: networkKey(network),
          routingRevision: revisionAtStart,
        };
      }
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
    const candidates = mode === "gateway"
      ? [
        ...(matchingWifi && config.wifiApiUrl
          ? [normalizeUrl(config.wifiApiUrl)]
          : []),
        // Gateway mode has no direct/basic fallback. The selected internet
        // route is always the configured portal's `/at` service.
        normalizeGatewayServiceUrl(
          options.gatewayApiUrl ?? `${config.portalUrl || DEFAULT_PORTAL_URL}/at`,
        ),
      ]
      : matchingWifi && config.wifiApiUrl
        ? [normalizeUrl(config.wifiApiUrl), publicOrBasic]
        : [publicOrBasic];

    return {
      candidates: uniqueCandidates(candidates),
      networkKey: networkKey(network),
      routingRevision: revisionAtStart,
      candidateModes: uniqueCandidates(candidates).map((candidate) =>
        mode === "gateway" && candidate === normalizeGatewayServiceUrl(
          options.gatewayApiUrl ?? `${config.portalUrl || DEFAULT_PORTAL_URL}/at`,
        )
          ? "gateway"
          : "direct",
      ),
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
    const resolution = await getNetworkEndpointResolution(fallbackApiUrl, {
      gatewayApiUrl: options.gatewayApiUrl,
      signal: options.signal,
    });
    options.onResolution?.(resolution);
    if (options.signal?.aborted) {
      throw Object.assign(new Error("Routing probe was aborted"), { name: "AbortError" });
    }
    const candidates = resolution.candidates;

    if (candidates.length === 0) {
      throw new ApiEndpointUnavailableError(candidates, resolution.networkKey);
    }

    // Routing disabled retains the old no-preflight behavior in direct mode.
    // Explicit route selections are probed before any mutation is sent.
    if (!options.probe || resolution.networkKey === "disabled") {
      return candidates[0] ?? "";
    }

    let resolutionChanged = false;
    const failedCandidates: string[] = [];
    for (const candidate of candidates) {
      if (routingRevision !== resolution.routingRevision) {
        resolutionChanged = true;
        break;
      }

      const cacheKey = routeHealthCacheKey(resolution, candidate);
      const now = Date.now();
      const ttl = options.healthTtlMs ?? NETWORK_ENDPOINT_HEALTH_TTL_MS;
      const cached = routeHealthCache.get(cacheKey);
      let reachableEndpoint: string | null;

      if (cached && now - cached.checkedAt <= ttl) {
        reachableEndpoint = cached.endpoint;
      } else {
        try {
          const probed = await options.probe(candidate, undefined, options.signal);
          reachableEndpoint =
            typeof probed === "string"
              ? probed.trim().replace(/\/+$/, "")
              : probed
                ? candidate
                : null;
        } catch (error) {
          if (options.signal?.aborted) {
            throw error;
          }
          reachableEndpoint = null;
        }
        if (routingRevision !== resolution.routingRevision) {
          resolutionChanged = true;
          break;
        }
        if (reachableEndpoint) {
          routeHealthCache.set(cacheKey, {
            endpoint: reachableEndpoint,
            checkedAt: Date.now(),
          });
        } else {
          // A single failed read-only probe is not durable evidence that the
          // route is down. Never pin it in the 30s success cache.
          routeHealthCache.delete(cacheKey);
          failedCandidates.push(candidate);
        }
      }

      if (reachableEndpoint) return reachableEndpoint;
    }

    if (resolutionChanged) continue;

    // All candidates failed once. Mutation requests have not been sent yet, so
    // it is safe to absorb a short transport/health blip by confirming with one
    // more round of read-only probes before declaring the route unavailable.
    if (failedCandidates.length > 0) {
      await delay(
        options.failureConfirmationDelayMs ??
          NETWORK_ENDPOINT_FAILURE_CONFIRMATION_DELAY_MS,
        options.signal,
      );
      for (const candidate of failedCandidates) {
        if (routingRevision !== resolution.routingRevision) {
          resolutionChanged = true;
          break;
        }
        let reachableEndpoint: string | null = null;
        try {
          const probed = await options.probe(candidate, undefined, options.signal);
          reachableEndpoint =
            typeof probed === "string"
              ? probed.trim().replace(/\/+$/, "")
              : probed
                ? candidate
                : null;
        } catch (error) {
          if (options.signal?.aborted) {
            throw error;
          }
          reachableEndpoint = null;
        }
        if (routingRevision !== resolution.routingRevision) {
          resolutionChanged = true;
          break;
        }
        if (reachableEndpoint) {
          routeHealthCache.set(routeHealthCacheKey(resolution, candidate), {
            endpoint: reachableEndpoint,
            checkedAt: Date.now(),
          });
          return reachableEndpoint;
        }
      }
    }

    if (resolutionChanged) continue;
    throw new ApiEndpointUnavailableError(candidates, resolution.networkKey);
  }
}
