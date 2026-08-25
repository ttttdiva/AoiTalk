"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  addProjectMember,
  changeProjectMemberRole,
  listProjectMemberCandidates,
  listProjectMembers,
  removeProjectMember,
  type ProjectMember,
  type ProjectMemberCandidate,
} from "@/lib/projects-workspace-api";

type ConfirmMemberRemoval = (options: {
  description: string;
  destructive: true;
}) => Promise<boolean>;

type UseProjectMembersOptions = {
  projectId: string | null;
  confirm: ConfirmMemberRemoval;
};

export type ProjectMemberAddResult = {
  total: number;
  succeeded: string[];
  failed: Array<{ userId: string; error: string }>;
};

export type ProjectMemberMutationResult = {
  action: "remove" | "role";
  targetId: string;
  success: boolean;
  error?: string;
};

function isAbortError(error: unknown) {
  return (
    typeof error === "object" &&
    error !== null &&
    "name" in error &&
    error.name === "AbortError"
  );
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message.trim()
    ? error.message
    : fallback;
}

function responseFailure(value: unknown): string | null {
  if (
    typeof value === "object" &&
    value !== null &&
    "success" in value &&
    value.success === false
  ) {
    if (
      "detail" in value &&
      typeof value.detail === "string" &&
      value.detail.trim()
    ) {
      return value.detail;
    }
    return "操作に失敗しました";
  }
  return null;
}

export function useProjectMembers({
  projectId,
  confirm,
}: UseProjectMembersOptions) {
  const [members, setMembers] = useState<ProjectMember[]>([]);
  const [membersLoading, setMembersLoading] = useState(false);
  const [allUsers, setAllUsers] = useState<ProjectMemberCandidate[]>([]);
  const [selectedUserIds, setSelectedUserIds] = useState<Set<string>>(
    () => new Set(),
  );
  const [addRole, setAddRole] = useState("member");
  const [addingMembers, setAddingMembers] = useState(false);
  const [addError, setAddError] = useState("");
  const [addResult, setAddResult] = useState<ProjectMemberAddResult | null>(
    null,
  );
  const [memberOperationError, setMemberOperationError] = useState("");
  const [memberOperationResult, setMemberOperationResult] =
    useState<ProjectMemberMutationResult | null>(null);
  const generationRef = useRef(0);
  const projectAbortRef = useRef<AbortController | null>(null);
  const addingRequestRef = useRef<number | null>(null);
  const memberMutationRef = useRef(new Set<string>());

  const isCurrentProject = useCallback(
    (targetProjectId: string, generation: number) =>
      projectId === targetProjectId && generationRef.current === generation,
    [projectId],
  );

  const loadMembers = useCallback(
    async (
      targetProjectId: string,
      generation: number,
      signal: AbortSignal,
    ) => {
      if (generationRef.current === generation) setMembersLoading(true);
      try {
        const nextMembers = await listProjectMembers(targetProjectId, signal);
        if (isCurrentProject(targetProjectId, generation)) {
          setMembers(nextMembers);
          return nextMembers;
        }
      } catch (error) {
        if (
          !isAbortError(error) &&
          isCurrentProject(targetProjectId, generation)
        ) {
          console.error("メンバー取得失敗:", error);
          setMembers([]);
        }
      } finally {
        if (isCurrentProject(targetProjectId, generation)) {
          setMembersLoading(false);
        }
      }
    },
    [isCurrentProject],
  );

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    void listProjectMemberCandidates(controller.signal)
      .then((users) => {
        if (active) setAllUsers(users);
      })
      .catch((error) => {
        if (active && !isAbortError(error)) {
          console.error("ユーザー取得失敗:", error);
        }
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, []);

  useEffect(() => {
    projectAbortRef.current?.abort();
    const controller = new AbortController();
    projectAbortRef.current = controller;
    const generation = ++generationRef.current;

    setMembers([]);
    setMembersLoading(Boolean(projectId));
    setSelectedUserIds(new Set());
    setAddRole("member");
    setAddingMembers(false);
    setAddError("");
    setAddResult(null);
    setMemberOperationError("");
    setMemberOperationResult(null);
    addingRequestRef.current = null;
    memberMutationRef.current.clear();

    if (projectId) {
      void loadMembers(projectId, generation, controller.signal);
    }

    return () => {
      controller.abort();
      if (projectAbortRef.current === controller) {
        projectAbortRef.current = null;
      }
      if (generationRef.current === generation) {
        generationRef.current += 1;
      }
    };
  }, [loadMembers, projectId]);

  const availableUsers = useMemo(() => {
    const memberUserIds = new Set(members.map((member) => member.user_id));
    return allUsers.filter((user) => !memberUserIds.has(user.id));
  }, [allUsers, members]);

  const toggleUser = useCallback((userId: string) => {
    setSelectedUserIds((previous) => {
      const next = new Set(previous);
      if (next.has(userId)) next.delete(userId);
      else next.add(userId);
      return next;
    });
  }, []);

  const handleAddMembers = useCallback(async () => {
    const targetProjectId = projectId;
    const generation = generationRef.current;
    const controller = projectAbortRef.current;
    if (
      !targetProjectId ||
      !controller ||
      selectedUserIds.size === 0 ||
      addingRequestRef.current !== null
    ) {
      return;
    }

    const userIds = Array.from(selectedUserIds);
    const requestedRole = addRole;
    addingRequestRef.current = generation;
    setAddingMembers(true);
    setAddError("");
    setAddResult(null);
    try {
      const settled = await Promise.allSettled(
        userIds.map((userId) =>
          Promise.resolve().then(() =>
            addProjectMember(
              targetProjectId,
              userId,
              requestedRole,
              controller.signal,
            ),
          ),
        ),
      );
      if (!isCurrentProject(targetProjectId, generation)) return;

      const succeeded = userIds.filter(
        (_userId, index) => {
          const result = settled[index];
          return result?.status === "fulfilled" && !responseFailure(result.value);
        },
      );
      const failed = userIds.flatMap((userId, index) => {
        const result = settled[index];
        if (!result) {
          return [{ userId, error: "追加に失敗しました" }];
        }
        if (result.status === "fulfilled") {
          const failure = responseFailure(result.value);
          if (!failure) return [];
          return [{ userId, error: failure }];
        }
        return [
          {
            userId,
            error: errorMessage(result.reason, "追加に失敗しました"),
          },
        ];
      });
      const result = { total: userIds.length, succeeded, failed };
      setAddResult(result);
      // Keep failed targets selected for a safe retry.  A successful target is
      // removed from the draft so retrying cannot create a duplicate member.
      setSelectedUserIds(new Set(failed.map((item) => item.userId)));
      if (failed.length === 0) {
        setAddRole("member");
        setAddError("");
      } else {
        const detail = failed
          .map((item) => `${item.userId}: ${item.error}`)
          .join(" / ");
        const message = `${failed.length}人の追加に失敗しました${detail ? `: ${detail}` : ""}`;
        setAddError(message);
      }

      // Re-read even when one or more requests failed. The backend may have
      // committed successful rows before a later request rejected.
      const refreshedMembers = await loadMembers(
        targetProjectId,
        generation,
        controller.signal,
      );
      if (refreshedMembers && isCurrentProject(targetProjectId, generation)) {
        // A concurrent request may have committed a failed/409 target before
        // the response arrived.  Drop it from the retry draft once the
        // authoritative member list confirms it is already present.
        const refreshedIds = new Set(
          refreshedMembers.map((member) => member.user_id),
        );
        const availableIds = new Set(
          allUsers
            .map((user) => user.id)
            .filter((userId) => !refreshedIds.has(userId)),
        );
        setSelectedUserIds((previous) => {
          const next = new Set(previous);
          for (const userId of next) {
            if (!availableIds.has(userId)) next.delete(userId);
          }
          return next;
        });
      }
    } catch (error) {
      if (
        !isAbortError(error) &&
        isCurrentProject(targetProjectId, generation)
      ) {
        const message = errorMessage(error, "追加に失敗しました");
        setAddError(message);
      }
    } finally {
      if (addingRequestRef.current === generation) {
        addingRequestRef.current = null;
      }
      if (isCurrentProject(targetProjectId, generation)) {
        setAddingMembers(false);
      }
    }
  }, [addRole, allUsers, isCurrentProject, loadMembers, projectId, selectedUserIds]);

  const handleRemoveMember = useCallback(
    async (memberId: string, displayName: string) => {
      const targetProjectId = projectId;
      if (!targetProjectId) return;
      const generation = generationRef.current;
      const controller = projectAbortRef.current;
      if (
        !(await confirm({
          description: `${displayName} をプロジェクトから除外しますか？`,
          destructive: true,
        }))
      ) {
        return;
      }

      if (!controller || !isCurrentProject(targetProjectId, generation)) return;
      const mutationKey = `${targetProjectId}:${generation}:remove:${memberId}`;
      if (memberMutationRef.current.has(mutationKey)) return;
      memberMutationRef.current.add(mutationKey);
      setMemberOperationError("");
      setMemberOperationResult(null);
      try {
        const result = await removeProjectMember(
          targetProjectId,
          memberId,
          controller.signal,
        );
        const failure = responseFailure(result);
        if (failure) throw new Error(failure);
        if (isCurrentProject(targetProjectId, generation)) {
          setMemberOperationResult({
            action: "remove",
            targetId: memberId,
            success: true,
          });
        }
      } catch (error) {
        if (
          !isAbortError(error) &&
          isCurrentProject(targetProjectId, generation)
        ) {
          const message = errorMessage(error, "メンバー除外に失敗しました");
          setMemberOperationError(message);
          setMemberOperationResult({
            action: "remove",
            targetId: memberId,
            success: false,
            error: message,
          });
        }
      } finally {
        memberMutationRef.current.delete(mutationKey);
        if (isCurrentProject(targetProjectId, generation)) {
          // Re-sync on both success and failure so a concurrent change cannot
          // leave the row displayed with a stale membership.
          await loadMembers(targetProjectId, generation, controller.signal);
        }
      }
    },
    [confirm, isCurrentProject, loadMembers, projectId],
  );

  const handleChangeRole = useCallback(
    async (memberId: string, newRole: string) => {
      const targetProjectId = projectId;
      const generation = generationRef.current;
      const controller = projectAbortRef.current;
      if (!targetProjectId || !controller) return;
      const mutationKey = `${targetProjectId}:${generation}:role:${memberId}`;
      if (memberMutationRef.current.has(mutationKey)) return;
      memberMutationRef.current.add(mutationKey);
      setMemberOperationError("");
      setMemberOperationResult(null);
      try {
        const result = await changeProjectMemberRole(
          targetProjectId,
          memberId,
          newRole,
          controller.signal,
        );
        const failure = responseFailure(result);
        if (failure) throw new Error(failure);
        if (isCurrentProject(targetProjectId, generation)) {
          setMemberOperationResult({
            action: "role",
            targetId: memberId,
            success: true,
          });
        }
      } catch (error) {
        if (
          !isAbortError(error) &&
          isCurrentProject(targetProjectId, generation)
        ) {
          const message = errorMessage(error, "ロール変更に失敗しました");
          setMemberOperationError(message);
          setMemberOperationResult({
            action: "role",
            targetId: memberId,
            success: false,
            error: message,
          });
        }
      } finally {
        memberMutationRef.current.delete(mutationKey);
        if (isCurrentProject(targetProjectId, generation)) {
          await loadMembers(targetProjectId, generation, controller.signal);
        }
      }
    },
    [isCurrentProject, loadMembers, projectId],
  );

  return {
    members,
    membersLoading,
    availableUsers,
    selectedUserIds,
    addRole,
    addingMembers,
    addError,
    addResult,
    memberOperationError,
    memberOperationResult,
    setAddRole,
    toggleUser,
    handleAddMembers,
    handleRemoveMember,
    handleChangeRole,
  };
}

type ProjectMembersHookReturn = ReturnType<typeof useProjectMembers>;

// Keep newly-added result fields optional for consumers that provide a small
// controller stub (for example, presentational component tests).
export type ProjectMembersController = Omit<
  ProjectMembersHookReturn,
  "addResult" | "memberOperationError" | "memberOperationResult"
> & {
  addResult?: ProjectMemberAddResult | null;
  memberOperationError?: string;
  memberOperationResult?: ProjectMemberMutationResult | null;
};
