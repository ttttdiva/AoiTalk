import React from 'react';
import { Alert, StyleSheet, View } from 'react-native';
import { useRouter } from 'expo-router';
import { goBackOrReplace } from '../../../lib/navigation';
import { Button, Surface, Text } from 'react-native-paper';
import { ScreenHeader } from '../../../components/screen-header';
import { checkForUpdate, getCurrentVersion, showUpdateAlert } from '../../../lib/update-service';

export default function SettingsAboutScreen() {
  const router = useRouter();

  return (
    <View style={styles.container}>
      <ScreenHeader
        title="アプリ情報"
        onBack={() => goBackOrReplace(router, '/(tabs)/settings')}
      />
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
            } else if (result.error) {
              Alert.alert(
                '更新の確認に失敗しました',
                `更新情報を取得できませんでした。\n\n理由: ${result.error}\n\n通信環境を確認して、もう一度お試しください。`,
              );
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
  card: { backgroundColor: '#1e1e2e', borderRadius: 12, padding: 16, margin: 16 },
  appName: { color: '#cdd6f4', fontSize: 20, fontWeight: '700' },
  meta: { color: '#a6adc8', fontSize: 13, marginTop: 4 },
  body: { color: '#cdd6f4', fontSize: 13, lineHeight: 19, marginVertical: 16 },
});
