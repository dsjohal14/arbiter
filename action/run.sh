#!/usr/bin/env bash
# The step behind the Arbiter GitHub Action.
#
# Kept as a file rather than inline in action.yml so it can be run and tested
# off GitHub: tests/integration/test_github_action.py drives it against the
# fake provider. Inputs arrive as environment variables, never interpolated
# into this script, so a value taken from an event payload cannot become code.
#
#   ARBITER_PLAN             --plan, if the project settings do not name one
#   ARBITER_DIFF             --diff; defaults to the pull request's own range
#   ARBITER_ARGS             extra arguments, split the way a shell would
#   ARBITER_FAIL_ON_UNCLEAR  "false" lets an `unclear` verdict pass the job
#   ARBITER_PR_BASE/HEAD     the pull request's base and head commits
#   ARBITER_PYTHON           the interpreter arbiter is installed in
set -uo pipefail

python="${ARBITER_PYTHON:-python}"
out_dir="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
json="$out_dir/arbiter-verdict.json"
summary="$out_dir/arbiter-summary.md"
rm -f "$json" "$summary"

args=(verify --json --summary "$summary")
if [ -n "${ARBITER_PLAN:-}" ]; then
  args+=(--plan "$ARBITER_PLAN")
fi

diff="${ARBITER_DIFF:-}"
if [ -z "$diff" ] && [ -n "${ARBITER_PR_BASE:-}" ] && [ -n "${ARBITER_PR_HEAD:-}" ]; then
  # What the pull request changes: from where it left its base, to its head.
  diff="${ARBITER_PR_BASE}...${ARBITER_PR_HEAD}"
fi
if [ -n "$diff" ]; then
  args+=(--diff "$diff")
fi

if [ -n "${ARBITER_ARGS:-}" ]; then
  # Split, not evaluated: `eval` would run whatever the input contained. One
  # word per line, so a quoted argument with spaces survives as one word.
  if ! words="$("$python" -c '
import os, shlex, sys
words = shlex.split(os.environ["ARBITER_ARGS"])
if any("\n" in word for word in words):
    sys.exit("an argument contains a newline")
print("\n".join(words))
')"; then
    echo "::error title=Arbiter::The 'args' input could not be parsed: ${ARBITER_ARGS}"
    exit 2
  fi
  while IFS= read -r word; do
    if [ -n "$word" ]; then
      args+=("$word")
    fi
  done <<< "$words"
fi

"$python" -m arbiter "${args[@]}" > "$json"
code=$?

case "$code" in
  0) verdict=pass ;;
  1) verdict=fail ;;
  3) verdict=unclear ;;
  *) verdict=error ;;
esac

if [ -n "${GITHUB_OUTPUT:-}" ]; then
  {
    echo "verdict=$verdict"
    echo "exit-code=$code"
    echo "json=$json"
    echo "summary=$summary"
  } >> "$GITHUB_OUTPUT"
fi
if [ -n "${GITHUB_STEP_SUMMARY:-}" ] && [ -s "$summary" ]; then
  cat "$summary" >> "$GITHUB_STEP_SUMMARY"
fi

if [ "$verdict" = unclear ] && [ "${ARBITER_FAIL_ON_UNCLEAR:-true}" = "false" ]; then
  echo "::warning title=Arbiter::The verdict is unclear. Not failing the job, because fail-on-unclear is false."
  exit 0
fi
exit "$code"
