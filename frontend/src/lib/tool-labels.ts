const TOOL_LABELS: Record<string, string> = {
  play_music: "音楽を再生",
  search_music: "音楽を検索",
  get_weather: "天気を取得",
  web_search: "Web検索",
  search_web: "Web検索",
  deep_research: "Deep Research",
  search_files: "ファイル検索",
  generate_image: "画像生成",
  read_file: "ファイル読み取り",
  write_file: "ファイル書き込み",
  execute_command: "コマンド実行",
  shell_command: "シェルコマンド",
  execute_code: "コード実行",
  create_task: "タスクを作成",
  list_tasks: "タスクを取得",
};

export function looksLikeShellCommand(value: string): boolean {
  const text = value.trim().toLowerCase();
  if (!text) return false;
  return [
    "powershell.exe",
    "\\pwsh.exe",
    "/pwsh",
    "cmd.exe",
    " -command ",
    " -command'",
    ' -command"',
    " /c ",
    " -c ",
  ].some((marker) => text.includes(marker));
}

export function normalizeToolName(toolName: string): string {
  const normalized = toolName.trim();
  if (!normalized) return "tool";
  if (looksLikeShellCommand(normalized)) return "shell_command";
  return normalized;
}

export function getToolLabel(toolName: string): string {
  const normalized = normalizeToolName(toolName);
  if (!normalized) return "ツール";
  if (TOOL_LABELS[normalized]) return TOOL_LABELS[normalized];
  if (/search/i.test(normalized)) {
    return `${normalized.replace(/_/g, " ")} 検索`;
  }
  if (/agent|delegate|assistant/i.test(normalized)) {
    return `${normalized.replace(/_/g, " ")} への委譲`;
  }
  return normalized.replace(/_/g, " ");
}
