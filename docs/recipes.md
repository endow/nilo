# Recipes

Recipe は、よくある作業を同じやり方で始めるための仕組みです。

変更依頼では `nilo work --intent change` が依頼文から必要な recipe を選びます。読み取り依頼は`--intent inspect`を使います。`nilo recipe run` は、明示実行、recipe のデバッグ、release などの advanced path として残します。

例:

- ドキュメントだけを更新する
- 実装前に設計メモを作る
- 小さな実装変更を、検証とレビュー観点つきで進める
- 不具合修正を、再現、原因特定、再発防止テスト、横展開確認、検証証跡つきで進める
- パフォーマンス改善を、改善前後の計測、比較、正しさ検証、副作用確認の証跡つきで進める
- リリースバージョン、日英リリースノート、GitHub リリース準備を進める

人間は自然文で依頼できます。

```text
README 更新用のレシピで進めて。
```

```bash
nilo work "README 更新用のレシピで進めて" --intent change
```

```text
いまやった修正作業を、次回も同じ進め方でできるようにレシピ化して。
```

必要な場合だけ、レシピはプロジェクト内の `.nilo/recipes/` に保存されます。

## Perf recipe

`perf` は、遅い処理を計測し、ボトルネックを特定し、改善前後の比較証跡を残すための標準レシピです。

```bash
nilo recipe run perf --project nilo
```

通常作業では次のように始められます。

```bash
nilo work "full check が重いので改善して" --intent change --project nilo
```

`performance` と `performance-investigation` も `perf` の alias として使えます。完了契約には、改善対象、測定条件、改善前ベースライン、ボトルネック分析、変更内容、改善後再計測、比較結果、正しさ検証、副作用確認が含まれます。

## Release recipe のバージョン提案

Release recipe は、`target_version` が未指定の場合、現在バージョンと最新 git tag から次の候補を出します。

小さな修正だけなら patch version を候補にします。

```bash
0.1.9 -> 0.1.10
```

CLI 追加、DB schema / migration、recipe 変更、AI 向け出力、roadmap / review / failure log など主要機能に関わる変更がある場合は、minor version を推奨します。

```bash
0.1.9 -> 0.2.0
```

Minor が推奨される場合、Nilo は理由と再実行コマンドを表示します。明示的に `target_version` を指定した場合、その値は上書きされません。

```bash
nilo recipe run release --project nilo --var target_version=0.2.0
```

release recipeは、開始時に作成した一つのリリースTaskが、準備、失敗修正、再検証、公開、完了または中止までを所有します。検証失敗時は`paused_for_fix`となりますが、別Taskや子Taskは作らず、同じTask内で修正して`nilo release resume --project <project_id>`を実行します。取りやめる場合は`nilo release cancel --project <project_id> --reason <理由> --human-confirm`を使います。公開には引き続き人間の明示承認が必要です。

recipe YAMLのinstruction、acceptance、completion contractは宣言的な説明と受入条件です。`steps`を汎用実行エンジンとして解釈するものではなく、releaseの状態制御と公開操作はrelease専用オーケストレーションが担います。完了または中止時はrecipe runと対応Taskを一つのトランザクションで閉じます。

Recipe の設計境界は [design.md](design.md) を参照してください。
