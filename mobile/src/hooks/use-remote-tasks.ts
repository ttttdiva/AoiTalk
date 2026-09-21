import { useCallback, useEffect, useRef, useState } from "react";

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
export type RemoteTaskScope = {
  /** プロジェクトを選択している場合のリモート側プロジェクト ID。 */
  projectId?: string | null;
  /** プロジェクト未選択でスペースを選択している場合の ID。 */
  spaceId?: string | null;
};

/**
 * 有効な外部接続先のタスクを現在のプロジェクト/スペース文脈で取得する。
 *
 * ``scope`` は任意で、未指定（または両方 null）の場合は従来どおり
 * リモート側の全タスクを取得する。ProjectContext は project/space のどちらか
 * 一方だけを選択するが、遷移中に両方が一時的に設定されても project を優先する。
 */
export function useRemoteTasks(
  enabled: boolean = true,
  scope?: RemoteTaskScope,
) {
  const [remoteTasks, setRemoteTasks] = useState<RemoteTask[]>([]);
  const [profiles, setProfiles] = useState<RemoteServerProfile[]>([]);
  const [loading, setLoading] = useState(false);
  // スコープ切替中に先行リクエストが完了しても、古い一覧で現在の表示を
  // 上書きしない。アンマウント後の state 更新も同じ世代チェックで抑止する。
  const requestGeneration = useRef(0);

  const projectId = scope?.projectId ?? null;
  const spaceId = scope?.spaceId ?? null;

  const reload = useCallback(async () => {
    const generation = ++requestGeneration.current;
    if (!enabled) {
      setRemoteTasks([]);
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const allProfiles = await listRemoteServers();
      if (generation !== requestGeneration.current) return;
      const activeProfiles = allProfiles.filter((p) => p.enabled);
      setProfiles(allProfiles);

      const query = projectId
        ? { project_id: projectId }
        : spaceId
          ? { space_id: spaceId }
          : undefined;

      const results = await Promise.all(
        activeProfiles.map(async (profile) => {
          try {
            const tasks = query
              ? await listRemoteTasks(profile.id, query)
              : await listRemoteTasks(profile.id);
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
      if (generation !== requestGeneration.current) return;
      setRemoteTasks(results.flat());
    } catch (err) {
      console.error("リモート接続先の読み込み失敗:", err);
    } finally {
      if (generation === requestGeneration.current) setLoading(false);
    }
  }, [enabled, projectId, spaceId]);

  useEffect(() => {
    void reload();
  }, [reload]);

  return { remoteTasks, profiles, loading, reload };
}
