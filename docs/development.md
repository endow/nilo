# 開発者向け手順

ここは Nilo 本体を開発する人向けです。通常利用では読む必要はありません。

## CLI help

```bash
nilo --help
nilo start --help
nilo check --help
nilo review --help
nilo roadmap --help
```

## テスト方針

テストは目的に合わせて `quick` / `targeted` / `full` を選びます。Timeout は選んだ範囲の保険であり、全体テストを常用するための前提ではありません。

```bash
nilo check --task <task_id> "python -m unittest tests.test_verification" --project nilo --mode quick --timeout 60
nilo check --task <task_id> "python tests/run_cli_group.py verification" --project nilo --mode targeted --timeout 120
nilo check --task <task_id> "python tests/run_shards.py --all --jobs auto" --project nilo --mode full --timeout 300
```

`quick` は狭い smoke check、`targeted` は変更領域や `tests.test_cli` の一部、`full` は release や広範囲の変更で使います。

検証レベルは「省略」ではなく、作業段階に応じた責務の分離です。

- changed check: `python tests/run_shards.py --changed --jobs auto`。変更ファイルから shard を選び、作業中の高速確認に使います。
- smoke / compat check: CLI の起動、互換 entry point、基本的な表示が壊れていないことを短時間で見ます。
- full check: `python tests/run_shards.py --all --jobs auto`。release publish や広範囲変更の最終確認に使います。
- audit snapshot: 証跡や完了判断が現在の git snapshot と一致するか、strict に確認する用途です。

Release workflow では、`release prepare` は reusable full verification がなければ changed check を実行し、`full_check: deferred` として full check を publish 前へ送れます。`release publish` は public operation の前に有効な full check を必ず確認し、なければ `RELEASE_FULL_CHECK_COMMAND` を実行します。full check が失敗した場合、tag / push / GitHub release などの公開操作には進みません。

`nilo check` は原則 `--task` を付けて実行します。省略できるのは、安全に一意な未完了 verification target が 1 件だけの場合です。

## 変更中の確認

```bash
python tests/run_shards.py --changed --jobs auto
nilo test plan --changed
nilo test run --changed
```

## 完了判断前

全テスト相当を shard 並列で実行します。結果は `.nilo/test-runs/<run_id>/summary.json` と shard ごとの stdout / stderr log に保存され、失敗時は failed shard と再実行コマンドが表示されます。

```bash
python tests/run_shards.py --all --jobs auto
nilo test run --full
nilo test rerun-failed <run_id-or-summary-json>
```

従来の直列実行も互換用に残しています。

```bash
python -m unittest discover tests
```

`tests.test_cli` の focused group は helper で実行できます。

```bash
python tests/run_cli_group.py review
python tests/run_cli_group.py verification
python tests/run_cli_group.py roadmap
```

設計の詳細は [design.md](design.md) を参照してください。
