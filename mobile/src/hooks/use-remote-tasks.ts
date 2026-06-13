import { useCallback, useEffect, useState } from "react";

import {
  listRemoteServers,
  type RemoteServerProfile,
} from "../lib/remote-servers";
import { listRemoteTasks, type RemoteTask } from "../lib/remote-tasks";

/**
 * 有効な外部AoiTalkサーバー接続先のタスクをまとめて取得するフック（モバイル）。
 *
 * 取得結果は表示用に保持するだけでローカルDBには保存しない。1つの接続先が
 * 失敗しても他の接続先の表示は継続する（部分失敗を許容）。
 */
export function useRemoteTasks(enabled: boolean = true) {
  const [remoteTasks, setRemoteTasks] = useState<RemoteTask[]>([]);
  const [profiles, setProfiles] = useState<RemoteServerProfile[]>([]);
  const [loading, setLoading] = useState(false);

  const reload = useCallback(async () => {
    if (!enabled) {
      setRemoteTasks([]);
      return;
    }
    setLoading(true);
    try {
      const allProfiles = await listRemoteServers();
      const activeProfiles = allProfiles.filter((p) => p.enabled);
      setProfiles(allProfiles);

      const results = await Promise.all(
        activeProfiles.map(async (profile) => {
          try {
            const tasks = await listRemoteTasks(profile.id);
            return tasks.map(
              (task): RemoteTask => ({
                ...task,
                remote_server_id: profile.id,
                remote_server_name: profile.name,
                remote_server_color: profile.display_color,
              }),
            );
          } catch (err) {
            console.error(`リモートタスク取得失敗 (${profile.name}):`, err);
            return [] as RemoteTask[];
          }
        }),
      );
      setRemoteTasks(results.flat());
    } catch (err) {
      console.error("リモート接続先の読み込み失敗:", err);
    } finally {
      setLoading(false);
    }
  }, [enabled]);

  useEffect(() => {
    void reload();
  }, [reload]);

  return { remoteTasks, profiles, loading, reload };
}
