# Examples

Small, self-contained repos for exercising Arbiter by hand.

## `totals/` — a seeded defect

A two-criterion plan and a diff that breaks one of them. `app.py` ships the
defect (`sum(items[1:])` drops the first element), so `AC-1` should come back
`unmet` with its cited test failing, while `AC-2` stays `met`.

```bash
make demo          # from the repo root
```

or by hand:

```bash
cd examples/totals
arbiter verify --plan plan.md --diff broken.diff \
  --models claude --test-command "python3 -m pytest"
```

Expected: verdict `fail`, exit code 1. `AC-1` is downgraded because the test the
plan selected as proof actually fails against this code — the reviewer's opinion
is overruled by running the test.

To see it pass, fix `app.py` to `return sum(items)` and run again.

## `rate-limit/` — a plan that isn't ready

A deliberately vague plan: "reasonable number of requests", "efficient",
"robust under load". Nothing here is checkable, and an implementer would have to
guess at least three things.

```bash
make demo-plan
```

Expected: exit code 1, with proposed criteria for the unstated pieces (how a
client is identified, what happens when the counter store is down, whether the
limit holds across instances) and `decide` findings handed back to the author
for the things a model shouldn't choose on your behalf.
