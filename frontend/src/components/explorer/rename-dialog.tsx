"use client";

import { useState, useEffect } from "react";
import { useExplorer } from "@/contexts/explorer-context";
import {
  type ExplorerDirectory,
  type ExplorerFile,
} from "@/lib/explorer-api";
import { useFilerOperations } from "@/hooks/use-filer-operations";
import { isRecordTableFile } from "@/lib/record-tables-api";
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
  const { refresh, capabilities, selectItem } = useExplorer();
  const { rename } = useFilerOperations({ capabilities, refresh });
  const [newName, setNewName] = useState("");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (item) setNewName(item.name);
  }, [item]);

  const handleRename = async () => {
    if (!item || !newName.trim() || newName === item.name) return;
    setLoading(true);
    try {
      // 実行・Undo登録は filer-operations 側へ集約
      let recordTable: { projectId: string; tableId: string } | null = null;
      if ("type" in item && isRecordTableFile(item)) {
        if (!item.project_id || !item.record_table_id) {
          onOpenChange(false);
          return;
        }
        recordTable = {
          projectId: item.project_id,
          tableId: item.record_table_id,
        };
      }
      const ok = await rename({
        path: item.path,
        currentName: item.name,
        newName,
        recordTable,
        // fetchDirectory intentionally clears selection for ordinary refresh
        // and navigation.  A successful rename is the one operation that
        // restores the server-confirmed path after that refresh.
        onRenamed: ({ newPath }) => selectItem(newPath),
      });
      if (ok) onOpenChange(false);
    } finally {
      setLoading(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
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
