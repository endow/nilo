# Security Policy

Nilo はサンドボックスではありません。コマンド実行、ファイル操作、AI エージェント連携の安全性を保証する仕組みではなく、本番システムの認証、権限分離、CI、監査の代替にもなりません。

`.nilo/nilo.db` には、作業状態、検証結果、レビュー結果、AI の報告などが保存されます。内容にはローカル環境や作業履歴に関する情報が含まれる可能性があります。

次のものは commit しないでください。

- `.nilo/`
- `.mcp.json`
- API keys
- access tokens
- private prompts
- local credentials

セキュリティ問題を見つけた場合は、public issue に exploit details や秘密情報を書かないでください。再現に必要な最小限の情報で issue を開くか、maintainer に連絡してください。
