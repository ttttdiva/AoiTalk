import { requireNativeModule } from "expo";

export type DocsKeyboardNativeModule = {
  supportsOutlineInput?: boolean;
  showForInput(viewTag: number, diagnostics: boolean): number;
  cancel(token: number): void;
};

export function getDocsKeyboardModule(): DocsKeyboardNativeModule {
  return requireNativeModule<DocsKeyboardNativeModule>("DocsKeyboard");
}
