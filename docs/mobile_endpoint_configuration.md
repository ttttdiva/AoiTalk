# Mobile API endpoint configuration

Mobile client の既定API URLは、ビルド時の環境変数から決まります。公開用の
既定値は `EXPO_PUBLIC_AOITALK_API_URL`、開発ビルドだけで上書きする値は
`EXPO_PUBLIC_AOITALK_DEV_API_URL` です。未設定時の既定値は空で、URLを設定する
までAPIリクエストは送信されません。

```powershell
$env:EXPO_PUBLIC_AOITALK_API_URL = "https://api.example.test"
$env:EXPO_PUBLIC_AOITALK_DEV_API_URL = "http://192.168.1.20:8000"
npx expo start
```

Android EmulatorからホストPCの開発サーバーへ接続する場合だけ、開発用変数に
`http://10.0.2.2:3000` などを明示的に設定します。この値はアプリへ組み込まれた
個人用・公開用の既定値ではありません。実機では端末から到達できるLAN URLまたは
HTTPS URLを設定してください。

アプリの `Server / Network` では、基本API URLとWi-Fi用・その他ネットワーク用の
route URLを別々に保存できます。routeをOFFにすると基本API URLだけを使います。
routeがONでもSSIDを取得できない場合やネットワーク情報の取得に失敗した場合は、
その他ネットワーク用URL、未設定なら基本API URLへ戻ります。
