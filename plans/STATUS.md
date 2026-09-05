# ATOMIC demo build — STATUS

Started 2026-09-05 (Sat night), autonomous run. Target: full demo on localhost (3 endpoints joined).
Branches: radical.orbit `feature/atomic-federation`, atomic `feature/demo-wm`. No pushes, no PRs.

## Log
- 2026-09-05 23:50 plans 00-07 written; plan review round 1 (fable) + local pool-path spike (opus) running in background.
- 2026-09-06 00:15 SPIKE OK: local dispatcher pool path works (pilot ACTIVE 4.6s, task DONE 5.1s). Env rules folded into 00-overview + 06. ve3 holds stale orbit 0.3.0 -> up.sh reinstalls from src branch.
- 2026-09-06 00:20 P3 DONE: package skeleton (pyproject, atomic_wm, .flake8) + three stdlib-only workload tools + CLI/plugin placeholders + examples + docs/workload.md. 38 tests green, flake8 clean, `ve3/bin/pip install .` OK, md->train->descriptors runs from ve3/bin. Envelope built in one place (`workload/common.py::make_envelope`); md mirrors `temperature` top-level to satisfy both 00-overview and 03. Runtime deps empty; `requests` moved to the `[cli]` extra (P2) so installs on a login node stay offline-safe.
- 2026-09-06 00:40 plan review round 1: 4 BLOCKING (min_pilots unhonoured, no pilot end timestamps, broker caller cannot address broker, session TTL kills pools) + 12 SHOULD -> all applied to plans 00-06; sid=default everywhere, in-process handle_request for plugin->plugin calls, two sanctioned dispatcher touches. P3 notified of seed/ordering amendment.
- 2026-09-06 01:00 launched: plan review round 2 (fable), P1 federation (opus, orbit), P5 UI (opus). client.py seeded with contract signatures (P2 implements). P2/P4 wait for P3 to land (shared skeleton files).
- 2026-09-06 01:10 P3 amendment applied: default seed = blake2b hash of the tool's own params (`--seed` overrides, resolved seed recorded in `params.seed`); plateau knots 0.99/0.93/0.85 piecewise-linear, ordering asserted with default seeds AND across 25 seeds; `die()` is NoReturn -> pyright clean (0 errors on P3 files). 43 tests green, flake8 clean. NOTE: `.flake8` mirrors orbit's (E501 ignored, 80 cols still the target) so P2/P4/P5 lint under the same rules.
- 2026-09-06 01:20 plan review round 2: NO BLOCKING; 7 SHOULD (on_tick guard structure, orphan-pool guard = dispatcher touch 3, session_class needed, pilot_history via _pilot_dict, ui_module class attr, no dispatcher state env override, stage_in body) applied to 01/04/05/06 and relayed to P1/P4/P5 in flight. Plans considered stable.
