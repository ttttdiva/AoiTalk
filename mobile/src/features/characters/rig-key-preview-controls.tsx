import React, { useCallback } from 'react';
import { useFocusEffect } from 'expo-router';
import { View } from 'react-native';
import { Button, Text } from 'react-native-paper';
import type { RuntimeCapabilities } from '@aoitalk/seethrough-rig-runtime';
import { EMPTY_RIG_KEY_PREVIEW, RIG_VOWELS, rigKeyPreviewCapabilities, type RigKeyPreview, type RigVowel } from './rig-key-preview';

const vowelLabels: Record<RigVowel, string> = { a: 'あ', i: 'い', u: 'う', e: 'え', o: 'お' };
export function NativeRigKeyPreviewControls({ capabilities, value, onChange }: {
  capabilities: RuntimeCapabilities | null;
  value: RigKeyPreview;
  onChange: (value: RigKeyPreview) => void;
}) {
  useFocusEffect(useCallback(() => {
    onChange({ ...EMPTY_RIG_KEY_PREVIEW });
    return () => onChange({ ...EMPTY_RIG_KEY_PREVIEW });
  }, [onChange]));
  const supported = rigKeyPreviewCapabilities(capabilities);
  const active = value.eyes !== null || value.vowel !== null;
  return <View style={{ gap: 4 }}>
    <Text variant="titleSmall">目・母音の形を確認</Text>
    <Text variant="bodySmall">一時的に動きを止めて形を表示します。「形の確認を解除」で戻ります。保存や音声送信は行いません。</Text>
    <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 4 }}>
      {([{ label: '開く', value: 1 }, { label: '半目', value: 0.5 }, { label: '閉じる', value: 0 }] as const).map((eye) =>
        <Button key={eye.label} compact disabled={!supported.eyes.length}
          accessibilityLabel={'目の形: ' + eye.label}
          mode={value.eyes === eye.value ? 'contained' : 'outlined'}
          onPress={() => onChange({ ...value, eyes: eye.value })}>{eye.label}</Button>,
      )}
      {supported.jitome && <Button compact
        accessibilityLabel="目の形: じと目"
        mode={value.eyes === 'jitome' ? 'contained' : 'outlined'}
        onPress={() => onChange({ ...value, eyes: 'jitome' })}>じと目</Button>}
      <Button compact disabled={value.eyes === null} onPress={() => onChange({ ...value, eyes: null })}>目の指定を解除</Button>
    </View>
    {!supported.eyes.length && <Text variant="bodySmall">このモデルでは目の開閉キーを利用できません。</Text>}
    <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 4 }}>
      {RIG_VOWELS.map((vowel) => <Button key={vowel} compact disabled={!supported.vowels.includes(vowel)}
        accessibilityLabel={'母音の形: ' + vowelLabels[vowel]}
        mode={value.vowel === vowel ? 'contained' : 'outlined'}
        onPress={() => onChange({ ...value, vowel })}>{vowelLabels[vowel]}</Button>)}
      <Button compact disabled={value.vowel === null} onPress={() => onChange({ ...value, vowel: null })}>口の指定を解除</Button>
    </View>
    {supported.vowels.length < RIG_VOWELS.length && <Text variant="bodySmall">
      このモデルでは{RIG_VOWELS.filter((vowel) => !supported.vowels.includes(vowel)).map((vowel) => vowelLabels[vowel]).join('・')}の母音キーを利用できません。
    </Text>}
    <Button compact disabled={!active} onPress={() => onChange({ ...EMPTY_RIG_KEY_PREVIEW })}>形の確認を解除</Button>
  </View>;
}
