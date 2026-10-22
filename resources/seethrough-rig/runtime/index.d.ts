export type ConversationState = 'idle' | 'listening' | 'thinking' | 'speaking';
export interface RuntimeProfile { id?: 'comparison-v1' | 'conversation-v1'; seed?: number; wind?: number; /** 0..1 wind blowing hair, ribbon cords and clothes (current core only; comparison-v1 must be 0) */ windBlow?: number; idle?: boolean; breath?: boolean; blink?: boolean; saccade?: boolean; lookAt?: boolean; follow?: number }
export interface PlaybackSettings {
 version: 1;
 wind: { enabled: boolean; wind: number; windBlow: number };
 blink: { enabled: boolean; intervalMode: 'random' | 'fixed'; intervalMin: number; intervalMax: number; closeDuration: number; holdDuration: number; openDuration: number; durationRandomness: number; doubleProbability: number; doubleGap: number };
 breath: { enabled: boolean; period: number; amplitude: number };
 idle: { enabled: boolean; speed: number; headAmplitude: number; bodyAmplitude: number };
 gaze: { enabled: boolean; intervalMin: number; intervalMax: number; range: number; tracking: boolean; follow: number };
 physics: { enabled: boolean; gain: number };
}
export type PlaybackSettingsGroup = Exclude<keyof PlaybackSettings, 'version'>;
export type PlaybackSettingsPatch = {version?:1} & {[K in PlaybackSettingsGroup]?: Partial<PlaybackSettings[K]>};
export interface PlaybackSettingsField { id: string; label: string; type: 'number' | 'boolean' | 'enum'; default: number | boolean | string; parameters: string[]; min?: number; max?: number; step?: number; unit?: string; description?: string; options?: {value: string; label: string}[]; enabledWhen?: Record<string, number | boolean | string>; supported?: boolean }
export interface PlaybackSettingsGroupDefinition { id: PlaybackSettingsGroup; label: string; fields: PlaybackSettingsField[]; supported?: boolean }
export interface PlaybackSettingsDefinition { version:1; groups: PlaybackSettingsGroupDefinition[]; constraints: {kind:'ordered'; paths:[string,string]; when?:Record<string,number|boolean|string>}[] }
export declare const PLAYBACK_SETTINGS_VERSION: 1;
export declare const PLAYBACK_SETTINGS_DEFINITION: Readonly<PlaybackSettingsDefinition>;
export declare const DEFAULT_PLAYBACK_SETTINGS: Readonly<PlaybackSettings>;
export declare function validatePlaybackSettings(value: unknown): PlaybackSettingsPatch;
export declare function resolvePlaybackSettings(...layers: (PlaybackSettingsPatch | undefined)[]): PlaybackSettings;
export declare function playbackSettingsFields(capabilities: RuntimeCapabilities | null | undefined, profile?: RuntimeProfile | string): PlaybackSettingsGroupDefinition[];
export declare function filterPlaybackSettings(settings: PlaybackSettingsPatch, capabilities: RuntimeCapabilities | null | undefined): PlaybackSettingsPatch;
export declare function updatePlaybackSetting(settings: PlaybackSettingsPatch, group: PlaybackSettingsGroup, field: string, value: number | boolean | string): PlaybackSettings;
export interface PlaybackSettingsPanelOptions { settings: PlaybackSettingsPatch; capabilities: RuntimeCapabilities; onChange: (settings: PlaybackSettings) => void; disabled?: boolean; label?: string; profile?: RuntimeProfile | string; /** Display only these groups; settings and change snapshots remain complete. Omit for all groups. */ groups?: string[] }
export declare function createPlaybackSettingsPanel(container: HTMLElement, options: PlaybackSettingsPanelOptions): {update(options: Partial<PlaybackSettingsPanelOptions>): void; dispose(): void};
export interface CanvasHost { width: number; height: number; clientWidth?: number; clientHeight?: number; getContext?(type: string, options?: object): any }
export interface RuntimeOptions {
 signal?: AbortSignal; fetch?: typeof fetch;
 loadImage?: (src: string, signal?: AbortSignal) => Promise<any>;
 settings?: Record<string, any> | null; settingsUrl?: string | null;
 /** Partial host overrides; model/tuning/manifest retain immutable defaults. */
 playbackSettings?: PlaybackSettingsPatch;
 seed?: number; profile?: RuntimeProfile | 'comparison-v1' | 'conversation-v1';
 preserveDrawingBuffer?: boolean; pixelRatio?: number;
 /** Native WebGL2-equivalent context; host supplies canvas dimensions and image loader. */
 gl?: any;
 /** Full immutable commit. Omit for canonical working engine. Never infer from profile. */
 coreRef?: string; manifest?: { runtime: { abi: number; version: string; core_ref?: string; requires?: string[] }; provenance?: { commit?: string; core_ref?: string }; profile?: RuntimeProfile; playbackSettings?: PlaybackSettingsPatch };
}
export interface RuntimeCapabilities { parameters: string[]; expressions: string[]; views: string[]; mouth: boolean; mouthVowels: readonly ('a' | 'i' | 'u' | 'e' | 'o')[]; gaze: boolean; blink: boolean; physics: boolean; headYaw: boolean; deterministicSeek: true; externalMouth: true; coreRef: string; playbackSettings: readonly string[] }
export interface MouthInput { vowel?: 'a' | 'i' | 'u' | 'e' | 'o' | null; open: number; form?: number; speaking: boolean }
export declare const VERSION: '0.4.0';
export declare const SUPPORTED_MODEL_VERSIONS: readonly ['0.1.0', '0.2.0', '0.3.0', '0.4.0'];
export declare const SUPPORTED_MODEL_FEATURES: readonly ['continuous-eyes-v1', 'continuous-eyes-v2', 'continuous-eyes-v3'];
export declare const ABI: 1;
export declare const BASELINE_CORE_REF: '46c0037779fb5e59715030925d66abdc6c09ca12';
export declare function deriveCapabilities(model: any, coreRef?: string): RuntimeCapabilities;
export declare class RigRuntime {
 static create(canvas: CanvasHost, modelUrl: string, options?: RuntimeOptions): Promise<RigRuntime>;
 /** Synchronous, uses already decoded model with images. Caller retains ownership of images. */
 static fromData(canvas: CanvasHost, model: any, options?: RuntimeOptions): RigRuntime;
 constructor(canvas: CanvasHost, decodedModel: any, options?: RuntimeOptions);
 readonly model: any; readonly capabilities: RuntimeCapabilities; readonly puppet: any;
 readonly time: number; readonly disposed: boolean; readonly paused: boolean;
 step(dt: number): Record<string, number>;
 /** Fixed-step replay from zero when seeking backwards, fps changes, or controls change. Replays current controls, not a recording of historical live input. */
 frame(absoluteSeconds: number, fps?: number): Record<string, number>;
 setExpression(id: string | null): void; setGaze(x: number, y: number): void;
 setConversation(state: ConversationState, cue?: { expression?: string | null; gaze?: { x: number; y: number } }): void;
 setMouth(input: MouthInput): void; setView(name: string, options?: { zoomScale?: number }): void;
 setParameters(values: Record<string, number>): void;
 getPlaybackSettings(): PlaybackSettings;
 /** Merge live overrides; full snapshots accepted. Reject changed unsupported fields and comparison/historical edits. */
 setPlaybackSettings(values: PlaybackSettingsPatch): void;
 pause(): void; resume(): void; resize(width?: number, height?: number, pixelRatio?: number): void;
 dispose(): void;
}
