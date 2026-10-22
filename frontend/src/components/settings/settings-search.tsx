"use client";

import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type FocusEvent,
  type KeyboardEvent,
} from "react";
import { Search as SearchIcon } from "lucide-react";
import { Input } from "@/components/ui/input";
import {
  SETTINGS_CATEGORIES,
  getVisibleSettingsTargetSearchDefinitions,
  type SettingsCategory,
  type SettingsCategoryId,
  type SettingsTargetSearchDefinition,
} from "./settings-target-registry";

export type SettingsSearchResult =
  | {
      type: "category";
      key: `category:${SettingsCategoryId}`;
      category: SettingsCategory;
    }
  | {
      type: "target";
      key: `target:${string}`;
      target: SettingsTargetSearchDefinition;
    };

export function normalizeSearchText(value: string) {
  return value.normalize("NFKC").trim().toLocaleLowerCase();
}
function includesQuery(values: readonly string[], query: string) {
  return values.some((value) => normalizeSearchText(value).includes(query));
}

function matchScore(values: readonly string[], query: string) {
  const normalizedValues = values.map(normalizeSearchText);
  if (normalizedValues.some((value) => value === query)) return 0;
  if (normalizedValues.some((value) => value.startsWith(query))) return 1;
  return 2;
}

/**
 * Search the same category and target registry that drives the page and
 * sidebar.  Physical target aliases are already merged by the registry
 * helper, so callers can render one result per disclosure.
 */
export function filterSettingsSearchResults(
  value: string,
  isAdmin: boolean,
  options: { petAvailable?: boolean } = {},
): SettingsSearchResult[] {
  const query = normalizeSearchText(value);
  if (!query) return [];

  const categories = SETTINGS_CATEGORIES.filter(
    (category) => !category.adminOnly || isAdmin,
  )
    .filter((category) =>
      includesQuery([category.label, category.description], query),
    )
    .map((category) => ({
      type: "category" as const,
      key: `category:${category.id}` as const,
      category,
      score: matchScore([category.label, category.description], query),
    }));

  const categoryById = new Map(SETTINGS_CATEGORIES.map((item) => [item.id, item]));
  const targets = getVisibleSettingsTargetSearchDefinitions(isAdmin, {
    availableCapabilities:
      options.petAvailable === false ? new Set<string>() : new Set(["entertainment"]),
  })
    .filter(
      (target) =>
        !(
          target.openDisclosure === false &&
          target.targetId === target.category
        ),
    )
    .filter((target) => {
      const category = categoryById.get(target.category);
      return includesQuery(
        [
          target.label,
          ...target.aliases,
          ...target.keywords,
          category?.label ?? "",
          category?.description ?? "",
        ],
        query,
      );
    })
    .map((target) => ({
      type: "target" as const,
      key: `target:${target.targetId}` as const,
      target,
      score: matchScore(
        [target.label, ...target.aliases, ...target.keywords],
        query,
      ),
    }));

  return [...categories, ...targets]
    .sort((left, right) => left.score - right.score)
    .map((result): SettingsSearchResult =>
      result.type === "category"
        ? { type: result.type, key: result.key, category: result.category }
        : { type: result.type, key: result.key, target: result.target },
    );
}

export function SettingsSearch({
  isAdmin,
  onSelect,
  className,
  petAvailable = false,
}: {
  isAdmin: boolean;
  onSelect: (result: SettingsSearchResult) => void;
  className?: string;
  petAvailable?: boolean;
}) {
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const results = useMemo(
    () => filterSettingsSearchResults(query, isAdmin, { petAvailable }),
    [isAdmin, petAvailable, query],
  );

  useEffect(() => {
    const handlePointerDown = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) {
        setOpen(false);
        setActiveIndex(-1);
      }
    };
    document.addEventListener("pointerdown", handlePointerDown);
    return () => document.removeEventListener("pointerdown", handlePointerDown);
  }, []);

  const selectResult = (result: SettingsSearchResult) => {
    onSelect(result);
    setOpen(false);
    setActiveIndex(-1);
    setQuery("");
  };

  const handleRootBlurCapture = (event: FocusEvent<HTMLDivElement>) => {
    if (event.relatedTarget && rootRef.current?.contains(event.relatedTarget)) {
      return;
    }
    setOpen(false);
    setActiveIndex(-1);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      setOpen(false);
      setActiveIndex(-1);
      return;
    }
    if (event.key === "ArrowDown") {
      if (!results.length) return;
      event.preventDefault();
      setOpen(true);
      setActiveIndex((current) =>
        current < results.length - 1 ? current + 1 : 0,
      );
      return;
    }
    if (event.key === "ArrowUp") {
      if (!results.length) return;
      event.preventDefault();
      setOpen(true);
      setActiveIndex((current) =>
        current > 0 ? current - 1 : results.length - 1,
      );
      return;
    }
    if (event.key === "Enter" && open && results.length) {
      event.preventDefault();
      selectResult(results[activeIndex >= 0 && activeIndex < results.length ? activeIndex : 0]);
    }
  };

  const listboxId = "settings-search-listbox";
  const selectedIndex =
    activeIndex >= 0 && activeIndex < results.length ? activeIndex : -1;
  const activeDescendant =
    open && selectedIndex >= 0
      ? `settings-search-option-${selectedIndex}`
      : undefined;

  return (
    <div
      ref={rootRef}
      data-settings-search
      onBlurCapture={handleRootBlurCapture}
      className={`relative ${className ?? ""}`.trim()}
    >
      <SearchIcon
        className="pointer-events-none absolute left-2.5 top-1/2 z-10 size-4 -translate-y-1/2 text-muted-foreground"
        aria-hidden="true"
      />
      <Input
        ref={inputRef}
        value={query}
        onChange={(event) => {
          const next = event.target.value;
          setQuery(next);
          setActiveIndex(-1);
          setOpen(Boolean(next.trim()));
        }}
        onFocus={() => {
          if (results.length) setOpen(true);
        }}
        onBlur={(event) => {
          if (event.relatedTarget && rootRef.current?.contains(event.relatedTarget)) {
            return;
          }
          setOpen(false);
          setActiveIndex(-1);
        }}
        onKeyDown={handleKeyDown}
        role="combobox"
        aria-label="設定を検索"
        aria-autocomplete="list"
        aria-controls={listboxId}
        aria-expanded={open && results.length > 0}
        aria-activedescendant={activeDescendant}
        data-settings-search-input
        placeholder="設定を検索…"
        className="h-9 pl-8 pr-3 text-sm"
      />
      {open && results.length > 0 && (
        <div
          id={listboxId}
          role="listbox"
          aria-label="設定検索候補"
          className="absolute inset-x-0 top-full z-30 mt-1 max-h-72 overflow-y-auto rounded-md border border-border bg-popover p-1 text-popover-foreground shadow-md"
        >
          {results.map((result, index) => {
            const isActive = index === selectedIndex;
            const Icon =
              result.type === "category" ? result.category.icon : result.target.icon;
            const label =
              result.type === "category"
                ? result.category.label
                : result.target.label;
            const description =
              result.type === "category"
                ? result.category.description
                : [
                    SETTINGS_CATEGORIES.find(
                      (category) => category.id === result.target.category,
                    )?.label ?? result.target.category,
                    result.target.aliases
                      .filter((alias) => alias !== result.target.label)
                      .join(" / "),
                  ]
                    .filter(Boolean)
                    .join(" · ");
            return (
              <button
                key={result.key}
                id={`settings-search-option-${index}`}
                type="button"
                role="option"
                aria-selected={isActive}
                data-settings-search-result={result.type}
                className={`flex w-full items-center gap-2 rounded-sm px-2.5 py-2 text-left text-sm outline-none transition-colors ${
                  isActive
                    ? "bg-accent text-accent-foreground"
                    : "hover:bg-accent/70"
                }`}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => selectResult(result)}
              >
                <Icon className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
                <span className="min-w-0 flex-1">
                  <span className="block truncate font-medium">{label}</span>
                  <span className="block truncate text-xs text-muted-foreground">
                    {description}
                  </span>
                </span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
