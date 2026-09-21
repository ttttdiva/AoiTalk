import { createContext, useContext } from "react";

export type AppSidebarController = {
  openSidebar: () => void;
};

export const AppSidebarContext =
  createContext<AppSidebarController | null>(null);

export function useAppSidebar(): AppSidebarController | null {
  return useContext(AppSidebarContext);
}
