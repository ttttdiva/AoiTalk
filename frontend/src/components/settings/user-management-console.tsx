"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import useSWR from "swr";
import { toast } from "sonner";
import {
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Copy,
  Edit3,
  Ellipsis,
  KeyRound,
  Loader2,
  RefreshCw,
  RotateCcw,
  Search,
  Shield,
  Trash2,
  UserPlus,
  UserX,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

type CurrentUser = {
  id: string;
  username: string;
  role: string | null;
};

type ManagedUser = {
  id: string;
  username: string;
  email: string | null;
  display_name: string | null;
  avatar_url: string | null;
  role: string | null;
  is_active: boolean | null;
  status: "active" | "inactive" | "deleted";
  is_deleted: boolean;
  password_reset_required: boolean | null;
  created_at: string | null;
  last_login: string | null;
  deleted_at: string | null;
};

type BlockingRelation = {
  label: string;
  count: number;
};

const ROLE_LABELS: Record<string, string> = {
  admin: "admin",
  user: "user",
};

const STATUS_LABELS: Record<ManagedUser["status"], string> = {
  active: "有効",
  inactive: "無効",
  deleted: "削除済み",
};

async function apiFetch<T = unknown>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail || res.statusText);
  }
  return res.json();
}

function formatDate(value: string | null) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "-";
  return new Intl.DateTimeFormat("ja-JP", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function displayName(user: ManagedUser) {
  return user.display_name || user.username;
}

function avatarFallback(user: ManagedUser) {
  return displayName(user).trim().charAt(0).toUpperCase() || "U";
}

function statusVariant(
  status: ManagedUser["status"],
): "default" | "secondary" | "destructive" {
  if (status === "active") return "default";
  if (status === "deleted") return "destructive";
  return "secondary";
}

export function UserManagementConsole({
  currentUser,
}: {
  currentUser: CurrentUser;
}) {
  // ユーザー一覧（サーバー状態）は SWR で管理。取得タイミングは従来どおり
  // 呼び出し側（展開/更新/各操作後）で駆動するため自動 revalidation は無効化する。
  // 取得失敗時は従来同様に直前値を保持する。
  const usersRef = useRef<ManagedUser[]>([]);
  const { data: users = [], mutate: mutateUsers } = useSWR<ManagedUser[]>(
    "settings/managed-users",
    async () => {
      try {
        return await apiFetch<ManagedUser[]>("/api/users");
      } catch (err) {
        toast.error(err instanceof Error ? err.message : "ユーザー一覧の取得に失敗しました");
        return usersRef.current;
      }
    },
    {
      revalidateOnMount: false,
      revalidateOnFocus: false,
      revalidateOnReconnect: false,
      revalidateIfStale: false,
      keepPreviousData: true,
      dedupingInterval: 0,
    },
  );
  usersRef.current = users;
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busyUserId, setBusyUserId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [roleFilter, setRoleFilter] = useState("all");
  const [selectedUserId, setSelectedUserId] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [recoveryOpen, setRecoveryOpen] = useState(false);
  const [editUser, setEditUser] = useState<ManagedUser | null>(null);
  const [deleteUser, setDeleteUser] = useState<ManagedUser | null>(null);
  const [purgeUser, setPurgeUser] = useState<ManagedUser | null>(null);
  const [resetUser, setResetUser] = useState<ManagedUser | null>(null);
  const [resetUrl, setResetUrl] = useState("");
  const [deleteConfirm, setDeleteConfirm] = useState("");
  const [purgeConfirm, setPurgeConfirm] = useState("");
  const [formError, setFormError] = useState("");

  const [createForm, setCreateForm] = useState({
    username: "",
    display_name: "",
    email: "",
    password: "",
    role: "user",
    require_password_change: true,
  });

  const [editForm, setEditForm] = useState({
    display_name: "",
    email: "",
    role: "user",
    status: "active",
  });

  const selectedUser = useMemo(
    () => users.find((user) => user.id === selectedUserId) || null,
    [selectedUserId, users],
  );

  const visibleUsers = useMemo(
    () => users.filter((user) => user.status !== "deleted"),
    [users],
  );

  const deletedUsers = useMemo(
    () => users.filter((user) => user.status === "deleted"),
    [users],
  );

  const activeAdminCount = useMemo(
    () =>
      users.filter(
        (user) =>
          user.role === "admin" &&
          user.status === "active" &&
          user.is_active !== false,
      ).length,
    [users],
  );

  const counts = useMemo(
    () => ({
      all: visibleUsers.length,
      active: visibleUsers.filter((user) => user.status === "active").length,
      inactive: visibleUsers.filter((user) => user.status === "inactive").length,
      deleted: deletedUsers.length,
      reset: users.filter((user) => user.password_reset_required).length,
    }),
    [deletedUsers.length, users, visibleUsers],
  );

  const filteredUsers = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return visibleUsers.filter((user) => {
      const matchesQuery =
        !needle ||
        [
          user.username,
          user.display_name || "",
          user.email || "",
          ROLE_LABELS[user.role || "user"] || user.role || "",
        ]
          .join(" ")
          .toLowerCase()
          .includes(needle);
      const matchesStatus =
        statusFilter === "all" || user.status === statusFilter;
      const matchesRole = roleFilter === "all" || user.role === roleFilter;
      return matchesQuery && matchesStatus && matchesRole;
    });
  }, [query, roleFilter, statusFilter, visibleUsers]);

  const loadUsers = useCallback(async () => {
    setLoading(true);
    try {
      await mutateUsers();
    } finally {
      setLoading(false);
    }
  }, [mutateUsers]);

  useEffect(() => {
    if (expanded && users.length === 0) void loadUsers();
  }, [expanded, loadUsers, users.length]);

  const updateUser = useCallback(
    async (user: ManagedUser, body: Record<string, unknown>) => {
      setBusyUserId(user.id);
      try {
        const updated = await apiFetch<ManagedUser>(`/api/users/${user.id}`, {
          method: "PATCH",
          body: JSON.stringify(body),
        });
        await mutateUsers(
          (prev = []) => prev.map((item) => (item.id === updated.id ? updated : item)),
          { revalidate: false },
        );
        if (selectedUserId === updated.id) setSelectedUserId(updated.id);
        toast.success("ユーザーを更新しました");
      } catch (err) {
        toast.error(err instanceof Error ? err.message : "更新に失敗しました");
        throw err;
      } finally {
        setBusyUserId(null);
      }
    },
    [selectedUserId, mutateUsers],
  );

  const canDeactivate = (user: ManagedUser) => {
    if (user.id === currentUser.id) return false;
    if (user.role === "admin" && activeAdminCount <= 1) return false;
    return true;
  };

  const openEdit = (user: ManagedUser) => {
    setFormError("");
    setEditForm({
      display_name: user.display_name || "",
      email: user.email || "",
      role: user.role || "user",
      status: user.status === "deleted" ? "inactive" : user.status,
    });
    setEditUser(user);
  };

  const createUser = async () => {
    setFormError("");
    if (!createForm.username.trim() || !createForm.password) {
      setFormError("ユーザー名と初期パスワードは必須です");
      return;
    }
    setBusyUserId("create");
    try {
      const created = await apiFetch<ManagedUser>("/api/users", {
        method: "POST",
        body: JSON.stringify({
          username: createForm.username.trim(),
          display_name: createForm.display_name.trim() || null,
          email: createForm.email.trim() || null,
          password: createForm.password,
          role: createForm.role,
          require_password_change: createForm.require_password_change,
        }),
      });
      await mutateUsers((prev = []) => [created, ...prev], { revalidate: false });
      setCreateOpen(false);
      setCreateForm({
        username: "",
        display_name: "",
        email: "",
        password: "",
        role: "user",
        require_password_change: true,
      });
      toast.success("ユーザーを追加しました");
    } catch (err) {
      setFormError(err instanceof Error ? err.message : "作成に失敗しました");
    } finally {
      setBusyUserId(null);
    }
  };

  const saveEdit = async () => {
    if (!editUser) return;
    setFormError("");
    try {
      await updateUser(editUser, {
        display_name: editForm.display_name.trim() || null,
        email: editForm.email.trim() || null,
        role: editForm.role,
        is_active: editForm.status === "active",
      });
      setEditUser(null);
    } catch (err) {
      setFormError(err instanceof Error ? err.message : "保存に失敗しました");
    }
  };

  const deleteSelectedUser = async () => {
    if (!deleteUser || deleteConfirm !== deleteUser.username) return;
    setBusyUserId(deleteUser.id);
    try {
      const updated = await apiFetch<ManagedUser>(`/api/users/${deleteUser.id}`, {
        method: "DELETE",
      });
      await mutateUsers(
        (prev = []) => prev.map((item) => (item.id === updated.id ? updated : item)),
        { revalidate: false },
      );
      if (selectedUserId === updated.id) setSelectedUserId(null);
      setDeleteUser(null);
      setDeleteConfirm("");
      toast.success("ユーザーを削除しました");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "削除に失敗しました");
    } finally {
      setBusyUserId(null);
    }
  };

  const restoreDeletedUser = async (user: ManagedUser) => {
    await updateUser(user, { is_active: true });
  };

  const purgeDeletedUser = async () => {
    if (!purgeUser || purgeConfirm !== purgeUser.username) return;
    setBusyUserId(purgeUser.id);
    try {
      const res = await fetch(`/api/users/${purgeUser.id}/purge`, {
        method: "DELETE",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({ detail: res.statusText }));
        const blockers = Array.isArray(detail.blocking_relations)
          ? (detail.blocking_relations as BlockingRelation[])
          : [];
        const blockerText =
          blockers.length > 0
            ? `: ${blockers.map((item) => `${item.label} ${item.count}件`).join("、")}`
            : "";
        throw new Error(`${detail.detail || res.statusText}${blockerText}`);
      }
      await mutateUsers((prev = []) => prev.filter((item) => item.id !== purgeUser.id), {
        revalidate: false,
      });
      if (selectedUserId === purgeUser.id) setSelectedUserId(null);
      setPurgeUser(null);
      setPurgeConfirm("");
      toast.success("ユーザーを完全削除しました");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "完全削除に失敗しました");
    } finally {
      setBusyUserId(null);
    }
  };

  const generateResetLink = async () => {
    if (!resetUser) return;
    setBusyUserId(resetUser.id);
    setResetUrl("");
    try {
      const data = await apiFetch<{ reset_url: string }>(
        `/api/users/${resetUser.id}/password-reset-link`,
        { method: "POST" },
      );
      setResetUrl(data.reset_url);
      await loadUsers();
      toast.success("再設定リンクを発行しました");
    } catch (err) {
      toast.error(
        err instanceof Error ? err.message : "再設定リンクの発行に失敗しました",
      );
    } finally {
      setBusyUserId(null);
    }
  };

  const copyResetUrl = async () => {
    if (!resetUrl) return;
    await navigator.clipboard.writeText(resetUrl);
    toast.success("再設定リンクをコピーしました");
  };

  const renderActions = (user: ManagedUser) => {
    const isSelf = user.id === currentUser.id;
    const isLastAdmin = user.role === "admin" && activeAdminCount <= 1;
    const disabledReason = isSelf
      ? "自分自身には実行できません"
      : isLastAdmin
        ? "最後の管理者には実行できません"
        : "";

    return (
      <DropdownMenu>
        <DropdownMenuTrigger
          className="inline-flex size-8 items-center justify-center rounded-md border bg-background text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          aria-label={`${user.username} の操作`}
        >
          <Ellipsis className="size-4" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-56">
          <DropdownMenuLabel>{user.username}</DropdownMenuLabel>
          <DropdownMenuItem
            mnemonic="O"
            onClick={() => setSelectedUserId(user.id)}
          >
            <Shield className="size-4" />
            詳細を開く
          </DropdownMenuItem>
          <DropdownMenuItem mnemonic="E" onClick={() => openEdit(user)}>
            <Edit3 className="size-4" />
            編集
          </DropdownMenuItem>
          <DropdownMenuItem
            mnemonic="P"
            disabled={user.status !== "active"}
            onClick={() => {
              setResetUser(user);
              setResetUrl("");
            }}
          >
            <KeyRound className="size-4" />
            パスワード再設定
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          {user.status === "active" ? (
            <DropdownMenuItem
              mnemonic="D"
              disabled={!canDeactivate(user)}
              title={disabledReason}
              onClick={() => void updateUser(user, { is_active: false })}
            >
              <UserX className="size-4" />
              無効化
            </DropdownMenuItem>
          ) : (
            <DropdownMenuItem
              mnemonic="R"
              disabled={user.status === "deleted" && isSelf}
              onClick={() => void updateUser(user, { is_active: true })}
            >
              <RotateCcw className="size-4" />
              復帰
            </DropdownMenuItem>
          )}
          <DropdownMenuItem
            mnemonic="X"
            disabled={!canDeactivate(user) || user.status === "deleted"}
            title={disabledReason}
            variant="destructive"
            onClick={() => {
              setDeleteUser(user);
              setDeleteConfirm("");
            }}
          >
            <Trash2 className="size-4" />
            削除
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    );
  };

  return (
    <>
      <Card size="sm" className="rounded-md border-border dark:border-[#333335] bg-card dark:bg-[#1a1a1b] py-0">
        <CardHeader
          className="cursor-pointer select-none gap-3"
          onClick={() => setExpanded((v) => !v)}
        >
          <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
            <div>
              <CardTitle className="flex items-center gap-2 text-sm">
                <Shield className="size-4" />
                ユーザー管理
                {visibleUsers.length > 0 && (
                  <span className="text-xs font-normal text-muted-foreground">
                    {visibleUsers.length}件
                  </span>
                )}
              </CardTitle>
              <p className="mt-1 text-xs text-muted-foreground">
                ユーザーの状態、権限、ログイン復旧を一覧から管理します。
              </p>
            </div>
            <div
              className="flex flex-wrap items-center gap-2"
              onClick={(event) => event.stopPropagation()}
            >
              {expanded ? (
                <ChevronUp className="size-4" />
              ) : (
                <ChevronDown className="size-4" />
              )}
              {expanded && (
                <>
              <Button variant="outline" size="sm" onClick={() => void loadUsers()}>
                <RefreshCw className="size-3.5" />
                更新
              </Button>
              <Button
                variant="outline"
                size="sm"
                disabled={deletedUsers.length === 0}
                onClick={() => setRecoveryOpen(true)}
              >
                <RotateCcw className="size-3.5" />
                復旧
              </Button>
              <Button size="sm" onClick={() => setCreateOpen(true)}>
                <UserPlus className="size-3.5" />
                ユーザー追加
              </Button>
                </>
              )}
            </div>
          </div>
          {expanded && (
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
            <div className="rounded-lg border p-3">
              <p className="text-xs text-muted-foreground">全ユーザー</p>
              <p className="text-xl font-semibold">{counts.all}</p>
            </div>
            <div className="rounded-lg border p-3">
              <p className="text-xs text-muted-foreground">有効</p>
              <p className="text-xl font-semibold">{counts.active}</p>
            </div>
            <div className="rounded-lg border p-3">
              <p className="text-xs text-muted-foreground">無効</p>
              <p className="text-xl font-semibold">{counts.inactive}</p>
            </div>
            <div className="rounded-lg border p-3">
              <p className="text-xs text-muted-foreground">削除済み</p>
              <p className="text-xl font-semibold">{counts.deleted}</p>
            </div>
          </div>
          )}
        </CardHeader>
        {expanded && (
        <CardContent className="space-y-3">
          <div className="flex flex-col gap-2 lg:flex-row">
            <div className="relative flex-1">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="名前、ユーザー名、メールで検索"
                className="pl-8"
              />
            </div>
            <Select
              value={statusFilter}
              onValueChange={(value) => value && setStatusFilter(value)}
            >
              <SelectTrigger className="w-full lg:w-40">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">すべての状態</SelectItem>
                <SelectItem value="active">有効</SelectItem>
                <SelectItem value="inactive">無効</SelectItem>
              </SelectContent>
            </Select>
            <Select
              value={roleFilter}
              onValueChange={(value) => value && setRoleFilter(value)}
            >
              <SelectTrigger className="w-full lg:w-40">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">すべてのロール</SelectItem>
                <SelectItem value="admin">admin</SelectItem>
                <SelectItem value="user">user</SelectItem>
              </SelectContent>
            </Select>
          </div>

          {loading ? (
            <div className="space-y-2">
              {Array.from({ length: 5 }).map((_, index) => (
                <Skeleton key={index} className="h-12 w-full rounded-lg" />
              ))}
            </div>
          ) : (
            <div className="rounded-lg border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>ユーザー</TableHead>
                    <TableHead>ロール</TableHead>
                    <TableHead>状態</TableHead>
                    <TableHead>最終ログイン</TableHead>
                    <TableHead>作成日</TableHead>
                    <TableHead className="w-12 text-right">操作</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {filteredUsers.map((user) => (
                    <TableRow
                      key={user.id}
                      className="cursor-pointer"
                      onClick={() => setSelectedUserId(user.id)}
                    >
                      <TableCell>
                        <div className="flex items-center gap-3">
                          <Avatar className="size-8">
                            {user.avatar_url && (
                              <AvatarImage
                                src={user.avatar_url}
                                alt={`${displayName(user)}のアイコン`}
                              />
                            )}
                            <AvatarFallback className="text-xs font-medium">
                              {avatarFallback(user)}
                            </AvatarFallback>
                          </Avatar>
                          <div className="min-w-0">
                            <div className="flex items-center gap-2">
                              <p className="truncate text-sm font-medium">
                                {displayName(user)}
                              </p>
                              {user.password_reset_required && (
                                <Badge variant="outline">再設定要求</Badge>
                              )}
                            </div>
                            <p className="truncate text-xs text-muted-foreground">
                              @{user.username}
                              {user.email ? ` / ${user.email}` : ""}
                            </p>
                          </div>
                        </div>
                      </TableCell>
                      <TableCell>
                        <Badge variant="outline">
                          {ROLE_LABELS[user.role || "user"] || user.role || "user"}
                        </Badge>
                      </TableCell>
                      <TableCell>
                        <Badge variant={statusVariant(user.status)}>
                          {STATUS_LABELS[user.status]}
                        </Badge>
                      </TableCell>
                      <TableCell>{formatDate(user.last_login)}</TableCell>
                      <TableCell>{formatDate(user.created_at)}</TableCell>
                      <TableCell
                        className="text-right"
                        onClick={(event) => event.stopPropagation()}
                      >
                        {busyUserId === user.id ? (
                          <Loader2 className="ml-auto size-4 animate-spin" />
                        ) : (
                          renderActions(user)
                        )}
                      </TableCell>
                    </TableRow>
                  ))}
                  {filteredUsers.length === 0 && (
                    <TableRow>
                      <TableCell colSpan={6} className="h-24 text-center">
                        該当するユーザーがいません
                      </TableCell>
                    </TableRow>
                  )}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
        )}
      </Card>

      <Sheet open={!!selectedUser} onOpenChange={(open) => !open && setSelectedUserId(null)}>
        <SheetContent className="w-full overflow-y-auto sm:max-w-xl">
          {selectedUser && (
            <>
              <SheetHeader>
                <SheetTitle className="flex items-center gap-2">
                  <Avatar className="size-8">
                    {selectedUser.avatar_url && (
                      <AvatarImage
                        src={selectedUser.avatar_url}
                        alt={`${displayName(selectedUser)}のアイコン`}
                      />
                    )}
                    <AvatarFallback className="text-xs font-medium">
                      {avatarFallback(selectedUser)}
                    </AvatarFallback>
                  </Avatar>
                  <span className="truncate">{displayName(selectedUser)}</span>
                </SheetTitle>
                <SheetDescription>
                  @{selectedUser.username} のアカウント状態とセキュリティ操作
                </SheetDescription>
              </SheetHeader>
              <div className="px-4 pb-4">
                <Tabs defaultValue="overview">
                  <TabsList>
                    <TabsTrigger value="overview">概要</TabsTrigger>
                    <TabsTrigger value="security">セキュリティ</TabsTrigger>
                    <TabsTrigger value="danger">危険操作</TabsTrigger>
                  </TabsList>
                  <TabsContent value="overview" className="mt-4 space-y-4">
                    <div className="grid gap-3 sm:grid-cols-2">
                      <Info label="ユーザー名" value={`@${selectedUser.username}`} />
                      <Info
                        label="表示名"
                        value={selectedUser.display_name || "-"}
                      />
                      <Info label="メール" value={selectedUser.email || "-"} />
                      <Info
                        label="ロール"
                        value={ROLE_LABELS[selectedUser.role || "user"] || selectedUser.role || "-"}
                      />
                      <Info
                        label="状態"
                        value={STATUS_LABELS[selectedUser.status]}
                      />
                      <Info
                        label="最終ログイン"
                        value={formatDate(selectedUser.last_login)}
                      />
                    </div>
                    <Button variant="outline" onClick={() => openEdit(selectedUser)}>
                      <Edit3 className="size-4" />
                      編集
                    </Button>
                  </TabsContent>
                  <TabsContent value="security" className="mt-4 space-y-4">
                    <div className="rounded-lg border p-3">
                      <div className="flex items-start gap-3">
                        <KeyRound className="mt-0.5 size-4 text-muted-foreground" />
                        <div className="space-y-2">
                          <div>
                            <p className="text-sm font-medium">
                              パスワード再設定リンク
                            </p>
                            <p className="text-xs text-muted-foreground">
                              管理者がパスワードを知る形にせず、ユーザー本人が新しいパスワードを設定します。
                            </p>
                          </div>
                          <Button
                            size="sm"
                            disabled={selectedUser.status !== "active"}
                            onClick={() => {
                              setResetUser(selectedUser);
                              setResetUrl("");
                            }}
                          >
                            <KeyRound className="size-3.5" />
                            再設定リンクを発行
                          </Button>
                        </div>
                      </div>
                    </div>
                  </TabsContent>
                  <TabsContent value="danger" className="mt-4 space-y-4">
                    <div className="rounded-lg border border-destructive/30 p-3">
                      <p className="text-sm font-medium text-destructive">
                        アカウント操作
                      </p>
                      <p className="mt-1 text-xs text-muted-foreground">
                        無効化はログインだけを止めます。削除は通常の一覧から外し、過去データの参照は壊しません。
                      </p>
                      <div className="mt-3 flex flex-wrap gap-2">
                        {selectedUser.status === "active" ? (
                          <Button
                            variant="outline"
                            size="sm"
                            disabled={!canDeactivate(selectedUser)}
                            onClick={() =>
                              void updateUser(selectedUser, { is_active: false })
                            }
                          >
                            <UserX className="size-3.5" />
                            無効化
                          </Button>
                        ) : (
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() =>
                              void updateUser(selectedUser, { is_active: true })
                            }
                          >
                            <RotateCcw className="size-3.5" />
                            復帰
                          </Button>
                        )}
                        <Button
                          variant="destructive"
                          size="sm"
                          disabled={
                            !canDeactivate(selectedUser) ||
                            selectedUser.status === "deleted"
                          }
                          onClick={() => {
                            setDeleteUser(selectedUser);
                            setDeleteConfirm("");
                          }}
                        >
                          <Trash2 className="size-3.5" />
                          削除
                        </Button>
                      </div>
                    </div>
                  </TabsContent>
                </Tabs>
              </div>
            </>
          )}
        </SheetContent>
      </Sheet>

      <Dialog open={recoveryOpen} onOpenChange={setRecoveryOpen}>
        <DialogContent size="2xl">
          <DialogHeader>
            <DialogTitle>削除済みユーザーの復旧</DialogTitle>
            <DialogDescription>
              削除したユーザーを復旧できます。完全削除は関連データがないユーザーだけ実行できます。
            </DialogDescription>
          </DialogHeader>
          {deletedUsers.length === 0 ? (
            <div className="rounded-lg border p-6 text-center text-sm text-muted-foreground">
              削除済みユーザーはいません
            </div>
          ) : (
            <div className="max-h-[60vh] overflow-y-auto rounded-lg border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>ユーザー</TableHead>
                    <TableHead>削除日時</TableHead>
                    <TableHead className="w-44 text-right">操作</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {deletedUsers.map((user) => (
                    <TableRow key={user.id}>
                      <TableCell>
                        <div className="flex items-center gap-3">
                          <Avatar className="size-8">
                            {user.avatar_url && (
                              <AvatarImage
                                src={user.avatar_url}
                                alt={`${displayName(user)}のアイコン`}
                              />
                            )}
                            <AvatarFallback className="text-xs font-medium">
                              {avatarFallback(user)}
                            </AvatarFallback>
                          </Avatar>
                          <div className="min-w-0">
                            <p className="truncate text-sm font-medium">
                              {displayName(user)}
                            </p>
                            <p className="truncate text-xs text-muted-foreground">
                              @{user.username}
                              {user.email ? ` / ${user.email}` : ""}
                            </p>
                          </div>
                        </div>
                      </TableCell>
                      <TableCell>{formatDate(user.deleted_at)}</TableCell>
                      <TableCell className="text-right">
                        <div className="flex justify-end gap-2">
                          <Button
                            variant="outline"
                            size="sm"
                            disabled={busyUserId === user.id}
                            onClick={() => void restoreDeletedUser(user)}
                          >
                            {busyUserId === user.id ? (
                              <Loader2 className="size-3.5 animate-spin" />
                            ) : (
                              <RotateCcw className="size-3.5" />
                            )}
                            復旧
                          </Button>
                          <Button
                            variant="destructive"
                            size="sm"
                            disabled={busyUserId === user.id}
                            onClick={() => {
                              setPurgeUser(user);
                              setPurgeConfirm("");
                            }}
                          >
                            <Trash2 className="size-3.5" />
                            完全削除
                          </Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setRecoveryOpen(false)}>
              閉じる
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent size="lg">
          <DialogHeader>
            <DialogTitle>ユーザー追加</DialogTitle>
            <DialogDescription>
              作成後、必要に応じて再設定リンクを発行できます。
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="ユーザー名">
              <Input
                value={createForm.username}
                onChange={(e) =>
                  setCreateForm((prev) => ({ ...prev, username: e.target.value }))
                }
              />
            </Field>
            <Field label="表示名">
              <Input
                value={createForm.display_name}
                onChange={(e) =>
                  setCreateForm((prev) => ({
                    ...prev,
                    display_name: e.target.value,
                  }))
                }
              />
            </Field>
            <Field label="メール">
              <Input
                type="email"
                value={createForm.email}
                onChange={(e) =>
                  setCreateForm((prev) => ({ ...prev, email: e.target.value }))
                }
              />
            </Field>
            <Field label="ロール">
              <Select
                value={createForm.role}
                onValueChange={(role) =>
                  role && setCreateForm((prev) => ({ ...prev, role }))
                }
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                <SelectItem value="admin">admin</SelectItem>
                <SelectItem value="user">user</SelectItem>
                </SelectContent>
              </Select>
            </Field>
            <Field label="初期ログインパスワード">
              <Input
                type="password"
                value={createForm.password}
                onChange={(e) =>
                  setCreateForm((prev) => ({ ...prev, password: e.target.value }))
                }
              />
            </Field>
            <label className="flex items-center gap-2 pt-6 text-sm">
              <Checkbox
                checked={createForm.require_password_change}
                onCheckedChange={(checked) =>
                  setCreateForm((prev) => ({
                    ...prev,
                    require_password_change: checked === true,
                  }))
                }
              />
              初回ログイン後に変更を要求
            </label>
          </div>
          {formError && <p className="text-sm text-destructive">{formError}</p>}
          <DialogFooter>
            <Button variant="outline" onClick={() => setCreateOpen(false)}>
              キャンセル
            </Button>
            <Button disabled={busyUserId === "create"} onClick={createUser}>
              {busyUserId === "create" ? (
                <Loader2 className="size-4 animate-spin" />
              ) : (
                <UserPlus className="size-4" />
              )}
              追加
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={!!editUser} onOpenChange={(open) => !open && setEditUser(null)}>
        <DialogContent size="lg">
          <DialogHeader>
            <DialogTitle>ユーザー編集</DialogTitle>
            <DialogDescription>
              表示情報、ロール、状態をまとめて更新します。
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="表示名">
              <Input
                value={editForm.display_name}
                onChange={(e) =>
                  setEditForm((prev) => ({
                    ...prev,
                    display_name: e.target.value,
                  }))
                }
              />
            </Field>
            <Field label="メール">
              <Input
                type="email"
                value={editForm.email}
                onChange={(e) =>
                  setEditForm((prev) => ({ ...prev, email: e.target.value }))
                }
              />
            </Field>
            <Field label="ロール">
              <Select
                value={editForm.role}
                onValueChange={(role) =>
                  role && setEditForm((prev) => ({ ...prev, role }))
                }
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="admin">admin</SelectItem>
                  <SelectItem value="user">user</SelectItem>
                </SelectContent>
              </Select>
            </Field>
            <Field label="状態">
              <Select
                value={editForm.status}
                onValueChange={(status) =>
                  status && setEditForm((prev) => ({ ...prev, status }))
                }
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="active">有効</SelectItem>
                  <SelectItem value="inactive">無効</SelectItem>
                </SelectContent>
              </Select>
            </Field>
          </div>
          {formError && <p className="text-sm text-destructive">{formError}</p>}
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditUser(null)}>
              キャンセル
            </Button>
            <Button disabled={busyUserId === editUser?.id} onClick={saveEdit}>
              {busyUserId === editUser?.id ? (
                <Loader2 className="size-4 animate-spin" />
              ) : (
                <CheckCircle2 className="size-4" />
              )}
              保存
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={!!resetUser}
        onOpenChange={(open) => {
          if (!open) {
            setResetUser(null);
            setResetUrl("");
          }
        }}
      >
        <DialogContent size="lg">
          <DialogHeader>
            <DialogTitle>パスワード再設定</DialogTitle>
            <DialogDescription>
              管理者はパスワードを見ません。リンクを対象ユーザーへ渡してください。
            </DialogDescription>
          </DialogHeader>
          {resetUser && (
            <div className="space-y-3">
              <div className="rounded-lg border p-3 text-sm">
                <p className="font-medium">{displayName(resetUser)}</p>
                <p className="text-muted-foreground">@{resetUser.username}</p>
              </div>
              {resetUrl ? (
                <div className="space-y-2">
                  <Label>再設定リンク</Label>
                  <div className="flex gap-2">
                    <Input value={resetUrl} readOnly />
                    <Button size="icon" variant="outline" onClick={copyResetUrl}>
                      <Copy className="size-4" />
                    </Button>
                  </div>
                  <p className="text-xs text-muted-foreground">
                    リンクは24時間有効です。再発行すると新しいリンクを使ってください。
                  </p>
                </div>
              ) : (
                <p className="text-sm text-muted-foreground">
                  発行すると、次回ログイン時の変更要求も有効になります。
                </p>
              )}
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setResetUser(null)}>
              閉じる
            </Button>
            <Button
              disabled={!!resetUser && busyUserId === resetUser.id}
              onClick={generateResetLink}
            >
              {!!resetUser && busyUserId === resetUser.id ? (
                <Loader2 className="size-4 animate-spin" />
              ) : (
                <KeyRound className="size-4" />
              )}
              {resetUrl ? "再発行" : "発行"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={!!deleteUser}
        onOpenChange={(open) => {
          if (!open) {
            setDeleteUser(null);
            setDeleteConfirm("");
          }
        }}
      >
        <DialogContent size="lg">
          <DialogHeader>
            <DialogTitle>ユーザーを削除</DialogTitle>
            <DialogDescription>
              ログイン不可にして通常の運用対象から外します。復旧画面から戻すこともできます。
            </DialogDescription>
          </DialogHeader>
          {deleteUser && (
            <div className="space-y-3">
              <div className="rounded-lg border border-destructive/30 p-3 text-sm">
                <p className="font-medium">{displayName(deleteUser)}</p>
                <p className="text-muted-foreground">@{deleteUser.username}</p>
                <Separator className="my-3" />
                <ul className="list-disc space-y-1 pl-5 text-xs text-muted-foreground">
                  <li>このユーザーはログインできなくなります。</li>
                  <li>通常のユーザー一覧には表示されなくなります。</li>
                  <li>過去のタスク、工数、会話履歴の参照は壊しません。</li>
                </ul>
              </div>
              <Field label={`確認のため ${deleteUser.username} と入力`}>
                <Input
                  value={deleteConfirm}
                  onChange={(e) => setDeleteConfirm(e.target.value)}
                />
              </Field>
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteUser(null)}>
              キャンセル
            </Button>
            <Button
              variant="destructive"
              disabled={
                !deleteUser ||
                deleteConfirm !== deleteUser.username ||
                busyUserId === deleteUser.id
              }
              onClick={deleteSelectedUser}
            >
              {deleteUser && busyUserId === deleteUser.id ? (
                <Loader2 className="size-4 animate-spin" />
              ) : (
                <Trash2 className="size-4" />
              )}
              削除する
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={!!purgeUser}
        onOpenChange={(open) => {
          if (!open) {
            setPurgeUser(null);
            setPurgeConfirm("");
          }
        }}
      >
        <DialogContent size="lg">
          <DialogHeader>
            <DialogTitle>ユーザーを完全削除</DialogTitle>
            <DialogDescription>
              関連データがない削除済みユーザーだけ、データベースから完全に削除します。
            </DialogDescription>
          </DialogHeader>
          {purgeUser && (
            <div className="space-y-3">
              <div className="rounded-lg border border-destructive/30 p-3 text-sm">
                <p className="font-medium">{displayName(purgeUser)}</p>
                <p className="text-muted-foreground">@{purgeUser.username}</p>
                <Separator className="my-3" />
                <ul className="list-disc space-y-1 pl-5 text-xs text-muted-foreground">
                  <li>この操作は取り消せません。</li>
                  <li>関連データが残っている場合は完全削除を拒否します。</li>
                  <li>通常の削除ではなく、ユーザーレコード自体を削除します。</li>
                </ul>
              </div>
              <Field label={`確認のため ${purgeUser.username} と入力`}>
                <Input
                  value={purgeConfirm}
                  onChange={(e) => setPurgeConfirm(e.target.value)}
                />
              </Field>
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setPurgeUser(null)}>
              キャンセル
            </Button>
            <Button
              variant="destructive"
              disabled={
                !purgeUser ||
                purgeConfirm !== purgeUser.username ||
                busyUserId === purgeUser.id
              }
              onClick={purgeDeletedUser}
            >
              {purgeUser && busyUserId === purgeUser.id ? (
                <Loader2 className="size-4 animate-spin" />
              ) : (
                <Trash2 className="size-4" />
              )}
              完全削除する
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

function Field({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <div className="space-y-1.5">
      <Label className="text-xs text-muted-foreground">{label}</Label>
      {children}
    </div>
  );
}

function Info({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border p-3">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="mt-1 text-sm font-medium">{value}</p>
    </div>
  );
}
