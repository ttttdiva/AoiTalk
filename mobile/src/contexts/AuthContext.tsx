/**
 * 認証コンテキスト — ログイン状態管理
 */

import React, {
  createContext,
  useContext,
  useState,
  useEffect,
  useCallback,
  useRef,
} from "react";
import {
  getAuthMode,
  getToken,
  saveAuthMode,
  saveToken,
  removeToken,
  decodeTokenPayload,
  inspectTokenPayload,
  saveApiUrl,
  saveAuthActor,
  readAuthActor,
  removeAuthActor,
} from "../lib/auth";
import type { AuthMode } from "../lib/auth";
import {
  fetchApiAtServerFingerprint,
  clearApiUrlCache,
  getConfiguredApiServerFingerprint,
  normalizeApiServerFingerprint,
  tryRefreshToken,
  subscribeAuthInvalidated,
} from "../lib/api-client";
import { clearFilesApiCaches } from "../lib/files-api";
import { filesLocationCache } from "../lib/files-location-cache";
import { clearLocalSyncCache } from "../repositories/sync-cache";
import { runAuthScopeTransition, runSync } from "../sync/engine";
import { readAuthRecoveryInventory } from "../lib/auth-recovery";
import type { AuthResult, UserInfo } from "../types/api";

export type AuthState =
  | "anonymous"
  | "authenticated"
  | "reauth_required"
  | "reauth_required_unknown";
export type ActualAuthMode = Exclude<AuthMode, "signed_out">;

export interface AuthContextValue {
  isLoading: boolean;
  isAuthenticated: boolean;
  isAnonymous: boolean;
  /** True after a terminal 401 until the user signs in again. */
  reauthRequired: boolean;
  /** UI-safe state that distinguishes anonymous mode from an expired session. */
  authState: AuthState;
  /** The account mode before validity of the current credential is considered. */
  actualAuthMode: ActualAuthMode;
  /** Whether a credential is currently stored; the credential itself is never exposed. */
  tokenPresent: boolean;
  /** True when a persisted authenticated mode has no attributable actor metadata. */
  reauthIdentityUnknown: boolean;
  canUseApp: boolean;
  user: UserInfo | null;
  /** Validate a login target without exposing the bound server fingerprint. */
  validateLoginEndpoint: (apiUrl: string) => void;
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
  const [reauthIdentityUnknown, setReauthIdentityUnknown] = useState(false);
  const [tokenPresent, setTokenPresent] = useState(false);
  const [authServerFingerprint, setAuthServerFingerprint] = useState<string | null>(null);
  const mountedRef = useRef(true);
  const lifecycleGenerationRef = useRef(0);
  const loginAttemptRef = useRef(0);

  useEffect(() => () => {
    mountedRef.current = false;
    lifecycleGenerationRef.current += 1;
    loginAttemptRef.current += 1;
  }, []);

  // fetchApi is deliberately UI-agnostic.  Listen for a terminal 401 only
  // while an account is active; failed login attempts for a signed-out user
  // must not put the app into a spurious re-auth state.
  useEffect(() => {
    if (
      !mountedRef.current
      || authMode !== "authenticated"
      || !user
      || !tokenPresent
      || reauthRequired
    ) return;
    return subscribeAuthInvalidated(() => {
      if (!mountedRef.current) return;
      loginAttemptRef.current += 1;
      setReauthRequired(true);
      setReauthIdentityUnknown(false);
      // Re-authentication is an account-preserving recovery path. Keep the
      // user identity/auth scope in state while removing only the invalid
      // credential; the same-user login can therefore replay its outbox.
      setTokenPresent(false);
      // Drop the credential so background sync cannot repeatedly submit with
      // a known-invalid JWT.  Keep `user`/auth scope in React state so the
      // pending outbox remains attributable and same-user login can replay it.
      void runAuthScopeTransition(async () => {
        // Persist the actor before deleting the credential so a process death
        // can restore the same-account re-authentication state.
        if (authServerFingerprint) {
          await saveAuthActor(user, authServerFingerprint).catch(() => undefined);
        }
        await removeToken();
      }).catch(() => {
        // SecureStore failures must not surface as an unhandled rejection.
        // Restore the diagnostic flag if the invalid credential could not be
        // removed, while keeping the explicit re-auth state visible.
        void getToken()
          .then((token) => setTokenPresent(Boolean(token)))
          .catch(() => setTokenPresent(true));
      });
    });
  }, [authMode, authServerFingerprint, reauthRequired, tokenPresent, user]);

  // 起動時にトークン確認
  useEffect(() => {
    let cancelled = false;
    const generation = ++lifecycleGenerationRef.current;
    const isCurrent = () =>
      mountedRef.current
      && !cancelled
      && lifecycleGenerationRef.current === generation;
    const currentServer = async (): Promise<string | null> => {
      try {
        return await getConfiguredApiServerFingerprint();
      } catch {
        return null;
      }
    };
    void (async () => {
      try {
        const token = await getToken();
        if (!isCurrent()) return;
        setTokenPresent(Boolean(token));
        if (token) {
          const inspection = inspectTokenPayload(token);
          const decoded = inspection.user;
          if (decoded) {
            const actorRead = await readAuthActor();
            const serverFingerprint = await currentServer();
            if (!isCurrent()) return;
            const actorBindingMismatch = Boolean(
              actorRead.actor
              && (!serverFingerprint
                || actorRead.actor.serverFingerprint !== serverFingerprint),
            );
            if (actorBindingMismatch || !actorRead.readable) {
              // Keep the old actor metadata intact. The token belongs to a
              // different/unknown endpoint, so it must not be sent to the
              // current server or rebound to the current endpoint.
              await runAuthScopeTransition(async () => {
                if (isCurrent()) await removeToken();
              });
              if (!isCurrent()) return;
              setTokenPresent(false);
              await saveAuthMode("authenticated");
              setUser(null);
              setAuthMode("authenticated");
              setReauthRequired(true);
              setReauthIdentityUnknown(true);
              setAuthServerFingerprint(
                actorRead.actor?.serverFingerprint ?? serverFingerprint,
              );
              return;
            }
            setUser(decoded);
            setAuthMode("authenticated");
            setReauthRequired(false);
            setReauthIdentityUnknown(false);
            setAuthServerFingerprint(serverFingerprint);
            await saveAuthMode("authenticated");
            if (!isCurrent()) return;
            if (serverFingerprint) {
              await saveAuthActor(decoded, serverFingerprint).catch(() => undefined);
            }
            if (inspection.expired) {
              try {
                const refreshed = await tryRefreshToken();
                if (!isCurrent()) return;
                const refreshedToken = await getToken();
                if (refreshed && refreshedToken) {
                  const refreshedUser = inspectTokenPayload(refreshedToken).user;
                  if (refreshedUser) {
                    setUser(refreshedUser);
                    setTokenPresent(true);
                    setReauthRequired(false);
                    setReauthIdentityUnknown(false);
                    if (serverFingerprint) {
                      await saveAuthActor(refreshedUser, serverFingerprint).catch(() => undefined);
                    }
                  }
                } else if (!refreshed && refreshedToken === token) {
                  await runAuthScopeTransition(async () => {
                    if (isCurrent()) await removeToken();
                  });
                  if (!isCurrent()) return;
                  setTokenPresent(false);
                  setReauthRequired(true);
                  setReauthIdentityUnknown(false);
                }
              } catch {
                // Transport/timeout/temporary refresh failures preserve the
                // initial authenticated projection for a later retry.
              }
            }
          } else {
            if (!isCurrent()) return;
            await removeToken();
            if (!isCurrent()) return;
            setTokenPresent(false);
            const serverFingerprint = await currentServer();
            if (!isCurrent()) return;
            // A stored but malformed actor/token is an unknown authenticated
            // session, not a clean guest start. Keep recovery fail-closed.
            await saveAuthMode("authenticated");
            setAuthMode("authenticated");
            setReauthRequired(true);
            setReauthIdentityUnknown(true);
            setUser(null);
            setAuthServerFingerprint(serverFingerprint);
          }
        } else {
          const storedMode = await getAuthMode();
          if (!isCurrent()) return;
          if (storedMode === "anonymous") {
            setAuthMode("anonymous");
            setReauthRequired(false);
            setReauthIdentityUnknown(false);
            setAuthServerFingerprint(null);
          } else if (storedMode === "authenticated") {
            const actorRead = await readAuthActor();
            const serverFingerprint = await currentServer();
            if (!isCurrent()) return;
            const actorMatchesServer = Boolean(
              actorRead.actor
              && serverFingerprint
              && actorRead.actor.serverFingerprint === serverFingerprint,
            );
            setAuthMode("authenticated");
            setReauthRequired(true);
            setReauthIdentityUnknown(!actorMatchesServer);
            setUser(actorMatchesServer && actorRead.actor
              ? {
                  user_id: actorRead.actor.user_id,
                  username: actorRead.actor.username,
                  role: actorRead.actor.role,
                }
              : null);
            setAuthServerFingerprint(
              actorRead.actor?.serverFingerprint ?? serverFingerprint,
            );
          } else {
            await saveAuthMode("anonymous");
            if (!isCurrent()) return;
            setAuthMode("anonymous");
            setReauthRequired(false);
            setReauthIdentityUnknown(false);
            setAuthServerFingerprint(null);
          }
        }
      } catch {
        if (!isCurrent()) return;
        // A SecureStore/auth-mode write can fail after a valid token has
        // already been read. Re-read the non-secret identity projection before
        // falling back to guest mode so an init race cannot mislabel an active
        // account or discard its scope in memory.
        const recoveredToken = await getToken().catch(() => null);
        const recoveredUser = recoveredToken
          ? decodeTokenPayload(recoveredToken, { ignoreExpiration: true })
          : null;
        if (recoveredUser) {
          const serverFingerprint = await currentServer();
          if (!isCurrent()) return;
          setUser(recoveredUser);
          setTokenPresent(true);
          setAuthMode("authenticated");
          setReauthRequired(false);
          setReauthIdentityUnknown(false);
          setAuthServerFingerprint(serverFingerprint);
          if (serverFingerprint) {
            await saveAuthActor(recoveredUser, serverFingerprint).catch(() => undefined);
          }
          await saveAuthMode("authenticated").catch(() => undefined);
        } else {
          const actorRead = await readAuthActor();
          const serverFingerprint = await currentServer();
          const actorMatchesServer = Boolean(
            actorRead.actor
            && serverFingerprint
            && actorRead.actor.serverFingerprint === serverFingerprint,
          );
          if (actorMatchesServer || !actorRead.readable || recoveredToken) {
            setUser(actorMatchesServer && actorRead.actor
              ? {
                  user_id: actorRead.actor.user_id,
                  username: actorRead.actor.username,
                  role: actorRead.actor.role,
                }
              : null);
            setTokenPresent(Boolean(recoveredToken));
            setAuthMode("authenticated");
            setReauthRequired(true);
            setReauthIdentityUnknown(!actorMatchesServer);
            setAuthServerFingerprint(
              actorRead.actor?.serverFingerprint ?? serverFingerprint,
            );
            await saveAuthMode("authenticated").catch(() => undefined);
            return;
          }
          await saveAuthMode("anonymous").catch(() => undefined);
          setUser(null);
          setTokenPresent(Boolean(recoveredToken));
          setAuthMode("anonymous");
          setReauthRequired(false);
          setReauthIdentityUnknown(false);
          setAuthServerFingerprint(null);
        }
      } finally {
        if (isCurrent()) setIsLoading(false);
      }
    })();
    return () => {
      cancelled = true;
      if (lifecycleGenerationRef.current === generation) lifecycleGenerationRef.current += 1;
    };
  }, []);

  const validateLoginEndpoint = useCallback(
    (apiUrl: string) => {
      if (authMode !== "authenticated") return;
      const targetFingerprint = normalizeApiServerFingerprint(apiUrl);
      if (!authServerFingerprint) {
        throw new Error(
          "現在の認証接続先を確認できないため、元の接続先で再試行してください。",
        );
      }
      if (targetFingerprint !== authServerFingerprint) {
        throw new Error(
          "接続先が変わったため、現在の認証セッションではログインできません。元の接続先で再試行してください。",
        );
      }
    },
    [authMode, authServerFingerprint],
  );

  const login = useCallback(
    async (apiUrl: string, username: string, password: string) => {
      // Run before any URL persistence or network request. LoginScreen can
      // call the same safe preflight before saving its endpoint settings.
      validateLoginEndpoint(apiUrl);
      const attempt = ++loginAttemptRef.current;
      lifecycleGenerationRef.current += 1;
      const isCurrent = () => mountedRef.current && loginAttemptRef.current === attempt;
      const loginTargetFingerprint = normalizeApiServerFingerprint(apiUrl);
      // API URL を保存
      await saveApiUrl(apiUrl);
      if (!isCurrent()) throw new Error("ログイン処理が取り消されました");
      clearApiUrlCache();

      // ログインリクエスト
      const result = await fetchApiAtServerFingerprint<AuthResult>(
        loginTargetFingerprint,
        "/api/auth/login/mobile",
        {
        method: "POST",
        body: JSON.stringify({ username, password }),
        },
      );
      if (!isCurrent()) throw new Error("ログイン処理が取り消されました");

      const resultUser = result && typeof result === "object"
        && typeof result.user_id === "string"
        && typeof result.username === "string"
        && typeof result.role === "string"
        && result.user_id.trim()
        && result.username.trim()
        && result.role.trim()
        ? {
            user_id: result.user_id,
            username: result.username,
            role: result.role,
          }
        : null;
      const accessToken = result && typeof result.access_token === "string"
        && result.access_token.trim()
        ? result.access_token
        : null;
      if (
        !result
        ||
        !result.success
        || !accessToken
        || !resultUser
      ) {
        throw new Error(result.error || "ログインに失敗しました");
      }

      if (
        authMode === "authenticated"
        && authServerFingerprint
        && loginTargetFingerprint !== authServerFingerprint
        && (
          reauthRequired
          || (user && resultUser.user_id === user.user_id)
        )
      ) {
        throw new Error(
          "接続先が変わったため、同じユーザーの保存済みデータを復元できません。元の接続先で再ログインしてください。",
        );
      }
      if (reauthRequired && !authServerFingerprint) {
        throw new Error(
          "再ログイン元の接続先を確認できないため、保存済みデータを復元できません。元の接続先で再試行してください。",
        );
      }

      const nextScope = `auth:${resultUser.user_id}`;
      let preserveUnknownRecovery = false;

      // A terminal 401 leaves the previous actor in memory so same-user
      // re-authentication can preserve its scope. Do not silently turn a
      // different account entered on that recovery screen into a cache
      // switch; the user must explicitly choose logout/guest mode first.
      if (
        reauthRequired
        && authMode === "authenticated"
        && user
        && resultUser.user_id !== user.user_id
      ) {
        throw new Error(
          "認証の有効期限が切れています。元のユーザーで再ログインしてください。別のユーザーへ切り替える場合は、先にログアウトしてください。",
        );
      }

      if (reauthRequired && reauthIdentityUnknown) {
        if (!isCurrent()) throw new Error("ログイン処理が取り消されました");
        const inventory = await readAuthRecoveryInventory();
        if (!isCurrent()) throw new Error("ログイン処理が取り消されました");
        const categories = [
          inventory.outbox,
          inventory.dirtyDocs,
          inventory.composerDrafts,
        ];
        if (categories.some((category) => !category.readable || category.pendingCount === null)) {
          throw new Error(
            "未送信データの所有者を確認できないため、再ログインを続行できません。元のユーザーで再試行してください。",
          );
        }
        for (const category of categories) {
          if (category.pendingCount === 0) continue;
          if (
            category.hasUnknownOwner
            || category.authScopes.length !== 1
            || category.authScopes[0] !== nextScope
          ) {
            throw new Error(
              "未送信データ（Docsや下書き）を元のユーザーへ結び付けられないため、別ユーザーへの切り替えを中止しました。元のユーザーで再ログインしてください。",
            );
          }
          preserveUnknownRecovery = true;
        }
      }

      const currentScope =
        authMode === "authenticated" && user
          ? `auth:${user.user_id}`
          : preserveUnknownRecovery
            ? nextScope
            : authMode;
      await runAuthScopeTransition(async () => {
        if (!isCurrent()) return;
        const nextUser = resultUser;
        // Persist the actor before any destructive cache transition. If the
        // metadata write fails, the login aborts while the old scope remains.
        await saveAuthActor(nextUser, loginTargetFingerprint);
        if (!isCurrent()) return;
        if (currentScope !== nextScope) {
          await clearLocalSyncCache();
        }
        if (!isCurrent()) return;
        filesLocationCache.clear();
        clearFilesApiCaches();
        await saveToken(accessToken);
        if (!isCurrent()) return;
        setTokenPresent(true);
        await saveAuthMode("authenticated");
        if (!isCurrent()) return;
        setUser(nextUser);
        setAuthMode("authenticated");
        setReauthRequired(false);
        setReauthIdentityUnknown(false);
        setAuthServerFingerprint(loginTargetFingerprint);
      });
      if (!isCurrent()) throw new Error("ログイン処理が取り消されました");
      // The transition has committed the new token/scope.  Start exactly one
      // sync for the restored account; runSync itself coalesces concurrent
      // focus/network requests.
      void runSync();
    },
    [authMode, authServerFingerprint, reauthIdentityUnknown, reauthRequired, user, validateLoginEndpoint],
  );

  const continueAsGuest = useCallback(async () => {
    const attempt = ++loginAttemptRef.current;
    lifecycleGenerationRef.current += 1;
    await runAuthScopeTransition(async () => {
      if (!mountedRef.current || loginAttemptRef.current !== attempt) return;
      if (authMode !== "anonymous" || user) {
        await clearLocalSyncCache();
      }
      if (!mountedRef.current || loginAttemptRef.current !== attempt) return;
      filesLocationCache.clear();
      clearFilesApiCaches();
      await removeToken();
      await removeAuthActor().catch(() => undefined);
      setTokenPresent(false);
      await saveAuthMode("anonymous");
      setUser(null);
      setAuthMode("anonymous");
      setReauthRequired(false);
      setReauthIdentityUnknown(false);
      setAuthServerFingerprint(null);
    });
  }, [authMode, user]);

  const logout = useCallback(async () => {
    const attempt = ++loginAttemptRef.current;
    lifecycleGenerationRef.current += 1;
    await runAuthScopeTransition(async () => {
      if (!mountedRef.current || loginAttemptRef.current !== attempt) return;
      if (authMode !== "anonymous" || user) {
        await clearLocalSyncCache();
      }
      if (!mountedRef.current || loginAttemptRef.current !== attempt) return;
      filesLocationCache.clear();
      clearFilesApiCaches();
      await removeToken();
      await removeAuthActor().catch(() => undefined);
      setTokenPresent(false);
      await saveAuthMode("anonymous");
      setUser(null);
      setAuthMode("anonymous");
      setReauthRequired(false);
      setReauthIdentityUnknown(false);
      setAuthServerFingerprint(null);
    });
  }, [authMode, user]);

  return (
    <AuthContext.Provider
      value={{
        isLoading,
        isAuthenticated: authMode === "authenticated" && !!user && !reauthRequired,
        isAnonymous: authMode === "anonymous" && !reauthRequired,
        reauthRequired,
        reauthIdentityUnknown,
        authState:
          authMode === "authenticated" && reauthRequired
            ? reauthIdentityUnknown
              ? "reauth_required_unknown"
              : "reauth_required"
            : authMode === "authenticated" && user
              ? "authenticated"
              : "anonymous",
        actualAuthMode: authMode === "authenticated" ? "authenticated" : "anonymous",
        tokenPresent,
        canUseApp: true,
        user,
        validateLoginEndpoint,
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
