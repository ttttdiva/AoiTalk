# Model bake-off 契約

`src/services/model_bakeoff.py` は、設定済みの Execution Profile / model
route を使って複数モデルを同じタスク fixture で比較するための、ネットワーク非依存な
runner/evaluator 契約です。サービス自身はモデルをダウンロードしたりサーバーを起動したり
しません。実運用では `runner` callback に既存の実行経路を渡し、単体テストでは fake runner
を渡します。

## 最小例

```python
from src.services.model_bakeoff import BakeoffCase, ModelBakeoffRunner

cases = [BakeoffCase(
    "rename-helper",
    "helper の名前を変更し、テストを更新してください",
    expected_paths=("src/helper.py", "tests/test_helper.py"),
)]

async def runner(*, model_id, case, route, context, attempt):
    # 実際の Agent/LLM 呼び出しをここで行い、結果を mapping で返す。
    return {
        "success": True,
        "tool_call_success": True,
        "patch_success": True,
        "test_success": True,
        "build_success": True,
        "rounds": 1,
        "context": {"input_tokens": 1200},
    }

report = await ModelBakeoffRunner(
    config,
    runner,
    cases=cases,
    # モデルは呼び出し側が選ぶ。特定の一モデルを既定値にしない。
    models=["model-a", "model-b"],
).run()
```

`run()` は入力した model/case/repeat の順に実行します。同期スクリプトでは
`run_sync()` または `run_model_bakeoff_sync()` を使用できます。runner と evaluator は
同期・非同期のどちらでも構いません。callback が受け取る引数名に合わせて渡されるため、
`case` と `model` だけを受け取る既存 callback も利用できます。

## route 解決

通常は `resolve_execution_main_route(config)` を起点にし、`subagent_id` が指定された場合は
`resolve_execution_profile_route(...)` を通します。従って provider、model、effort、
`execution_profile_id` は AoiTalk の既存ルーティングから取得されます。モデル fixture に
`{"model_id": "…", "provider": "…", "model": "…"}` または `{"route": {...}}` を渡すと、
比較対象ごとの route を明示できます。`route_resolver` を注入すれば本番の session/team
境界を壊さずに最終 route だけを適用できます。

## 1 回あたりの metrics

runner の flat mapping、`{"metrics": {...}}`、属性を持つ結果オブジェクトを受け付けます。
`BakeoffMetrics` へ正規化される主要フィールドは次のとおりです。

| フィールド | 意味 |
| --- | --- |
| `tool_call_success` | tool-call が成功したか。`tool_calls` の status からも導出 |
| `malformed_calls` | malformed / parse error の tool-call 数 |
| `hallucinated_path` | `expected_paths` 外を探索したか |
| `exploration_precision` | 探索パス中の期待パスの割合（0〜1） |
| `patch_success` | パッチ適用に成功したか |
| `test_success`, `build_success` | test / build の成否。両方が true のとき `test_build_success` |
| `director_required_fixes` | Director review が要求した修正数 |
| `rounds` | 実行 round/turn 数 |
| `latency_ms` | callback 実測時間（結果に明示された値があればそれを優先） |
| `context` | callback が返した context/token 等の opaque metadata |

`explored_paths` と `expected_paths` があれば、malformed path と exploration precision は
自動計算されます。曖昧な provider 固有値は破棄せず、既知の canonical field だけを
正規化します。

## 集計と決定性

`ModelBakeoffReport.results` は各試行の監査可能な行、`summaries` はモデルごとの率・合計・
平均です。summary には tool-call、patch、test/build の成功率、malformed/hallucinated の
件数または率、Director 修正数、rounds、latency、context samples が含まれます。
`best_model` は test/build → patch → tool-call → exploration precision → Director 修正数 →
latency → model ID の順で固定的に比較します。同点時に時計や乱数で順位が変わることは
ありません。再現可能なテストでは `clock` callback を注入して latency も固定してください。

この契約のテストは `tests/test_model_bakeoff.py` にあり、fake runner のみを使います。
モデル download、外部 API、実サーバーはテスト完了条件ではありません。
