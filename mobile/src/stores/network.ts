/**
 * Network connectivity store (zustand) powered by @react-native-community/netinfo.
 *
 * Two axes are tracked separately:
 *   - `online`: device has internet reachability (NetInfo.isInternetReachable)
 *   - `serverReachable`: our AoiTalk API responded within the last ping
 *
 * `serverReachable` is only true after the AoiTalk API responds. Internet
 * reachability alone must not block local-first startup or local writes.
 */

import { create } from 'zustand';
import NetInfo, { type NetInfoState } from '@react-native-community/netinfo';

interface NetworkStoreState {
  /**
   * 何らかのネットワークへ接続している（NetInfo.isConnected）。
   *
   * `online` と分けているのは、AoiTalkサーバーがLAN内にある構成があるため。
   * インターネットへ出られなくても、同じLANのサーバーへは到達できる。
   */
  connected: boolean;
  online: boolean;
  serverReachable: boolean;
  serverCheckedAt: number | null;
  lastChange: number;
  _unsubscribe: (() => void) | null;
  start: () => void;
  stop: () => void;
  setServerReachable: (ok: boolean) => void;
}

/**
 * NetInfo's `isInternetReachable` is deliberately not used for AoiTalk
 * requests.  A device can have a perfectly usable route to a server on the
 * same LAN while Android reports that the network has no internet access.
 * Treat a missing `connected` value as connected for old test doubles and
 * embedders; an explicit `false` is the only value that means no network
 * path.
 */
export function hasAoiTalkNetworkPath(
  state: Pick<NetworkStoreState, "connected"> | { connected?: boolean } =
    useNetworkStore.getState(),
): boolean {
  return state.connected !== false;
}

function normalizedWifiSsid(state: NetInfoState): string | null {
  if (state.type !== "wifi") return null;
  const details = state.details;
  if (!details || typeof details !== "object") return null;
  const ssid = (details as { ssid?: unknown }).ssid;
  if (typeof ssid !== "string") return null;
  const normalized = ssid.trim();
  return normalized && normalized.toLowerCase() !== "<unknown ssid>"
    ? normalized
    : null;
}

// Keep the last transport identity outside the persisted store.  A NetInfo
// transition invalidates a recent failure: the old failure may belong to a
// different Wi-Fi/cellular path and must not make the new path permanently
// offline.  SSID can be unavailable without location permission; in that case
// the stable `type:ssid-unavailable` identity avoids resetting on every event,
// while a later known SSID still invalidates the old path result safely.
let lastObservedNetworkIdentity: string | null = null;

export const useNetworkStore = create<NetworkStoreState>((set, get) => ({
  connected: true,
  online: true,
  serverReachable: false,
  serverCheckedAt: null,
  lastChange: Date.now(),
  _unsubscribe: null,

  start: () => {
    if (get()._unsubscribe) return;
    const handler = (state: NetInfoState) => {
      const connected = Boolean(state.isConnected);
      const online = Boolean(connected && state.isInternetReachable !== false);
      const ssid = normalizedWifiSsid(state);
      const networkIdentity = `${state.type}:${ssid ?? "ssid-unavailable"}`;
      // `null -> first NetInfo type` is also a transition: the store may have
      // recorded a failure before the listener was started (for example while
      // the app was backgrounded), and that failure must not survive the first
      // fresh path observation.
      const networkTransition =
        lastObservedNetworkIdentity !== networkIdentity;
      const connectionTransition = get().connected !== connected;
      const resetServerReachability =
        networkTransition || connectionTransition || !connected;
      lastObservedNetworkIdentity = networkIdentity;
      const now = Date.now();
      set({
        connected,
        online,
        // A reachability result is scoped to the active network path.  Do not
        // carry a Wi-Fi/cellular failure across a transition, and clear it on
        // disconnect so the next connected path can probe immediately.
        serverReachable: resetServerReachability
          ? false
          : get().serverReachable,
        serverCheckedAt: resetServerReachability
          ? null
          : get().serverCheckedAt,
        lastChange: now,
      });
    };
    const unsub = NetInfo.addEventListener(handler);
    NetInfo.fetch().then(handler);
    set({ _unsubscribe: unsub });
  },

  stop: () => {
    const unsub = get()._unsubscribe;
    if (unsub) unsub();
    lastObservedNetworkIdentity = null;
    set({ _unsubscribe: null });
  },

  setServerReachable: (ok: boolean) =>
    set((state) => {
      // A late response from a request that started before disconnect must not
      // resurrect server availability while NetInfo says there is no path.
      if (ok && state.connected === false) return state;
      // 成功応答のたびに同じ到達状態を再通知すると、チャット画面を含む
      // 購読コンポーネントが不要に再描画される。状態が変わらない成功は
      // そのまま維持し、失敗や復旧時だけ疎通時刻も更新する。
      if (ok && state.serverReachable) return state;
      const now = Date.now();
      return { serverReachable: ok, serverCheckedAt: now, lastChange: now };
    }),
}));

/**
 * 直近の疎通失敗を、送信前に再度長時間待たないための短期キャッシュとして使う。
 * ローカル環境ではバックエンド再起動などで一時的な失敗が起きやすいため、
 * ブロック時間が長引かないよう TTL は短め（既定 30 秒）に保つ。
 */
export function isServerKnownUnreachable(maxAgeMs = 30_000): boolean {
  const state = useNetworkStore.getState();
  return (
    state.connected !== false &&
    state.serverReachable === false &&
    state.serverCheckedAt !== null &&
    Date.now() - state.serverCheckedAt <= maxAgeMs
  );
}

/**
 * Whether an AoiTalk operation may start a request now.
 *
 * `serverReachable === false` is an observation, not a permanent circuit
 * breaker: the initial unknown state (no checked timestamp) and an expired
 * failure both allow a fresh read/probe.  A recent failure is the only
 * short-lived pre-request gate, and a physical network transition clears it.
 */
export function canAttemptAoiTalkServer(maxAgeMs = 30_000): boolean {
  return hasAoiTalkNetworkPath() && !isServerKnownUnreachable(maxAgeMs);
}
