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
  online: boolean;
  serverReachable: boolean;
  serverCheckedAt: number | null;
  lastChange: number;
  _unsubscribe: (() => void) | null;
  start: () => void;
  stop: () => void;
  setServerReachable: (ok: boolean) => void;
}

export const useNetworkStore = create<NetworkStoreState>((set, get) => ({
  online: true,
  serverReachable: false,
  serverCheckedAt: null,
  lastChange: Date.now(),
  _unsubscribe: null,

  start: () => {
    if (get()._unsubscribe) return;
    const handler = (state: NetInfoState) => {
      const online = Boolean(state.isConnected && state.isInternetReachable !== false);
      set({
        online,
        serverReachable: online ? get().serverReachable : false,
        lastChange: Date.now(),
      });
    };
    const unsub = NetInfo.addEventListener(handler);
    NetInfo.fetch().then(handler);
    set({ _unsubscribe: unsub });
  },

  stop: () => {
    const unsub = get()._unsubscribe;
    if (unsub) unsub();
    set({ _unsubscribe: null });
  },

  setServerReachable: (ok: boolean) =>
    set({ serverReachable: ok, serverCheckedAt: Date.now(), lastChange: Date.now() }),
}));

/** 直近の疎通失敗を、送信前に再度長時間待たないための短期キャッシュとして使う。 */
export function isServerKnownUnreachable(maxAgeMs = 90_000): boolean {
  const state = useNetworkStore.getState();
  return (
    state.online &&
    state.serverReachable === false &&
    state.serverCheckedAt !== null &&
    Date.now() - state.serverCheckedAt <= maxAgeMs
  );
}
