import React, { useCallback, useEffect, useRef, useState } from "react";
import { StyleSheet, View } from "react-native";
import { useRouter } from "expo-router";
import { goBackOrReplace } from "../../../lib/navigation";
import {
  Button,
  Surface,
  Switch,
  Text,
  TextInput,
  HelperText,
} from "react-native-paper";
import { ScreenHeader } from "../../../components/screen-header";
import { ScreenShell } from "../../../components/screen-primitives";
import {
  DEFAULT_API_URL,
  DEFAULT_GATEWAY_SERVICE_PATH,
  DEFAULT_PORTAL_URL,
} from "../../../constants/config";
import { getApiUrl } from "../../../lib/auth";
import {
  getCurrentNetworkInfo,
  getNetworkEndpointRoutingConfig,
} from "../../../lib/connection-routing";
import { saveEndpointSettings } from "../../../lib/endpoint-settings";
import {
  authenticatePortal,
  getPortalAuthStatus,
  isGatewayTarget,
  normalizeGatewayServiceUrl,
  revokePortalAuth,
  retryPortalRevocation,
  subscribePortalAuth,
  testPortalAuth,
  normalizePortalUrl,
} from "../../../lib/portal-auth";
import { getRequestAuthHeaders } from "../../../lib/auth-headers";
import { CredentialTargetError } from "../../../lib/credential-target";
import { normalizeApiUrl, requireConfiguredApiUrl } from "../../../lib/api-url";
import type { AuthStatus } from "@aoi-talk/mobile-gateway-auth";
import { useNetworkStore } from "../../../stores/network";

type RoutingSettingsWithPortal = {
  mode?: "direct" | "gateway";
  enabled: boolean;
  wifiSsid: string;
  wifiApiUrl: string;
  cellularApiUrl: string;
  portalUrl?: string;
};

function portalStatusLabel(status: AuthStatus["status"]): string {
  switch (status) {
    case "authenticated": return "認証済み";
    case "authorizing": return "認証画面を開いています";
    case "exchanging": return "認証を確認しています";
    case "refreshing": return "認証を更新しています";
    case "reauth_required": return "再認証が必要です";
    case "revoked": return "連携が解除されています";
    case "transient": return "一時的なエラーです";
    case "cancelled": return "認証をキャンセルしました";
    default: return "未認証";
  }
}

function formatPortalExpiry(value: number | null): string {
  return value ? `有効期限: ${new Date(value).toLocaleString()}` : "有効期限: 未取得";
}

function identityPreflightMessage(error: unknown): string {
  if (error instanceof CredentialTargetError) {
    if (error.code === "identity_mismatch" || error.code === "target_namespace_mismatch") {
      return "接続先のサーバー identity が保存済みの接続先と異なります。既存アカウントの再認証で置き換えず、接続先を確認してください。";
    }
    if (error.code === "primary_pin_missing") {
      return "先に基本接続先の identity を『接続先を確認』で登録してください。";
    }
  }
  return error instanceof Error
    ? error.message
    : "接続先の identity を確認できませんでした。";
}

export default function SettingsConnectionScreen() {
  const router = useRouter();
  const safeLifecycle = useNetworkStore((state) => state.safeLifecycle);
  const safeLifecycleError = useNetworkStore((state) => state.safeLifecycleError);
  const [apiUrl, setApiUrl] = useState(DEFAULT_API_URL);
  const [routeEnabled, setRouteEnabled] = useState(false);
  const [routeMode, setRouteMode] = useState<"direct" | "gateway">("direct");
  const [wifiSsid, setWifiSsid] = useState("");
  const [wifiApiUrl, setWifiApiUrl] = useState("");
  const [cellularApiUrl, setCellularApiUrl] = useState("");
  const [portalUrl, setPortalUrl] = useState(DEFAULT_PORTAL_URL);
  const [currentNetwork, setCurrentNetwork] = useState("Checking...");
  const [routingSaved, setRoutingSaved] = useState(false);
  const [error, setError] = useState("");
  const [hydrating, setHydrating] = useState(true);
  const [hydrationFailed, setHydrationFailed] = useState(false);
  const [saving, setSaving] = useState(false);
  const [portalStatus, setPortalStatus] = useState<AuthStatus>(() =>
    getPortalAuthStatus(),
  );
  const [portalBusy, setPortalBusy] = useState(false);
  const [identityBusy, setIdentityBusy] = useState(false);
  const mountedRef = useRef(true);
  const loadInFlightRef = useRef(false);
  const saveInFlightRef = useRef(false);
  const modeWasPersistedRef = useRef(false);
  const portalUrlWasPersistedRef = useRef(false);

  const loadSettings = useCallback(async () => {
    if (loadInFlightRef.current) return;
    loadInFlightRef.current = true;
    setHydrating(true);
    setHydrationFailed(false);
    setError("");
    try {
      const storedApiUrl = await getApiUrl();
      const routing = await getNetworkEndpointRoutingConfig();
      const routingWithPortal = routing as typeof routing & { portalUrl?: unknown };
      modeWasPersistedRef.current = routing.mode !== undefined;
      portalUrlWasPersistedRef.current = typeof routingWithPortal.portalUrl === "string";
      if (!mountedRef.current) return;
      setApiUrl(storedApiUrl || DEFAULT_API_URL);
      setRouteEnabled(routing.enabled);
      setRouteMode(routing.mode === "gateway" ? "gateway" : "direct");
      setWifiSsid(routing.wifiSsid);
      setWifiApiUrl(routing.wifiApiUrl);
      setCellularApiUrl(routing.cellularApiUrl);
      setPortalUrl(
        typeof routingWithPortal.portalUrl === "string" && routingWithPortal.portalUrl.trim()
          ? routingWithPortal.portalUrl
          : DEFAULT_PORTAL_URL,
      );

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

  useEffect(() => {
    mountedRef.current = true;
    void loadSettings();
    return () => {
      mountedRef.current = false;
    };
  }, [loadSettings]);

  const handleSaveRouting = async (): Promise<boolean> => {
    if (
      hydrating ||
      hydrationFailed ||
      saving ||
      saveInFlightRef.current
    ) {
      return false;
    }
    saveInFlightRef.current = true;
    setSaving(true);
    setError("");

    try {
      const normalizedPortalUrl = routeMode === "gateway"
        ? normalizePortalUrl(portalUrl)
        : portalUrl.trim() || DEFAULT_PORTAL_URL;
      const routingConfig: RoutingSettingsWithPortal = {
        ...(routeMode === "gateway" || modeWasPersistedRef.current
          ? { mode: routeMode as "direct" | "gateway" }
          : {}),
        enabled: routeEnabled,
        wifiSsid,
        wifiApiUrl,
        // Keep the user's direct internet URL even while Portal mode is active.
        cellularApiUrl,
        ...(routeMode === "gateway" || portalUrlWasPersistedRef.current
          ? { portalUrl: normalizedPortalUrl }
          : {}),
      };
      await saveEndpointSettings({
        apiUrl,
        routing: routingConfig,
      });
      modeWasPersistedRef.current = true;
      portalUrlWasPersistedRef.current = true;
      setRoutingSaved(true);
      setTimeout(() => setRoutingSaved(false), 2000);
      return true;
    } catch (saveError: unknown) {
      setError(
        saveError instanceof Error
          ? saveError.message
          : "接続設定を保存できませんでした",
      );
      return false;
    } finally {
      saveInFlightRef.current = false;
      setSaving(false);
    }
  };

  const canEdit = !hydrating && !hydrationFailed && !saving;
  const gatewayServiceUrl = `${portalUrl.replace(/\/+$/, "")}${DEFAULT_GATEWAY_SERVICE_PATH}`;
  const portalUrlIsValid = (() => {
    try { normalizePortalUrl(portalUrl); return true; } catch { return false; }
  })();

  useEffect(() => {
    if (routeMode !== "gateway" || !portalUrlIsValid) return;
    let target: string;
    try {
      target = normalizeGatewayServiceUrl(gatewayServiceUrl);
    } catch {
      setPortalStatus(getPortalAuthStatus());
      return;
    }
    const unsubscribe = subscribePortalAuth(setPortalStatus, target);
    return () => {
      void unsubscribe();
    };
  }, [gatewayServiceUrl, portalUrlIsValid, routeMode]);

  const runPortalAction = async (
    action: () => Promise<unknown>,
  ): Promise<void> => {
    if (portalBusy || hydrating || hydrationFailed) return;
    if (!(await handleSaveRouting())) return;
    setPortalBusy(true);
    setError("");
    try {
      await action();
      setPortalStatus(getPortalAuthStatus(gatewayServiceUrl));
    } catch (portalError: unknown) {
      setError(
        portalError instanceof Error
          ? portalError.message
          : "Gateway認証を更新できませんでした",
      );
      setPortalStatus(getPortalAuthStatus(gatewayServiceUrl));
    } finally {
      setPortalBusy(false);
    }
  };

  const runIdentityPreflight = async (target: string, gateway: boolean): Promise<void> => {
    const primaryBaseUrl = apiUrl.trim()
      ? requireConfiguredApiUrl(normalizeApiUrl(apiUrl))
      : target;
    const primaryUsesGatewayTransport = gateway
      && Boolean(apiUrl.trim())
      && isGatewayTarget(primaryBaseUrl, target);
    if (gateway && apiUrl.trim() && !primaryUsesGatewayTransport) {
      await getRequestAuthHeaders(primaryBaseUrl, {
        applicationToken: null,
        primaryBaseUrl,
        targetBaseUrl: primaryBaseUrl,
        pinPrimary: true,
      });
    }
    await getRequestAuthHeaders(target, {
      gateway,
      gatewayServiceUrl: gateway ? target : undefined,
      primaryBaseUrl,
      targetBaseUrl: target,
      applicationToken: null,
      pinPrimary: primaryUsesGatewayTransport || !gateway || !apiUrl.trim(),
    });
  };

  const verifyCurrentIdentity = async (target: string, gateway: boolean): Promise<void> => {
    if (identityBusy || hydrating || hydrationFailed) return;
    setIdentityBusy(true);
    setError("");
    try {
      // Persist the edited route before probing it so the target namespace is
      // inside the current durable routing boundary.
      if (!(await handleSaveRouting())) return;
      // This is an explicit read-only user action. No app JWT is exposed to
      // the identity challenge; the builder returns credentials only after
      // the signed proof is verified and the primary pin is accepted.
      await runIdentityPreflight(target, gateway);
      setError("");
      setRoutingSaved(true);
      setTimeout(() => setRoutingSaved(false), 2000);
    } catch (identityError: unknown) {
      setError(identityPreflightMessage(identityError));
    } finally {
      setIdentityBusy(false);
    }
  };

  return (
    <ScreenShell
      scroll
      style={styles.container}
      contentContainerStyle={styles.content}
      header={
        <ScreenHeader
          title="Server / Network"
          subtitle={`Current: ${currentNetwork}`}
          onBack={() => goBackOrReplace(router, "/(tabs)/settings")}
        />
      }
    >

      <Surface style={styles.card} elevation={0}>
        <Text style={styles.cardTitle}>基本サーバー</Text>
        {safeLifecycle === "starting" ? (
          <Text style={styles.helperText}>サーバーを起動しています…</Text>
        ) : safeLifecycle === "failed" ? (
          <HelperText type="error" visible>
            サーバーを起動できませんでした{safeLifecycleError ? ` (${safeLifecycleError})` : ""}
          </HelperText>
        ) : null}
        <TextInput
          mode="outlined"
          label="基本API URL"
          value={apiUrl}
          onChangeText={(value) => {
            if (canEdit) setApiUrl(value);
          }}
          style={styles.input}
          autoCapitalize="none"
          autoCorrect={false}
          keyboardType="url"
          disabled={hydrating || hydrationFailed || saving}
        />
        <Text style={styles.helperText}>
          Wi-Fi別切り替えをOFFにすると、この基本API URLを使います。
        </Text>
        <View style={styles.switchRow}>
          <View style={styles.switchText}>
            <Text style={styles.cardTitle}>接続先</Text>
            <Text style={styles.helperText}>
              ONにすると指定Wi-Fi名に一致した時だけWi-Fi用URLへ切り替え、それ以外では公開用URLを使います。
            </Text>
          </View>
          <Switch
            value={routeEnabled}
            onValueChange={(value) => {
              if (canEdit) setRouteEnabled(value);
            }}
            disabled={hydrating || hydrationFailed || saving}
          />
        </View>
        <View style={styles.modeRow}>
          <Button
            mode={routeMode === "direct" ? "contained" : "outlined"}
            compact
            onPress={() => {
              if (canEdit) setRouteMode("direct");
            }}
            disabled={!canEdit}
          >
            Direct
          </Button>
          <Button
            mode={routeMode === "gateway" ? "contained" : "outlined"}
            compact
            onPress={() => {
              if (canEdit) setRouteMode("gateway");
            }}
            disabled={!canEdit}
          >
            Portal / Gateway
          </Button>
        </View>
        <Text style={styles.helperText}>
          {routeMode === "gateway"
            ? "ポータルで認証後に、AoiTalkのログインを続けられます。直接接続用URLは保持されます。"
            : "インターネットは設定したAoiTalk APIへ直接接続します。ポータル設定も保持されます。"}
        </Text>
        {routeMode === "gateway" ? (
          <Text style={styles.helperText}>
            ポータルに接続できない場合も、直接接続URLへ自動切替しません。
          </Text>
        ) : null}
        <TextInput
          mode="outlined"
          label="指定Wi-Fi名"
          value={wifiSsid}
          onChangeText={(value) => {
            if (canEdit) setWifiSsid(value);
          }}
          style={styles.input}
          autoCapitalize="none"
          disabled={!routeEnabled || hydrating || hydrationFailed || saving}
        />
        <TextInput
          mode="outlined"
          label="そのWi-Fiで使うURL"
          value={wifiApiUrl}
          onChangeText={(value) => {
            if (canEdit) setWifiApiUrl(value);
          }}
          style={styles.input}
          autoCapitalize="none"
          autoCorrect={false}
          keyboardType="url"
          disabled={!routeEnabled || hydrating || hydrationFailed || saving}
        />
        <TextInput
          mode="outlined"
          label="インターネット直接接続 URL"
          value={cellularApiUrl}
          onChangeText={(value) => {
            if (canEdit) setCellularApiUrl(value);
          }}
          style={styles.input}
          autoCapitalize="none"
          autoCorrect={false}
          keyboardType="url"
          disabled={hydrating || hydrationFailed || saving}
        />
        <Text style={styles.helperText}>
          {routeEnabled
            ? "指定Wi-Fi以外では、直接接続またはポータル経由の選択に従います。直接接続URLはポータル利用時も保存されます。"
            : "Wi-Fi別切り替えはOFFです。基本API URLを使います。"}
        </Text>
        {routeMode === "gateway" ? (
          <Surface style={styles.portalCard} elevation={0}>
            <Text style={styles.cardTitle}>ポータル接続</Text>
            <TextInput
              mode="outlined"
              label="ポータル URL"
              value={portalUrl}
              onChangeText={(value) => {
                if (canEdit) setPortalUrl(value);
              }}
              style={styles.input}
              autoCapitalize="none"
              autoCorrect={false}
              keyboardType="url"
              disabled={hydrating || hydrationFailed || saving || portalBusy}
            />
            <Text style={styles.helperText}>
              ポータルURLはルートだけを指定します。AoiTalkサービスのパスは自動で選択されます。
            </Text>
            {!portalUrlIsValid ? (
              <HelperText type="error" visible>
                HTTPSのポータルURLを入力してください。
              </HelperText>
            ) : null}
            <Text style={styles.helperText}>
              認証状態: {portalStatusLabel(portalStatus.status)} · {formatPortalExpiry(portalStatus.expiresAt)}
            </Text>
            {portalStatus.refreshExpiresAt ? (
              <Text style={styles.helperText}>{`再認証期限: ${new Date(portalStatus.refreshExpiresAt).toLocaleString()}`}</Text>
            ) : null}
            <View style={styles.buttonRow}>
              <Button
                mode="contained"
                compact
                loading={portalBusy}
                disabled={portalBusy || hydrating || hydrationFailed || !portalUrlIsValid}
                onPress={() =>
                  void runPortalAction(() =>
                    authenticatePortal({ target: gatewayServiceUrl, force: true }),
                  )}
              >
                {portalStatus.status === "authenticated" ? "再認証" : "ポータル認証"}
              </Button>
              <Button
                mode="outlined"
                compact
                loading={portalBusy}
                disabled={portalBusy || hydrating || hydrationFailed || !portalUrlIsValid}
                onPress={() =>
                  void runPortalAction(async () => {
                    await testPortalAuth(gatewayServiceUrl);
                    try {
                      await runIdentityPreflight(gatewayServiceUrl, true);
                    } catch (identityError: unknown) {
                      throw new Error(identityPreflightMessage(identityError));
                    }
                  })
                }
              >
                接続テストとidentity確認
              </Button>
              {portalStatus.status !== "signed_out" ? (
                <Button
                  mode="outlined"
                  compact
                  loading={portalBusy}
                  disabled={portalBusy || hydrating || hydrationFailed || !portalUrlIsValid}
                  onPress={() =>
                    void runPortalAction(() => revokePortalAuth(gatewayServiceUrl))
                  }
                >
                  Gateway連携解除
                </Button>
              ) : null}
              {portalStatus.status === "transient" ? (
                <Button
                  mode="outlined"
                  compact
                  loading={portalBusy}
                  disabled={portalBusy || hydrating || hydrationFailed || !portalUrlIsValid}
                  onPress={() =>
                    void runPortalAction(() => retryPortalRevocation(gatewayServiceUrl))
                  }
                >
                  解除を再試行
                </Button>
              ) : null}
            </View>
          </Surface>
        ) : null}
        {routeMode === "direct" ? (
          <View style={styles.buttonRow}>
            <Button
              mode="outlined"
              compact
              loading={identityBusy}
              disabled={identityBusy || hydrating || hydrationFailed || !apiUrl.trim()}
              onPress={() => {
                let target = "";
                try {
                  target = requireConfiguredApiUrl(normalizeApiUrl(apiUrl));
                } catch (identityError: unknown) {
                  setError(identityPreflightMessage(identityError));
                  return;
                }
                void verifyCurrentIdentity(target, false);
              }}
            >
              接続先を確認
            </Button>
          </View>
        ) : null}
        {error ? (
          <HelperText type="error" visible>
            {error}
          </HelperText>
        ) : null}
        {hydrationFailed ? (
          <Button
            mode="outlined"
            onPress={() => void loadSettings()}
            disabled={hydrating}
          >
            再読み込み
          </Button>
        ) : null}
        <View style={styles.buttonRow}>
          <Button
            mode="contained"
            buttonColor="#7c3aed"
            textColor="#cdd6f4"
            onPress={handleSaveRouting}
            loading={saving}
            disabled={hydrating || hydrationFailed || saving}
          >
            {routingSaved ? "Saved" : "保存"}
          </Button>
        </View>
      </Surface>
    </ScreenShell>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#11111b" },
  content: { paddingBottom: 32 },
  card: {
    backgroundColor: "#1e1e2e",
    borderRadius: 12,
    padding: 16,
    margin: 16,
    marginBottom: 0,
  },
  portalCard: {
    backgroundColor: "#171724",
    borderRadius: 10,
    padding: 12,
    marginBottom: 12,
  },
  cardTitle: {
    color: "#7c3aed",
    fontSize: 13,
    fontWeight: "700",
    marginBottom: 10,
  },
  input: { marginBottom: 12 },
  buttonRow: { flexDirection: "row", flexWrap: "wrap", gap: 8 },
  switchRow: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
    marginBottom: 16,
  },
  switchText: { flex: 1, paddingRight: 12 },
  modeRow: { flexDirection: "row", flexWrap: "wrap", gap: 8, marginBottom: 8 },
  helperText: {
    color: "#a6adc8",
    fontSize: 13,
    lineHeight: 19,
    marginBottom: 12,
  },
});
