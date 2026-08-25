/**
 * 認証コンテキスト — ログイン状態管理
 */

import React, {
  createContext,
  useContext,
  useState,
  useEffect,
  useCallback,
} from "react";
import {
  getAuthMode,
  getToken,
  saveAuthMode,
  saveToken,
  removeToken,
  decodeTokenPayload,
  saveApiUrl,
} from "../lib/auth";
import {
  fetchApi,
  clearApiUrlCache,
  tryRefreshToken,
  subscribeAuthInvalidated,
} from "../lib/api-client";
import { clearFilesApiCaches } from "../lib/files-api";
import { filesLocationCache } from "../lib/files-location-cache";
import { clearLocalSyncCache } from "../repositories/sync-cache";
import { runAuthScopeTransition, runSync } from "../sync/engine";
import type { AuthResult, UserInfo } from "../types/api";

interface AuthContextValue {
  isLoading: boolean;
  isAuthenticated: boolean;
  isAnonymous: boolean;
  /** True after a terminal 401 until the user signs in again. */
  reauthRequired: boolean;
  canUseApp: boolean;
  user: UserInfo | null;
  continueAsGuest: () => Promise<void>;
  login: (apiUrl: string, username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [isLoading, setIsLoading] = useState(true);
  const [user, setUser] = useState<UserInfo | null>(null);
  const [authMode, setAuthMode] = useState<
    "signed_out" | "anonymous" | "authenticated"
  >("anonymous");
  const [reauthRequired, setReauthRequired] = useState(false);

  // fetchApi is deliberately UI-agnostic.  Listen for a terminal 401 only
  // while an account is active; failed login attempts for a signed-out user
  // must not put the app into a spurious re-auth state.
  useEffect(() => {
    if (authMode !== "authenticated" || !user) return;
    return subscribeAuthInvalidated(() => {
      setReauthRequired(true);
      // Drop the credential so background sync cannot repeatedly submit with
      // a known-invalid JWT.  Keep `user`/auth scope in React state so the
      // pending outbox remains attributable and same-user login can replay it.
      void runAuthScopeTransition(() => removeToken());
    });
  }, [authMode, user]);

  // 起動時にトークン確認
  useEffect(() => {
    (async () => {
      try {
        const token = await getToken();
        if (token) {
          let decoded = decodeTokenPayload(token, { ignoreExpiration: true });
          if (decoded) {
            setUser(decoded);
            setAuthMode("authenticated");
            await saveAuthMode("authenticated");
            if (!decodeTokenPayload(token)) {
              void tryRefreshToken().then(async (refreshed) => {
                if (!refreshed) return;
                const refreshedToken = await getToken();
                const refreshedUser = refreshedToken
                  ? decodeTokenPayload(refreshedToken, {
                      ignoreExpiration: true,
                    })
                  : null;
                if (refreshedUser) {
                  setUser(refreshedUser);
                  setAuthMode("authenticated");
                  await saveAuthMode("authenticated");
                }
              });
            }
          } else {
            await removeToken();
            await saveAuthMode("anonymous");
            setAuthMode("anonymous");
          }
        } else {
          const storedMode = await getAuthMode();
          if (storedMode === "anonymous") {
            setAuthMode("anonymous");
          } else {
            await saveAuthMode("anonymous");
            setAuthMode("anonymous");
          }
        }
      } catch {
        await saveAuthMode("anonymous").catch(() => undefined);
        setAuthMode("anonymous");
      } finally {
        setIsLoading(false);
      }
    })();
  }, []);

  const login = useCallback(
    async (apiUrl: string, username: string, password: string) => {
      // API URL を保存
      await saveApiUrl(apiUrl);
      clearApiUrlCache();

      // ログインリクエスト
      const result = await fetchApi<AuthResult>("/api/auth/login/mobile", {
        method: "POST",
        body: JSON.stringify({ username, password }),
      });

      if (!result.success || !result.access_token) {
        throw new Error(result.error || "ログインに失敗しました");
      }

      const nextScope = `auth:${result.user_id!}`;
      const currentScope =
        authMode === "authenticated" && user
          ? `auth:${user.user_id}`
          : authMode;
      await runAuthScopeTransition(async () => {
        if (currentScope !== nextScope) {
          await clearLocalSyncCache();
        }
        filesLocationCache.clear();
        clearFilesApiCaches();
        await saveToken(result.access_token!);
        await saveAuthMode("authenticated");
        setUser({
          user_id: result.user_id!,
          username: result.username!,
          role: result.role!,
        });
        setAuthMode("authenticated");
        setReauthRequired(false);
      });
      // The transition has committed the new token/scope.  Start exactly one
      // sync for the restored account; runSync itself coalesces concurrent
      // focus/network requests.
      void runSync();
    },
    [authMode, user],
  );

  const continueAsGuest = useCallback(async () => {
    await runAuthScopeTransition(async () => {
      if (authMode !== "anonymous" || user) {
        await clearLocalSyncCache();
      }
      filesLocationCache.clear();
      clearFilesApiCaches();
      await removeToken();
      await saveAuthMode("anonymous");
      setUser(null);
      setAuthMode("anonymous");
      setReauthRequired(false);
    });
  }, [authMode, user]);

  const logout = useCallback(async () => {
    await runAuthScopeTransition(async () => {
      if (authMode !== "anonymous" || user) {
        await clearLocalSyncCache();
      }
      filesLocationCache.clear();
      clearFilesApiCaches();
      await removeToken();
      await saveAuthMode("anonymous");
      setUser(null);
      setAuthMode("anonymous");
      setReauthRequired(false);
    });
  }, [authMode, user]);

  return (
    <AuthContext.Provider
      value={{
        isLoading,
        isAuthenticated:
          authMode === "authenticated" && !!user && !reauthRequired,
        isAnonymous: authMode === "anonymous",
        reauthRequired,
        canUseApp: true,
        user,
        continueAsGuest,
        login,
        logout,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
