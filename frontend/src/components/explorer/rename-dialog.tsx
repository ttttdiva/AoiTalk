"use client";

import { useState, useEffect } from "react";
import { useExplorer } from "@/contexts/explorer-context";
import {
  explorerRename,
  type ExplorerDirectory,
  type ExplorerFile,
} from "@/lib/explorer-api";
import {
  isRecordTableFile,
  RECORD_TABLE_EXTENSION,
  updateProjectRecordTable,
} from "@/lib/record-tables-api";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

interface RenameDialogProps {
  item: (ExplorerDirectory | ExplorerFile) | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function RenameDialog({ item, open, onOpenChange }: RenameDialogProps) {
  const { refresh } = useExplorer();
  const [newName, setNewName] = useState("");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (item) setNewName(item.name);
  }, [item]);

  const handleRename = async () => {
    if (!item || !newName.trim() || newName === item.name) return;
    setLoading(true);
    try {
      if ("type" in item && isRecordTableFile(item)) {
        const tableName = newName.trim().endsWith(RECORD_TABLE_EXTENSION)
          ? newName.trim().slice(0, -RECORD_TABLE_EXTENSION.length)
          : newName.trim();
        if (item.project_id && item.record_table_id && tableName) {
          await updateProjectRecordTable(
            item.project_id,
            item.record_table_id,
            {
              name: tableName,
            },
          );
        }
      } else {
        await explorerRename(item.path, newName.trim());
      }
      refresh();
      onOpenChange(false);
    } catch {
      // rename error
    } finally {
      setLoading(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle>名前の変更</DialogTitle>
        </DialogHeader>
        <Input
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") handleRename();
          }}
          autoFocus
        />
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            キャンセル
          </Button>
          <Button
            onClick={handleRename}
            disabled={!newName.trim() || newName === item?.name || loading}
          >
            {loading ? "変更中..." : "変更"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
