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
import { DEFAULT_API_URL } from "../../../constants/config";
import { getApiUrl } from "../../../lib/auth";
import {
  getCurrentNetworkInfo,
  getNetworkEndpointRoutingConfig,
} from "../../../lib/connection-routing";
import { saveEndpointSettings } from "../../../lib/endpoint-settings";

export default function SettingsConnectionScreen() {
  const router = useRouter();
  const [apiUrl, setApiUrl] = useState(DEFAULT_API_URL);
  const [routeEnabled, setRouteEnabled] = useState(false);
  const [wifiSsid, setWifiSsid] = useState("");
  const [wifiApiUrl, setWifiApiUrl] = useState("");
  const [cellularApiUrl, setCellularApiUrl] = useState("");
  const [currentNetwork, setCurrentNetwork] = useState("Checking...");
  const [routingSaved, setRoutingSaved] = useState(false);
  const [error, setError] = useState("");
  const [hydrating, setHydrating] = useState(true);
  const [hydrationFailed, setHydrationFailed] = useState(false);
  const [saving, setSaving] = useState(false);
  const mountedRef = useRef(true);
  const loadInFlightRef = useRef(false);
  const saveInFlightRef = useRef(false);

  const loadSettings = useCallback(async () => {
    if (loadInFlightRef.current) return;
    loadInFlightRef.current = true;
    setHydrating(true);
    setHydrationFailed(false);
    setError("");
    try {
      const storedApiUrl = await getApiUrl();
      const routing = await getNetworkEndpointRoutingConfig();
      if (!mountedRef.current) return;
      setApiUrl(storedApiUrl || DEFAULT_API_URL);
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

  useEffect(() => {
    mountedRef.current = true;
    void loadSettings();
    return () => {
      mountedRef.current = false;
    };
  }, [loadSettings]);

  const handleSaveRouting = async () => {
    if (
      hydrating ||
      hydrationFailed ||
      saving ||
      saveInFlightRef.current
    ) {
      return;
    }
    saveInFlightRef.current = true;
    setSaving(true);
    setError("");

    try {
      await saveEndpointSettings({
        apiUrl,
        routing: {
          enabled: routeEnabled,
          wifiSsid,
          wifiApiUrl,
          cellularApiUrl,
        },
      });
      setRoutingSaved(true);
      setTimeout(() => setRoutingSaved(false), 2000);
    } catch (saveError: unknown) {
      setError(
        saveError instanceof Error
          ? saveError.message
          : "接続設定を保存できませんでした",
      );
    } finally {
      saveInFlightRef.current = false;
      setSaving(false);
    }
  };

  const canEdit = !hydrating && !hydrationFailed && !saving;

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
          label="それ以外で使うURL"
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
            ? "指定Wi-Fi以外では、設定した公開用URLを使います。空欄なら基本API URLに戻ります。"
            : "Wi-Fi別切り替えはOFFです。基本API URLを使います。"}
        </Text>
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
  helperText: {
    color: "#a6adc8",
    fontSize: 13,
    lineHeight: 19,
    marginBottom: 12,
  },
});
