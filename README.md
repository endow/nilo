# Nilo

> English version: [README.en.md](README.en.md)
>
> この README が正本です。英語版は補助的な案内として提供しています。

Nilo は、AI に開発作業を任せるときの **現在地・完了条件・検証証跡・レビュー結果** を、プロジェクト内に残すための CLI ツールです。

人間が毎回コマンドを覚えて操作するための道具ではありません。多くの操作は Codex や Claude Code などの AI エージェントが裏側で実行します。人間が見るべきものは、いま何が起きているか、何が未解決か、完了として受け入れてよいかです。

Nilo は現在、個人開発での AI 開発フローを安定させるための実験的なツールです。API、DB スキーマ、CLI 出力は今後変更される可能性があります。本番システムの安全性、サンドボックス、認証、CI の代替ではありません。Nilo は security boundary ではなく、証跡、監査、作業規律を記録するための道具です。

```text
人間が AI に依頼する
    ↓
AI が Nilo を使って、作業状態・検証・報告・レビューを記録する
    ↓
人間が「採用する / 差し戻す / 追加確認する」を判断する
```

Nilo はタスク管理アプリではありません。AI 開発で散らばりがちな作業の根拠を、あとから確認できる状態にするための作業台です。

## なぜ必要か

AI コーディングでは、会話だけを見ると進んでいるように見えても、実際には次の確認が抜けやすくなります。

- いま何をやっているのか
- 何を満たせば終わりなのか
- 本当に検証したのか
- 検証ログは AI の自己申告なのか、Nilo が記録したものなのか
- レビュー指摘が残っていないか
- 最後に完了と認めるのは誰か

Nilo はこの曖昧さを `.nilo/nilo.db` に記録します。AI の作業速度は活かしつつ、完了判断だけは証跡に基づいて行えるようにします。

## Nilo の考え方

Nilo の中心にある考え方はシンプルです。

> Evidence Before Trust  
> AI の自己申告より、実体のある変更と検証証跡を先に見る。

AI が「完了しました」と言っても、それは Nilo 上では完了候補にすぎません。完了条件、変更ファイル、検証結果、レビュー結果を見て、人間が受け入れた時点で完了になります。

Nilo が残す主なものは次の通りです。

- いま取り組んでいる作業
- AI に渡した作業指示
- 完了条件
- 実行した検証とその結果
- 検証・レビュー・完了判断が対象にした git snapshot
- AI の作業報告
- 人間または別 AI からのレビュー
- 繰り返したくない失敗
- 後から参照するための方針メモ

検証結果やレビュー結果は、特定のコード状態に対する記録です。作業後にコードが変わった場合、Nilo は古い検証やレビューを現在の証跡として扱わず、表示時に `stale` として区別します。

## インストール

必要なものは Python 3.12 以上と Git です。

```bash
git clone https://github.com/endow/nilo.git
cd nilo
python -m pip install -e .
```

このリポジトリ自体の動作確認は次で行えます。

```bash
python -m unittest discover tests
nilo status --project nilo
```

`python -m unittest discover tests` はテストスイートの確認、`nilo status --project nilo` は Nilo の状態 DB を読めるかの確認です。

## 使い始める

Nilo を使いたいプロジェクトのルートで一度だけ初期化します。

```bash
nilo init
```

作成される主なものは次の通りです。

- `.nilo/nilo.db`: 作業状態を保存する SQLite DB
- `.nilo/agent-instructions.md`: AI エージェント向けの共通 runtime 指示
- `AGENTS.override.md` / `CLAUDE.local.md`: 作業ツリーごとのローカル指示ファイル
- 必要に応じたレポートや検証ログの置き場

初期化後は、AI エージェントに普段どおり依頼できます。

```text
README を直して。Nilo の状態を確認してから進めて。
```

AI 側が Nilo の現在地を確認し、必要な作業単位を作り、指示を読み、検証と報告を Nilo に戻します。

## 人間が日常的にすること

日常的には、Nilo のコマンド体系を覚える必要はありません。

人間の入口は、AI に自然文で聞くことです。

```text
次は？
```

```text
何か残ってる？
```

```text
今どういう状態？
```

```text
この内容で完了にしていい？
```

AI エージェントは、その質問に答えるために必要なら Nilo を読みます。人間は「どのコマンドを打つか」ではなく、返ってきた状態を見ます。

望ましい返答は、たとえば次のようなものです。

```text
今はレビュー待ちです。
検証は通っています。
未解決の指摘が 1 件あります。
次はその指摘への対応です。
```

Nilo が目指すのは、このように人間が一目で判断できる状態表示です。CLI の出力そのものを読ませることではありません。

## 人間の使い方

人間がやることは、基本的に次の 3 つです。

### 1. AI に依頼する

```text
バグを直して。Nilo の状態を確認してから進めて。
```

### 2. 必要なときに聞く

```text
次は？
```

```text
どこで止まってる？
```

```text
検証は通ってる？
```

AI が裏側で Nilo を読み、現在地と次の行動を説明します。

### 3. 最後に判断する

AI の報告を見て、受け入れるか、差し戻すか、追加確認を頼むかを決めます。

```text
この内容で完了にして
```

```text
このレビュー指摘が残っているので直して
```

完了、差し戻し、commit、作業方針の確定は人間の判断点です。AI が勝手に確定する前提にはしていません。ただし actor 名は監査用ラベルであり、OS や Git の認可情報ではありません。Nilo は誰が何を完了扱いにしたかを記録しますが、違反を完全に防ぐ認可システムではありません。

## AI が裏側でやること

AI エージェントは、必要に応じて Nilo を使います。人間が細かい操作手順を覚える必要はありません。

たとえば AI は裏側で次のようなことを行います。

- 現在地を確認する
- 作業単位を作る
- 作業指示と完了条件を読む
- 検証結果を記録する
- 作業報告を残す
- 別 AI または人間へレビューを依頼する
- レビュー結果を取り込む
- 未解決の指摘があれば次の作業に戻す

これらは README を読む人間が日常的に手で実行する前提ではありません。AI が作業したあとに「何を根拠にそう言っているのか」を残すための操作です。

## AI エージェントとの連携

Nilo は Codex、Claude Code、ChatGPT、ローカル LLM など、特定の AI に依存しません。

AI エージェントは、通常の CLI または MCP（Model Context Protocol）を通じて Nilo にアクセスします。MCP を使うと、AI は会話中の tool として Nilo の状態を読み、検証結果やレビュー結果を書き戻せます。

`nilo init` は、AI エージェント向けの runtime 指示を作業ツリーごとのローカルファイルへ書き込みます。通常、`CLAUDE.md` や `AGENTS.md` のような git 管理されるファイルへ現在状態を書き込みません。

### AI エージェント向けのローカル指示

Nilo は通常、`CLAUDE.md` や `AGENTS.md` のような git 管理されるファイルへ現在状態を書き込みません。

代わりに、作業ツリーごとのローカルファイルへ出力します。

- Claude Code: `CLAUDE.local.md`
- Codex: `AGENTS.override.md`
- 共通の生成本文: `.nilo/agent-instructions.md`

これらは Nilo が生成する runtime file であり、commit しません。

`.nilo/` や local override file の ignore 設定は、tracked `.gitignore` ではなく Git の local exclude に追加されます。そのため、新しい clone や作業ツリーでは最初に `nilo init` を実行してください。`nilo init` が `.git/info/exclude` 相当の local ignore を整えます。

Git linked worktree では、Git が返す `info/exclude` の場所が common git directory 側になることがあります。その場合、ignore 設定は同じ repository の worktree 間で共有されます。

古いバージョンの Nilo が `CLAUDE.md` / `AGENTS.md` に自動生成 block を書いていた場合、`nilo init` は警告と移行コマンドを表示します。`nilo init` 自体は tracked file を変更しません。

```bash
nilo migrate
```

は移行できる古い block を診断します。実際に tracked file から古い Nilo block を削除し、local runtime files を更新する場合だけ次を実行します。

```bash
nilo migrate --apply
```

AI エージェントには、次のように依頼します。

```text
Nilo の状態を見てから進めて。
```

```text
必要なら別の AI にレビューを依頼して。
```

```text
検証結果と未解決事項を残してから報告して。
```

MCP や CLI の接続が使えない場合は、代替手段に逃げず、Nilo を読めないことを人間に報告するのが基本です。

レビュー連携でも、人間が内部プロトコルを覚える必要はありません。重要なのは、実際にレビューした reviewer / agent セッションが存在し、その結果が Nilo に記録されていることです。「接続できているように見える」だけの状態や、固定ファイルを取り込んだだけの結果は、実体のあるレビューとして扱いません。

Reviewer は Codex や Claude Code に限らず、`review_diff`、`summarize`、`propose_tests` などの capability と availability で扱います。ローカル LLM や OpenAI-compatible endpoint も thin local reviewer として登録できますが、local reviewer の低 confidence や limitations はそのまま保存され、task completion の直接根拠にはなりません。完了には引き続き tests、command output、diff inspection、必要な human / trusted reviewer approval が必要です。

詳しい CLI、MCP、reviewer の仕様は、開発者向けのヘルプや設計文書で扱います。

## 後でやることと大きな方針

Nilo には、いま進める作業とは別に、後で扱う依頼や、複数作業にまたがる方針をメモとして残す仕組みがあります。

会話中に出た気づき、後続作業、今すぐやらない改善案は、すぐ実装せずに保留できます。方針メモは参照専用であり、Task の実行を許可する権限は持ちません。実行するかどうかは、明示された単発依頼から Task を切り出して判断します。

日常的には内部名を覚える必要はありません。次のように依頼すれば十分です。

```text
今すぐやらないけど、後で検討する項目として残して
```

```text
この方向で進めてよいので、Nilo の状態を見て次に進めて
```

AI エージェントが必要な範囲で、保留項目、実行する作業、大きな方針を使い分けます。

## 保存されるもの

Nilo は、プロジェクトルートの `.nilo/nilo.db` に作業状態を保存します。

保存されるのは、AI と進めた作業の履歴です。たとえば、作業内容、検証結果、レビュー結果、未解決事項などです。

このリポジトリでは、作業中に生成されるローカルファイルは Git に入れません。

- `.nilo/`: 作業状態 DB、検証ログ、レポート一時ファイル
- `HANDOFF.md`: 必要時に生成する人間向け引き継ぎファイル
- `.mcp.json`: ローカル MCP 設定
- Python キャッシュ、仮想環境、coverage 出力、build artifact

Git に残すのは、ソースコード、テスト、README、設計文書、AI エージェント向け手順など、プロジェクトとして共有したいファイルです。

## 開発者向け

CLI の詳細は各コマンドの `--help` を参照してください。

```bash
nilo --help
nilo start --help
nilo check --help
nilo review --help
nilo roadmap --help
```

テストを実行します。

```bash
python -m unittest discover tests
```

設計の詳細は [docs/design.md](docs/design.md) を参照してください。

## ライセンス

Apache License 2.0

詳細は [LICENSE](LICENSE) を参照してください。
