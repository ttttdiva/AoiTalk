import { z } from "zod";

export const loginSchema = z.object({
  username: z.string().min(1, "ユーザー名は必須です"),
  password: z.string().min(1, "パスワードは必須です"),
});

export const createTaskSchema = z.object({
  title: z.string().min(1, "タイトルは必須です").max(500),
  description: z.string().optional(),
  status: z.string().optional(),
  priority: z.string().optional(),
  start_at: z.string().datetime().optional().nullable(),
  end_at: z.string().datetime().optional().nullable(),
  all_day: z.boolean().optional(),
  tag_ids: z.array(z.string().uuid()).optional(),
  assignee_ids: z.array(z.string().uuid()).optional(),
});

export const updateTaskSchema = z.object({
  title: z.string().min(1).max(500).optional(),
  description: z.string().optional().nullable(),
  status: z.string().optional(),
  priority: z.string().optional(),
  start_at: z.string().datetime().optional().nullable(),
  end_at: z.string().datetime().optional().nullable(),
  all_day: z.boolean().optional(),
  tag_ids: z.array(z.string().uuid()).optional(),
  assignee_ids: z.array(z.string().uuid()).optional(),
});

export const createProjectSchema = z.object({
  name: z.string().min(1, "プロジェクト名は必須です"),
  description: z.string().optional(),
  slug: z.string().optional(),
});

export const createTagSchema = z.object({
  name: z.string().min(1, "タグ名は必須です"),
  color: z.string().optional(),
});

export const timeEntrySchema = z.object({
  task_id: z.string().uuid("有効なタスクIDが必要です"),
  note: z.string().optional(),
});

// 型エクスポート
export type LoginInput = z.infer<typeof loginSchema>;
export type CreateTaskInput = z.infer<typeof createTaskSchema>;
export type UpdateTaskInput = z.infer<typeof updateTaskSchema>;
export type CreateProjectInput = z.infer<typeof createProjectSchema>;
export type CreateTagInput = z.infer<typeof createTagSchema>;
export type TimeEntryInput = z.infer<typeof timeEntrySchema>;
