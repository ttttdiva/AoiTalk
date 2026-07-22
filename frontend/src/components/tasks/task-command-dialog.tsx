"use client";

import type React from "react";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  SlashCommandInput,
  TASK_SLASH_COMMANDS as DEFAULT_TASK_SLASH_COMMANDS,
  type CommandCandidateSelection,
} from "@/components/tasks/slash-command-input";
import {
  taskValueCompletion,
  taskValuePreview,
} from "@/components/tasks/task-form-utils";
import type { Task } from "@/lib/task-api";

const TASK_LIST_SLASH_COMMANDS = DEFAULT_TASK_SLASH_COMMANDS;

type CommandCandidates = React.ComponentProps<
  typeof SlashCommandInput
>["commandCandidates"];

/**
 * フォーカス行に対するタスクコマンド入力ダイアログ。
 */
export function TaskCommandDialog({
  open,
  onClose,
  taskCommandTaskId,
  tasks,
  value,
  onValueChange,
  error,
  onErrorClear,
  loading,
  commandCandidates,
  onSubmit,
}: {
  open: boolean;
  onClose: () => void;
  taskCommandTaskId: string | null;
  tasks: Task[];
  value: string;
  onValueChange: (value: string) => void;
  error: string | null;
  onErrorClear: () => void;
  loading: boolean;
  commandCandidates: CommandCandidates;
  onSubmit: (raw: string, selectedTargetProjectId?: string) => Promise<string>;
}) {
  return (
    <Dialog
      open={open}
      onOpenChange={(nextOpen) => {
        if (!nextOpen) onClose();
      }}
    >
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>タスクコマンド</DialogTitle>
          <DialogDescription>
            {taskCommandTaskId
              ? `対象: ${tasks.find((task) => task.id === taskCommandTaskId)?.title ?? "不明なタスク"}`
              : "対象タスクを選択してください"}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <SlashCommandInput
            value={value}
            onChange={(nextValue) => {
              onValueChange(nextValue);
              if (error) onErrorClear();
            }}
            commands={TASK_LIST_SLASH_COMMANDS}
            commandCandidates={commandCandidates}
            getValuePreview={taskValuePreview}
            getValueCompletion={taskValueCompletion}
            onParseSlashCommands={(
              text,
              selection?: CommandCandidateSelection,
            ) => {
              const selectedTargetProjectId =
                selection?.command === "/m"
                  ? selection.candidate.projectId
                  : undefined;
              void onSubmit(text, selectedTargetProjectId);
              return "";
            }}
            onSubmitIntent={() => {
              void onSubmit(value);
            }}
            placeholder="/ でコマンド一覧"
            autoFocus
            disabled={loading}
          />
          <div className="text-xs text-muted-foreground">
            利用可能: `/start`, `/due`, `/status`, `/priority`, `/t`, `/m`
          </div>
          {error && <div className="text-sm text-red-500">{error}</div>}
        </div>
      </DialogContent>
    </Dialog>
  );
}
