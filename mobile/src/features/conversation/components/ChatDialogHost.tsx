import React from "react";
import { Portal } from "react-native-paper";
import { conversationPerformanceDiagnostics } from "../performance-diagnostics";

/** Keeps closed Paper dialogs out of the mounted tree. */
export function ChatDialogHost({ children }: { children: React.ReactNode }) {
  conversationPerformanceDiagnostics.recordRender("ChatDialogHost");
  const visibleDialogs = React.Children.toArray(children).filter(
    (child) =>
      React.isValidElement<{ visible?: boolean }>(child) &&
      child.props.visible === true,
  );
  conversationPerformanceDiagnostics.increment(
    "render",
    "ChatDialogHost.visible-dialogs",
    visibleDialogs.length,
  );
  return <Portal>{visibleDialogs}</Portal>;
}
