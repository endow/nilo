# Recipes

Recipe は、よくある作業を同じやり方で始めるための仕組みです。

例:

- ドキュメントだけを更新する
- 実装前に設計メモを作る
- 小さな実装変更を、検証とレビュー観点つきで進める
- 不具合修正を、再現、原因特定、再発防止テスト、横展開確認、検証証跡つきで進める
- リリースバージョン、日英リリースノート、GitHub リリース準備を進める

人間は自然文で依頼できます。

```text
README 更新用のレシピで進めて。
```

```text
いまやった修正作業を、次回も同じ進め方でできるようにレシピ化して。
```

必要な場合だけ、レシピはプロジェクト内の `.nilo/recipes/` に保存されます。

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

Recipe の設計境界は [design.md](design.md) を参照してください。
