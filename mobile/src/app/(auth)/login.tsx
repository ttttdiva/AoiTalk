/**
 * 接続画面 — 匿名開始を優先し、必要なら後からサーバーログイン
 */

import React, { useCallback, useRef, useState } from "react";
import {
  View,
  StyleSheet,
  ScrollView,
  Switch,
} from "react-native";
import {
  TextInput,
  Button,
  Text,
  Surface,
  HelperText,
  Divider,
} from "react-native-paper";
import { useRouter } from "expo-router";
import { useAuth } from "../../contexts/AuthContext";
import {
  DEFAULT_API_URL,
  DEFAULT_GATEWAY_SERVICE_PATH,
  DEFAULT_PORTAL_URL,
} from "../../constants/config";
import { getApiUrl } from "../../lib/auth";
import {
  getCurrentNetworkInfo,
  getNetworkEndpointRoutingConfig,
  saveNetworkEndpointRoutingConfig,
} from "../../lib/connection-routing";
import {
  clearApiUrlCache,
  isApiConnectionError,
  isApiHttpError,
  isApiTimeoutError,
} from "../../lib/api-client";
import { normalizeApiUrl, requireConfiguredApiUrl } from "../../lib/api-url";
import { saveEndpointSettings } from "../../lib/endpoint-settings";
import { normalizePortalUrl } from "../../lib/portal-auth";
import {
  bootstrapPrimaryIdentity,
  getRequestAuthHeaders,
} from "../../lib/auth-headers";
import { CredentialTargetError } from "../../lib/credential-target";
import { useNetworkStore } from "../../stores/network";

const SAFE_AUTH_RECOVERY_MESSAGES = new Set([
  "現在の認証接続先を確認できないため、元の接続先で再試行してください。",
  "接続先が変わったため、現在の認証セッションではログインできません。元の接続先で再試行してください。",
  "接続先が変わったため、同じユーザーの保存済みデータを復元できません。元の接続先で再ログインしてください。",
  "再ログイン元の接続先を確認できないため、保存済みデータを復元できません。元の接続先で再試行してください。",
  "認証の有効期限が切れています。元のユーザーで再ログインしてください。別のユーザーへ切り替える場合は、先にログアウトしてください。",
  "未送信データの所有者を確認できないため、再ログインを続行できません。元のユーザーで再試行してください。",
  "未送信データ（Docsや下書き）を元のユーザーへ結び付けられないため、別ユーザーへの切り替えを中止しました。元のユーザーで再ログインしてください。",
]);

async function getEffectiveLoginApiUrl(
  fallbackApiUrl: string,
  routing: {
    mode?: "direct" | "gateway";
    enabled: boolean;
    wifiSsid: string;
    wifiApiUrl: string;
    cellularApiUrl: string;
  },
): Promise<string> {
  if (routing.mode === "gateway") return fallbackApiUrl;
  if (!routing.enabled) return fallbackApiUrl;

  let network: Awaited<ReturnType<typeof getCurrentNetworkInfo>>;
  try {
    network = await getCurrentNetworkInfo();
  } catch {
    network = { type: "unknown", ssid: null, connected: null };
  }

  const matchingWifi =
    network.connected === true
    && network.type === "wifi"
    && Boolean(routing.wifiSsid.trim())
    && routing.wifiSsid.trim() === (network.ssid ?? "").trim();
  const selectedApiUrl =
    matchingWifi && routing.wifiApiUrl.trim()
      ? routing.wifiApiUrl
      : routing.cellularApiUrl.trim() || fallbackApiUrl;
  return requireConfiguredApiUrl(normalizeApiUrl(selectedApiUrl));
}

function getSafeLoginErrorMessage(
  error: unknown,
  isReauthRequired: boolean,
): string {
  if (isApiHttpError(error)) {
    if (error.status === 401 || error.status === 403) {
      return isReauthRequired
        ? "認証を拒否されました。同じアカウントのユーザー名とパスワードを確認して再試行してください。"
        : "認証を拒否されました。ユーザー名とパスワードを確認して再試行してください。";
    }
    return `サーバーからHTTP ${error.status}エラーが返りました。接続先を確認して再試行してください。`;
  }

  if (isApiConnectionError(error) || isApiTimeoutError(error)) {
    return "AoiTalkサーバーへ接続できませんでした。接続設定とネットワークを確認して再試行してください。";
  }

  if (error instanceof Error && SAFE_AUTH_RECOVERY_MESSAGES.has(error.message)) {
    return error.message;
  }

  if (error instanceof CredentialTargetError) {
    if (error.code === "identity_mismatch" || error.code === "target_namespace_mismatch") {
      return "接続先のサーバー identity が保存済み接続先と異なります。既存アカウントの再認証で置き換えず、接続設定を確認してください。";
    }
    if (error.code === "primary_pin_missing") {
      return "ログイン前に『接続先を確認』を実行して、サーバー identity を登録してください。";
    }
  }

  return "ログインに失敗しました。入力内容と接続設定を確認して再試行してください。";
}

export default function LoginScreen() {
  const router = useRouter();
  const {
    login,
    continueAsGuest,
    isAnonymous,
    isAuthenticated,
    user,
    authState,
    actualAuthMode,
    reauthRequired,
    reauthIdentityUnknown,
    validateLoginEndpoint,
  } = useAuth();
  const safeLifecycle = useNetworkStore((state) => state.safeLifecycle);
  const safeLifecycleError = useNetworkStore((state) => state.safeLifecycleError);
  const isReauthRequired =
    reauthRequired
    || authState === "reauth_required"
    || authState === "reauth_required_unknown";
  const isReauthIdentityUnknown =
    reauthIdentityUnknown
    || authState === "reauth_required_unknown"
    || (isReauthRequired && actualAuthMode === "authenticated" && !user);
  const hasKnownReauthIdentity =
    isReauthRequired
    && actualAuthMode === "authenticated"
    && !isReauthIdentityUnknown
    && !!user;

  const [apiUrl, setApiUrl] = useState(DEFAULT_API_URL);
  const [username, setUsername] = useState(
    hasKnownReauthIdentity ? user?.username ?? "" : "",
  );
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [guestLoading, setGuestLoading] = useState(false);
  const [error, setError] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [showLoginForm, setShowLoginForm] = useState(
    isAuthenticated || isReauthRequired,
  );
  const [routeEnabled, setRouteEnabled] = useState(false);
  const [wifiSsid, setWifiSsid] = useState("");
  const [wifiApiUrl, setWifiApiUrl] = useState("");
  const [cellularApiUrl, setCellularApiUrl] = useState("");
  const [routeMode, setRouteMode] = useState<"direct" | "gateway">("direct");
  const [portalUrl, setPortalUrl] = useState(DEFAULT_PORTAL_URL);
  const [identityChecking, setIdentityChecking] = useState(false);
  const [currentNetwork, setCurrentNetwork] = useState("Checking...");
  const [routingSaved, setRoutingSaved] = useState(false);
  const [hydrating, setHydrating] = useState(true);
  const [hydrationFailed, setHydrationFailed] = useState(false);
  const [routingSaving, setRoutingSaving] = useState(false);
  const mountedRef = useRef(true);
  const loadInFlightRef = useRef(false);
  const operationInFlightRef = useRef(false);
  const usernameEditedRef = useRef(false);
  const modeWasPersistedRef = useRef(false);
  const portalUrlWasPersistedRef = useRef(false);

  React.useEffect(() => {
    if (isReauthRequired) setShowLoginForm(true);
  }, [isReauthRequired]);

  React.useEffect(() => {
    if (
      !hasKnownReauthIdentity
      || !user?.username
      || usernameEditedRef.current
      || username !== ""
    ) {
      return;
    }
    setUsername(user.username);
  }, [hasKnownReauthIdentity, user?.username, username]);

  const loadSettings = useCallback(async () => {
    if (loadInFlightRef.current) return;
    loadInFlightRef.current = true;
    setHydrating(true);
    setHydrationFailed(false);
    setError("");
    try {
      const stored = await getApiUrl();
      const routing = await getNetworkEndpointRoutingConfig();
      modeWasPersistedRef.current = routing.mode !== undefined;
      portalUrlWasPersistedRef.current = typeof (routing as typeof routing & { portalUrl?: unknown }).portalUrl === "string";
      if (!mountedRef.current) return;
      if (stored) setApiUrl(stored);
      setRouteEnabled(routing.enabled);
      setRouteMode(routing.mode === "gateway" ? "gateway" : "direct");
      setWifiSsid(routing.wifiSsid);
      setWifiApiUrl(routing.wifiApiUrl);
      setCellularApiUrl(routing.cellularApiUrl);
      const configuredPortalUrl = (routing as typeof routing & { portalUrl?: unknown }).portalUrl;
      if (typeof configuredPortalUrl === "string" && configuredPortalUrl.trim()) {
        setPortalUrl(configuredPortalUrl);
      }
      try {
        const network = await getCurrentNetworkInfo();
        if (!mountedRef.current) return;
        setCurrentNetwork(
          network.type === "wifi"
            ? `Wi-Fi${network.ssid ? `: ${network.ssid}` : ""}`
            : network.type,
        );
      } catch {
        if (mountedRef.current) setCurrentNetwork("unknown");
      }
    } catch (loadError: unknown) {
      if (mountedRef.current) {
        setHydrationFailed(true);
        setError(
          loadError instanceof Error
            ? loadError.message
            : "接続設定を読み込めませんでした",
        );
      }
    } finally {
      if (mountedRef.current) setHydrating(false);
      loadInFlightRef.current = false;
    }
  }, []);

  React.useEffect(() => {
    mountedRef.current = true;
    void loadSettings();
    return () => {
      mountedRef.current = false;
    };
  }, [loadSettings]);

  const saveRouting = async () => {
    let normalizedWifiApiUrl = wifiApiUrl;
    let normalizedCellularApiUrl = cellularApiUrl;
    if (routeEnabled) {
      normalizedWifiApiUrl = normalizeApiUrl(wifiApiUrl);
      normalizedCellularApiUrl = normalizeApiUrl(cellularApiUrl);
      if (normalizedWifiApiUrl) {
        normalizedWifiApiUrl = requireConfiguredApiUrl(normalizedWifiApiUrl);
      }
      if (normalizedCellularApiUrl) {
        normalizedCellularApiUrl = requireConfiguredApiUrl(
          normalizedCellularApiUrl,
        );
      }
    }

    try {
      const routingWithPortal = {
        ...(routeMode === "gateway" || modeWasPersistedRef.current
          ? { mode: routeMode }
          : {}),
        enabled: routeEnabled,
        wifiSsid: wifiSsid.trim(),
        wifiApiUrl: normalizedWifiApiUrl,
        cellularApiUrl: normalizedCellularApiUrl,
        ...(routeMode === "gateway" || portalUrlWasPersistedRef.current
          ? { portalUrl: routeMode === "gateway" ? normalizePortalUrl(portalUrl) : portalUrl.trim() || DEFAULT_PORTAL_URL }
          : {}),
      } as Parameters<typeof saveNetworkEndpointRoutingConfig>[0];
      await saveNetworkEndpointRoutingConfig(routingWithPortal);
      modeWasPersistedRef.current = true;
      portalUrlWasPersistedRef.current = true;
    } finally {
      clearApiUrlCache();
    }
  };

  const handleSaveRouting = async () => {
    if (
      hydrating ||
      hydrationFailed ||
      loading ||
      guestLoading ||
      routingSaving ||
      operationInFlightRef.current
    ) {
      return;
    }
    operationInFlightRef.current = true;
    setRoutingSaving(true);
    setError("");
    try {
      await saveRouting();
      setRoutingSaved(true);
      setTimeout(() => setRoutingSaved(false), 2000);
    } catch (saveError: unknown) {
      setError(
        saveError instanceof Error
          ? saveError.message
          : "接続設定を保存できませんでした",
      );
    } finally {
      operationInFlightRef.current = false;
      setRoutingSaving(false);
    }
  };

  const buildRoutingSettings = () => ({
    ...(routeMode === "gateway" || modeWasPersistedRef.current
      ? { mode: routeMode }
      : {}),
    enabled: routeEnabled,
    wifiSsid,
    wifiApiUrl,
    cellularApiUrl,
    ...(routeMode === "gateway" || portalUrlWasPersistedRef.current
      ? {
          portalUrl: routeMode === "gateway"
            ? normalizePortalUrl(portalUrl)
            : portalUrl.trim() || DEFAULT_PORTAL_URL,
        }
      : {}),
  } as Parameters<typeof saveEndpointSettings>[0]["routing"]);

  const handleLogin = async () => {
    if (
      hydrating ||
      hydrationFailed ||
      loading ||
      guestLoading ||
      routingSaving ||
      operationInFlightRef.current
    ) {
      return;
    }
    if (!username.trim() || !password.trim()) {
      setError("ユーザー名とパスワードを入力してください");
      return;
    }

    let normalizedApiUrl: string;
    try {
      normalizedApiUrl = requireConfiguredApiUrl(apiUrl);
    } catch (validationError: unknown) {
      setError(
        validationError instanceof Error
          ? validationError.message
          : "API URLが未設定または無効です",
      );
      return;
    }

    operationInFlightRef.current = true;
    setLoading(true);
    setError("");

    try {
      validateLoginEndpoint(normalizedApiUrl);
      const effectiveApiUrl = await getEffectiveLoginApiUrl(normalizedApiUrl, {
        mode: routeMode,
        enabled: routeEnabled,
        wifiSsid,
        wifiApiUrl,
        cellularApiUrl,
      });
      if (effectiveApiUrl !== normalizedApiUrl) {
        validateLoginEndpoint(effectiveApiUrl);
      }
      await saveEndpointSettings({
        apiUrl: normalizedApiUrl,
        routing: buildRoutingSettings(),
      });
      await login(normalizedApiUrl, username.trim(), password);
      router.replace("/(tabs)/chat");
    } catch (e: unknown) {
      setError(getSafeLoginErrorMessage(e, isReauthRequired));
    } finally {
      operationInFlightRef.current = false;
      setLoading(false);
    }
  };

  const handleContinueAsGuest = async () => {
    if (
      isReauthRequired
      || guestLoading
      || loading
      || operationInFlightRef.current
    ) {
      return;
    }
    operationInFlightRef.current = true;
    setGuestLoading(true);
    setError("");
    try {
      await continueAsGuest();
      router.replace("/(tabs)/chat");
    } finally {
      operationInFlightRef.current = false;
      setGuestLoading(false);
    }
  };

  const handleVerifyIdentity = async () => {
    if (identityChecking || hydrating || hydrationFailed || loading || guestLoading) return;
    setIdentityChecking(true);
    setError("");
    try {
      const primary = apiUrl.trim() ? requireConfiguredApiUrl(apiUrl) : "";
      const portalRoot = normalizePortalUrl(portalUrl);
      const portalTarget = `${portalRoot.replace(/\/+$/, "")}${DEFAULT_GATEWAY_SERVICE_PATH}`;
      let verifiedPrimary: string | null = null;
      if (routeMode === "gateway") {
        if (primary.trim()) {
          validateLoginEndpoint(primary);
          const verifyPortalTarget = () => getRequestAuthHeaders(portalTarget, {
            gateway: true,
            gatewayServiceUrl: portalTarget,
            applicationToken: null,
            primaryBaseUrl: primary,
            targetBaseUrl: portalTarget,
          });
          try {
            // A known primary pin lets the portal prove the same physical
            // server while the LAN is offline. The portal proof is still
            // required; no URL or server id is trusted by itself.
            await verifyPortalTarget();
          } catch (error) {
            if (!(error instanceof CredentialTargetError) || error.code !== "primary_pin_missing") {
              throw error;
            }
            // Only an explicit primary candidate may establish the first
            // anchor. This probe carries no application JWT; a Gateway
            // credential is used only when the primary is the selected Portal service.
            await bootstrapPrimaryIdentity(primary, { gatewayServiceUrl: portalTarget });
            await verifyPortalTarget();
          }
          verifiedPrimary = primary;
        } else {
          await getRequestAuthHeaders(portalTarget, {
            gateway: true,
            gatewayServiceUrl: portalTarget,
            applicationToken: null,
            primaryBaseUrl: portalTarget,
            targetBaseUrl: portalTarget,
            pinPrimary: true,
          });
          // A first Portal connection has no direct primary URL. Promote the
          // already verified Gateway service namespace to the canonical
          // primary so the login form can proceed without inventing a LAN
          // dependency.
          verifiedPrimary = portalTarget;
        }
      } else {
        if (!primary) throw new Error("直接接続URLを入力してください。");
        validateLoginEndpoint(primary);
        // The candidate primary is not yet inside the central configured
        // target boundary. Use the explicit identity bootstrap, which sends
        // no app JWT and pins only after the signed proof is verified.
        await bootstrapPrimaryIdentity(primary);
        verifiedPrimary = primary;
      }
      if (verifiedPrimary) {
        await saveEndpointSettings({
          apiUrl: verifiedPrimary,
          routing: buildRoutingSettings(),
        });
        setApiUrl(verifiedPrimary);
      }
      setError("接続先を確認しました。ログインを続けてください。");
    } catch (identityError: unknown) {
      setError(getSafeLoginErrorMessage(identityError, isReauthRequired));
    } finally {
      setIdentityChecking(false);
    }
  };

  const canEditRouting =
    !hydrating &&
    !hydrationFailed &&
    !loading &&
    !guestLoading &&
    !routingSaving;

  return (
    <View style={styles.container}>
      <ScrollView
        contentContainerStyle={styles.scrollContent}
        keyboardShouldPersistTaps="handled"
      >
        <Surface style={styles.card} elevation={2}>
          <Text variant="headlineMedium" style={styles.title}>
            AoiTalk
          </Text>
          <Text variant="bodyMedium" style={styles.subtitle}>
            ローカルだけでも始められます。サーバー接続は後から追加できます。
          </Text>

          <Surface style={styles.modePanel} elevation={0}>
            <Text style={styles.modeTitle}>
              {isReauthRequired
                ? isReauthIdentityUnknown
                  ? "再ログインが必要です"
                  : `再ログインが必要: ${user?.username ?? "前回のアカウント"}`
                : isAuthenticated
                ? `接続中: ${user?.username ?? "server user"}`
                : isAnonymous
                  ? "現在は匿名モードです"
                  : "まず使い方を選んでください"}
            </Text>
            <Text style={styles.modeText}>
              {isReauthIdentityUnknown
                ? "前回のサーバーアカウントを確認できません。未送信データの所有者を確認できるまで、ローカルデータを保全します。元のユーザーで再ログインしてください。"
                : isReauthRequired
                  ? "サーバーの認証の有効期限が切れました。同じユーザーで再ログインすると、未送信の変更を保持したまま同期を再開できます。"
                  : "匿名モードではローカルのタスク・カレンダー・レポート・下書きチャットを使えます。"}
            </Text>
            {!isReauthRequired ? (
              <>
                <Button
                  mode="contained"
                  onPress={handleContinueAsGuest}
                  loading={guestLoading}
                  disabled={guestLoading || loading}
                  style={styles.primaryButton}
                  contentStyle={styles.buttonContent}
                >
                  {isAuthenticated
                    ? "匿名モードに切り替える"
                    : isAnonymous
                      ? "匿名モードを続ける"
                      : "匿名で始める"}
                </Button>
                <Button
                  mode="outlined"
                  onPress={() => setShowLoginForm((value) => !value)}
                  disabled={
                    guestLoading || loading || routingSaving || hydrationFailed
                  }
                  style={styles.secondaryButton}
                  contentStyle={styles.buttonContent}
                >
                  {showLoginForm
                    ? "サーバーログインを閉じる"
                    : "サーバーに接続してログイン"}
                </Button>
              </>
            ) : null}
            {isReauthRequired ? (
              <Button
                mode="outlined"
                onPress={() => router.replace("/(tabs)/chat")}
                disabled={guestLoading || loading || routingSaving}
                style={styles.secondaryButton}
                contentStyle={styles.buttonContent}
              >
                保存済みデータを確認
              </Button>
            ) : null}
            {(isAnonymous || isAuthenticated) && !showLoginForm ? (
              <Button
                mode="text"
                onPress={() => router.replace("/(tabs)/chat")}
                disabled={guestLoading || loading || routingSaving}
              >
                アプリを開く
              </Button>
            ) : null}
          </Surface>

          <Surface style={styles.routingPanel} elevation={0}>
            <View style={styles.routingHeader}>
              <View style={styles.routingHeaderText}>
                <Text style={styles.formTitle}>Network Routing</Text>
                <Text style={styles.helperText}>
                  Current network: {currentNetwork}
                </Text>
                {safeLifecycle === "starting" ? (
                  <Text style={styles.helperText}>サーバーを起動しています…</Text>
                ) : safeLifecycle === "failed" ? (
                  <HelperText type="error" visible>
                    サーバーを起動できませんでした{safeLifecycleError ? ` (${safeLifecycleError})` : ""}
                  </HelperText>
                ) : null}
              </View>
              <Switch
                value={routeEnabled}
                onValueChange={(value) => {
                  if (canEditRouting) setRouteEnabled(value);
                }}
                disabled={
                  hydrating ||
                  hydrationFailed ||
                  loading ||
                  guestLoading ||
                  routingSaving
                }
              />
            </View>
            <View style={styles.buttonRow}>
              <Button
                mode={routeMode === "direct" ? "contained" : "outlined"}
                compact
                onPress={() => {
                  if (canEditRouting) setRouteMode("direct");
                }}
                disabled={!canEditRouting}
              >
                直接接続
              </Button>
              <Button
                mode={routeMode === "gateway" ? "contained" : "outlined"}
                compact
                onPress={() => {
                  if (canEditRouting) setRouteMode("gateway");
                }}
                disabled={!canEditRouting}
              >
                ポータル経由
              </Button>
              <Button
                mode="text"
                compact
                onPress={() => router.push("/(tabs)/settings/connection")}
                disabled={hydrating || hydrationFailed || loading || guestLoading}
              >
                ポータル認証を設定
              </Button>
            </View>
            <Text style={styles.helperText}>
              {routeMode === "gateway"
                ? "ポータルで認証後に、AoiTalkのログインを続けられます。直接接続用URLは保持されます。"
                : "直接接続URLを使います。ポータル設定は保持されます。"}
            </Text>
            {routeMode === "gateway" ? (
              <Text style={styles.helperText}>
                ポータルに接続できない場合も、直接接続URLへ自動切替しません。
              </Text>
            ) : null}
            <TextInput
              label="Wi-Fi SSID"
              value={wifiSsid}
              onChangeText={(value) => {
                if (canEditRouting) setWifiSsid(value);
              }}
              mode="outlined"
              style={styles.input}
              autoCapitalize="none"
              disabled={
                !routeEnabled ||
                hydrating ||
                hydrationFailed ||
                loading ||
                guestLoading ||
                routingSaving
              }
            />
            <TextInput
              label="API URL on that Wi-Fi"
              value={wifiApiUrl}
              onChangeText={(value) => {
                if (canEditRouting) setWifiApiUrl(value);
              }}
              mode="outlined"
              style={styles.input}
              autoCapitalize="none"
              autoCorrect={false}
              keyboardType="url"
              disabled={
                !routeEnabled ||
                hydrating ||
                hydrationFailed ||
                loading ||
                guestLoading ||
                routingSaving
              }
            />
            <TextInput
              label="API URL on cellular / other networks"
              value={cellularApiUrl}
              onChangeText={(value) => {
                if (canEditRouting) setCellularApiUrl(value);
              }}
              mode="outlined"
              style={styles.input}
              autoCapitalize="none"
              autoCorrect={false}
              keyboardType="url"
              disabled={
                !routeEnabled ||
                hydrating ||
                hydrationFailed ||
                loading ||
                guestLoading ||
                routingSaving
              }
            />
            {routeMode === "gateway" ? (
              <TextInput
                label="ポータル URL"
                value={portalUrl}
                onChangeText={(value) => {
                  if (canEditRouting) setPortalUrl(value);
                }}
                mode="outlined"
                style={styles.input}
                autoCapitalize="none"
                autoCorrect={false}
                keyboardType="url"
                disabled={hydrating || hydrationFailed || loading || guestLoading || routingSaving}
              />
            ) : null}
            <View style={styles.buttonRow}>
              <Button
                mode="outlined"
                onPress={() => void handleVerifyIdentity()}
                loading={identityChecking}
                disabled={
                  hydrating ||
                  hydrationFailed ||
                  loading ||
                  guestLoading ||
                  routingSaving ||
                  identityChecking
                }
              >
                接続先を確認
              </Button>
              <Button
                mode="outlined"
                onPress={handleSaveRouting}
                loading={routingSaving}
                disabled={
                  hydrating ||
                  hydrationFailed ||
                  loading ||
                  guestLoading ||
                  routingSaving
                }
              >
                {routingSaved ? "Saved" : "Save Routing"}
              </Button>
            </View>
            {error ? (
              <HelperText type="error" visible>
                {error}
              </HelperText>
            ) : null}
            {hydrationFailed ? (
              <Button
                mode="outlined"
                onPress={() => void loadSettings()}
                disabled={hydrating || loading || guestLoading || routingSaving}
              >
                再読み込み
              </Button>
            ) : null}
          </Surface>

          {showLoginForm ? (
            <>
              <Divider style={styles.divider} />
              <Text style={styles.formTitle}>
                {isReauthRequired
                  ? hasKnownReauthIdentity
                    ? "再ログイン（同じアカウント）"
                    : "元のユーザーで再ログイン"
                  : "サーバーログイン"}
              </Text>

              {!routeEnabled ? (
                <TextInput
                  label="サーバー URL"
                  value={apiUrl}
                  onChangeText={(value) => {
                    if (canEditRouting) setApiUrl(value);
                  }}
                  mode="outlined"
                  style={styles.input}
                  autoCapitalize="none"
                  autoCorrect={false}
                  keyboardType="url"
                  left={<TextInput.Icon icon="server" />}
                  disabled={
                    hydrating ||
                    hydrationFailed ||
                    loading ||
                    guestLoading ||
                    routingSaving
                  }
                />
              ) : (
                <Text style={styles.helperText}>
                  Login uses the URL selected by Network Routing.
                </Text>
              )}

              <TextInput
                label="ユーザー名"
                value={username}
                onChangeText={(value) => {
                  usernameEditedRef.current = true;
                  setUsername(value);
                }}
                mode="outlined"
                style={styles.input}
                autoCapitalize="none"
                autoCorrect={false}
                left={<TextInput.Icon icon="account" />}
              />

              <TextInput
                label="パスワード"
                value={password}
                onChangeText={setPassword}
                mode="outlined"
                style={styles.input}
                secureTextEntry={!showPassword}
                autoCapitalize="none"
                left={<TextInput.Icon icon="lock" />}
                right={
                  <TextInput.Icon
                    icon={showPassword ? "eye-off" : "eye"}
                    onPress={() => setShowPassword(!showPassword)}
                  />
                }
                onSubmitEditing={handleLogin}
              />

              <Button
                mode="contained"
                onPress={handleLogin}
                loading={loading}
                disabled={
                  hydrating ||
                  hydrationFailed ||
                  loading ||
                  guestLoading ||
                  routingSaving
                }
                style={styles.button}
                contentStyle={styles.buttonContent}
              >
                ログイン
              </Button>
            </>
          ) : null}
        </Surface>
      </ScrollView>
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: "#11111b",
  },
  scrollContent: {
    flexGrow: 1,
    justifyContent: "center",
    padding: 24,
  },
  card: {
    padding: 24,
    borderRadius: 16,
    backgroundColor: "#1e1e2e",
  },
  title: {
    textAlign: "center",
    color: "#cdd6f4",
    fontWeight: "bold",
    marginBottom: 4,
  },
  subtitle: {
    textAlign: "center",
    color: "#a6adc8",
    marginBottom: 20,
    lineHeight: 20,
  },
  modePanel: {
    padding: 16,
    borderRadius: 12,
    backgroundColor: "#181825",
  },
  modeTitle: {
    color: "#cdd6f4",
    fontSize: 16,
    fontWeight: "600",
    marginBottom: 8,
  },
  modeText: {
    color: "#a6adc8",
    fontSize: 13,
    lineHeight: 19,
    marginBottom: 14,
  },
  divider: {
    backgroundColor: "#313244",
    marginVertical: 18,
  },
  formTitle: {
    color: "#cdd6f4",
    fontSize: 15,
    fontWeight: "600",
    marginBottom: 12,
  },
  input: {
    marginBottom: 12,
  },
  helperText: {
    color: "#a6adc8",
    fontSize: 13,
    lineHeight: 19,
  },
  routingPanel: {
    padding: 16,
    borderRadius: 12,
    backgroundColor: "#181825",
    marginTop: 14,
  },
  routingHeader: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    marginBottom: 12,
  },
  routingHeaderText: {
    flex: 1,
  },
  buttonRow: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: 8,
  },
  primaryButton: {
    borderRadius: 8,
  },
  secondaryButton: {
    borderRadius: 8,
    marginTop: 10,
    borderColor: "#585b70",
  },
  button: {
    marginTop: 8,
    borderRadius: 8,
  },
  buttonContent: {
    paddingVertical: 6,
  },
});
