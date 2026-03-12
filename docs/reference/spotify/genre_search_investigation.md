# Spotify Genre Search Investigation Report

## Summary

SpotifyのWeb APIにおけるジャンル検索（`genre:` フィルター）について調査した結果、**ジャンル検索は動作するが、期待通りではない**ことが判明しました。

## Key Findings

### 1. ジャンル検索は技術的に動作する

テストコードで確認した結果：
- `genre:"dubstep"` と `genre:dubstep` の両方で結果が返ってくる
- ただし、**返される楽曲は必ずしも指定したジャンルと一致しない**

### 2. Spotifyの制限事項

1. **トラックにはジャンル情報がない**
   - Spotifyはアーティストにのみジャンル情報を付与
   - 個別のトラックやアルバムにはジャンル属性が存在しない

2. **ジャンル検索の動作**
   - `genre:` フィルターは、アーティストのジャンルを基に検索
   - 結果の精度は低く、関連性の薄いトラックも含まれる

### 3. Recommendations API の状況

- `/recommendations/available-genre-seeds` エンドポイントは**404エラー**を返す（deprecated）
- Recommendations API自体は動作するが、Client Credentials Flowでは制限がある

## 現在の実装の評価

`src/tools/entertainment/spotify/recommendations.py` の実装は適切です：

1. **正しいフォールバック戦略**
   ```python
   # ジャンル検索クエリを構築
   search_queries = [
       f'genre:"{genre}"',  # 正確なジャンル
       f'genre:{genre}',    # ジャンル名
       f'{genre}',          # 一般検索
   ]
   ```

2. **適切なエラーハンドリング**
   - Recommendations APIが失敗した場合、検索APIにフォールバック
   - 複数の検索戦略を試行

3. **ローカルジャンルリスト**
   - `seed_genres.json` に126個のジャンルを保持
   - API依存を減らし、安定性を向上

## 推奨事項

1. **現在の実装を維持**
   - ジャンル検索は完璧ではないが、ある程度機能している
   - 複数の検索戦略により、最良の結果を得られる

2. **ユーザー体験の改善**
   - ジャンル検索の精度について適切な期待値設定
   - 人気度でソートすることで、より関連性の高い結果を上位に表示

3. **将来の改善案**
   - アーティストのジャンル情報を活用した追加フィルタリング
   - 機械学習モデルによるジャンル分類の独自実装

## 技術的詳細

### ジャンル検索の実際の動作例

```
Query: genre:"dubstep"
Results: 
- SLANDER (genres: melodic bass, dubstep, edm, future bass) ✓
- Culture Code (genres: melodic bass, future bass) △
- ILLENIUM (genres: melodic dubstep, edm, future bass) △
```

結果には dubstep に関連するアーティストの楽曲が含まれるが、純粋な dubstep とは限らない。

### API制限の回避策

現在の実装は以下の工夫により、API制限を効果的に回避：
1. ローカルジャンルキャッシュ
2. 複数の検索戦略
3. 人気度によるソート

## 結論

Spotify Web APIのジャンル検索は完全ではないが、現在の実装は最適な妥協点を実現している。ジャンル検索の制限を理解した上で、複数の戦略を組み合わせることで、ユーザーに価値のある結果を提供できている。