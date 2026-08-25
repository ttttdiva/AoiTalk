import type {
  ConversationMessage,
  ConversationSession,
} from "../../types/api";

const TITLE_MAX_LENGTH = 50;
const TITLE_CONTENT_LIMIT = 500;
const REPLACEABLE_TITLES = new Set(["", "ローカルチャット", "無題の会話"]);
const REJECTED_TITLE_FRAGMENTS = [
  "ローカルLLMはモデルを読み込み中です",
  "ローカルLLMから本文のない応答が返りました",
  "ローカルLLMの呼び出しでエラーが発生しました",
  "ツール実行の検証に失敗しました",
];

function compactText(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}

function firstUserMessage(messages: ConversationMessage[]): ConversationMessage | null {
  return (
    messages.find(
      (message) =>
        message.role === "user" && compactText(message.content).length > 0,
    ) ?? null
  );
}

function hasTitleContext(messages: ConversationMessage[]): boolean {
  let userCount = 0;
  let hasAssistant = false;
  for (const message of messages) {
    if (!compactText(message.content)) continue;
    if (message.role === "user") userCount += 1;
    if (message.role === "assistant") hasAssistant = true;
  }
  return userCount >= 1 && (hasAssistant || userCount >= 2);
}

function fallbackTitleFromFirstMessage(content: string): string {
  const compact = compactText(content);
  return compact.length > 40 ? `${compact.slice(0, 37)}...` : compact;
}

function isScenarioSession(session: ConversationSession): boolean {
  const characterName = session.character_name || "";
  return (
    /^scenario_roleplay:[^:]+:[^:]+$/.test(characterName) ||
    characterName.startsWith("scenario_") ||
    characterName.startsWith("trpg_room_") ||
    session.title?.startsWith("[シナリオ]") ||
    session.title?.startsWith("[執筆]") ||
    session.title?.startsWith("[TRPG]")
  );
}

export function shouldGenerateConversationTitle(
  session: ConversationSession | null,
  messages: ConversationMessage[],
): boolean {
  if (!session || isScenarioSession(session) || !hasTitleContext(messages)) {
    return false;
  }
  const title = compactText(session.title || "");
  const firstUser = firstUserMessage(messages);
  if (!firstUser) return false;
  return (
    REPLACEABLE_TITLES.has(title) ||
    title === fallbackTitleFromFirstMessage(firstUser.content)
  );
}

export function buildConversationTitlePrompt(
  messages: ConversationMessage[],
): string | null {
  if (!hasTitleContext(messages)) return null;
  const selected: ConversationMessage[] = [];
  let userCount = 0;
  let assistantCount = 0;
  for (const message of messages) {
    if (!compactText(message.content)) continue;
    if (message.role === "user") {
      if (userCount >= 2) continue;
      userCount += 1;
    } else if (message.role === "assistant") {
      if (assistantCount >= 2) continue;
      assistantCount += 1;
    } else {
      continue;
    }
    selected.push(message);
  }
  const excerpt = selected
    .map((message) => {
      const role = message.role === "user" ? "ユーザー" : "アシスタント";
      return `${role}: ${compactText(message.content).slice(0, TITLE_CONTENT_LIMIT)}`;
    })
    .join("\n");

  return `以下は会話の最初の1〜2往復です。この会話を履歴一覧で識別しやすい短い日本語タイトルにしてください。

条件:
- 15文字以内を目安にする
- 固有名詞や主題を優先する
- ユーザー本文の丸写しを避ける
- タイトルのみ出力する

会話:
${excerpt}`;
}

export function cleanGeneratedConversationTitle(value: string): string | null {
  let title = value.trim().split(/\r?\n/, 1)[0] ?? "";
  title = title.replace(/^(タイトル|題名)\s*[:：]\s*/, "");
  title = title.replace(/^[\s"'`「」『』【】[\]]+|[\s"'`「」『』【】[\]]+$/g, "");
  title = compactText(title);
  if (!title || title.length > TITLE_MAX_LENGTH) return null;
  if (REJECTED_TITLE_FRAGMENTS.some((fragment) => title.includes(fragment))) {
    return null;
  }
  return title;
}
