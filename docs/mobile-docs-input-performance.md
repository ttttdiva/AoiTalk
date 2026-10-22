# Mobile Docs 入力応答の検証

## ジェスチャ契約

- 通常行の本文は、完了した単一タップでそのまま編集する。二度目のタップを待たない
- 編集中のカーソル移動、ダブルタップ単語選択、長押し選択はネイティブ TextInput に任せる
- 詳細は行の `⋮ → ノードを開く`、またはガター長押しの同メニューから開く。遷移前にその行の保存を完了する
- ガターの展開・格納、スクロール開始、別行タップは古い自動フォーカス／スクロール再試行を取り消す。新規行作成自体を取り消したことにはしない
- 行切替では前行を保存キューへ渡すが、SQLite や同期完了を次の行の編集開始条件にしない
- 通常行の Enter は、カーソル位置にかかわらず本文を元行に保持して直後に空行を作る。文字列をカーソル位置で分割しない。IME の変換確定はネイティブ IME に任せ、`onKeyPress` や時間閾値から行作成を推測しない
- キーボード上の常設ツールバーは48dpの字上げ・字下げ・編集操作アイコンのみ。字下げは既存行を直前の兄弟の子にし、字上げは親と同じ階層へ戻す。操作説明と子作成ショートカットは編集操作メニューで確認できる
- Androidは同じネイティブ入力欄・Editable・InputConnectionを保持してEnter時に論理行を進める。未確定変換のEnterは確定だけ、次のEnterは本文全体のsnapshotを元行へ渡し、入力欄を空にして世代を進める。JSは世代を最終UUIDへ割り当て、古いcontrolled値・重複submit・旧世代changeで新行を上書きしない。前行保存・SQLite作成・同期完了は入力の開始条件にしない。連続Enterの永続化は基準行の作成を先に完了し、作成失敗時も行・本文・IDを保持して同IDで再試行する
- Android以外の入力欄は各行にmountし、レイアウト通知からフォーカスする。Androidの本文受付上限は20,000字。受け付けた本文を保存時に500字へ切り詰めない。typed blockの本文は既存の専用編集経路を維持する
- A→B→Aの再編集は、JSへ未配送の本文をネイティブの行世代snapshotから全文復元する。復元は新しい入力を受け付ける前に行い、受領済みcounterより古いsnapshotは使わない。cacheは入力欄のメモリ内だけに置き、root・actorの異なるscopeへ持ち越さない。復元本文は元の編集時の認証・保存先で検証する。手動切替時の旧IME接続は拒否し、明示blur後の再開は必要な場合だけ接続を作り直す
- Androidの行選択によるfocus要求は対象epochを指定する。value・active・attach・layout・window focusが揃うまで要求を保持し、それらの通知から実行する。スクロールや別行選択による取消は対応epochの保留要求だけを消す。古い要求で別の行をfocusせず、Enterごとの再focusや固定時間の待機は追加しない
- 保持したAndroid画面が同じ大きさで再attachされる場合も、ExpoのAndroid layoutを要求する。detachで消えた子入力欄のlayout済み状態を復旧し、既存のlayout通知から保留focusを処理する
- 明示的に本文へ編集を戻すときは、blur後や取得済み本文が変わった行を新しいepochへ結び直す。入力欄に残った保存済みの旧本文を再利用しない。未配送の入力は既存のsnapshot復元で保持し、focusは新しいvalueがadapterへ反映されてから要求する
- 本文更新のCAS基準・保存後snapshotは既存SQLite connectionの非同期読取を使う。foreground transaction内の最新ACL・本文・outbox再検証は維持する。通常行作成の親メタデータ再読込通知は連続入力中にまとめ、本文編集が終了したとき通知する
- 未永続化の空行も navigation / background flush の保存対象。全体snapshotを再読込するときは、作成待ちの行を新しい順位に基づく基準行の直後へ配置する。保存完了した一行だけの数値順位を採用して他の行と異なる順位世代を混ぜない

## 本文を記録しない端末計測

1,000兄弟の連続入力では、入力をネイティブで受け付けても、JSの同期SQLite読取と
保存完了ごとのpending行再配置、無関係な行layoutの測定が表示を遅らせる場合がある。
新規行の準備読取はネイティブ非同期APIへ渡し、最終transactionのACL・原子性は維持する。
単一行の保存完了では現在の視覚的順位を保ってmetadataだけ更新し、pending行の再配置は
完全snapshotの合流時に行う。保持する入力欄は仮想化されたセルの外、同じネイティブ
ScrollViewのcontent内に固定keyで置く。行anchorからcontentへの相対座標を測り、
スクロールに伴う移動はネイティブへ任せる。window座標をスクロールごとにJSで測って
追いかける方式は使わない。contentと入力欄のclippingによるdetachを防ぎ、行追加後も
同じ入力接続を維持する。 タップ制御は入力矩形だけの非collapsibleなRN Viewが担当し、非編集中はpointerEvents=noneとして背後の行へ通す。Expo独自Viewへ渡した汎用pointerEventsやopacityだけには依存しない。無関係な行のlayoutで有効な計測を取り消さない。行数の計測値だけの変更で行の
編集callbackを作り直さない。固定待機やEnterごとの再focusは追加しない。

連続Enterのスクロールは行ID・root・現在の入力世代の所有を確認し、古い行の
再試行が次の入力行を引き戻さないようにする。Androidはリストの実測領域をIME上端で
制限し、48dpのツールバーと6dpの余白・現在行の高さを除いて配置する。
ツールバーは本文と重ねず通常フローで高さを確保し、長い段落のネイティブなキャレット
スクロールにも実際のリスト下端を適用する。縮小前のキャッシュ高と縮小済み実測高の
どちらでもツールバー分を二重に引かない。キャッシュされたリストの可視長だけへ依存せず、初回と再試行の非アニメーション配置を
揃える。比較ビルドの `[DocsScroll]` はaction・index・viewOffset・viewportHeight・
keyboardTop・generationの数値だけを出す。行IDや本文は出力しない。

通常ビルドでは計測を出力しない。比較用ビルドで `EXPO_PUBLIC_DOCS_INPUT_METRICS=1` を指定すると、ローカルコンソールに `[DocsInput]` の数値サンプルを出す。次のビルドではフラグを外す。外部送信・永続保存の実装はない。

- `visibleRowCount`: 展開状態での行数。画面上に同時 mount した行数ではない
- `pressToInputMountMs`: JS が完了した press を処理してから、その入力 ref が利用可能になるまで
- `pressToNativeFocusMs`: 同じ開始点からネイティブ `onFocus` 通知まで
- `source`: 固定値 `title-tap`

Enter では `[DocsEnter]` に `source=enter`、端末セッション内の数値 `sample`、`visibleRowCount`、`stage`、`elapsedMs` だけを出す。計測はJS submitの検証直後から開始し、兄弟の画面上の順位計算も含む。

- `input-mount`: Androidは新しい論理行の入力位置を測れた時点。その他は新しい入力refが利用可能になった時点
- `native-focus`: Androidは同じ入力欄がフォーカス済みの新世代をJSが受領した時点。その他は新行のnative onFocus通知
- `first-change`: 新行の最初の本文change。Androidのclearによる空changeは除く。人やADBが入力を始めるまでの時間も含む
- `local-commit`: 新行作成のSQLite＋membership＋outbox transaction完了。前行の本文保存完了やサーバー同期完了は表さない
- `save-error`: 作成失敗を検知。入力と後続行は保持し、再試行は同じIDを使う

同じmilestoneは一度だけ出力する。診断callbackの例外は編集・永続化へ伝播しない。通常の公開APKでは環境フラグを解除する。

Androidの`DocsOutlineInput`には数値のepoch/generation/count/actionを出す。action6はnative Enter入口から同じEditableのclearとsubmit発行まで、action7はそのEnterから新世代の最初の非空native change発行までの`elapsedMs`。action7も人・ADBの入力待ちを含むため、touch・画面録画と照合する。本文やnode IDをログに渡さない。

行選択のfocus要求ではaction8/9に対象epochとactive・attach・layout・window focus・focused・結果の数値だけを出す。`[DocsOutlineCommand]`も数値のaction・epoch・outcomeだけを出し、bridgeの例外内容は記録しない。

本文、タイトル、node/root ID、アカウント情報は渡さない。focus 前に別操作でキャンセルされた試行は報告しない。`onInputMeasurement` callback で同じスキーマをテスト用に受け取れる。callback の例外で編集を停止しない。

これは画面上の描画完了時間・キー入力表示時間・キーボード表示時間ではない。OS の touch→JS 配送待ちも含まない。画面録画／端末の frame trace と併せて評価する。Jest の仮想時間や VM の実行時間を実機の入力遅延として報告しない。

## 受入ケース

同じ端末・同じリリース設定で旧版／修正版を比較する。本文を含まない合成文書で 20 行と 1,000 行を用意し、長い Markdown/code block と深い階層も混ぜる。各条件で 30 回以上の完了タップを測り、サンプル数・中央値・p95・最大値・失敗/キャンセル件数を分ける。

1. キーボード閉／開、オフライン／通常同期／保存待ち中で別行をタップし、初回入力を取りこぼさない。即時にカーソルが現れ、旧版の固定 300ms 判定待ちがない
2. 文字の間をタップ、ダブルタップ単語選択、長押し範囲選択、日本語 IME の未確定→確定、コピー／貼り付けを確認。選択で詳細へ遷移しない
3. 本文上からスクロールして意図しない入力を開かず、キーボード表示中も別行へ一度のタップで移れる。ガター展開・格納、長押し、行操作メニューが密な隣接行のタップを奪わない
4. Enter で新規行を作成中／作成直後に別行を選び、続けて入力する。古いフォーカスやスクロールが現在行を奪わず、新規行は一度だけ保存される
5. 連続行切替、直後の画面移動、再読込、同期後に各行の最終文字列が一致する。空行・typed block の本文、末尾改行、空白、タブが保持される
6. `⋮` とガター長押しの両方から詳細へ到達する。保存待ち中の連打で二重遷移せず、readOnly／保護ノードを編集しない
7. 長い文書で行切替・キーボード表示・別行の入力が未変更 Markdown/code block の再描画を繰り返さない。行高 30、fontSize 15、lineHeight 21 の既存の密度を維持する

合格はソース上の待ち撤去だけでは決めず、上記の端末操作・保存後照合まで確認する。未測定の数値欄は未測定のまま残す。

Androidの新規行作成・通常本文保存は、既存の450ms保存休止時間を入力欄全体で共有する。native changeとEnterで休止時間を延長し、連続入力中の同期SQLite ACL/outbox処理が行の表示を先取りしないようにする。フォーカス・入力受付はこのtimerを待たない。画面移動・バックグラウンドのflush、blur、unmountは待機を解除して捕捉済みscopeの保存を開始し、空行を含むstable IDと作成依存を維持する。保存前の即時強制停止は無損失保証の対象に含めず、通常の移動・保存後再起動で本文を照合する。
