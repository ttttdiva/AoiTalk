"use client";

import { Send } from "lucide-react";

import { Button } from "@/components/ui/button";
import { MarkdownContent } from "@/components/ui/markdown-content";
import { Textarea } from "@/components/ui/textarea";
import { formatDateTime } from "@/components/tasks/task-detail/task-detail-utils";

type CommentItem = {
  id: string;
  content: string;
  created_at?: string | null;
  user_id?: string | null;
};

/** タスクコメント一覧と入力欄。 */
export function TaskDetailComments({
  comments,
  commentText,
  setCommentText,
  sendingComment,
  onSendComment,
  readOnly = false,
}: {
  comments: CommentItem[];
  commentText: string;
  setCommentText: (value: string) => void;
  sendingComment: boolean;
  onSendComment: () => void;
  readOnly?: boolean;
}) {
  return (
    <div className="space-y-4">
      <h2 className="text-sm font-medium">コメント</h2>
      {comments.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          コメントはまだありません
        </p>
      ) : (
        <div className="space-y-3">
          {comments.map((c) => (
            <div
              key={c.id}
              className="rounded-lg border p-3 text-sm space-y-1"
            >
              {/* 改行とMarkdown書式をそのまま反映する。 */}
              <div className="min-w-0 [overflow-wrap:anywhere]">
                <MarkdownContent content={c.content} breaks />
              </div>
              <p className="text-xs text-muted-foreground">
                {formatDateTime(c.created_at)}
              </p>
            </div>
          ))}
        </div>
      )}
      {!readOnly && <div className="flex gap-2">
        <Textarea
          value={commentText}
          onChange={(e) => setCommentText(e.target.value)}
          placeholder="コメントを入力..."
          rows={2}
          className="resize-none flex-1"
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
              onSendComment();
            }
          }}
        />
        <Button
          size="icon"
          onClick={onSendComment}
          disabled={sendingComment || !commentText.trim()}
          className="shrink-0 self-end"
        >
          <Send className="size-4" />
        </Button>
      </div>}
    </div>
  );
}
