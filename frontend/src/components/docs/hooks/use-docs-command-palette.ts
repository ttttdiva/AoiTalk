"use client";

import {
  createContext,
  createElement,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import type { DocsField, DocsNode, DocsSupertag } from "../types";
import type {
  DocsAiCommand,
  DocsCommandMode,
  DocsSupertagTool,
  SearchView,
} from "../docs-workspace-shared";

export const DOCS_COMMAND_OPEN_EVENT = "docs-command-open";

export function requestDocsCommand(mode: DocsCommandMode = { kind: "root" }) {
  if (typeof window === "undefined") return;
  window.dispatchEvent(
    new CustomEvent<DocsCommandMode>(DOCS_COMMAND_OPEN_EVENT, { detail: mode }),
  );
}

export type DocsCommandRegistration = {
  selectedNode: DocsNode | null;
  selectionCount: number;
  onOpenNode?: (nodeId: string) => void;
  tags: DocsSupertag[];
  fields: DocsField[];
  moveTargets: DocsNode[];
  nodeTools: DocsSupertagTool[];
  onAddChild: (node: DocsNode) => void;
  onOpenSplit: (node: DocsNode) => void;
  onToggleCheckbox: (node: DocsNode) => void;
  onApplyTag: (node: DocsNode, tag: DocsSupertag) => void;
  onMove: (node: DocsNode, target: DocsNode, leaveReference: boolean) => void;
  onSetView: (node: DocsNode, view: SearchView) => void;
  onSetField: (node: DocsNode, field: DocsField, value: string) => void;
  onRunAi: (node: DocsNode, command: DocsAiCommand) => void;
  onGoBack: (node: DocsNode) => void;
};

type DocsCommandRegistry = {
  register: (value: DocsCommandRegistration) => symbol;
  update: (token: symbol, value: DocsCommandRegistration) => void;
  unregister: (token: symbol) => void;
};

const DocsCommandRegistryContext = createContext<DocsCommandRegistry | null>(
  null,
);
const DocsCommandActiveContext = createContext<DocsCommandRegistration | null>(
  null,
);

export function DocsCommandProvider({ children }: { children: ReactNode }) {
  const [registrations, setRegistrations] = useState(
    () => new Map<symbol, DocsCommandRegistration>(),
  );

  const register = useCallback((value: DocsCommandRegistration) => {
    const token = Symbol("docs-command-registration");
    setRegistrations((current) => {
      const next = new Map(current);
      next.set(token, value);
      return next;
    });
    return token;
  }, []);

  const update = useCallback(
    (token: symbol, value: DocsCommandRegistration) => {
      setRegistrations((current) => {
        if (!current.has(token)) return current;
        const next = new Map(current);
        next.set(token, value);
        return next;
      });
    },
    [],
  );

  const unregister = useCallback((token: symbol) => {
    setRegistrations((current) => {
      if (!current.has(token)) return current;
      const next = new Map(current);
      next.delete(token);
      return next;
    });
  }, []);

  const values = Array.from(registrations.values());
  const active = values[values.length - 1] ?? null;

  const registry = useMemo(
    () => ({ register, update, unregister }),
    [register, unregister, update],
  );

  return createElement(
    DocsCommandRegistryContext.Provider,
    { value: registry },
    createElement(
      DocsCommandActiveContext.Provider,
      { value: active },
      children,
    ),
  );
}

export function useDocsCommandContext() {
  return useContext(DocsCommandActiveContext);
}

export function useRegisterDocsCommand(value: DocsCommandRegistration) {
  const registry = useContext(DocsCommandRegistryContext);
  const tokenRef = useRef<symbol | null>(null);
  const valueRef = useRef(value);
  const register = registry?.register;
  const update = registry?.update;
  const unregister = registry?.unregister;

  useEffect(() => {
    valueRef.current = value;
  }, [value]);

  useEffect(() => {
    if (!register || !unregister) return;
    const token = register(valueRef.current);
    tokenRef.current = token;
    return () => {
      unregister(token);
      if (tokenRef.current === token) tokenRef.current = null;
    };
  }, [register, unregister]);

  useEffect(() => {
    const token = tokenRef.current;
    if (token && update) update(token, value);
  }, [update, value]);
}
