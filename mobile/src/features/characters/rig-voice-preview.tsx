import React, { useCallback, useRef, useState } from 'react';
import { View } from 'react-native';
import { Button, Text } from 'react-native-paper';
import { useFocusEffect } from 'expo-router';
import type { components } from '../../types/api-types.gen';
import { playVoiceBase64 } from '../conversation/voice-audio';
import { assertRigScope, rigRequest, type RigScope } from './rig-api';

type SpeechResponse = components['schemas']['MobileVoiceSpeechResponse'];

/** The same authenticated TTS endpoint and expo-av player used by conversation. */
export function NativeRigVoicePreview({ scope, character, onPlaybackStart }: {
  scope: RigScope;
  character: string;
  onPlaybackStart?: () => void;
}) {
  const lifetime = useRef<AbortController | null>(null);
  const playback = useRef<AbortController | null>(null);
  const [phase, setPhase] = useState<'idle' | 'loading' | 'playing'>('idle');
  const [error, setError] = useState('');

  useFocusEffect(useCallback(() => {
    const controller = new AbortController();
    lifetime.current = controller;
    setPhase('idle');
    setError('');
    return () => {
      controller.abort();
      playback.current?.abort();
      if (lifetime.current === controller) lifetime.current = null;
    };
  }, [character, scope.server, scope.authScope]));

  async function start() {
    const focus = lifetime.current;
    if (!focus || focus.signal.aborted) return;
    if (playback.current) {
      playback.current.abort();
      return;
    }
    const controller = new AbortController();
    playback.current = controller;
    const stop = () => controller.abort();
    focus.signal.addEventListener('abort', stop, { once: true });
    setError('');
    setPhase('loading');
    try {
      const response = await rigRequest<SpeechResponse>(scope, '/api/mobile-voice/speech', {
        method: 'POST',
        signal: controller.signal,
        body: JSON.stringify({
          character_name: character,
          text: 'こんにちは。今日はどんなお話をしましょうか。',
        }),
      }, 120000);
      if (!response.audio_base64 || response.audio_base64.length > 12 * 1024 * 1024) {
        throw new Error('試聴音声を読み込めませんでした。');
      }
      await assertRigScope(scope, controller.signal);
      onPlaybackStart?.();
      setPhase('playing');
      await playVoiceBase64(response.audio_base64, controller.signal, character);
    } catch (cause) {
      if (!controller.signal.aborted && !focus.signal.aborted) {
        setError(cause instanceof Error ? cause.message : '音声を再生できませんでした。');
      }
    } finally {
      focus.signal.removeEventListener('abort', stop);
      if (playback.current === controller) {
        playback.current = null;
        if (!focus.signal.aborted) setPhase('idle');
      }
    }
  }

  return <View style={{ gap: 4 }}>
    <Button compact loading={phase === 'loading'} onPress={() => void start()}>
      {phase === 'loading' ? '音声の準備を中止' : phase === 'playing' ? '試聴を停止' : '声と口パクを確認'}
    </Button>
    {!!error && <Text accessibilityLiveRegion="polite">{error}</Text>}
  </View>;
}
