import { findNodeHandle, Platform, type TextInput } from "react-native";
import { getDocsKeyboardModule } from "../../../modules/docs-keyboard";

/** Called only after native onFocus for an explicit programmatic Docs edit. */
export function showOutlineInputKeyboard(input: TextInput | { getNativeTag(): number | null }): () => void {
  if (Platform.OS !== "android") return () => undefined;
  const tag = "getNativeTag" in input ? input.getNativeTag() : findNodeHandle(input);
  if (tag == null) return () => undefined;
  try {
    const native = getDocsKeyboardModule();
    const token = native.showForInput(tag, process.env.EXPO_PUBLIC_DOCS_INPUT_METRICS === "1");
    let cancelled = false;
    return () => {
      if (cancelled) return;
      cancelled = true;
      try { native.cancel(token); } catch {
        // The native runtime may already have been destroyed during unmount.
      }
    };
  } catch {
    // A stale development client may lack the local module. Do not disturb
    // focus/composition or pretend that another JS focus would show the IME.
    console.warn("[DocsInputIme] Native keyboard module is unavailable; rebuild the Android app.");
    return () => undefined;
  }
}
