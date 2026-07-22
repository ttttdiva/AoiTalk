"use client";

import { useCallback } from "react";

import { toast } from "sonner";

import { taskApi, type Tag, type Task } from "@/lib/task-api";
import { resolveTaskTagIds } from "@/components/tasks/task-form-utils";

/**
 * タスク詳細モーダルのタグ管理ロジックをまとめた hook。
 * state（tags / draftTagIds / task）は呼び出し側が所有し、setter を受け取る。
 * 挙動は元の TaskDetailModal と完全一致させている。
 */
export function useTaskTagManagement({
  tags,
  setTags,
  setDraftTagIds,
  setTask,
  slashSelectedTagIds,
  currentProjectId,
  spaces,
  onTaskUpdated,
}: {
  tags: Tag[];
  setTags: React.Dispatch<React.SetStateAction<Tag[]>>;
  setDraftTagIds: React.Dispatch<React.SetStateAction<string[]>>;
  setTask: React.Dispatch<React.SetStateAction<Task | null>>;
  slashSelectedTagIds: string[];
  currentProjectId: string | null;
  spaces: { id: string; name: string }[];
  onTaskUpdated: () => void;
}) {
  const resolveTagUpdates = useCallback(
    async (tagNames: string[], targetProjectId?: string | null) => {
      const projectId = targetProjectId || currentProjectId;
      let availableTags = tags;
      if (projectId && projectId !== currentProjectId) {
        try {
          availableTags = await taskApi.listTags(projectId);
        } catch (err) {
          console.error("移動先プロジェクトのタグ取得に失敗しました", err);
          availableTags = [];
        }
      }
      const { tagIds, createdTags } = await resolveTaskTagIds({
        tagNames,
        currentTagIds:
          projectId && projectId === currentProjectId ? slashSelectedTagIds : [],
        availableTags,
        createTag: async (name) => {
          if (!projectId) return null;
          return taskApi.createTag(projectId, { name });
        },
      });
      if (createdTags.length > 0 && projectId === currentProjectId) {
        setTags((prev) => {
          const existingIds = new Set(prev.map((tag) => tag.id));
          const nextCreated = createdTags.filter(
            (tag) => !existingIds.has(tag.id),
          );
          return nextCreated.length > 0 ? [...prev, ...nextCreated] : prev;
        });
      }
      return { tag_ids: tagIds };
    },
    [currentProjectId, slashSelectedTagIds, tags, setTags],
  );

  const syncManagedTag = useCallback(
    (tagId: string, updater: (tag: Tag) => Tag) => {
      setTags((prev) =>
        prev.map((tag) => (tag.id === tagId ? updater(tag) : tag)),
      );
      setTask((prev) =>
        prev
          ? ({
              ...prev,
              tags: prev.tags.map((tag) =>
                tag.id === tagId ? updater(tag) : tag,
              ),
            } as Task)
          : prev,
      );
    },
    [setTags, setTask],
  );

  const announceTagChange = useCallback(() => {
    onTaskUpdated();
  }, [onTaskUpdated]);

  const handleRenameTag = useCallback(
    async (tagId: string, name: string) => {
      const updated = await taskApi.updateTag(tagId, { name });
      syncManagedTag(tagId, (tag) => ({ ...tag, name: updated.name }));
      announceTagChange();
    },
    [announceTagChange, syncManagedTag],
  );

  const handleChangeTagColor = useCallback(
    async (tagId: string, color: string) => {
      const updated = await taskApi.updateTag(tagId, { color });
      syncManagedTag(tagId, (tag) => ({ ...tag, color: updated.color }));
      announceTagChange();
    },
    [announceTagChange, syncManagedTag],
  );

  const handleDeleteTag = useCallback(
    async (tagId: string) => {
      await taskApi.deleteTag(tagId);
      setTags((prev) => prev.filter((tag) => tag.id !== tagId));
      setDraftTagIds((prev) => prev.filter((id) => id !== tagId));
      setTask((prev) =>
        prev
          ? ({
              ...prev,
              tags: prev.tags.filter((tag) => tag.id !== tagId),
            } as Task)
          : prev,
      );
      announceTagChange();
      toast.success("Tag deleted");
    },
    [announceTagChange, setDraftTagIds, setTags, setTask],
  );

  const handleCopyTagToSpace = useCallback(
    async (tagId: string, spaceId: string) => {
      const copied = await taskApi.copyTagToSpace(tagId, spaceId);
      const targetSpace = spaces.find((space) => space.id === copied.space_id);
      toast.success(
        targetSpace ? `Copied to ${targetSpace.name}` : "Copied to another space",
      );
    },
    [spaces],
  );

  return {
    resolveTagUpdates,
    handleRenameTag,
    handleChangeTagColor,
    handleDeleteTag,
    handleCopyTagToSpace,
  };
}
