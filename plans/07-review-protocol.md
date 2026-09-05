# Review + implementation protocol (autonomous run)

## Roles

- Supervisor (this session): sequences work, commits, keeps STATUS.md.
- Implementers: `opus` sub-agents, one piece each, worktree-free (disjoint
  paths), **no git commands**.
- Reviewers: `fable` sub-agents, fresh context each round, given the plan
  files + the diff/paths; they report findings only, never edit.

## Plan review (before implementation)

1. Reviewer reads `00-overview.md` + all piece plans; checks: internal
   consistency of the contract, feasibility against the verified Orbit
   facts, missing failure modes, test adequacy, anything that would block
   parallel implementation. Output: numbered findings tagged
   `BLOCKING` / `SHOULD` / `NIT`, each with the concrete edit proposed.
2. Supervisor applies BLOCKING + SHOULD (or records why not), re-runs
   review. Stop when a round yields no BLOCKING, max 3 rounds.

## Implementation review (per piece)

1. Implementer delivers: files, tests green, flake8 clean, docs, a short
   report (what/why/how tested/known gaps).
2. Reviewer checks against the piece plan + contract: correctness,
   failure handling, tests actually exercising behaviour, docs accuracy,
   no scope creep, no Orbit-internal vocabulary leaking into the ATOMIC UI.
   Same finding tags.
3. Implementer (same agent, resumed) fixes BLOCKING + SHOULD. Max 3 rounds;
   leftovers go to STATUS.md "Known gaps".
4. Supervisor commits the piece on the repo's feature branch with a
   conventional message; STATUS.md gets a dated line.

## Definition of done (per piece)

- Unit tests added and green for the repo (`pytest`), lint clean.
- Docs written where the plan says; CLI `--help` accurate.
- No changes outside the planned paths; nothing pushed; no PR.
