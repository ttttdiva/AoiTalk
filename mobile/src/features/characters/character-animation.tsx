import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Image, View } from 'react-native';
import { Text } from 'react-native-paper';
import { useFocusEffect } from 'expo-router';
import { useIsFocused } from '@react-navigation/native';
import { useAuth } from '../../contexts/AuthContext';
import { captureRigScope, assertRigScope, readCharacterRig, rigPath, rigCacheRoot, activeRigFormat, type CharacterRig, type RigScope } from './rig-api';
import { getBaseUrl, getCredentialIdentityRevision } from '../../lib/api-client';
import { sendWithTransportAuth } from '../../lib/transport-auth';
import * as FileSystem from 'expo-file-system/legacy';
import { cacheProtectedMediaResource } from '../../lib/media-cache';
import { NativeRigCanvas } from './rig-canvas';
import { clearRigState, setRigReaction, type RigConversationState } from './rig-playback';
import { RigReplyTracker, type RigReplyMessage } from './rig-replies';

export function NativeCharacterAnimation({ character, state = 'idle', messages, historyReady = false, sessionKey = '' }: {
  character: string;
  state?: RigConversationState;
  messages?: readonly RigReplyMessage[];
  historyReady?: boolean;
  sessionKey?: string;
}) {
  const { user, isAuthenticated } = useAuth();
  const focused = useIsFocused();
  const reactionOwner = useRef({});
  const replies = useRef(new RigReplyTracker());
  const [value, setValue] = useState<{
    scope: RigScope;
    owner?: string;
    rig: CharacterRig;
    fallback?: { uri: string };
  } | null>(null);

  useFocusEffect(useCallback(() => {
    const controller = new AbortController();
    setValue(null);
    if (!isAuthenticated) return;
    void (async () => {
      const scope = await captureRigScope();
      const rig = await readCharacterRig(scope, character, controller.signal);
      if (controller.signal.aborted) return;
      setValue({ scope, rig, owner: user?.user_id });
      if (rig.has_source) {
        const endpoint = await getBaseUrl();
        await assertRigScope(scope, controller.signal);
        const url = endpoint + rigPath(character) + '/source.png';
        const cacheRoot = await rigCacheRoot(scope);
        const cachePath = cacheRoot + encodeURIComponent(character) + '/source-preview.png';
        await FileSystem.makeDirectoryAsync(cacheRoot + encodeURIComponent(character) + '/', { intermediates: true });
        const localUri = await cacheProtectedMediaResource(
          url,
          cachePath,
          {},
          controller.signal,
          {
            revisionReader: getCredentialIdentityRevision,
                request: async (send, requestSignal) => await sendWithTransportAuth(
              url,
                  {
                    primaryBaseUrl: scope.server,
                    targetBaseUrl: endpoint,
                    signal: requestSignal ?? controller.signal,
                  },
              send,
            ),
          },
        );
        if (!controller.signal.aborted) setValue({
          scope, rig, owner: user?.user_id,
          fallback: {
            uri: localUri,
          },
        });
      }
    })().catch(() => undefined);
    return () => controller.abort();
  }, [character, user?.user_id, isAuthenticated]));

  useEffect(() => {
    if (!focused || !isAuthenticated) {
      replies.current.reset();
      clearRigState(character, reactionOwner.current);
      return;
    }
    if (messages && replies.current.observe(
      [user?.user_id, sessionKey, character].join(':'), messages, historyReady, state === 'thinking',
    )) setRigReaction(character, reactionOwner.current, { expression: 'happy' });
  }, [character, focused, historyReady, isAuthenticated, messages, sessionKey, state, user?.user_id]);
  useEffect(() => {
    const owner = reactionOwner.current;
    return () => clearRigState(character, owner);
  }, [character]);

  if (!value || value.owner !== user?.user_id || (!value.rig.has_source && !value.rig.has_model && !value.rig.active_revision)) return null;
  const { rig, scope } = value;
  const revision = rig.active_revision;
  const playable = Boolean(revision) && activeRigFormat(rig) === 'seethrough-rig';
  return <View style={{ paddingHorizontal: 12 }}>
    {!rig.has_source && !rig.active_revision && <Text style={{ fontSize: 12 }}>取り込んだモデルを設定で選択してください。</Text>}
    {playable && revision
      ? <NativeRigCanvas key={scope.server + scope.authScope + character + revision} scope={scope} character={character} settingsCharacter={rig.character_id} revision={revision} state={state} />
      : <>
        {rig.has_source && value.fallback && <Image source={value.fallback} style={{ height: 140, width: '100%' }} resizeMode="contain" />}
        {!rig.has_source && revision && <Text style={{ fontSize: 12 }}>See-through Rigモデル未設定</Text>}
      </>}
  </View>;
}
