"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import useSWR from "swr";
import { 
  ComfyUIConfig, 
  ComfyUIWorkflow, 
  comfyuiApi 
} from "@/lib/comfyui-api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { 
  Card, 
  CardContent, 
  CardDescription, 
  CardFooter, 
  CardHeader, 
  CardTitle 
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { 
  RefreshCcw, 
  Upload, 
  Trash2, 
  CheckCircle2, 
  XCircle, 
  Settings2,
  FileJson,
  Star,
  ExternalLink,
  ChevronDown,
  ChevronUp
} from "lucide-react";
import { toast } from "sonner";
import { 
  Table, 
  TableBody, 
  TableCell, 
  TableHead, 
  TableHeader, 
  TableRow 
} from "@/components/ui/table";
import { format } from "date-fns";
import { Checkbox } from "@/components/ui/checkbox";
import { useConfirm } from "@/hooks/use-confirm";

type ComfyUIConfigResult = ComfyUIConfig & { success: boolean };

interface ComfyUIData {
  config: ComfyUIConfigResult | null;
  workflows: ComfyUIWorkflow[];
}

const EMPTY_COMFYUI_DATA: ComfyUIData = { config: null, workflows: [] };

export function ComfyUISection() {
  const confirm = useConfirm();
  const [expanded, setExpanded] = useState(false);
  // config / workflows（サーバー状態）は SWR で管理。取得タイミングは従来どおり
  // 呼び出し側（展開/各操作/接続確認）で駆動するため自動 revalidation は無効化する。
  // 接続状態(isAvailable)と入力中URL(tempUrl)は fetchData の制御に合わせて別管理する。
  const comfyRef = useRef<ComfyUIData>(EMPTY_COMFYUI_DATA);
  const { data: comfyData = EMPTY_COMFYUI_DATA, mutate: mutateComfy } = useSWR<ComfyUIData>(
    "settings/comfyui",
    async () => {
      try {
        const [configRes, workflowsRes] = await Promise.all([
          comfyuiApi.getConfig(),
          comfyuiApi.listWorkflows(),
        ]);
        const prev = comfyRef.current;
        return {
          config: configRes.success ? configRes : prev.config,
          workflows: workflowsRes.success ? workflowsRes.workflows : prev.workflows,
        };
      } catch (error) {
        console.error("Failed to fetch ComfyUI data:", error);
        toast.error("ComfyUI設定の取得に失敗しました");
        return comfyRef.current;
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
  comfyRef.current = comfyData;
  const config = comfyData.config;
  const workflows = comfyData.workflows;
  const [isAvailable, setIsAvailable] = useState<boolean | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [tempUrl, setTempUrl] = useState("");

  const fetchData = useCallback(async (checkStatus = true) => {
    setIsLoading(true);
    try {
      const result = await mutateComfy();
      const nextConfig = result?.config;
      if (nextConfig?.success) {
        setTempUrl(nextConfig.url);
      }
      if (checkStatus && nextConfig?.enabled) {
        const statusRes = await comfyuiApi.getStatus();
        if (statusRes.success) {
          setIsAvailable(statusRes.is_available);
        }
      } else {
        setIsAvailable(false);
      }
    } finally {
      setIsLoading(false);
    }
  }, [mutateComfy]);

  useEffect(() => {
    if (expanded && !config) fetchData(false);
  }, [expanded, config, fetchData]);

  const handleToggleExpanded = () => {
    setExpanded((v) => !v);
  };

  const handleEnabledChange = async (enabled: boolean) => {
    try {
      const res = await comfyuiApi.updateConfig({ enabled });
      if (res.success) {
        // 楽観的更新：更新レスポンスでローカルキャッシュの config を差し替える。
        await mutateComfy((prev = EMPTY_COMFYUI_DATA) => ({ ...prev, config: res }), {
          revalidate: false,
        });
        setIsAvailable(false);
        toast.success(enabled ? "ComfyUI連携を有効にしました" : "ComfyUI連携を無効にしました");
        if (enabled) fetchData(true);
      }
    } catch {
      toast.error("ComfyUI連携設定の更新に失敗しました");
    }
  };

  const handleUpdateConfig = async () => {
    try {
      const res = await comfyuiApi.updateConfig({ url: tempUrl });
      if (res.success) {
        await mutateComfy((prev = EMPTY_COMFYUI_DATA) => ({ ...prev, config: res }), {
          revalidate: false,
        });
        toast.success("設定を更新しました");
        fetchData(false);
      }
    } catch {
      toast.error("設定の更新に失敗しました");
    }
  };

  const handleFileUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;

    if (!file.name.endsWith(".json")) {
      toast.error("JSONファイルのみアップロード可能です");
      return;
    }

    setIsUploading(true);
    try {
      const res = await comfyuiApi.uploadWorkflow(file);
      if (res.success) {
        toast.success(`ワークフロー "${file.name}" をアップロードしました`);
        fetchData(false);
      }
    } catch {
      toast.error("アップロードに失敗しました");
    } finally {
      setIsUploading(false);
      e.target.value = "";
    }
  };

  const handleDelete = async (name: string) => {
    if (
      !(await confirm({
        description: `ワークフロー "${name}" を削除してもよろしいですか？`,
        destructive: true,
      }))
    )
      return;

    try {
      const res = await comfyuiApi.deleteWorkflow(name);
      if (res.success) {
        toast.success("削除しました");
        fetchData(false);
      }
    } catch {
      toast.error("削除に失敗しました");
    }
  };

  const handleSetDefault = async (workflowPath: string) => {
    try {
      const res = await comfyuiApi.updateConfig({ default_workflow: workflowPath });
      if (res.success) {
        toast.success("デフォルトワークフローを設定しました");
        fetchData(false);
      }
    } catch {
      toast.error("設定に失敗しました");
    }
  };

  return (
    <Card size="sm">
      <CardHeader
        className="cursor-pointer select-none"
        onClick={handleToggleExpanded}
      >
          <div className="flex items-center justify-between gap-3">
            <div>
              <CardTitle className="flex items-center gap-2 text-sm">
                <Settings2 className="size-4" />
                ComfyUI 連携設定
              </CardTitle>
              <CardDescription>
                ローカル画像生成連携と画像生成ワークフローを管理します。
              </CardDescription>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              {config?.enabled === false ? (
                <Badge variant="secondary">OFF</Badge>
              ) : isAvailable === true ? (
                <Badge variant="outline" className="bg-emerald-500/10 text-emerald-500 hover:bg-emerald-500/10 border-emerald-500/20 gap-1">
                  <CheckCircle2 className="h-3 w-3" />
                  オンライン
                </Badge>
              ) : isAvailable === false ? (
                <Badge variant="destructive" className="gap-1">
                  <XCircle className="h-3 w-3" />
                  オフライン
                </Badge>
              ) : isLoading ? (
                <Badge variant="secondary">確認中</Badge>
              ) : (
                <Badge variant="secondary">要確認</Badge>
              )}
              {expanded ? <ChevronUp className="size-4" /> : <ChevronDown className="size-4" />}
            </div>
          </div>
      </CardHeader>
      {expanded && (
        <CardContent className="space-y-4">
          {isLoading && !config ? (
            <p className="text-sm text-muted-foreground">読み込み中...</p>
          ) : (
            <>
          <div className="flex items-center justify-between rounded-md border p-3">
            <div>
              <p className="text-sm font-medium">ComfyUI連携</p>
              <p className="text-xs text-muted-foreground">
                OFFの間は設定画面でも生成処理でもComfyUIへ接続しません。
              </p>
            </div>
            <Checkbox
              checked={config?.enabled !== false}
              onCheckedChange={(checked) => handleEnabledChange(!!checked)}
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="comfyui-url">ComfyUI サーバー URL</Label>
            <div className="flex gap-2">
              <Input 
                id="comfyui-url" 
                value={tempUrl} 
                onChange={(e) => setTempUrl(e.target.value)}
                placeholder="http://127.0.0.1:8188"
              />
              <Button onClick={handleUpdateConfig}>保存</Button>
            </div>
            <p className="text-xs text-muted-foreground">
              ComfyUIを起動し、APIが有効な状態で待機させてください。
            </p>
          </div>
          <div className="rounded-md border">
          <div className="flex items-center justify-between">
            <div>
              <p className="flex items-center gap-2 px-3 pt-3 text-sm font-medium">
                <FileJson className="size-4" />
                画像生成ワークフロー
              </p>
              <p className="px-3 pt-1 text-xs text-muted-foreground">
                ComfyUI用JSONワークフローです。エージェントワークフローとは別物です。
              </p>
            </div>
            <div className="relative mr-3 mt-3">
              <Input
                type="file"
                className="absolute inset-0 opacity-0 cursor-pointer"
                accept=".json"
                onChange={handleFileUpload}
                disabled={isUploading}
              />
              <Button disabled={isUploading} size="sm" className="gap-2">
                <Upload className="h-4 w-4" />
                {isUploading ? "アップロード中..." : "ワークフローをアップロード"}
              </Button>
            </div>
          </div>
          <div className="p-3">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>名前</TableHead>
                  <TableHead>更新日時</TableHead>
                  <TableHead className="w-[100px]">状態</TableHead>
                  <TableHead className="text-right">アクション</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {workflows.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={4} className="h-24 text-center text-muted-foreground">
                      ワークフローがありません。JSONファイルをアップロードしてください。
                    </TableCell>
                  </TableRow>
                ) : (
                  workflows.map((w) => (
                    <TableRow key={w.name}>
                      <TableCell className="font-medium flex items-center gap-2">
                        <FileJson className="h-4 w-4 text-muted-foreground" />
                        {w.name}
                      </TableCell>
                      <TableCell className="text-muted-foreground text-xs">
                        {format(new Date(w.mtime * 1000), "yyyy/MM/dd HH:mm:ss")}
                      </TableCell>
                      <TableCell>
                        {w.is_default && (
                          <Badge variant="outline" className="bg-amber-500/10 text-amber-500 hover:bg-amber-500/10 border-amber-500/20 gap-1">
                            <Star className="h-3 w-3 fill-current" />
                            デフォルト
                          </Badge>
                        )}
                      </TableCell>
                      <TableCell className="text-right space-x-2">
                        {!w.is_default && (
                          <Button 
                            variant="ghost" 
                            size="icon"
                            onClick={() => handleSetDefault(w.path)}
                            title="デフォルトに設定"
                          >
                            <Star className="h-4 w-4" />
                          </Button>
                        )}
                        <Button 
                          variant="ghost" 
                          size="icon"
                          onClick={() => handleDelete(w.name)}
                          className="text-destructive hover:text-destructive hover:bg-destructive/10"
                        >
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))
                )}
              </TableBody>
            </Table>
          </div>
        <CardFooter className="flex justify-between items-center text-xs text-muted-foreground border-t bg-muted/30 p-4">
          <div className="flex items-center gap-1">
            <CheckCircle2 className="h-3 w-3" />
            ワークフローは config/comfyui_workflows に保存されます
          </div>
          <a 
            href="https://github.com/comfyanonymous/ComfyUI" 
            target="_blank" 
            rel="noopener noreferrer"
            className="flex items-center gap-1 hover:text-foreground transition-colors"
          >
            ComfyUI documentation
            <ExternalLink className="h-3 w-3" />
          </a>
        </CardFooter>
          </div>
          <div className="flex justify-end">
            <Button
              variant="outline"
              size="sm"
              onClick={() => fetchData(true)}
              disabled={isLoading || config?.enabled === false}
            >
              <RefreshCcw className={`mr-1 size-3 ${isLoading ? "animate-spin" : ""}`} />
              接続確認
            </Button>
          </div>
          </>
          )}
        </CardContent>
      )}
    </Card>
  );
}
