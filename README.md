# LINE Vault Bot

LINE公式アカウント（Messaging API）のボットサーバーです。

- 届いたメッセージをテキスト化して、Obsidian Vault（GitHubリポジトリ）に日別のMarkdownで記録します。
- オーナーとの1対1では、Vault全体を調べて質問に答えます。
- オーナーの指示で、グループへのお知らせ投稿と定期リマインドを行います。

Python（FastAPI）で書かれ、Google Cloud Run（東京リージョン asia-northeast1）で動きます。

## 目次

1. [できること](#できること)
2. [全体の構成](#全体の構成)
3. [Vault に作られるファイルと書式](#vault-に作られるファイルと書式)
4. [LINE での使い方（オーナー）](#line-での使い方オーナー)
5. [使用する Claude のモデル](#使用する-claude-のモデル)
6. [事前に用意するもの](#事前に用意するもの)
7. [Google Cloud の初期設定（Cloud Shell）](#google-cloud-の初期設定cloud-shell)
8. [Secret Manager への登録手順](#secret-manager-への登録手順)
9. [GitHub の設定と初回デプロイ](#github-の設定と初回デプロイ)
10. [LINE 側の設定手順](#line-側の設定手順)
11. [動作確認の手順](#動作確認の手順)
12. [設定値（環境変数）の一覧](#設定値環境変数の一覧)
13. [制約事項・できないこと](#制約事項できないこと)
14. [テスト](#テスト)
15. [トラブルシューティング](#トラブルシューティング)

---

## できること

| 機能 | 内容 |
|---|---|
| 記録 | グループと1対1の全メッセージを、日本時間の日別Markdownに1行ずつ記録 |
| テキスト化 | 画像：Claude で文字の書き起こしと内容の説明／PDF：Claude で全文テキスト化（長いものはページで分割）／Word・Excel・PowerPoint：ライブラリで抽出（旧形式は LibreOffice で変換してから） |
| 添付 | 全文は添付ノートに、日別ログにはリンクと2〜3行の要約。原本ファイルは保存しない |
| 送信取消・編集 | 該当行を「（送信取消）」に置き換え（添付ノートも削除）。編集は本文を書き換えて「（編集済み）」を付ける |
| 質問回答 | オーナーとの1対1だけ。Claude が「全文検索」「ファイル読み取り」「ファイル一覧」の3つの道具で Vault を調べ、根拠のノートのパスと日付を添えて回答 |
| お知らせ | オーナーが「○○グループに『…』と送って」と言うと、送り先と文面を確認し、「はい」で送信 |
| リマインド | オーナーが1対1で追加・変更・停止・一覧。`ボット設定/リマインド.md` に保存し、5分おきに時刻が来たものを送信 |
| 通数の確認 | お知らせ・リマインドは、送る前に当月の残り通数を確認し、足りなければ送らずオーナーに知らせる |

---

## 全体の構成

```text
LINE ──POST /webhook──▶ Cloud Run（このサーバー）
                         │ 署名を検証し、イベントごとに Cloud Tasks へ積んで すぐ 200 を返す
                         ▼
               Cloud Tasks（2つのキュー）
                 ├─ line-ingest（記録用・同時実行1）──▶ POST /tasks/ingest ──▶ GitHub（Vault）へ記録
                 └─ line-ask   （質問用）          ──▶ POST /tasks/ask    ──▶ Claude で回答 → LINE へ返信

Cloud Scheduler（5分おき）──▶ POST /cron/reminders ──▶ 時刻が来たリマインドを LINE グループへ送信
```

| エンドポイント | 呼び出し元 | 内容 |
|---|---|---|
| `POST /webhook` | LINE | `x-line-signature` を検証し、不正なら 400。イベントごとに Cloud Tasks へ積み、すぐ 200 を返す |
| `POST /tasks/ingest` | Cloud Tasks のみ | 記録処理 |
| `POST /tasks/ask` | Cloud Tasks のみ | 質問回答と、オーナーからの指示の処理 |
| `POST /cron/reminders` | Cloud Scheduler のみ | リマインドの送信 |
| `GET /health` | 誰でも | 動作確認用（`{"status":"ok"}` を返すだけ） |

`/tasks` と `/cron` は、Cloud Tasks・Cloud Scheduler が付ける OIDC トークン（Google が署名した ID トークン）を検証し、**署名・発行者・宛先（このサービスの URL）・サービスアカウントのメールアドレス**がすべて正しいときだけ受け付けます。`/tasks` は Cloud Tasks 用のサービスアカウント、`/cron` は Cloud Scheduler 用のサービスアカウントだけを許可し、それ以外は 403 で拒否します。

秘密情報（LINE のチャネルシークレットなど）は Secret Manager に保存し、Cloud Run が環境変数として読み込みます。デプロイは GitHub Actions（main への push で自動、Workload Identity 連携でキー不要）です。

### コードの構成

| ファイル | 役割 |
|---|---|
| `app/main.py` | エンドポイントの定義（Webhook に必要な軽い部品だけを読み込む） |
| `app/workers.py` | 重い部品（Claude・LINE API クライアント・文書処理）の組み立て。起動後に裏で読み込む |
| `app/ingest.py` | 記録処理（重複防止・送信取消・編集・添付ノート） |
| `app/logformat.py` | 日別ログの1行の書式と、行の追加・置き換え |
| `app/transcribe.py` / `app/extract.py` | 画像・PDF の文字起こし（Claude）／Office 文書の抽出（ライブラリ・LibreOffice） |
| `app/owner.py` / `app/agent.py` | オーナーの指示の振り分け・お知らせ・リマインド操作／質問回答（3つの道具） |
| `app/vault.py` | Vault の浅い複製と、全文検索・読み取り・一覧の道具 |
| `app/schedule.py` / `app/reminders.py` / `app/cron.py` | スケジュール書式の解析と時刻判定／リマインド.md の読み書き／送信処理 |
| `app/github_store.py` | GitHub Contents API での読み書き（409 競合時の再試行、書き込み範囲の制限） |
| `app/auth.py` | `/tasks`・`/cron` の呼び出し元（OIDC トークン）の検証 |
| `app/line_api.py` / `app/claude.py` / `app/tasks_client.py` | LINE 公式SDK・Claude API・Cloud Tasks の呼び出し |

---

## Vault に作られるファイルと書式

ボットが書き換えるのは **`LINEログ/` と `ボット設定/` の中だけ**です（それ以外への書き込み・削除はコードで必ず拒否します）。

```text
LINEログ/
├── 家族グループ/
│   └── 2026-11-14.md                 … グループの日別ログ
├── 個別/
│   └── 山田/
│       └── 2026-11-14.md             … 1対1の日別ログ（相手の表示名ごと）
└── 添付/
    ├── 2026-11-14_0930_見積書.pdf.md  … 添付の全文
    └── 2026-11-14_0945_画像.md
ボット設定/
├── グループ一覧.md                    … グループIDと名前（初めてのグループで自動追記）
├── リマインド.md                      … リマインドの一覧
├── 送信待ち.md                        … 「はい」待ちのお知らせ（ボットが自動管理）
└── 通知待ち.md                        … 次にオーナーへ知らせる内容（ボットが自動管理）
```

### 日別ログ

1行の形式は `- HH:MM 表示名：本文 ^m<メッセージID>`（日本時間）です。

```markdown
# 2026-11-14（土） 家族グループ

- 09:30 山田：明日は10時集合です ^m587263915830192
- 09:31 佐藤：了解！<br>車で行きます ^m587263915830201
- 09:45 山田：[画像] [[LINEログ/添付/2026-11-14_0945_画像.md|画像]]<br>集合場所の地図<br>駅の北口が目印 ^m587263915830222
- 10:02 鈴木：[PDF] [[LINEログ/添付/2026-11-14_1002_見積書.pdf.md|見積書.pdf]]<br>BBQ用品の見積書<br>合計12,345円 ^m587263915830245
- 10:05 鈴木：（送信取消） ^m587263915830250
- 10:10 山田：雨なら中止です（編集済み） %%edited:1763082600000%% ^m587263915830261
- 18:00 記録ボット：明日の集合は10時です ^m587263915830300
```

| メッセージ | 記録の仕方 |
|---|---|
| テキスト | そのまま記録（改行は `<br>`。1メッセージを必ず1行にするため） |
| 画像 | Claude で書き起こし・説明 → 添付ノートに全文、日別ログにリンクと要約 |
| ファイル（PDF） | Claude で全文テキスト化（20ページずつに分割して処理）→ 同上 |
| ファイル（Word・Excel・PowerPoint） | ライブラリで抽出（`.doc` `.xls` `.ppt` などは LibreOffice で変換してから）→ 同上 |
| ファイル（txt・csv など） | 文字コードを判定して全文 → 同上 |
| 動画・音声・スタンプ・位置情報 | `[動画]` `[音声]` `[スタンプ]` `[位置情報]` の印だけ |
| 上記以外のファイル（zip など） | `[ファイル] 名前（テキスト化の対象外の形式）` |
| ボットが送ったお知らせ・リマインド | ボットの表示名で記録 |

- **重複防止**：同じメッセージIDの行が日別ログにあれば書き込みません（LINE の再送や Cloud Tasks の再試行で二重にならない）。
- **テキスト化の失敗**：`[画像] [テキスト化失敗：理由]` と記録して、Cloud Tasks の再試行に任せます。再試行で成功すると、その行が正しい内容に置き換わります。パスワード付きPDFのように再試行しても直らないものは、記録だけして再試行しません。
- **送信取消**：該当行を `（送信取消）` に置き換え、添付ノートがあれば削除します（日付をまたいだ取り消しにも対応）。元のメッセージがまだ記録されていない場合も、取り消した日のログに `（送信取消）` の行を残し、後から元の内容が書き込まれないようにします（元のメッセージが前日以前のものでも同様です）。
- **編集**：該当行の本文を書き換えて `（編集済み）` を付けます。編集イベントは届く順番が前後することがあるため、編集時刻を Obsidian の非表示コメント `%%edited:…%%` として行内に持ち、より新しい編集だけを反映します（このコメントは Obsidian の閲覧表示では見えません）。元のメッセージより先に編集が届いた場合は、編集後の本文で1行だけ記録します（日付をまたいだ場合も同様です）。
- **表示名**はプロフィール取得APIで取得し、キャッシュします。グループ名はグループ概要APIで取得して `グループ一覧.md` に登録し、**以降はその登録名をフォルダ名に使います**（LINE 上で改名されても過去ログと同じフォルダに記録し続けます。名前を変えたいときは `グループ一覧.md` を書き換えてください）。
- オーナーとの1対1でボットが返した回答・確認メッセージは記録しません（次の検索でボット自身の回答が根拠として拾われるのを防ぐため）。オーナーの発言は記録します。

### 添付ノート（`LINEログ/添付/YYYY-MM-DD_HHMM_<ファイル名>.md`）

```markdown
---
type: "LINE添付"
kind: "PDF"
file_name: "見積書.pdf"
chat: "家族グループ"
sender: "鈴木"
date: "2026-11-14"
time: "10:02"
message_id: "587263915830245"
summary: ["BBQ用品の見積書", "合計12,345円"]
---
# 見積書.pdf

- 送信元：[[LINEログ/家族グループ/2026-11-14|家族グループ 2026-11-14]]（10:02 鈴木）

## 要約
- BBQ用品の見積書
- 合計12,345円

## 全文
--- p.1 ---
（PDFの全文）
```

同じ分に同じ名前の添付が届いた場合は、2つ目以降のファイル名の末尾に `_m<メッセージID>` を付けて区別します。

### グループ一覧（`ボット設定/グループ一覧.md`）

```markdown
- 家族グループ：C1234567890abcdef1234567890abcdef
- 町内会：Cfedcba0987654321fedcba0987654321
```

- 書式は `- グループ名：グループID`。名前は書き換えて構いません（IDは変えないでください）。
- 同じ名前のグループが既にある場合は `家族グループ（cdef）` のように IDの末尾4文字を付けて登録します。

### リマインド（`ボット設定/リマインド.md`）

人が直接編集しても壊れにくいよう、1件ごとに見出しと箇条書きで書きます。

```markdown
## R1
- 送り先: 家族グループ
- スケジュール: 毎週 月,木 07:00
- 本文: 今日は燃えるゴミの日です
  前の晩には出さないでください
- 状態: 有効
- 最終送信日時: 2026-11-12 07:00
- 更新日時: 2026-11-01 12:00
```

| 項目 | 内容 |
|---|---|
| `## R1` | ID（見出し）。ID の後ろに説明を書いても構いません（例：`## R1 ゴミ出し`） |
| 送り先 | `グループ一覧.md` にあるグループ名 |
| スケジュール | 下の表の書式（日本時間・祝日は考慮しない） |
| 本文 | 送る文面。2行目以降は行頭に半角スペース2つ |
| 状態 | `有効` または `無効` |
| 最終送信日時 | ボットが記録（`YYYY-MM-DD HH:MM`。未送信なら `未送信`） |
| 更新日時 | ボットが記録。この日時より前の予定時刻の回は送りません（登録・変更した直後に、すでに過ぎた回を送らないため） |

- `:` と `：` のどちらでもよく、「送信先」「内容」「日時」などの別名も読めます。
- ボットが変更するときは該当する項目の行だけを書き換えます（人が書き足したメモはそのまま残ります）。
- 書式が読めない件は送信せず、LINE で一覧を表示したときにエラーとして表示します。

| スケジュールの書き方 | 意味 |
|---|---|
| `毎日 08:00` / `毎日 08:00,20:00` | 毎日（複数の時刻も可） |
| `平日 07:30` | 月〜金 |
| `毎週 月,木 09:00` | 指定した曜日（`土日 10:00` は `毎週 土,日 10:00` になります） |
| `毎月 1,15日 10:00` / `毎月 末日 18:00` | 毎月の指定日／月末 |
| `毎月 第2火曜 19:00` / `毎月 第1,3水曜 09:00` / `毎月 最終金曜 18:00` | 第n曜日／最終曜日 |
| `毎年 12-25 09:00` | 毎年 |
| `2026-12-01 09:00` | 1回のみ（送信後に自動で `無効` になります） |

時刻は `9時` `9時半` `午後3時` なども読み、保存時は上の形に整えます。31日のように存在しない日がある月は、その月は送りません（月末に送りたい場合は `末日`）。

---

## LINE での使い方（オーナー）

オーナー（`OWNER_LINE_USER_ID` のユーザー）がボットとの1対1で話しかけます。受け付けるとローディング表示が出ます。

| 言い方の例 | 動き |
|---|---|
| 「先週の家族グループで決まった集合時間は？」 | Vault を調べて回答。末尾に `📝 根拠` としてノートのパスと日付を表示。見つからなければ見つからないと答える |
| 「家族グループに『明日は10時集合です』と送って」 | 送り先と文面の確認を返信（「はい」「いいえ」のボタン付き）→「はい」で送信 |
| 「毎週月曜と木曜の朝7時に家族グループへ『ゴミ出しの日です』とリマインドして」 | リマインドを登録し、ID と次回の日時を返信 |
| 「R1 を毎日6時半に変えて」「R1 を止めて」「R1 を再開して」 | リマインドの変更・停止・再開 |
| 「リマインドの一覧」 | 全件の状態・次回の日時・最終送信日時を表示 |

- 受信から50秒以内に答えられたら応答メッセージ、超えたらプッシュメッセージで返します（応答メッセージは通数にカウントされません）。
- お知らせの確認は30分以内に「はい」と答えてください。確認待ちの間に別のことを話しかけると、その確認は取り消されます。
- リマインドを残り通数不足などで送れなかったときは、次にオーナーが1対1で話しかけたときに、応答メッセージの先頭で `📢 お知らせ` として知らせます。LINE 側の一時的な障害などでそのお知らせ自体を届けられなかった場合は、消さずに残して次の機会にもう一度知らせます。
- オーナー以外の人との1対1には返信しません（記録はします）。

---

## 使用する Claude のモデル

モデル名は環境変数で指定し、文字起こし用と回答用を分けています。実装時点（2026年11月）の公式のモデル一覧（Claude Fable 5.2／Claude Opus 5.5／Claude Sonnet 5.5／Claude Haiku 5.5）から、次のように選んでいます。

- **質問回答・指示の振り分け**：公式ドキュメントで「迷ったらまず使う」とされている **Claude Opus 5.5**
- **文字起こし・要約**（写真・PDF の文字起こしと、添付ファイルの要約）：**Claude Sonnet 5.5**。Opus 5.5 の半額で、PDF・画像の読み取りと構造化出力にも対応しています

| 用途 | 環境変数 | 既定値 | 思考の深さ（effort） |
|---|---|---|---|
| 文字起こし・要約 | `CLAUDE_MODEL_TRANSCRIBE` | `claude-sonnet-5-5` | `CLAUDE_EFFORT_TRANSCRIBE=medium` |
| 質問回答 | `CLAUDE_MODEL_ANSWER` | `claude-opus-5-5` | `CLAUDE_EFFORT_ANSWER=medium` |
| オーナーの指示の振り分け | `CLAUDE_MODEL_ANSWER` を使用 | 〃 | `CLAUDE_EFFORT_ROUTER=low` |

- 料金の目安（100万トークンあたり、入力／出力）：Opus 5.5 は $4／$20、Sonnet 5.5 は $2／$10、Haiku 5.5 は $0.10〜／$0.50〜。
- モデルを変えるときは、GitHub のリポジトリ変数 `CLAUDE_MODEL_TRANSCRIBE`（文字起こし・要約）や `CLAUDE_MODEL_ANSWER`（質問回答）を設定して再デプロイします（例：文字起こしを Opus 5.5 に戻すなら `CLAUDE_MODEL_TRANSCRIBE=claude-opus-5-5`）。リポジトリ変数を設定すると、上の既定値より優先されます。
- 安全上の理由で応答が拒否された場合に、Anthropic が推奨するモデルで自動的にやり直す「サーバー側フォールバック」（ベータ機能）を有効にしています。使わない場合は `CLAUDE_FALLBACKS=off` にしてください。フォールバックに対応していないモデルを指定した場合は、自動的にフォールバック無しで呼び出します。

---

## 事前に用意するもの

1. **Google Cloud のプロジェクト**（課金が有効なもの）。以降の操作は [Cloud Shell](https://shell.cloud.google.com/) で行います。
2. **LINE公式アカウントと Messaging API チャネル**（[LINE Developers コンソール](https://developers.line.biz/console/)）
   - 「チャネル基本設定」の **チャネルシークレット** と **あなたのユーザーID**（オーナーのユーザーID）
   - 「Messaging API設定」で発行する **チャネルアクセストークン（長期）**
3. **Anthropic の API キー**（[Claude Console の API Keys](https://platform.claude.com/settings/keys) で作成）
4. **Vault の GitHub リポジトリ**（Obsidian Vault を入れたリポジトリ。例：`yourname/obsidian-vault`）と、その **fine-grained personal access token**
   - [トークンの作成画面](https://github.com/settings/personal-access-tokens/new) で、Repository access を「Only select repositories」にして Vault のリポジトリだけを選び、Repository permissions の **Contents を「Read and write」** にします（Metadata の読み取りは自動で付きます）。
   - 有効期限が切れると記録できなくなります。更新方法は [Secret Manager の値を更新する](#値を更新するときトークンの更新など) を参照してください。
5. **このリポジトリ**（ボットのコード。GitHub Actions でデプロイします）

> 画面の名前や表記は、各サービスの変更で変わることがあります。

---

## Google Cloud の初期設定（Cloud Shell）

[Cloud Shell](https://shell.cloud.google.com/) を開き、上から順に実行します。各ブロックはコードの右上のボタンでコピーできます。

### 1. 変数の設定

`PROJECT_ID` は自分のプロジェクトID に書き換えてください。`GITHUB_REPO` は **このボットのコードのリポジトリ**（GitHub の表記どおり、大文字小文字も一致させる）です。

```bash
export PROJECT_ID="ここにプロジェクトID"
export REGION="asia-northeast1"
export SERVICE_NAME="line-vault-bot"
export AR_REPO="line-bot"
export GITHUB_REPO="tanukichiyamaguchi/LINE"
gcloud config set project "$PROJECT_ID"
export PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
export RUN_SA="line-bot-run@${PROJECT_ID}.iam.gserviceaccount.com"
export TASKS_SA="line-bot-tasks@${PROJECT_ID}.iam.gserviceaccount.com"
export SCHEDULER_SA="line-bot-scheduler@${PROJECT_ID}.iam.gserviceaccount.com"
export DEPLOYER_SA="line-bot-deployer@${PROJECT_ID}.iam.gserviceaccount.com"
echo "プロジェクト: $PROJECT_ID（番号 $PROJECT_NUMBER）"
```

> Cloud Shell は一定時間操作しないと切断され、変数が消えます。再接続したら、このブロックをもう一度実行してください。

### 2. API の有効化

```bash
gcloud services enable \
  run.googleapis.com \
  cloudtasks.googleapis.com \
  cloudscheduler.googleapis.com \
  secretmanager.googleapis.com \
  artifactregistry.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  sts.googleapis.com
```

### 3. コンテナの保存先（Artifact Registry）

```bash
gcloud artifacts repositories create "$AR_REPO" \
  --repository-format=docker \
  --location="$REGION" \
  --description="LINE Vault Bot のコンテナ"
```

### 4. サービスアカウントの作成

| サービスアカウント | 用途 |
|---|---|
| `line-bot-run` | Cloud Run でボットを動かす |
| `line-bot-tasks` | Cloud Tasks が `/tasks` を呼び出すときの身元 |
| `line-bot-scheduler` | Cloud Scheduler が `/cron` を呼び出すときの身元 |
| `line-bot-deployer` | GitHub Actions がデプロイするときの身元 |

```bash
gcloud iam service-accounts create line-bot-run --display-name="LINE bot (Cloud Run runtime)"
gcloud iam service-accounts create line-bot-tasks --display-name="LINE bot (Cloud Tasks invoker)"
gcloud iam service-accounts create line-bot-scheduler --display-name="LINE bot (Cloud Scheduler invoker)"
gcloud iam service-accounts create line-bot-deployer --display-name="LINE bot (GitHub Actions deployer)"
```

### 5. 権限の付与

```bash
# 実行用：Cloud Tasks にタスクを積む
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${RUN_SA}" --role="roles/cloudtasks.enqueuer" --condition=None

# 実行用：タスクに line-bot-tasks の OIDC トークンを付ける（そのサービスアカウントとして振る舞う）
gcloud iam service-accounts add-iam-policy-binding "$TASKS_SA" \
  --member="serviceAccount:${RUN_SA}" --role="roles/iam.serviceAccountUser"

# デプロイ用：Cloud Run へのデプロイ
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${DEPLOYER_SA}" --role="roles/run.developer" --condition=None

# デプロイ用：コンテナの登録
gcloud artifacts repositories add-iam-policy-binding "$AR_REPO" --location="$REGION" \
  --member="serviceAccount:${DEPLOYER_SA}" --role="roles/artifactregistry.writer"

# デプロイ用：line-bot-run を Cloud Run に割り当てる
gcloud iam service-accounts add-iam-policy-binding "$RUN_SA" \
  --member="serviceAccount:${DEPLOYER_SA}" --role="roles/iam.serviceAccountUser"
```

### 6. 秘密情報の登録

[Secret Manager への登録手順](#secret-manager-への登録手順) を実行してください（6つの値を登録し、`line-bot-run` に読み取り権限を付けます）。

### 7. Cloud Tasks のキュー

記録用は同時実行1（GitHub への書き込みを順番に行うため）、質問用も同時実行1（オーナーとのやり取りを順番どおりに処理するため）です。

```bash
gcloud tasks queues create line-ingest --location="$REGION" \
  --max-concurrent-dispatches=1 --max-attempts=8 --min-backoff=30s --max-backoff=1800s

gcloud tasks queues create line-ask --location="$REGION" \
  --max-concurrent-dispatches=1 --max-attempts=2 --min-backoff=10s
```

- `line-ingest` は、テキスト化や GitHub への書き込みに失敗すると、間隔をあけて最大8回まで実行します。
- `line-ask` は、回答の途中で失敗しても二重に返信しないよう、処理の中でエラーを返信してから正常終了します（2回目が動くのは、サーバー自体が落ちた場合だけです）。

### 8. Cloud Run サービスの作成（公開設定と URL の確定）

LINE から `/webhook` に届く必要があるため、サービスは誰でも呼び出せる設定にします（`/tasks` と `/cron` はアプリ側で OIDC トークンを検証して守ります）。ここでは仮のコンテナで作成し、URL を確定させます。本番のコンテナは GitHub Actions がデプロイします。

```bash
gcloud run deploy "$SERVICE_NAME" \
  --image=us-docker.pkg.dev/cloudrun/container/hello \
  --region="$REGION" \
  --service-account="$RUN_SA" \
  --allow-unauthenticated
```

```bash
export SERVICE_URL="$(gcloud run services describe "$SERVICE_NAME" --region="$REGION" --format='value(status.url)')"
echo "$SERVICE_URL"
```

> 組織のポリシーで「allUsers」を許可できない場合は、`--allow-unauthenticated` が失敗します。その場合は代わりに次を実行してください（呼び出し時の IAM チェックを無効にして公開します）。
>
> ```bash
> gcloud run services update "$SERVICE_NAME" --region="$REGION" --no-invoker-iam-check
> ```

### 9. Workload Identity 連携（GitHub Actions からのデプロイ用）

サービスアカウントのキーを作らずに、`GITHUB_REPO` のリポジトリの GitHub Actions だけがデプロイできるようにします。

```bash
gcloud iam workload-identity-pools create github \
  --location=global --display-name="GitHub Actions"
```

```bash
gcloud iam workload-identity-pools providers create-oidc line-bot-repo \
  --location=global \
  --workload-identity-pool=github \
  --display-name="LINE bot repository" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner" \
  --attribute-condition="assertion.repository == '${GITHUB_REPO}'"
```

```bash
gcloud iam service-accounts add-iam-policy-binding "$DEPLOYER_SA" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github/attribute.repository/${GITHUB_REPO}"
export WIF_PROVIDER="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github/providers/line-bot-repo"
```

> 権限の反映には数分かかることがあります。

### 10. GitHub に設定する値の表示

次を実行すると、[GitHub のリポジトリ変数](#github-の設定と初回デプロイ) に設定する値が表示されます。

```bash
cat <<EOF
GCP_PROJECT_ID=${PROJECT_ID}
GCP_REGION=${REGION}
SERVICE_NAME=${SERVICE_NAME}
AR_REPOSITORY=${AR_REPO}
SERVICE_URL=${SERVICE_URL}
WIF_PROVIDER=${WIF_PROVIDER}
DEPLOYER_SA=${DEPLOYER_SA}
RUNTIME_SA=${RUN_SA}
TASKS_INVOKER_SA=${TASKS_SA}
SCHEDULER_INVOKER_SA=${SCHEDULER_SA}
EOF
```

### 11. Cloud Scheduler（5分おき）

**初回デプロイが終わってから**作成してください（先に作ると、仮のコンテナを呼び出して失敗が記録されます）。

```bash
gcloud scheduler jobs create http line-reminders \
  --location="$REGION" \
  --schedule="*/5 * * * *" \
  --time-zone="Asia/Tokyo" \
  --uri="${SERVICE_URL}/cron/reminders" \
  --http-method=POST \
  --oidc-service-account-email="$SCHEDULER_SA" \
  --oidc-token-audience="$SERVICE_URL" \
  --attempt-deadline=300s
```

---

## Secret Manager への登録手順

次の6つを登録します。値は画面に表示されず、シェルの履歴にも残らない方法で入力します（コマンドを実行すると入力待ちになるので、値を貼り付けて Enter）。

| シークレット名 | 環境変数 | 値 |
|---|---|---|
| `line-channel-secret` | `LINE_CHANNEL_SECRET` | LINE のチャネルシークレット |
| `line-channel-access-token` | `LINE_CHANNEL_ACCESS_TOKEN` | LINE のチャネルアクセストークン（長期） |
| `owner-line-user-id` | `OWNER_LINE_USER_ID` | オーナーのユーザーID（`U` で始まる33文字） |
| `anthropic-api-key` | `ANTHROPIC_API_KEY` | Anthropic の API キー |
| `github-token` | `GITHUB_TOKEN` | Vault リポジトリ用の fine-grained personal access token |
| `vault-repo` | `VAULT_REPO` | Vault のリポジトリ名（`owner/repo` の形。例：`yourname/obsidian-vault`） |

```bash
read -rsp "LINE チャネルシークレット: " VALUE && echo && printf '%s' "$VALUE" | gcloud secrets create line-channel-secret --replication-policy=automatic --data-file=- ; unset VALUE
```

```bash
read -rsp "LINE チャネルアクセストークン（長期）: " VALUE && echo && printf '%s' "$VALUE" | gcloud secrets create line-channel-access-token --replication-policy=automatic --data-file=- ; unset VALUE
```

```bash
read -rsp "オーナーのユーザーID（U から始まる）: " VALUE && echo && printf '%s' "$VALUE" | gcloud secrets create owner-line-user-id --replication-policy=automatic --data-file=- ; unset VALUE
```

```bash
read -rsp "Anthropic API キー: " VALUE && echo && printf '%s' "$VALUE" | gcloud secrets create anthropic-api-key --replication-policy=automatic --data-file=- ; unset VALUE
```

```bash
read -rsp "GitHub のトークン（Vault 用）: " VALUE && echo && printf '%s' "$VALUE" | gcloud secrets create github-token --replication-policy=automatic --data-file=- ; unset VALUE
```

```bash
read -rsp "Vault のリポジトリ（owner/repo）: " VALUE && echo && printf '%s' "$VALUE" | gcloud secrets create vault-repo --replication-policy=automatic --data-file=- ; unset VALUE
```

Cloud Run の実行用サービスアカウントに、読み取り権限を付けます。

```bash
for s in line-channel-secret line-channel-access-token owner-line-user-id anthropic-api-key github-token vault-repo; do
  gcloud secrets add-iam-policy-binding "$s" \
    --member="serviceAccount:${RUN_SA}" --role="roles/secretmanager.secretAccessor"
done
```

登録できたかを確認します（値は表示されません）。

```bash
gcloud secrets list --format="table(name,createTime)"
```

### 値を更新するとき（トークンの更新など）

新しいバージョンを追加し、Cloud Run の新しいリビジョンを作って読み込ませます（例は GitHub のトークン）。

```bash
read -rsp "新しい GitHub のトークン: " VALUE && echo && printf '%s' "$VALUE" | gcloud secrets versions add github-token --data-file=- ; unset VALUE
```

```bash
gcloud run services update "$SERVICE_NAME" --region="$REGION" --update-env-vars="SECRETS_UPDATED_AT=$(date +%s)"
```

---

## GitHub の設定と初回デプロイ

### GitHub のリポジトリ変数を設定する

このリポジトリの **Settings → Secrets and variables → Actions → Variables タブ → New repository variable** で、[初期設定の手順10](#10-github-に設定する値の表示) で表示された10個を1つずつ登録します（`=` の左が Name、右が Value）。いずれも秘密情報ではないため「Variables」に登録します。

| 変数名 | 例 |
|---|---|
| `GCP_PROJECT_ID` | `my-project` |
| `GCP_REGION` | `asia-northeast1` |
| `SERVICE_NAME` | `line-vault-bot` |
| `AR_REPOSITORY` | `line-bot` |
| `SERVICE_URL` | `https://line-vault-bot-xxxxxxxxxx-an.a.run.app` |
| `WIF_PROVIDER` | `projects/123456789/locations/global/workloadIdentityPools/github/providers/line-bot-repo` |
| `DEPLOYER_SA` | `line-bot-deployer@my-project.iam.gserviceaccount.com` |
| `RUNTIME_SA` | `line-bot-run@my-project.iam.gserviceaccount.com` |
| `TASKS_INVOKER_SA` | `line-bot-tasks@my-project.iam.gserviceaccount.com` |
| `SCHEDULER_INVOKER_SA` | `line-bot-scheduler@my-project.iam.gserviceaccount.com` |

必要に応じて、次の変数も設定できます（未設定なら既定値）。

| 変数名 | 既定値 | 内容 |
|---|---|---|
| `CLAUDE_MODEL_TRANSCRIBE` | `claude-sonnet-5-5` | 文字起こし・要約のモデル |
| `CLAUDE_MODEL_ANSWER` | `claude-opus-5-5` | 質問回答・指示の振り分けのモデル |
| `CLAUDE_EFFORT_TRANSCRIBE` / `CLAUDE_EFFORT_ANSWER` / `CLAUDE_EFFORT_ROUTER` | `medium` / `medium` / `low` | 思考の深さ（`low` `medium` `high` `xhigh` `max`） |
| `CLAUDE_FALLBACKS` | `default` | 拒否時のサーバー側フォールバック（使わないなら `off`） |
| `VAULT_BRANCH` | （Vault の既定ブランチ） | Vault の書き込み・読み取りに使うブランチ |
| `MIN_INSTANCES` | `1` | Cloud Run の最小インスタンス数（下の「起動待ちと最小インスタンス数」を参照） |

### デプロイの流れ

`.github/workflows/deploy.yml` が次のように動きます。

| きっかけ | 実行内容 |
|---|---|
| プルリクエスト | テスト → コンテナのビルド → コンテナ内での動作確認（LibreOffice による旧形式の変換を含む） |
| `main` への push | 上に加えて → Artifact Registry へ登録 → Cloud Run へデプロイ → `/health` で起動確認 |

初回は、このプルリクエストを `main` にマージするとデプロイされます（手動で動かす場合は Actions タブ →「テストとデプロイ」→「Run workflow」で `main` を選択）。リポジトリ変数が未設定のままだと、どの変数が足りないかを表示して止まります。

デプロイ後、[初期設定の手順11（Cloud Scheduler）](#11-cloud-scheduler5分おき) を実行してください。

---

## LINE 側の設定手順

[LINE Developers コンソール](https://developers.line.biz/console/) で、ボットのチャネルを開きます。

1. **Messaging API設定** タブ
   1. 「Webhook URL」に `<SERVICE_URL>/webhook` を入力して更新し、「検証」を押して「成功」と表示されることを確認します（例：`https://line-vault-bot-xxxxxxxxxx-an.a.run.app/webhook`）。
   2. 「Webhookの利用」をオンにします。
   3. 「Webhookの再送」をオンにします（強く推奨。既定ではオフです。サーバーが一時的に応答できなかったときに LINE が再送します。重複は自動で防ぎます）。
   4. 「グループトーク・複数人トークへの参加を許可する」を許可にします（LINE Official Account Manager 側の設定画面に移動します）。
   5. 「応答メッセージ」をオフにします（LINE Official Account Manager の「応答設定」で、Webhook をオン・応答メッセージをオフ）。
2. **チャネル基本設定** タブ
   - 「あなたのユーザーID」がオーナーのユーザーID です（Secret Manager の `owner-line-user-id` に登録した値）。このユーザーとの1対1だけが質問回答・指示の対象になります。
3. オーナーの LINE アプリでボットを友だち追加し、記録したいグループにボットを招待します。

---

## 動作確認の手順

Cloud Shell で [初期設定の手順1（変数の設定）](#1-変数の設定) を実行してから行ってください。

### 1. サーバーの起動確認

```bash
curl -s "${SERVICE_URL}/health"
```

`{"status":"ok"}` と表示されれば起動しています。

### 2. 署名検証の確認

署名の無いリクエストが 400 で拒否されることを確認します。

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST -H "Content-Type: application/json" -d '{"events":[]}' "${SERVICE_URL}/webhook"
```

`400` と表示されれば正常です。続けて、LINE Developers コンソールの「Webhook URL」の「検証」が「成功」になることを確認します。

### 3. `/tasks` と `/cron` が守られていることの確認

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST "${SERVICE_URL}/tasks/ingest"
curl -s -o /dev/null -w "%{http_code}\n" -X POST "${SERVICE_URL}/cron/reminders"
```

どちらも `403` と表示されれば正常です。

### 4. 記録の確認

| 操作 | 確認すること |
|---|---|
| ボットとの1対1で「テスト」と送る | Vault に `LINEログ/個別/<あなたの表示名>/<今日>.md` ができ、`- HH:MM 表示名：テスト ^m…` の行がある |
| ボットを招待したグループで発言する | `ボット設定/グループ一覧.md` にグループが追記され、`LINEログ/<グループ名>/<今日>.md` に記録される |
| グループに写真・PDF・Word ファイルを送る | `LINEログ/添付/` に全文のノートができ、日別ログにリンクと要約が入る |
| 送ったメッセージを送信取消する | 該当行が `（送信取消）` になり、添付ノートがあれば消える |
| グループで送ったテキストを編集する | 該当行の本文が書き換わり `（編集済み）` が付く |

画像やPDFは文字起こしに時間がかかるため、数十秒〜数分後に反映されます。キューの状態は次で確認できます。

```bash
gcloud tasks list --queue=line-ingest --location="$REGION"
```

### 5. 質問回答・お知らせ・リマインドの確認

1. ボットとの1対1で「今日の家族グループでは何を話した？」のように質問し、ローディング表示のあとに回答と `📝 根拠` が返ることを確認します。
2. 「（グループ名）に『テストです』と送って」と送り、確認メッセージに「はい」と答えて、グループに届くこと・日別ログにボットの発言として記録されることを確認します。
3. 「5分後に（グループ名）へ『リマインドのテスト』と送って」と送り、`ボット設定/リマインド.md` に追加されることを確認します。予定時刻を過ぎてから次を実行すると、すぐに送信処理を動かせます（5分おきの自動実行でも送られます）。

```bash
gcloud scheduler jobs run line-reminders --location="$REGION"
```

### 6. ログの確認

```bash
gcloud run services logs read "$SERVICE_NAME" --region="$REGION" --limit=50
```

---

## 設定値（環境変数）の一覧

| 環境変数 | 設定元 | 内容 |
|---|---|---|
| `LINE_CHANNEL_SECRET` | Secret Manager | 署名検証に使うチャネルシークレット |
| `LINE_CHANNEL_ACCESS_TOKEN` | Secret Manager | LINE API の呼び出しに使うトークン |
| `OWNER_LINE_USER_ID` | Secret Manager | オーナーのユーザーID |
| `ANTHROPIC_API_KEY` | Secret Manager | Claude API のキー |
| `GITHUB_TOKEN` | Secret Manager | Vault リポジトリの読み書きに使うトークン |
| `VAULT_REPO` | Secret Manager | Vault のリポジトリ（`owner/repo`） |
| `GCP_PROJECT_ID` / `GCP_REGION` | デプロイ時（リポジトリ変数） | Cloud Tasks のプロジェクトとリージョン |
| `SERVICE_URL` | 〃 | このサービスの URL（Cloud Tasks の呼び出し先と、OIDC トークンの宛先の確認に使う） |
| `TASKS_INVOKER_SA` / `SCHEDULER_INVOKER_SA` | 〃 | `/tasks`・`/cron` の呼び出しを許可するサービスアカウント |
| `INGEST_QUEUE` / `ASK_QUEUE` | ワークフローに記載 | キュー名（`line-ingest` / `line-ask`） |
| `CLAUDE_MODEL_TRANSCRIBE` / `CLAUDE_MODEL_ANSWER` | 〃 | Claude のモデル名（文字起こし用／回答用） |
| `CLAUDE_EFFORT_TRANSCRIBE` / `CLAUDE_EFFORT_ANSWER` / `CLAUDE_EFFORT_ROUTER` | 〃 | 思考の深さ |
| `CLAUDE_FALLBACKS` | 〃 | 拒否時のサーバー側フォールバック（`default` / `off`） |
| `VAULT_BRANCH` | 〃（任意） | Vault のブランチ |
| `PDF_PAGES_PER_CHUNK` | 任意（既定 20） | PDF を何ページずつ Claude に渡すか |
| `MAX_ATTACHMENT_MB` | 任意（既定 50） | これより大きい添付はテキスト化しない |
| `MAX_EXTRACT_CHARS` | 任意（既定 1000000） | 添付ノートに書く全文の上限文字数 |
| `REMINDER_CATCHUP_MINUTES` | 任意（既定 30） | 予定時刻からこの分数以内なら遅れても送る（それより古い回は送らない） |
| `REPLY_DEADLINE_SECONDS` | 任意（既定 50） | 受信からこの秒数以内なら応答メッセージで返す |
| `CONFIRM_TTL_MINUTES` | 任意（既定 30） | お知らせの「はい」を待つ時間 |
| `ASK_TIME_BUDGET_SECONDS` | 任意（既定 300） | 質問回答で Vault を調べる時間の目安 |

任意の値を変えるときは、`.github/workflows/deploy.yml` の `env_vars` に追記してください（ワークフローの設定が正となり、Cloud Run 上で直接変えた値は次のデプロイで上書きされます）。

---

## 制約事項・できないこと

- **編集イベントはグループのテキストメッセージだけ**：LINE の仕様で、LINE公式アカウントとの1対1ではメッセージの編集機能が使えないため、1対1の編集は記録に反映できません。
- **グループで送信者が分からない場合がある**：LINE の仕様で、グループの発言のうち LINE for iOS / Android 以外（PC版など）から送られたものには送信者のIDが付かないため、表示名は「不明なユーザー」になります。
- **1対1のフォルダは表示名ごと**：同じ表示名の人が複数いると同じフォルダに記録されます。表示名を変えた人は新しいフォルダになります。
- **動画・音声・スタンプ・位置情報**は印だけを記録し、内容はテキスト化しません。
- **50MB を超える添付・パスワード付きPDF・対象外の形式（zip など）**はテキスト化しません（記録の行には残ります）。
- **原本ファイルは保存しません**。LINE 上のファイルは一定期間で取得できなくなるため、テキスト化に失敗し続けたものは後から取り直せないことがあります。
- **リマインドは5分刻み**（Cloud Scheduler の実行間隔）で、祝日は考慮しません。サーバーが30分以上止まっていた間の回は、まとめて送りません（`REMINDER_CATCHUP_MINUTES`）。
- **通数の判定**：グループへのプッシュは、グループの人数分が通数としてカウントされます（[LINE Developers の FAQ](https://developers.line.biz/ja/faq/) の説明による）。そのため送信前に「当月の残り通数 ≧ グループの人数（ボットを除く）」かを確認しています。上限のないプランでは確認を省きます。
- **GitHub の書き込み制限**：GitHub には短時間の大量書き込みに対する制限があります。非常に多くのメッセージが短時間に届くと記録が遅れることがありますが、Cloud Tasks の再試行で順に記録されます。
- **Vault の大きさ**：Vault は Cloud Run のメモリ上（`/tmp`）に浅く複製します。Vault が非常に大きい場合は、デプロイ設定のメモリ（`--memory=2Gi`）を増やしてください。
- **起動待ちと最小インスタンス数**：LINE の Webhook は、ボットサーバーが2秒以内に応答しないと時間切れになります（[LINE Developers：Webhook のエラーの原因と統計情報](https://developers.line.biz/ja/docs/messaging-api/check-webhook-error-statistics/)）。このボットは Webhook に必要な部品だけを先に読み込んで素早く応答するようにしていますが、Cloud Run のインスタンスが0台から起動する場合は間に合わないことがあります。そのため既定で最小インスタンス数を1にしています（待機中のインスタンス1台分の費用がかかります。料金は Cloud Run の料金表を確認してください）。費用を抑えたい場合はリポジトリ変数 `MIN_INSTANCES` を `0` にできますが、その場合は LINE 側の「Webhookの再送」を必ずオンにしてください（時間切れになったイベントを LINE が再送し、重複はボットが防ぎます）。
- **応答時間**：質問への回答は Vault の大きさや質問の内容によって数十秒〜数分かかります。50秒を超えた回答はプッシュメッセージになるため、通数を消費します。

---

## テスト

テストは GitHub Actions でプルリクエストと `main` への push のたびに自動で実行されます（`pytest`）。外部サービス（LINE・GitHub・Claude・Cloud Tasks）には接続せず、偽物（GitHub は Contents API をまねた偽サーバー）で置き換えています。

| ファイル | 内容 |
|---|---|
| `tests/test_signature.py` | 署名検証（正しい署名・不正な署名・署名なし・改ざん・再送の重複防止） |
| `tests/test_logformat.py` | ログの書式（1行の形式・改行・時刻順の挿入・編集） |
| `tests/test_dedupe.py` | 重複防止（同じイベント・再送・失敗からの再試行・高価な処理の前の重複判定） |
| `tests/test_unsend.py` | 送信取消（行の置き換え・添付ノートの削除・日付またぎ・記録前の取り消し（日付をまたぐ場合を含む）） |
| `tests/test_reminder_timing.py` | リマインドの時刻判定（予定時刻・遅延・二重送信防止・曜日・月末・第n曜日・1回のみ・書式） |
| `tests/test_cron.py` | リマインドの送信（同時実行でも1回だけ・通数不足・送信失敗時の巻き戻し） |
| `tests/test_auth.py` | `/tasks`・`/cron` の呼び出し元の検証（OIDC トークン） |
| `tests/test_github_store.py` | 409 競合時の取得し直しと再試行、書き込み範囲の制限 |
| その他 | 記録処理全般、Office 抽出（LibreOffice の変換を含む）、画像・PDF の前処理、質問回答、オーナーの指示、リマインド.md の読み書き |

---

## トラブルシューティング

| 症状 | 確認すること |
|---|---|
| LINE の「検証」が失敗する | Webhook URL の末尾が `/webhook` か。`curl ${SERVICE_URL}/health` が返るか。Cloud Run が公開設定か（手順8）。ログに「署名が不正」と出ていればチャネルシークレットが違う |
| 記録されない | `gcloud tasks list --queue=line-ingest --location=$REGION` でタスクが溜まっていないか。ログに GitHub のエラー（401：トークン切れ・権限不足、404：`VAULT_REPO` の誤り）が出ていないか |
| ログに「呼び出しを拒否しました」 | Cloud Tasks・Scheduler のサービスアカウントとリポジトリ変数 `TASKS_INVOKER_SA`・`SCHEDULER_INVOKER_SA`、`SERVICE_URL` が一致しているか |
| タスクの登録でエラー（権限） | 手順5の `roles/cloudtasks.enqueuer` と、`line-bot-tasks` への `roles/iam.serviceAccountUser` が付いているか |
| 質問に答えない | 送信者が `OWNER_LINE_USER_ID` と一致しているか（チャネル基本設定の「あなたのユーザーID」）。1対1で送っているか |
| デプロイが権限エラーで失敗 | 手順5のデプロイ用の権限、手順9の Workload Identity 連携、リポジトリ変数 `WIF_PROVIDER`・`DEPLOYER_SA`。権限の反映に数分かかることがある |
| デプロイがシークレットの権限エラーで失敗 | Secret Manager の6つすべてに `line-bot-run` の読み取り権限が付いているか |
| Scheduler の作成で権限エラー | 実行しているユーザーに `line-bot-scheduler` を使う権限（`roles/iam.serviceAccountUser`）があるか |
