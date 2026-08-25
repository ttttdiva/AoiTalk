import React from "react";
import { Platform, StyleSheet } from "react-native";
import { KeyboardAvoidingView } from "react-native-keyboard-controller";
import { Portal } from "react-native-paper";

/**
 * アプリ全体の画面と Paper の Portal を同じ keyboard-aware surface に置く。
 *
 * `PaperProvider` の Portal.Host は通常、画面とは別の absolute-fill manager を
 * 持つため、画面側だけを KeyboardAvoidingView で包んでも Dialog/Portal 内の
 * TextInput はキーボードの下に残ることがある。ここで inner Portal.Host を
 * 用意し、通常画面とモーダルの双方を同じ visible frame に揃える。
 *
 * Android は app.json で adjustResize を指定しているが、edge-to-edge 等で
 * window が縮まらない端末もある。keyboard-controller の `height` behavior は
 * keyboard overlap の計算をここへ集約し、画面側の局所 KAV/StickyView は併用しない。
 * これで resize/pan と JS 側の追加 offset の責務を一か所に保つ。
 */
export function KeyboardAwareLayout({ children }: { children: React.ReactNode }) {
  return (
    <KeyboardAvoidingView
      testID="keyboard-aware-layout"
      style={styles.container}
      behavior="height"
      keyboardVerticalOffset={0}
      // Web では keyboard-controller が no-op のため、通常の flex layout を保つ。
      enabled={Platform.OS !== "web"}
    >
      <Portal.Host>{children}</Portal.Host>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1 },
});
