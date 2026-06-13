import React, { useEffect, useState } from "react";
import { ScrollView, StyleSheet, View } from "react-native";
import { useRouter } from "expo-router";
import { goBackOrReplace } from "../../../lib/navigation";
import {
  Button,
  IconButton,
  Surface,
  Switch,
  Text,
  TextInput,
} from "react-native-paper";
import { DEFAULT_API_URL, EXTERNAL_API_URL } from "../../../constants/config";
import { clearApiUrlCache } from "../../../lib/api-client";
import { getApiUrl, saveApiUrl } from "../../../lib/auth";
import {
  getCurrentNetworkInfo,
  getNetworkEndpointRoutingConfig,
  saveNetworkEndpointRoutingConfig,
} from "../../../lib/connection-routing";

export default function SettingsConnectionScreen() {
  const router = useRouter();
  const [routeEnabled, setRouteEnabled] = useState(false);
  const [wifiSsid, setWifiSsid] = useState("");
  const [wifiApiUrl, setWifiApiUrl] = useState("");
  const [cellularApiUrl, setCellularApiUrl] = useState("");
  const [currentNetwork, setCurrentNetwork] = useState("Checking...");
  const [routingSaved, setRoutingSaved] = useState(false);

  useEffect(() => {
    (async () => {
      const defaultApiUrl = (await getApiUrl()) || DEFAULT_API_URL;
      const routing = await getNetworkEndpointRoutingConfig();
      setRouteEnabled(routing.enabled);
      setWifiSsid(routing.wifiSsid);
      setWifiApiUrl(routing.wifiApiUrl || defaultApiUrl);
      setCellularApiUrl(routing.cellularApiUrl || EXTERNAL_API_URL);
      const network = await getCurrentNetworkInfo();
      setCurrentNetwork(
        network.type === "wifi"
          ? `Wi-Fi${network.ssid ? `: ${network.ssid}` : ""}`
          : network.type,
      );
    })();
  }, []);

  const handleSaveRouting = async () => {
    const defaultUrl = cellularApiUrl.trim() || DEFAULT_API_URL;
    await saveApiUrl(defaultUrl);
    await saveNetworkEndpointRoutingConfig({
      enabled: routeEnabled,
      wifiSsid: wifiSsid.trim(),
      wifiApiUrl: wifiApiUrl.trim(),
      cellularApiUrl: defaultUrl,
    });
    clearApiUrlCache();
    setRoutingSaved(true);
    setTimeout(() => setRoutingSaved(false), 2000);
  };

  return (
    <ScrollView style={styles.container} contentContainerStyle={styles.content}>
      <Surface style={styles.header} elevation={1}>
        <View style={styles.headerRow}>
          <IconButton
            icon="arrow-left"
            iconColor="#cdd6f4"
            onPress={() => goBackOrReplace(router, '/(tabs)/settings')}
          />
          <View style={{ flex: 1 }}>
            <Text variant="titleLarge" style={styles.headerTitle}>
              Server / Network
            </Text>
            <Text style={styles.headerSubtext}>Current: {currentNetwork}</Text>
          </View>
        </View>
      </Surface>

      <Surface style={styles.card} elevation={0}>
        <View style={styles.switchRow}>
          <View style={styles.switchText}>
            <Text style={styles.cardTitle}>接続先</Text>
            <Text style={styles.helperText}>
              基本は「それ以外のURL」を使います。指定Wi-Fi名に一致した時だけWi-Fi用URLに切り替えます。
            </Text>
          </View>
          <Switch value={routeEnabled} onValueChange={setRouteEnabled} />
        </View>
        <TextInput
          mode="outlined"
          label="指定Wi-Fi名"
          value={wifiSsid}
          onChangeText={setWifiSsid}
          style={styles.input}
          autoCapitalize="none"
          disabled={!routeEnabled}
        />
        <TextInput
          mode="outlined"
          label="そのWi-Fiで使うURL"
          value={wifiApiUrl}
          onChangeText={setWifiApiUrl}
          style={styles.input}
          autoCapitalize="none"
          autoCorrect={false}
          keyboardType="url"
          disabled={!routeEnabled}
        />
        <TextInput
          mode="outlined"
          label="それ以外で使うURL"
          value={cellularApiUrl}
          onChangeText={setCellularApiUrl}
          style={styles.input}
          autoCapitalize="none"
          autoCorrect={false}
          keyboardType="url"
        />
        <Text style={styles.helperText}>
          Wi-Fi別切り替えをOFFにすると「それ以外で使うURL」だけを使います。
        </Text>
        <View style={styles.buttonRow}>
          <Button
            mode="contained"
            buttonColor="#7c3aed"
            textColor="#cdd6f4"
            onPress={handleSaveRouting}
          >
            {routingSaved ? "Saved" : "保存"}
          </Button>
        </View>
      </Surface>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#11111b" },
  content: { paddingBottom: 32 },
  header: {
    paddingTop: 52,
    paddingHorizontal: 8,
    paddingBottom: 16,
    backgroundColor: "#1e1e2e",
  },
  headerRow: { flexDirection: "row", alignItems: "center" },
  headerTitle: { color: "#cdd6f4", fontWeight: "bold" },
  headerSubtext: { color: "#a6adc8", marginTop: 2 },
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
