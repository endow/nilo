# Nilo concept model

本書はNiloで使う中核語彙と境界を定義する。詳細な設計原則は [design.md](design.md)、table対応は [schema-inventory.md](schema-inventory.md) を参照する。

## 中核概念

### Task

人間が実行を許可した具体的な作業単位。目的、制約、完了条件、risk、開始snapshotを持つ。Taskの現在状態は単一の`tasks.status`ではなく、関連する一次事実から射影する。

### Todo

依頼、候補、発見事項、保留事項の受付。作成だけでは実行許可にならない。実行時はTaskへ変換し、変換元と変換先を追跡する。

### Instruction

Taskに対して確定し、agentへ渡した目的、制約、完了条件、適用rule、report形式のsnapshot。後からruleが変わっても、発行済みInstructionの意味は変えない。

### AgentReport

AI/agentが「何を変更し、どの状態だと考えるか」を提出した自己申告。changed filesと本文を保存するが、command実行や成果物の正しさの証明にはしない。

### VerificationRun

実行command、cwd、stdout/stderr、exit code、timeoutと、その実行が観測したgit snapshotを保存する一次事実。成功はcommandの終了を証明するだけで、test範囲の十分性や実装の正しさを自動的には保証しない。

### ReviewRequest

特定Taskの特定snapshotについて、reviewerへ評価を依頼した一次事実。requester、reviewer、理由、lifecycle状態を持つ。snapshotが変わったrequest/resultはstaleとして残す。

### ReviewResult

ReviewRequestに対してreviewerが返したverdict、summary、本文と観測snapshot。VerificationRunの代替ではなく、現在snapshotと一致する場合だけ現在の完了判断材料にできる。

### ReviewFinding

ReviewResultから得た、file/line、severity、blocking、解決状態を持つ具体的指摘。状態変更はReviewFindingUpdateとして追記し、元findingを消さない。

### ReviewFindingUpdate

findingのprevious/new status、理由、actor、判断sourceを記録する状態遷移事実。人間判断の有無も明示する。

### TaskCompletion

誰が、どのsnapshotを、どのVerificationRun/ReviewResultを根拠に完了扱いしたかを記録する一次事実。懸念付き完了と判断noteを保持する。後から無効化できるが、履歴を削除・上書きしない。

### FailureLog

失敗、診断、resolution/decision noteを後から参照するための補助記録。自動rule生成、Instructionへの自動注入、現在Taskの完了gateには使わない。

### RoadmapCommitment / RoadmapRevision

RoadmapRevisionは承認前の計画提案とその判断履歴。RoadmapCommitmentは人間が受け入れた複数Taskのscope、success criteria、non-goals、review/evidence方針。Roadmapだけでは個々のTask実行を代替しない。

### TransitionEvent

entityの状態変更について、前後状態、actor、理由、関連ID、snapshot、warningを残す監査履歴。既存recordの単なるコピーではなく、何のtransitionがいつ承認されたかを表す。

## 事実、projection、transportの関係

```text
Human intent
  ├─ Todo（受付のみ）
  └─ Task（実行許可）
       ↓
   Instruction → AgentReport
                       ↓
               VerificationRun
                       ↓
     ReviewRequest → ReviewResult → ReviewFinding/Update
                       ↓
               TaskCompletion

これらの一次事実 + current git snapshot
                       ↓
 EvidenceStatus / CompletionProjection / WorkProjection / human status
```

transportであるReviewAttempt、ReviewDispatchLog、RecipeRun、OverdriveRunは「どう届けたか・どこまで実行したか」を表す。review verdict、finding、completionというdomain factをtransport adapter自身が確定してはならない。

## 境界ルール

- AgentReport、VerificationRun、ReviewResult、TaskCompletionは互いに代替しない。
- EvidenceStatus、current task、next action、review/completion stateは保存せず再計算する。
- FailureLogとQualityAnnotationは参照・注記であり、自動完了ruleにしない。
- snapshot-boundなrecordは観測snapshotを失わず、current snapshotとの差を`stale`として表現する。
- CLI / MCP / ViewはApplication Serviceと共通Projectionを経由し、独自の完了判定を持たない。
- repository導入時もdomain別に小さく分け、汎用Store facadeを新たなdomain layerにしない。
