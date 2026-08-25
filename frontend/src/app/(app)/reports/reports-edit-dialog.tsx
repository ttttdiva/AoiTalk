import { type KeyboardEvent as ReactKeyboardEvent } from "react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
} from "@/components/ui/select";
import {
  Clock,
  Trash2,
  ExternalLink,
  Undo2,
  PlayCircle,
  StopCircle,
  Copy,
  MoreVertical,
  X as XIcon,
  FolderKanban,
  Folder,
} from "lucide-react";
import {
  type Project,
  type Space,
  type TimeEntry,
} from "@/lib/task-api";

export function ReportsEditDialog({
  editingEntry,
  closeEditDialog,
  isEditingRunning,
  editSaving,
  spaces,
  allProjects,
  currentEditingSpace,
  currentEditingProject,
  projectsForEditingSpace,
  isProjectReadOnly,
  editStart,
  editEnd,
  editDate,
  editDuration,
  editNote,
  setEditStart,
  setEditEnd,
  setEditDate,
  setEditDuration,
  setEditNote,
  handleEditStartBlur,
  handleEditEndBlur,
  handleEditDurationBlur,
  handleEditInputEnter,
  handleEditSave,
  handleEditStopTimer,
  handleEditRestartTimer,
  handleEditDuplicate,
  handleOpenTaskDetail,
  handleEditRevertToOriginal,
  handleEditDelete,
  handleEditMoveTaskSpace,
  handleEditMoveTaskProject,
}: {
  editingEntry: TimeEntry | null;
  closeEditDialog: () => void;
  isEditingRunning: boolean;
  editSaving: boolean;
  spaces: Space[];
  allProjects: Project[];
  currentEditingSpace: Space | null;
  currentEditingProject: Project | null;
  projectsForEditingSpace: Project[];
  isProjectReadOnly: (projectId: string | null | undefined) => boolean;
  editStart: string;
  editEnd: string;
  editDate: string;
  editDuration: string;
  editNote: string;
  setEditStart: (value: string) => void;
  setEditEnd: (value: string) => void;
  setEditDate: (value: string) => void;
  setEditDuration: (value: string) => void;
  setEditNote: (value: string) => void;
  handleEditStartBlur: () => void;
  handleEditEndBlur: () => void;
  handleEditDurationBlur: () => void;
  handleEditInputEnter: (e: ReactKeyboardEvent<HTMLInputElement>) => void;
  handleEditSave: () => void;
  handleEditStopTimer: () => void;
  handleEditRestartTimer: () => void;
  handleEditDuplicate: () => void;
  handleOpenTaskDetail: () => void;
  handleEditRevertToOriginal: () => void;
  handleEditDelete: () => void;
  handleEditMoveTaskSpace: (spaceId: string) => void | Promise<void>;
  handleEditMoveTaskProject: (projectId: string) => void | Promise<void>;
}) {
  const hasWritableProjectInSpace = (spaceId: string) =>
    allProjects.some(
      (project) =>
        project.space_id === spaceId && !isProjectReadOnly(project.id),
    );

  return (
    <Dialog
      open={!!editingEntry}
      onOpenChange={(open) => {
        if (!open) closeEditDialog();
      }}
    >
      <DialogContent
        size="lg"
        className="p-4 gap-3"
        showCloseButton={false}
      >
        {/* ヘッダー: アクション群 */}
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-0.5">
            {isEditingRunning ? (
              <Button
                variant="ghost"
                size="icon-sm"
                onClick={handleEditStopTimer}
                disabled={editSaving}
                title="タイマー停止"
                className="text-destructive hover:text-destructive"
              >
                <StopCircle className="size-5" />
              </Button>
            ) : (
              <Button
                variant="ghost"
                size="icon-sm"
                onClick={handleEditRestartTimer}
                disabled={editSaving}
                title="このタスクでタイマー再開"
                className="text-primary hover:text-primary"
              >
                <PlayCircle className="size-5" />
              </Button>
            )}
            <Button
              variant="ghost"
              size="icon-sm"
              onClick={handleEditDuplicate}
              disabled={editSaving || isEditingRunning}
              title="複製"
            >
              <Copy className="size-4" />
            </Button>
            <DropdownMenu>
              <DropdownMenuTrigger
                disabled={editSaving}
                className="inline-flex size-8 items-center justify-center rounded-md hover:bg-accent disabled:opacity-50"
                title="その他"
              >
                <MoreVertical className="size-4" />
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start">
                <DropdownMenuItem mnemonic="O" onClick={handleOpenTaskDetail}>
                  <ExternalLink className="mr-2 size-3.5" />
                  タスク詳細を開く
                </DropdownMenuItem>
                {editingEntry?.original_started_at &&
                  editingEntry?.original_ended_at && (
                    <DropdownMenuItem
                      mnemonic="R"
                      onClick={handleEditRevertToOriginal}
                    >
                      <Undo2 className="mr-2 size-3.5" />
                      タイマー記録値に戻す
                    </DropdownMenuItem>
                  )}
                <DropdownMenuSeparator />
                <DropdownMenuItem
                  mnemonic="D"
                  onClick={handleEditDelete}
                  className="text-destructive focus:text-destructive"
                >
                  <Trash2 className="mr-2 size-3.5" />
                  削除
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={closeEditDialog}
            disabled={editSaving}
          >
            <XIcon className="size-4" />
          </Button>
        </div>

        {/* タイトル */}
        <div>
          <button
            type="button"
            onClick={handleOpenTaskDetail}
            className="text-left text-base font-medium leading-tight hover:underline"
          >
            {editingEntry?.task_title || "タスク"}
          </button>
          <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
            {spaces.length > 0 &&
              editingEntry?.project_id &&
              allProjects.length > 1 && (
                <Select
                  value={
                    currentEditingSpace?.id ?? editingEntry.space_id ?? ""
                  }
                  onValueChange={(value) => {
                    if (value) void handleEditMoveTaskSpace(value);
                  }}
                  disabled={editSaving}
                >
                  <SelectTrigger className="h-7 w-auto border-none px-0 text-xs text-muted-foreground shadow-none hover:text-foreground">
                    <span className="inline-flex items-center gap-1">
                      <FolderKanban className="size-3" />
                      {currentEditingSpace?.name ||
                        editingEntry.space_name ||
                        "スペース未設定"}
                    </span>
                  </SelectTrigger>
                  <SelectContent>
                    {spaces
                      .filter((space) =>
                        allProjects.some(
                          (project) => project.space_id === space.id,
                        ),
                      )
                      .map((space) => (
                        <SelectItem
                          key={space.id}
                          value={space.id}
                          disabled={!hasWritableProjectInSpace(space.id)}
                        >
                          {space.name}
                        </SelectItem>
                      ))}
                  </SelectContent>
                </Select>
              )}
            {editingEntry?.project_id && allProjects.length > 1 ? (
              <Select
                value={editingEntry.project_id}
                onValueChange={(value) => {
                  if (value) void handleEditMoveTaskProject(value);
                }}
                disabled={editSaving}
              >
                <SelectTrigger className="h-7 w-auto border-none px-0 text-xs text-muted-foreground shadow-none hover:text-foreground">
                  <span className="inline-flex items-center gap-1">
                    <Folder className="size-3" />
                    {currentEditingProject?.name ||
                      editingEntry.project_name ||
                      "プロジェクト未設定"}
                  </span>
                </SelectTrigger>
                <SelectContent>
                  {projectsForEditingSpace.length > 0 ? (
                    <SelectGroup>
                      <SelectLabel>
                        {currentEditingSpace?.name ||
                          editingEntry.space_name ||
                          "スペースなし"}
                      </SelectLabel>
                      {projectsForEditingSpace.map((project) => (
                        <SelectItem
                          key={project.id}
                          value={project.id}
                          disabled={isProjectReadOnly(project.id)}
                        >
                          {project.name}
                        </SelectItem>
                      ))}
                    </SelectGroup>
                  ) : (
                    <SelectGroup>
                      <SelectLabel>{"プロジェクト"}</SelectLabel>
                      {allProjects.map((project) => (
                        <SelectItem
                          key={project.id}
                          value={project.id}
                          disabled={isProjectReadOnly(project.id)}
                        >
                          {project.name}
                        </SelectItem>
                      ))}
                    </SelectGroup>
                  )}
                </SelectContent>
              </Select>
            ) : (
              editingEntry?.project_name && (
                <span className="inline-flex items-center gap-1">
                  <Folder className="size-3" />
                  {editingEntry.project_name}
                </span>
              )
            )}
            {isEditingRunning && (
              <span className="inline-flex items-center gap-1 text-primary">
                <Clock className="size-3" />
                {"計測中"}
              </span>
            )}
          </div>
        </div>

        {/* Time row */}
        <div className="flex flex-wrap items-center gap-2">
          <Input
            type="text"
            inputMode="numeric"
            value={editStart}
            onChange={(e) => setEditStart(e.target.value)}
            onBlur={handleEditStartBlur}
            onKeyDown={handleEditInputEnter}
            placeholder="10:00"
            className="w-20 text-center font-mono tabular-nums"
            aria-label="開始時刻"
          />
          <span className="text-muted-foreground">→</span>
          <Input
            type="text"
            inputMode="numeric"
            value={isEditingRunning ? "計測中" : editEnd}
            onChange={(e) => setEditEnd(e.target.value)}
            onBlur={handleEditEndBlur}
            onKeyDown={handleEditInputEnter}
            placeholder="11:00"
            className="w-20 text-center font-mono tabular-nums"
            aria-label="終了時刻"
            disabled={isEditingRunning}
          />
          <Input
            type="date"
            value={editDate}
            onChange={(e) => setEditDate(e.target.value)}
            onKeyDown={handleEditInputEnter}
            className="w-40"
            aria-label="日付"
          />
          <Input
            type="text"
            inputMode="numeric"
            value={editDuration}
            onChange={(e) => setEditDuration(e.target.value)}
            onBlur={handleEditDurationBlur}
            onKeyDown={handleEditInputEnter}
            placeholder="0:00:00"
            className="w-24 text-center font-mono tabular-nums ml-auto"
            aria-label="経過時間"
          />
        </div>

        {/* メモ + 保存 */}
        <div className="flex items-center gap-2">
          <Input
            value={editNote}
            onChange={(e) => setEditNote(e.target.value)}
            onKeyDown={handleEditInputEnter}
            placeholder="メモ (任意)"
            className="flex-1"
          />
          <Button size="sm" onClick={handleEditSave} disabled={editSaving}>
            {editSaving ? "保存中..." : "Save"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
