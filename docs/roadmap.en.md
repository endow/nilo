# Roadmap

Roadmaps keep large AI-assisted work from turning into one unchecked implementation step.

Small changes can proceed as normal tasks. Use roadmap planning for multi-module work, DB schema or migration changes, CLI additions, AI-facing output changes, or work that spans README, docs, and tests.

Roadmaps are not created automatically. They are created only after human approval, then split goals, non-goals, and success criteria into executable tasks.

## Basic Flow

```bash
nilo roadmap discuss --project <project>
nilo roadmap import --project <project> --file <roadmap_proposal.md>
nilo roadmap accept --revision <roadmap_rev_id> --reason "<reason>" --actor human --human-confirm
nilo roadmap task-plan --commitment <commitment_id>
```

Natural-language requests are enough:

```text
This work is large, so organize it as a Nilo roadmap.
```

```text
Continue from the next task in the accepted roadmap.
```

For design boundaries, see [design.md](design.md).
