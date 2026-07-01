# Roadmap

Roadmap は、大きな作業を一気に実装させないための作業計画です。

小さな修正は通常 task として進めます。複数モジュールにまたがる変更、DB schema / migration、CLI 追加、AI 向け状態表示の変更、README / docs / tests まで含む変更では、roadmap で整理することを推奨します。

Roadmap は自動で作りません。人間が承認した場合だけ、目的、非目的、成功条件を固定し、実装 task に分けます。

## 基本の流れ

```bash
nilo roadmap discuss --project <project>
nilo roadmap import --project <project> --file <roadmap_proposal.md>
nilo roadmap accept --revision <roadmap_rev_id> --reason "<reason>" --actor human --human-confirm
nilo roadmap task-plan --commitment <commitment_id>
```

AI に頼む場合は、自然文で十分です。

```text
この作業は大きいので、Nilo の roadmap として整理して。
```

```text
承認済み roadmap の次の task から進めて。
```

Roadmap の詳細な設計境界は [design.md](design.md) を参照してください。
