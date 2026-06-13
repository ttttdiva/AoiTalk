import React from 'react';
import { Alert, StyleSheet, View } from 'react-native';
import { useRouter } from 'expo-router';
import { goBackOrReplace } from '../../../lib/navigation';
import { Button, IconButton, Surface, Text } from 'react-native-paper';
import { checkForUpdate, getCurrentVersion, showUpdateAlert } from '../../../lib/update-service';

export default function SettingsAboutScreen() {
  const router = useRouter();

  return (
    <View style={styles.container}>
      <Surface style={styles.header} elevation={1}>
        <View style={styles.headerRow}>
          <IconButton icon="arrow-left" iconColor="#cdd6f4" onPress={() => goBackOrReplace(router, '/(tabs)/settings')} />
          <Text variant="titleLarge" style={styles.headerTitle}>
            アプリ情報
          </Text>
        </View>
      </Surface>
      <Surface style={styles.card} elevation={0}>
        <Text style={styles.appName}>AoiTalk Mobile</Text>
        <Text style={styles.meta}>バージョン {getCurrentVersion()}</Text>
        <Text style={styles.body}>
          Android APKの自動更新チェック、GitHub Releaseの配布メタデータ、アプリ内更新通知に対応しています。
        </Text>
        <Button
          mode="contained"
          buttonColor="#7c3aed"
          textColor="#cdd6f4"
          onPress={async () => {
            const result = await checkForUpdate();
            if (result.available) {
              showUpdateAlert(result);
            } else {
              Alert.alert(
                'アプリ',
                `最新バージョンです: v${result.currentVersion}`,
              );
            }
          }}
        >
          更新を確認
        </Button>
      </Surface>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: '#11111b', paddingBottom: 24 },
  header: { paddingTop: 52, paddingHorizontal: 8, paddingBottom: 16, backgroundColor: '#1e1e2e' },
  headerRow: { flexDirection: 'row', alignItems: 'center' },
  headerTitle: { color: '#cdd6f4', fontWeight: 'bold' },
  card: { backgroundColor: '#1e1e2e', borderRadius: 12, padding: 16, margin: 16 },
  appName: { color: '#cdd6f4', fontSize: 20, fontWeight: '700' },
  meta: { color: '#a6adc8', fontSize: 13, marginTop: 4 },
  body: { color: '#cdd6f4', fontSize: 13, lineHeight: 19, marginVertical: 16 },
});
