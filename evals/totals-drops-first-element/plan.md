# Plan: fix the totals bug

`total()` under-reports because it drops the first element of the list. Fix it
so every element is counted, without changing the empty-list behaviour that
callers already rely on.

<!-- arbiter:criteria -->

## Acceptance criteria

- **AC-1** — total() returns the sum of every element in the list  [proof: tests/test_app.py::test_sums_every_item]
- **AC-2** — total([]) still returns 0  [proof: tests/test_app.py::test_empty_list_returns_zero]

<!-- /arbiter:criteria -->
