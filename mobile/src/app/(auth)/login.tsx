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
import { DEFAULT_API_URL } from "../../constants/config";
import { getApiUrl } from "../../lib/auth";
import {
  getCurrentNetworkInfo,
  getNetworkEndpointRoutingConfig,
  saveNetworkEndpointRoutingConfig,
} from "../../lib/connection-routing";
import { clearApiUrlCache } from "../../lib/api-client";
import { normalizeApiUrl, requireConfiguredApiUrl } from "../../lib/api-url";
import { saveEndpointSettings } from "../../lib/endpoint-settings";

export default function LoginScreen() {
  const router = useRouter();
  const { login, continueAsGuest, isAnonymous, isAuthenticated, user } =
    useAuth();

  const [apiUrl, setApiUrl] = useState(DEFAULT_API_URL);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [guestLoading, setGuestLoading] = useState(false);
  const [error, setError] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [showLoginForm, setShowLoginForm] = useState(isAuthenticated);
  const [routeEnabled, setRouteEnabled] = useState(false);
  const [wifiSsid, setWifiSsid] = useState("");
  const [wifiApiUrl, setWifiApiUrl] = useState("");
  const [cellularApiUrl, setCellularApiUrl] = useState("");
  const [currentNetwork, setCurrentNetwork] = useState("Checking...");
  const [routingSaved, setRoutingSaved] = useState(false);
  const [hydrating, setHydrating] = useState(true);
  const [hydrationFailed, setHydrationFailed] = useState(false);
  const [routingSaving, setRoutingSaving] = useState(false);
  const mountedRef = useRef(true);
  const loadInFlightRef = useRef(false);
  const operationInFlightRef = useRef(false);

  const loadSettings = useCallback(async () => {
    if (loadInFlightRef.current) return;
    loadInFlightRef.current = true;
    setHydrating(true);
    setHydrationFailed(false);
    setError("");
    try {
      const stored = await getApiUrl();
      const routing = await getNetworkEndpointRoutingConfig();
      if (!mountedRef.current) return;
      if (stored) setApiUrl(stored);
      setRouteEnabled(routing.enabled);
      setWifiSsid(routing.wifiSsid);
      setWifiApiUrl(routing.wifiApiUrl);
      setCellularApiUrl(routing.cellularApiUrl);
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
      await saveNetworkEndpointRoutingConfig({
        enabled: routeEnabled,
        wifiSsid: wifiSsid.trim(),
        wifiApiUrl: normalizedWifiApiUrl,
        cellularApiUrl: normalizedCellularApiUrl,
      });
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
      await saveEndpointSettings({
        apiUrl: normalizedApiUrl,
        routing: {
          enabled: routeEnabled,
          wifiSsid,
          wifiApiUrl,
          cellularApiUrl,
        },
      });
      await login(normalizedApiUrl, username.trim(), password);
      router.replace("/(tabs)/chat");
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "ログインに失敗しました";
      setError(msg);
    } finally {
      operationInFlightRef.current = false;
      setLoading(false);
    }
  };

  const handleContinueAsGuest = async () => {
    if (guestLoading || loading || operationInFlightRef.current) return;
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
              {isAuthenticated
                ? `接続中: ${user?.username ?? "server user"}`
                : isAnonymous
                  ? "現在は匿名モードです"
                  : "まず使い方を選んでください"}
            </Text>
            <Text style={styles.modeText}>
              匿名モードではローカルのタスク・カレンダー・レポート・下書きチャットを使えます。
            </Text>
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
            <View style={styles.buttonRow}>
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
              <Text style={styles.formTitle}>サーバーログイン</Text>

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
                onChangeText={setUsername}
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
