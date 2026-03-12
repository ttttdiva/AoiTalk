# 音声入出力実装のリファクタリング提案

## 概要
Discord実装とLocal実装の音声入出力処理を分析した結果、多くの共通処理が存在することが判明しました。
これらの共通処理を基底クラスに抽出することで、コードの重複を削減し、保守性を向上させることができます。

## 共通処理の特定結果

### 音声入力（AudioInput）の共通処理
1. **状態管理**: `_active`, `_callback` フラグ
2. **キュー管理**: 音声データのキューイング処理
3. **start/stop処理**: リソースの初期化・クリーンアップの基本フロー
4. **read_audio処理**: duration指定による読み取りロジック
5. **コールバック処理**: 音声データ受信時のコールバック呼び出し

### 音声出力（AudioOutput）の共通処理
1. **状態管理**: `_playing`, `_paused`, `_volume`
2. **再生制御**: play前の停止処理、状態遷移
3. **音量制御**: 0.0〜1.0の範囲での音量管理
4. **データ変換**: numpy配列とバイト列の相互変換
5. **音量適用**: 音声データへの音量値の適用

## 提案する基底クラス

### 1. BaseAudioInput
```python
class BaseAudioInput(AudioInputInterface, ABC):
    """音声入力実装の基底クラス"""
```

**提供する機能:**
- 共通の状態管理
- キューの作成・クリア処理
- start/stopの基本フロー
- read_audioの共通ロジック
- コールバック管理

**サブクラスで実装が必要なメソッド:**
- `_initialize_resources()`: リソースの初期化
- `_cleanup_resources()`: リソースのクリーンアップ
- `_create_queue()`: 適切なキュータイプの作成
- `_read_all_available()`: 利用可能データの読み取り
- `_read_duration()`: 指定時間分のデータ読み取り

### 2. BaseAudioOutput
```python
class BaseAudioOutput(AudioOutputInterface, ABC):
    """音声出力実装の基底クラス"""
```

**提供する機能:**
- 共通の状態管理
- play前の停止処理
- 音量管理（0.0〜1.0）
- numpy配列とバイト列の変換
- 音量適用処理

**サブクラスで実装が必要なメソッド:**
- `_prepare_audio_data()`: 音声データの準備
- `_play_internal()`: 実際の再生処理
- `_stop_internal()`: 実際の停止処理
- `_pause_internal()`: 実際の一時停止処理
- `_resume_internal()`: 実際の再開処理
- `_apply_volume_change()`: 音量変更の適用

## リファクタリングの利点

1. **コードの重複削減**: 共通処理を一箇所に集約
2. **保守性の向上**: バグ修正や機能追加が一箇所で可能
3. **一貫性の確保**: 全実装で同じ動作を保証
4. **拡張性の向上**: 新しい実装の追加が容易
5. **テストの簡素化**: 共通処理のテストを集約可能

## 実装手順

1. 基底クラスの作成（完了）
   - `base_input.py`: 音声入力の基底クラス
   - `base_output.py`: 音声出力の基底クラス

2. 既存実装のリファクタリング
   - `VoiceChannelInput`を`BaseAudioInput`から継承
   - `MicrophoneInput`を`BaseAudioInput`から継承
   - `VoiceChannelOutput`を`BaseAudioOutput`から継承
   - `SpeakerOutput`を`BaseAudioOutput`から継承

3. テストの更新
   - 基底クラスの共通処理のテスト追加
   - 各実装固有のテスト更新

4. ドキュメントの更新
   - 新しいクラス階層の説明
   - 実装ガイドの追加

## 注意事項

- 既存のAPIとの互換性を維持する
- 段階的なリファクタリングを行い、各ステップでテストを実施
- パフォーマンスへの影響を監視する