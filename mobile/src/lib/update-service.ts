import Constants from 'expo-constants';
import { Alert, Linking, Platform } from 'react-native';
import { UPDATE_CHECK_URL } from '../constants/config';

type UpdateInfo = {
  version: string;
  url: string;
  notes: string;
  date: string;
};

type LatestJson = {
  mobile: UpdateInfo;
};

export type UpdateCheckResult = {
  available: boolean;
  currentVersion: string;
  version?: string;
  url?: string;
  notes?: string;
  /**
   * 更新チェックが失敗した場合の理由。
   * 値が入っている場合は「最新です」ではなく、通信/取得失敗として扱う。
   */
  error?: string;
};

function isNewerVersion(current: string, latest: string): boolean {
  const currentParts = current.split('.').map(Number);
  const latestParts = latest.split('.').map(Number);
  for (let i = 0; i < 3; i += 1) {
    const currentValue = currentParts[i] ?? 0;
    const latestValue = latestParts[i] ?? 0;
    if (latestValue > currentValue) return true;
    if (latestValue < currentValue) return false;
  }
  return false;
}

async function installApk(url: string): Promise<void> {
  const module = require('../../modules/apk-installer').default as {
    installApk(downloadUrl: string): Promise<void>;
  };
  await module.installApk(url);
}

export function getCurrentVersion(): string {
  return Constants.expoConfig?.version ?? '0.0.0';
}

export async function checkForUpdate(): Promise<UpdateCheckResult> {
  const currentVersion = getCurrentVersion();
  try {
    const response = await fetch(UPDATE_CHECK_URL, { cache: 'no-store' });
    if (!response.ok) {
      throw new Error(`サーバー応答エラー (HTTP ${response.status})`);
    }
    const data = (await response.json()) as LatestJson;
    const info = data.mobile;
    if (info && isNewerVersion(currentVersion, info.version)) {
      return {
        available: true,
        currentVersion,
        version: info.version,
        url: info.url,
        notes: info.notes,
      };
    }
    return { available: false, currentVersion };
  } catch (error) {
    const message =
      error instanceof Error
        ? error.message
        : '更新情報の取得に失敗しました';
    console.warn('[update-service] checkForUpdate failed:', message);
    return { available: false, currentVersion, error: message };
  }
}

export function showUpdateAlert(result: UpdateCheckResult): void {
  if (!result.available || !result.version || !result.url) return;
  const message = result.notes
    ? `v${result.currentVersion} → v${result.version}\n\n${result.notes}`
    : `v${result.currentVersion} → v${result.version}`;

  Alert.alert('アプリの更新があります', message, [
    { text: '後で', style: 'cancel' },
    {
      text: '更新する',
      onPress: async () => {
        try {
          if (Platform.OS === 'android') {
            await installApk(result.url!);
            Alert.alert(
              'ダウンロード開始',
              '通知バーにダウンロードの進捗が表示されます。\n完了後、通知をタップしてインストールしてください。',
            );
          } else {
            await Linking.openURL(result.url!);
          }
        } catch (error) {
          const reason =
            error instanceof Error ? error.message : String(error);
          console.warn('[update-service] APK download/install failed:', reason);
          const messageText = `更新の開始に失敗しました。\n\n理由: ${reason}\n\nブラウザから直接ダウンロードできます。`;
          Alert.alert('ダウンロードに失敗', messageText, [
            { text: 'キャンセル', style: 'cancel' },
            {
              text: 'ブラウザで開く',
              onPress: () => {
                void Linking.openURL(result.url!);
              },
            },
          ]);
        }
      },
    },
  ]);
}
