"use client";

import { Loader2, Plus, UserPlus, Users, X } from "lucide-react";
import { AppSelect } from "@/components/ui/app-select";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import type { ProjectMembersController } from "@/components/projects/hooks/use-project-members";

const ROLE_OPTIONS = [
  { value: "owner", label: "オーナー" },
  { value: "admin", label: "管理者" },
  { value: "member", label: "メンバー" },
  { value: "viewer", label: "閲覧者" },
];

type ProjectMembersPanelProps = {
  projectName: string;
  controller: ProjectMembersController;
};

export function ProjectMembersPanel({
  projectName,
  controller,
}: ProjectMembersPanelProps) {
  const {
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
  } = controller;

  return (
    <Card className="flex-1">
      <CardHeader>
        <CardTitle
          role="heading"
          aria-level={2}
          className="flex items-center gap-2 text-sm"
        >
          <Users aria-hidden="true" className="size-4" />
          メンバー管理: {projectName}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <section aria-busy={membersLoading} aria-label="現在のメンバー">
          {membersLoading ? (
            <div className="space-y-2" role="status">
              <span className="sr-only">メンバーを読み込んでいます</span>
              {Array.from({ length: 3 }).map((_, index) => (
                <Skeleton key={index} className="h-10 w-full rounded-lg" />
              ))}
            </div>
          ) : members.length > 0 ? (
            <div className="space-y-2">
              {members.map((member) => {
                const displayName = member.display_name || member.username;
                return (
                  <div
                    key={member.id}
                    className="flex items-center justify-between rounded-lg border p-2.5"
                  >
                    <div className="flex items-center gap-3">
                      <div
                        aria-hidden="true"
                        className="flex size-8 items-center justify-center rounded-full bg-muted text-xs font-medium"
                      >
                        {displayName.charAt(0).toUpperCase()}
                      </div>
                      <div>
                        <p className="text-sm font-medium">{displayName}</p>
                        <p className="text-xs text-muted-foreground">
                          @{member.username}
                        </p>
                      </div>
                    </div>
                    <div className="flex items-center gap-2">
                      <AppSelect
                        aria-label={`${displayName} のロール`}
                        value={member.role || "member"}
                        onValueChange={(role) =>
                          void handleChangeRole(member.id, role)
                        }
                        className="h-7 rounded-md border border-input bg-transparent px-2 text-xs outline-none focus-visible:border-ring focus-visible:ring-1 focus-visible:ring-ring/50 dark:bg-input/30"
                      >
                        {ROLE_OPTIONS.map((option) => (
                          <option key={option.value} value={option.value}>
                            {option.label}
                          </option>
                        ))}
                      </AppSelect>
                      {member.joined_at && (
                        <span className="text-xs text-muted-foreground">
                          {new Date(member.joined_at).toLocaleDateString(
                            "ja-JP",
                          )}
                        </span>
                      )}
                      <Button
                        aria-label={`${displayName} をプロジェクトから除外`}
                        size="sm"
                        variant="ghost"
                        className="h-6 w-6 p-0 text-destructive hover:text-destructive"
                        onClick={() =>
                          void handleRemoveMember(member.id, displayName)
                        }
                      >
                        <X aria-hidden="true" className="size-3" />
                      </Button>
                    </div>
                  </div>
                );
              })}
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">メンバーがいません</p>
          )}
        </section>

        <Separator />
        <section
          aria-labelledby="project-members-add-heading"
          className="space-y-3"
        >
          <h3
            id="project-members-add-heading"
            className="flex items-center gap-1.5 text-sm font-medium"
          >
            <UserPlus aria-hidden="true" className="size-3.5" />
            メンバー追加
          </h3>

          {availableUsers.length > 0 ? (
            <>
              <div className="max-h-48 space-y-1 overflow-auto rounded-lg border p-2">
                {availableUsers.map((user) => {
                  const displayName = user.display_name || user.username;
                  return (
                    <label
                      key={user.id}
                      className="flex cursor-pointer items-center gap-3 rounded-md px-2 py-1.5 hover:bg-accent"
                    >
                      <Checkbox
                        aria-label={`${displayName} を選択`}
                        checked={selectedUserIds.has(user.id)}
                        onCheckedChange={() => toggleUser(user.id)}
                      />
                      <div className="flex min-w-0 items-center gap-2">
                        <div
                          aria-hidden="true"
                          className="flex size-6 shrink-0 items-center justify-center rounded-full bg-muted text-xs font-medium"
                        >
                          {displayName.charAt(0).toUpperCase()}
                        </div>
                        <span className="truncate text-sm">{displayName}</span>
                        <span className="truncate text-xs text-muted-foreground">
                          @{user.username}
                        </span>
                      </div>
                    </label>
                  );
                })}
              </div>

              <div className="flex items-center gap-2">
                <div className="space-y-1">
                  <Label
                    htmlFor="project-members-add-role"
                    className="text-xs text-muted-foreground"
                  >
                    ロール
                  </Label>
                  <AppSelect
                    id="project-members-add-role"
                    value={addRole}
                    onValueChange={setAddRole}
                    className="h-8 w-32 rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 dark:bg-input/30"
                  >
                    {ROLE_OPTIONS.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </AppSelect>
                </div>
                <div className="pt-5">
                  <Button
                    size="sm"
                    onClick={() => void handleAddMembers()}
                    disabled={addingMembers || selectedUserIds.size === 0}
                  >
                    {addingMembers ? (
                      <Loader2
                        aria-hidden="true"
                        className="mr-1 size-3 animate-spin"
                      />
                    ) : (
                      <Plus aria-hidden="true" className="mr-1 size-3" />
                    )}
                    {selectedUserIds.size > 0
                      ? `${selectedUserIds.size}人を追加`
                      : "追加"}
                  </Button>
                </div>
              </div>
            </>
          ) : (
            <p className="text-xs text-muted-foreground">
              追加可能なユーザーがいません
            </p>
          )}

          {addError && (
            <p className="text-xs text-destructive" role="alert">
              {addError}
            </p>
          )}
          {addResult && (
            <div className="space-y-1 text-xs" role="status">
              {addResult.succeeded.length > 0 && (
                <p className="text-muted-foreground">
                  成功: {addResult.succeeded.join(", ")}
                </p>
              )}
              {addResult.failed.length > 0 && (
                <ul className="space-y-1 text-destructive">
                  {addResult.failed.map((item) => (
                    <li key={item.userId}>
                      {item.userId}: {item.error}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
          {memberOperationError && (
            <p className="text-xs text-destructive" role="alert">
              {memberOperationError}
            </p>
          )}
          {memberOperationResult?.success && (
            <p className="text-xs text-muted-foreground" role="status">
              {memberOperationResult.action === "remove"
                ? "メンバーを除外しました。"
                : "ロールを変更しました。"}
            </p>
          )}
        </section>
      </CardContent>
    </Card>
  );
}
