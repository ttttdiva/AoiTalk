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
      set({
        connected,
        online,
        serverReachable: connected ? get().serverReachable : false,
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
    set((state) => {
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
    state.connected &&
    state.serverReachable === false &&
    state.serverCheckedAt !== null &&
    Date.now() - state.serverCheckedAt <= maxAgeMs
  );
}
