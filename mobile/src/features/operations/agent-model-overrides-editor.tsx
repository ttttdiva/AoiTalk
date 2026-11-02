import React, { useState } from 'react';
import { FlatList, Modal, ScrollView, useWindowDimensions, View } from 'react-native';
import { Button, Checkbox, RadioButton, Text, TextInput, useTheme } from 'react-native-paper';
import type { AgentModelOverrides, Fallback, ModelCatalog, ModelRoute, Role } from '../../lib/agent-employees-api';
const roles: [Role, string][] = [['chat','会話'],['code','コード'],['review','レビュー'],['research','調査'],['summarize','要約']];
const reasons: [Fallback,string][] = [['model_unavailable','モデル利用不可'],['provider_unavailable','プロバイダー利用不可'],['rate_limited','レート制限'],['connect_timeout','接続タイムアウト']];
export function AgentModelOverridesEditor({ value, onChange, catalog, pools }: { value: AgentModelOverrides; onChange: (value: AgentModelOverrides) => void; catalog: ModelCatalog; pools: string[] }) {
  const { height } = useWindowDimensions();
  const theme = useTheme();
  const [pickerRole, setPickerRole] = useState<Role | null>(null);
  const [providerId, setProviderId] = useState('');
  const [search, setSearch] = useState('');
  const providers = catalog.providers.filter(provider => provider.selection_kind !== 'routing_profile');
  const candidates: { key: string; label: string; route: ModelRoute }[] = pickerRole ? [...providers.filter(provider => !providerId || provider.id === providerId).flatMap(provider => provider.models.filter(model => model.selection_kind !== 'routing_profile').map(model => ({ key: `llm:${provider.id}:${model.id}`, label: `${provider.label} / ${model.label ?? model.id}`, route: { kind: 'llm' as const, provider: provider.id, model: model.id } }))), ...(!providerId || providerId === '__pools__' ? pools.map(pool => ({ key: `pool:${pool}`, label: `pool: ${pool}`, route: { kind: 'pool' as const, pool_id: pool } })) : [])].filter(item => item.label.toLocaleLowerCase().includes(search.toLocaleLowerCase())) : [];
  const update = (role: Role, routes: ModelRoute[]) => { if (!routes.length) { const next = { ...value }; delete next[role]; onChange(next); } else if (routes.length <= 64) onChange({ ...value, [role]: { ...value[role], routes } }); };
  return <View><Text variant="titleMedium">役割別モデル候補</Text><Text>未設定の役割は全体設定を使用します。候補は上から順に使用します。</Text>
    {roles.map(([role,label]) => { const block = value[role]; const routes = block?.routes ?? []; return <View key={role} style={{ marginVertical: 12 }}><Text variant="titleSmall">{label}</Text>
      {routes.map((route,index) => <View key={index} style={{ marginVertical: 4 }}><Text>{index + 1}. {route.kind === 'pool' ? `pool: ${route.pool_id}` : `${route.provider} / ${route.model}`}</Text>
        {route.kind === 'llm' ? <TextInput label="effort（任意）" value={route.effort ?? ''} onChangeText={effort => update(role, routes.map((r,i) => i === index ? { ...route, effort: effort || null } : r))} /> : null}
        <View style={{ flexDirection: 'row', flexWrap: 'wrap' }}><Button disabled={index === 0} onPress={() => { const next = [...routes]; [next[index - 1],next[index]] = [next[index],next[index - 1]]; update(role,next); }}>上へ</Button><Button disabled={index === routes.length - 1} onPress={() => { const next = [...routes]; [next[index + 1],next[index]] = [next[index],next[index + 1]]; update(role,next); }}>下へ</Button><Button onPress={() => update(role,routes.filter((_,i) => i !== index))}>削除</Button></View></View>)}
      <Button disabled={routes.length >= 64} onPress={() => { setProviderId(''); setSearch(''); setPickerRole(role); }}>候補追加</Button>
      {block ? <><Text>有料 fallback</Text><RadioButton.Group value={block.allow_paid_fallback == null ? 'inherit' : block.allow_paid_fallback ? 'allow' : 'deny'} onValueChange={choice => onChange({ ...value, [role]: { ...block, allow_paid_fallback: choice === 'inherit' ? null : choice === 'allow' } })}><RadioButton.Item label="全体設定に従う" value="inherit" /><RadioButton.Item label="許可" value="allow" /><RadioButton.Item label="不許可" value="deny" /></RadioButton.Group><Text>fallback 条件（未指定は既定）</Text><Button onPress={() => onChange({ ...value, [role]: { ...block, fallback_on: null } })}>既定の条件に戻す</Button>{reasons.map(([reason,name]) => <Checkbox.Item key={reason} label={name} status={block.fallback_on?.includes(reason) ? 'checked' : 'unchecked'} onPress={() => { const current = block.fallback_on ?? []; onChange({ ...value, [role]: { ...block, fallback_on: current.includes(reason) ? current.filter(item => item !== reason) : [...current,reason] } }); }} />)}<Button onPress={() => { const next = { ...value }; delete next[role]; onChange(next); }}>この役割を全体設定に戻す</Button></> : null}
    </View>; })}
    <Modal visible={pickerRole !== null} animationType="slide" onRequestClose={() => setPickerRole(null)}>
      <View style={{ height, padding: 16, backgroundColor: theme.colors.background }}>
        <View style={{ flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center' }}><Text variant="titleMedium" style={{ color: theme.colors.onSurface }}>モデル / pool を選択</Text><Button onPress={() => setPickerRole(null)}>キャンセル</Button></View>
        <TextInput accessibilityLabel="検索" label="検索" value={search} onChangeText={setSearch} />
        <View><ScrollView horizontal keyboardShouldPersistTaps="handled"><Button mode={!providerId ? 'contained' : 'text'} onPress={() => setProviderId('')}>すべて</Button>{providers.map(provider => <Button key={provider.id} mode={providerId === provider.id ? 'contained' : 'text'} onPress={() => setProviderId(provider.id)}>{provider.label}</Button>)}<Button mode={providerId === '__pools__' ? 'contained' : 'text'} onPress={() => setProviderId('__pools__')}>pool</Button></ScrollView></View>
        {pickerRole ? <FlatList data={candidates} keyExtractor={item => item.key} keyboardShouldPersistTaps="handled" renderItem={({ item }) => <Button onPress={() => { update(pickerRole, [...(value[pickerRole]?.routes ?? []), item.route]); setPickerRole(null); }}>{item.label}</Button>} ListEmptyComponent={<Text style={{ color: theme.colors.onSurface }}>候補がありません</Text>} /> : null}
      </View>
    </Modal></View>;
}
