import React, { useEffect } from "react";
import { Platform, StyleSheet } from "react-native";
import {
  AndroidSoftInputModes,
  KeyboardAvoidingView,
  KeyboardController,
} from "react-native-keyboard-controller";
import { Portal } from "react-native-paper";

/**
 * アプリ全体の画面と Paper の Portal を同じ keyboard-aware surface に置く。
 *
 * `PaperProvider` の Portal.Host は通常、画面とは別の absolute-fill manager を
 * 持つため、画面側だけを KeyboardAvoidingView で包んでも Dialog/Portal 内の
 * TextInput はキーボードの下に残ることがある。ここで inner Portal.Host を
 * 用意し、通常画面とモーダルの双方を同じ visible frame に揃える。
 *
 * KeyboardAvoidingView は内部で Android の soft input mode を adjustResize にする。
 * OS 側の resize とこの view の height 回避を重ねないよう、Android では
 * adjustNothing に切り替えて、この view だけで画面と Portal の高さを調整する。
 */
export function KeyboardAwareLayout({ children }: { children: React.ReactNode }) {
  useEffect(() => {
    if (Platform.OS !== "android") {
      return;
    }

    // KeyboardAvoidingView sets adjustResize in its own effect; this parent effect
    // runs after it and leaves keyboard avoidance to the animated view below.
    KeyboardController.setInputMode(AndroidSoftInputModes.SOFT_INPUT_ADJUST_NOTHING);
    return () => KeyboardController.setDefaultMode();
  }, []);

  return (
    <KeyboardAvoidingView
      testID="keyboard-aware-layout"
      style={styles.container}
      behavior="height"
      keyboardVerticalOffset={0}
      enabled
    >
      <Portal.Host>{children}</Portal.Host>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1 },
});
