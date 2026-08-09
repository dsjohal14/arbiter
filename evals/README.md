# Evaluation cases

A case is one run whose right answer a human wrote down. Scoring a run against
its case turns "the gate feels reliable" into a number: how often a broken
implementation still passes, how often a real gap is caught, how often a plan
finding is one you would act on.

```
evals/<case-id>/
  plan.md        the plan; criteria pinned for a verify case
  change.diff    the implementation under review     (verify cases)
  repo/          the tree the diff applies to        (verify cases, optional)
  expected.json  the answer key
```

## The loop

Recording costs one paid run. Everything after it is free, because scoring
replays the recorded responses rather than calling a model again.

```bash
# 1. record — a verify case must run where its diff applies
cd evals/totals-drops-first-element/repo
arbiter verify --plan ../plan.md --diff ../change.diff \
  --record ../../../.arbiter/runs/totals.json

# 2. score, as often as you like, for nothing
arbiter eval score evals/totals-drops-first-element \
  --record .arbiter/runs/totals.json

# 3. check the corpus itself is well formed
arbiter eval validate evals/*/
```

Scoring refuses a record whose plan or diff digest differs from the case's, so
an accuracy number can never come from a run of something else.

## expected.json

```json
{
  "schema_version": 1,
  "command": "verify",
  "description": "why this case exists and what a correct answer looks like",
  "verdict": "fail",
  "criteria": { "AC-1": "unmet", "AC-2": "met" },
  "must_find":     [{ "label": "...", "severity": "blocking", "match": ["empty list"] }],
  "must_not_find": [{ "label": "...", "severity": "blocking" }]
}
```

| Field | Applies to | Meaning |
|---|---|---|
| `schema_version` | both | Required. Currently `1`. |
| `command` | both | `verify` or `plan review`. |
| `id` | both | Optional; defaults to the directory name. |
| `description` | both | Why the case exists. Write the reasoning down — a label you cannot defend in six months is not evidence. |
| `verdict` | both | `pass` \| `fail` \| `unclear` for verify; `ready` \| `blocked` for plan review. Omit to leave it unscored. |
| `criteria` | verify | Expected status per criterion id: `met` \| `untested` \| `unmet` \| `unverifiable`. Every id must be pinned in `plan.md`. |
| `must_find` | both | Findings the run has to produce. Missing one is a recall failure. |
| `must_not_find` | both | Findings it must not produce. Producing one is a false positive. |

### Matching a finding

A finding's prose is written by a model and no two runs phrase it the same way,
so the key matches on the enumerated fields exactly and on required substrings
loosely. An expectation must constrain at least one of the two.

| Key | verify | plan review |
|---|---|---|
| exact fields | `severity` | `kind`, `action`, `target` |
| `match` searches | `claim`, `evidence` | `problem`, `proposed_criterion`, `proposed_test`, `quote` |

`match` is a list; every entry must appear (case-insensitively) somewhere in
that finding's searched text. `label` is what shows up in the score output —
write it as the thing you wanted found, not as a restatement of the constraint.

## Writing a case

- **Label what a correct Arbiter would say, not what yours said.** A case
  written from an actual run measures nothing.
- **Prefer per-criterion labels to finding labels for verify cases.** Status is
  the unit Arbiter promises, and it is exact.
- **Seed one defect at a time.** A case that fails for three reasons cannot tell
  you which one Arbiter missed.
- **Include `repo/` for a verify case.** Evidence checking reads cited lines and
  runs cited tests; without a tree to read, a case only measures the model layer.
- **Expect to revise the corpus.** A case Arbiter fails is a claim about
  Arbiter, until you have re-read it and confirmed the key is right.

## Cases here

| Case | Command | Measures |
|---|---|---|
| `totals-drops-first-element/` | verify | Whether a plan-selected proof test that fails is allowed to be overruled by reviewer confidence. |
| `rate-limit-vague-plan/` | plan review | Whether unmeasurable wording, an unmade decision, and an unhandled failure path are all caught — without inventing a contradiction. |
