# Mini PCホストと複数ファイルストレージ

この変更は、AoiTalkのDB・検索サービスと、大容量ファイルの置き場所を分離するためのものです。PostgreSQL、Qdrant、cache、logsの設定・ボリュームは変更しません。

## 設定しない場合

`AOITALK_WORKSPACES_DIR` と、その配下のPersonal / Project等の既存Storage Contextは従来通りです。`AOITALK_GENERATED_MEDIA_DIR` が未設定なら `data/generated_media` を使用します。追加ストレージの設定ファイルがなくてもアプリ起動時にネットワークディスクを調べたり、新しい保存先を作成したりしません。

## 追加ストレージの登録

OS側でメインPC / NAS等を通常のディレクトリとしてマウントしてから、管理者でFilesの「ストレージ設定」を開きます。WebとNative Mobileの双方に設定画面があります。

| 項目 | 意味 |
| --- | --- |
| 識別ID | `main-pc`、`nas`等の安定した小文字ID。ファイルAPIはこのIDと相対パスを使用する |
| 表示名 | `Main PC`、`NAS`等、Filesでの表示名 |
| root path | AoiTalkサーバーから見た絶対パス。スマホ側のパスではない |
| 読み取り専用 | 有効なら管理者を含めFilesの更新操作を拒否する |
| 有効 | 無効なrootにはアクセスしない。設定・データは削除しない |
| 外部マウント | 識別ファイルを必須にし、未マウントの空ディレクトリへのフォールバックを防ぐ |
| 全ユーザーと共有 | 認証済みの全ユーザーにrootへのアクセスを付与する |
| User UUID | 指定ユーザーにrootへのアクセスを付与する |
| Project UUID | 既存Projectのread / write権限を使って、root全体を公開する |

共有・User・Projectの指定がないrootは管理者だけが利用できます。Project関連付けは既存のProject managed workspaceの移転ではありません。個々の追加rootはPersonal / Projectとは別に表示され、rootごとに権限確認します。read-onlyはすべての更新APIで再確認します。

既存workspacesと重なるroot、追加root同士が重なるパス、ストレージ設定ファイルそのものを含むrootは登録できません。同じ物理領域に別のACLで到達する経路を作らないためです。SMB/CIFSクライアント、マウント操作、symlink/junctionによる別rootへの誘導は実装しません。

### 初回の識別ファイル

外部rootは「設定を保存」だけではオンラインになりません。目的のディスクが実際に接続されていることをOS側で確認してから「接続済みストレージを初回登録」を実行します。root直下に `.aoitalk-storage-<id>.identity` を1個作成します。既存の識別ファイルは上書きしません。

識別ファイルは切断時に空のマウントポイントが残る場合を検出するためのもので、SMB資格情報ではありません。OS側が読み取り専用の場合はストレージ所有者が識別ファイルを作成します。ファイル名と内容はWebの管理画面で確認できます。未マウント状態で初回登録を実行しないでください。

通常の一覧表示、アップロード、再接続確認はrootや識別ファイルを自動作成しません。接続確認はrootごとに独立し、応答しないrootへの処理枠を制限します。OSが受理済みの書き込みをタイムアウトで必ず取り消せるという意味ではありません。結果未確定の操作は明示し、自動再送しません。

## Filesの操作

追加rootのブラウザは、一覧、フォルダ移動、名前での絞り込み、UTF-8テキストの読み書き、新規ファイル／フォルダ、同一root内での名前変更／移動、ダウンロード、アップロード、ごみ箱と直前の削除の復元に対応します。テキスト編集の上限は2MiBです。上書きはetagを検証し、他の操作による更新を黙って上書きしません。

大容量アップロードは最大4MiBのchunkに分割し、転送先rootの `.aoitalk-storage/uploads` に一時保存します。受信offsetと所有者を検証し、受信完了後に同じストレージ内で公開します。タイムアウト後は受信状態を照会してから再開できます。切断時にミニPCへ勝手に保存先を切り替えません。Native Mobileは既存Document Pickerの一時キャッシュを利用し、ファイル全体をJSメモリへ読み込みません。

ごみ箱は同じrootの `.aoitalk-storage/trash` に保持します。今回の実装は自動完全削除や保持期限削除を行いません。放置された一時アップロード／ごみ箱は、そのストレージの管理対象として監視してください。プロセス強制終了や切断により `write.lock` が残った場合は、更新処理が終了していることを確認してから復旧する必要があります。稼働中の別処理のlockを自動破壊しません。

追加rootのIDを選択したままオフラインになっても、既存rootへ暗黙に切り替えません。別のストレージは明示的に選択できます。ストレージ設定の変更により権限・path等が変わった場合は、設定revisionによって表示状態を切り替えます。

## 環境変数

| 変数 | デフォルト | 用途 |
| --- | --- | --- |
| `AOITALK_WORKSPACES_DIR` | 既存の定義通り | 従来の単一ローカルroot。変更しない |
| `AOITALK_STORAGE_ROOTS_FILE` | `data/storage_roots.json` | 追加root設定。ミニPC内蔵SSDに置く |
| `AOITALK_GENERATED_MEDIA_DIR` | 未設定なら `data/generated_media` | 生成メディアの物理保存先 |
| `AOITALK_STORAGE_MAX_FILE_BYTES` | `1099511627776`（1TiB） | 追加rootの単一アップロード上限。ディスク空き容量の保証ではない |

設定のJSONにはID、名前、root path、権限・有効状態、公開先、識別情報を保存します。online / offlineはファイルに固定せず、その時点の接続確認結果として返します。

## generated_mediaの変更

例として `nas` のrootを `/mnt/nas/aoitalk` として登録・初回登録済みにし、その中に `generated_media` ディレクトリを用意した場合、次をAoiTalkサーバーの環境に設定します。

```dotenv
AOITALK_GENERATED_MEDIA_DIR=/mnt/nas/aoitalk/generated_media
```

**カスタム保存先は追加rootとして登録済みの領域内にあり、ディレクトリ自体が存在する必要があります。** カスタムのローカルSSD領域も追加rootとして登録できます。その場合は「外部マウント」を無効にできます。未登録のカスタムpathへ無検証で書き込むことはしません。

DBの `relative_path` は引き続き `generated_media/<storage-key>.<ext>` です。`generated_media/` は論理prefixとして扱い、物理rootのフォルダ名が `pictures` 等でも正しく解決します。絶対パスを生成メディアのDBレコードへ書き込みません。外部rootの切断は配信時にstorage-unavailableとして返し、古いpending/failedレコードの掃除もファイル確認ができなければ保留します。

設定変更だけでは既存ファイルを移動しません。既存レコードを継続利用する場合、停止・バックアップ・ファイルのコピー／照合を含む移行が別途必要です。接続不能時に古い保存先へ自動フォールバックする動作はありません。未設定の既存環境は変更しません。

## Knowledge Source

`KnowledgeSource.root_path` を正本の場所として扱う既存構造は維持します。既存の未登録ディレクトリSourceも引き続き利用できます。外部領域を追加rootとして登録しておけば、Knowledgeの同期と本文取得でも同じ識別ファイルによる接続確認を利用します。

走査失敗、読み込み失敗、走査件数上限、同期途中の切断では未確認資料を削除扱いにせず、既存チャンクを保持します。ファイル抽出エラー時も既存本文を空の本文で置換しません。登録前から存在するSourceが突然空になった場合は、空のマウントポイントとの区別がつかないため、全件削除を実行せずエラーとして保持します。全資料の意図的な削除が必要な場合は明示的なSource管理操作で処理してください。

走査・本文取得のファイルI/Oは、DBのAsyncSessionをワーカースレッドへ持ち込まず、rootごとの制限付きI/Oで実施します。読めないSourceを再同期する場合でも、他のSourceのDB内の検索情報を削除しません。GROWIソースの同期処理は変更しません。

## この変更に含めない統合

既存チャット／Docsの添付ピッカー、既存添付アップロードのStorage Context、Project managed workspaceの既定保存先を追加rootへ付け替える機構は含めません。外部Filesの資料をそれらから直接選択・参照する共通のstorage-id付き参照機構は未統合です。追加rootへのファイルの直接アップロードは可能ですが、既存のすべての添付経路が外部ストレージに対応したという意味ではありません。

追加ブラウザへ、既存Filesの全メディアプレーヤー、アーカイブ編集、ブックマーク等を移植したものでもありません。既存ローカルブラウザの機能はそのまま保持します。

## 検証上の制約

この成果物はsource integration前の実装です。独立した追加モジュールのテストと構文確認を実施しましたが、現行リポジトリ全体への適用、OpenAPI生成、全体の型検証・ビルド、別AIによるWebUI QA、Android実機、実SMB/NASの切断・再接続、Windows実機のjunction/reparse point、PostgreSQL/Qdrantとの全体回帰、GitHub push/CIは未完了です。追加APIを登録した完全なアプリから、既存の手順でOpenAPIと生成型を再生成する必要があります。生成ファイルは手編集していません。

## 参照した仕様

- 対象コード: `ttttdiva/41_AoiTalk` の `d3bacafad31f0b34e193f38f9ba0400b37468985`。既存 `storage_context.py`、`file_explorer_routes.py`、`document_storage_routes.py`、`generated_media_service.py`、`knowledge/service.py`、Web/Native Filesと認証クライアントを参照。
- Expo FileSystem legacy ReadingOptions: https://docs.expo.dev/versions/v55.0.0/sdk/filesystem-legacy/ 。`length` の原文は “Optional number of bytes to read.”（読み込むバイト数を任意指定）。Base64とpositionの指定を伴う分割読み込みを使用する。
