import type { ConversationMessage } from "../../types/api";

export type VoicePhase = "off" | "idle" | "listening" | "transcribing" | "waiting" | "speaking";
export type VoiceState = {
  enabled: boolean;
  micMuted: boolean;
  speakerMuted: boolean;
  phase: VoicePhase;
  error: string | null;
};
export type VoiceRecording = { uri: string; dispose: () => Promise<void> };
export type VoiceDependencies = {
  record: (signal: AbortSignal) => Promise<VoiceRecording | null>;
  transcribe: (uri: string, signal: AbortSignal) => Promise<string>;
  speak: (text: string, character: string, signal: AbortSignal) => Promise<void>;
  send: (text: string) => Promise<void>;
  onState: (state: VoiceState) => void;
};

export const INITIAL_VOICE_STATE: VoiceState = {
  enabled: false, micMuted: true, speakerMuted: true, phase: "off", error: null,
};

/** Strip non-spoken Markdown; preserve the complete answer via bounded chunks. */
export function speechChunks(source: string, limit = 400): string[] {
  const text = source
    .replace(/```[\s\S]*?```/g, " コード部分は画面を確認してください。 ")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/https?:\/\/[^\s)]+/g, "")
    .replace(/<[^>]+>/g, "")
    .replace(/^[\s]*[>#*-]+\s*/gm, "")
    .replace(/[*_`~]/g, "")
    .trim();
  const chars = Array.from(text);
  const result: string[] = [];
  while (chars.length) {
    let size = Math.min(limit, chars.length);
    if (size < chars.length) {
      const prefix = chars.slice(0, size).join("");
      const boundary = Math.max(prefix.lastIndexOf("。"), prefix.lastIndexOf("！"), prefix.lastIndexOf("？"), prefix.lastIndexOf("\n"));
      if (boundary > limit / 3) size = Array.from(prefix.slice(0, boundary + 1)).length;
    }
    const chunk = chars.splice(0, size).join("").trim();
    if (chunk) result.push(chunk);
  }
  return result;
}

/** One foreground half-duplex loop. Epochs suppress every late native/HTTP result. */
export class VoiceSession {
  state = { ...INITIAL_VOICE_STATE };
  private epoch = 0;
  private running = false;
  private abort: AbortController | null = null;
  private seen = new Set<string>();
  private queue: Array<{ text: string; character: string }> = [];
  private busy = false;
  private awaitingReply = false;
  private replyTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(private readonly deps: VoiceDependencies) {}

  private update(patch: Partial<VoiceState>) {
    this.state = { ...this.state, ...patch };
    this.deps.onState(this.state);
  }
  private invalidate() {
    this.epoch += 1;
    this.abort?.abort();
    this.abort = null;
  }
  private clearWait() {
    this.awaitingReply = false;
    if (this.replyTimer) clearTimeout(this.replyTimer);
    this.replyTimer = null;
  }
  private waitForReply() {
    this.clearWait();
    this.awaitingReply = true;
    this.replyTimer = setTimeout(() => {
      this.clearWait();
      this.update({ micMuted: true, phase: "idle", error: "応答を待機しています。マイクはミュートしました。チャットの状態を確認してください。" });
    }, 120_000);
  }
  enable(messages: ConversationMessage[], input: boolean, output: boolean) {
    this.invalidate();
    this.clearWait();
    this.seen = new Set(messages.map((message) => message.id));
    this.queue = [];
    this.update({ enabled: true, micMuted: !input, speakerMuted: !output, phase: "idle", error: null });
    this.wake();
  }
  disable() {
    this.invalidate();
    this.clearWait();
    this.queue = [];
    this.update({ ...INITIAL_VOICE_STATE });
  }
  pauseForBackground() {
    if (!this.state.enabled) return;
    this.invalidate();
    this.clearWait();
    this.queue = [];
    this.update({ micMuted: true, speakerMuted: true, phase: "idle", error: "音声を停止しました。再開する場合はマイク・スピーカーをオンにしてください。" });
  }
  setMicMuted(muted: boolean) {
    if (muted && (this.state.phase === "listening" || this.state.phase === "transcribing")) this.invalidate();
    this.update({ micMuted: muted, error: null });
    this.wake();
  }
  setSpeakerMuted(muted: boolean) {
    if (muted) {
      this.queue = [];
      if (this.state.phase === "speaking") this.invalidate();
    }
    this.update({ speakerMuted: muted, error: null });
    this.wake();
  }
  beforeTextSend() {
    if (!this.state.enabled) return;
    this.invalidate();
    this.queue = [];
    this.waitForReply();
    this.update({ phase: "waiting", error: null });
  }
  submissionFailed() {
    this.clearWait();
    this.update({ micMuted: true, phase: this.state.enabled ? "idle" : "off" });
  }
  observe(messages: ConversationMessage[], busy: boolean, defaultCharacter: string) {
    const wasBusy = this.busy;
    this.busy = busy;
    if (!this.state.enabled) return;
    if (busy && !wasBusy && (this.state.phase === "listening" || this.state.phase === "transcribing")) {
      this.invalidate();
      this.update({ phase: "waiting" });
    }
    if (busy) return;
    let received = false;
    for (const message of messages) {
      if (this.seen.has(message.id)) continue;
      this.seen.add(message.id);
      if (message.role !== "assistant" || message.is_active_branch === false) continue;
      received = true;
      const metadata = message.metadata ?? {};
      if (!message.content.trim() || metadata.error || metadata.cancelled || metadata.interrupted
        || ["failed", "cancelled", "error"].includes(String(metadata.status))) continue;
      if (!this.state.speakerMuted) {
        const character = typeof metadata.character_name === "string" && metadata.character_name.trim()
          ? metadata.character_name : defaultCharacter;
        for (const text of speechChunks(message.content)) this.queue.push({ text, character });
      }
    }
    if (received || (wasBusy && !busy)) this.clearWait();
    if (this.queue.length && (this.state.phase === "listening" || this.state.phase === "transcribing")) this.invalidate();
    this.wake();
  }
  private wake() {
    if (this.running || !this.state.enabled) return;
    if (this.busy || this.awaitingReply) {
      this.update({ phase: "waiting" });
      return;
    }
    if (!this.queue.length && this.state.micMuted) {
      this.update({ phase: "idle" });
      return;
    }
    this.running = true;
    const epoch = this.epoch;
    const abort = new AbortController();
    this.abort = abort;
    const current = () => this.state.enabled && epoch === this.epoch && !abort.signal.aborted;
    const run = async () => {
      const speech = !this.state.speakerMuted ? this.queue.shift() : null;
      if (speech) {
        this.update({ phase: "speaking" });
        await this.deps.speak(speech.text, speech.character, abort.signal);
        return;
      }
      if (this.state.micMuted) return;
      this.update({ phase: "listening" });
      const recording = await this.deps.record(abort.signal);
      if (!recording) return;
      try {
        if (!current()) return;
        this.update({ phase: "transcribing" });
        const text = (await this.deps.transcribe(recording.uri, abort.signal)).trim();
        if (!current() || !text) return;
        if (text.length > 4000) throw new Error("認識結果が長すぎます。短く区切って話してください。");
        this.waitForReply();
        this.update({ phase: "waiting" });
        await this.deps.send(text);
      } finally {
        await recording.dispose();
      }
    };
    void run().catch((error: unknown) => {
      if (!current()) return;
      this.clearWait();
      this.queue = [];
      this.update({ micMuted: true, phase: "idle", error: error instanceof Error ? error.message : "音声処理に失敗しました。" });
    }).finally(() => {
      this.running = false;
      if (this.abort === abort) this.abort = null;
      this.wake();
    });
  }
}
