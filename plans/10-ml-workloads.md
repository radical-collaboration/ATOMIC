# ATOMIC ML workloads on ATOMIC_WM — call prep

2026-10-02.

Figures (sources: `fig-ml-architecture.svg`, `fig-ml-bigsim.svg`):

![Architecture: client, central service host with the portal, endpoints per resource](fig-ml-architecture.png)

![Use case: the bigsim per-frame chain over endpoints, with data sizes](fig-ml-bigsim.png)

Goal of the call: agree a development line that runs the bigsim chain on the
stack we demoed on 2026-09-08, and the order of the work after it.

## 1. Workload digest

- **Science:** defect detection in MD snapshots. Frames come from Qianqian's
  LAMMPS runs on SOE NFS. No ML → MD feedback today.
- **Per-frame chain:**
  1. denoise: GNN, Amarel, 1 GPU, Slurm.
  2. PTM: OVITO, soenfs6, 1 CPU, no scheduler.
  3. ACE features: LAMMPS+PACE, SOE sills, 2 nodes × 4 MPI ranks.
  4. extract flagged rows: soenfs6.
  5. table, MLP predict, post-process, score: laptop.

  Steps 2 and 3 are independent.
- **Sizes per 1M-atom frame:** 36 MB in, 12.7 GB ACE intermediate, 21–59 MB extracted.
- **Workloads:**

  | Workload | Atoms per frame | Shape | Status |
  |---|---|---|---|
  | Scenario matrix | ~4k | 1,820 two-minute feature jobs, 270 MLP trainings | done |
  | Grain boundaries | 4–7k | 88 frames at ~4 min, serial today | 2 boundaries done |
  | Bigsim | ~1M | 35–45 min compute per frame, 246 frames left | demo done |

- **Side tasks:** denoiser retraining, 3–4.5 h on 1 GPU. Training-crystal builds,
  140 crystals per dataset, one SOE job each.
- **Machines:** Amarel (Slurm, GPU), SOE sills (Slurm, MPI, stripped login
  shell), soenfs6 (no scheduler, NFS head node), laptop. Environments are
  hand-built, with no containers. All hosts are behind the campus VPN.
- **Today's glue:** zsh scripts with ssh, rsync and polling of Slurm and
  `pgrep`. Resume relies on stamp files. Provenance lives in two hand-kept
  documents per project.
- **First target, proposed by the ML team:** the bigsim chain on the remaining
  frames. Baseline: ~1.5 h of compute and ~4 h wall clock for two frames.

## 2. Architecture

See the architecture figure above.

- **Central service host:**
  - The ORBIT broker runs there, plus the `atomic_campaign` plugin, which hosts
    one Campaign Manager instance per campaign: bigsim, GB, matrix.
  - The Campaign Manager is the Rutgers one (Masha's design). The plugin is its
    host. Today's plugin is a mock-up of that seam.
  - The `federation`, `task_dispatcher` and staging plugins sit beside it.
  - The ATOMIC portal is a plugin on the central host, served by the gateway:
    campaign UI, live state and logs, results browser, provenance queries.
  - The host's location (r3 or a Rutgers-hosted VM) is left open; see §6.
- **Endpoints:** one per resource class, started under the user's account. Each
  endpoint opens its connection to the central host.
  - `gpu` on Amarel: denoise, retraining, optional MLP training.
  - `mpi` on SOE sills: ACE, training-crystal builds.
  - `host` on soenfs6: PTM, extract, predict, score.

  How an endpoint gets its resources is an implementation detail: a pilot inside
  a Slurm allocation, or a plain host process.
- **Data:**
  - Placement follows the data: ACE, PTM, extract and predict run on SOE, next
    to the shared NFS.
  - Only the denoise round trip crosses sites: 36 MB in and 36 MB out.
  - Results of 20–60 MB per frame go to the results store; the client fetches
    them.
- **Client:** a browser on the portal, or the atomic CLI against the gateway.
  It submits, watches, fetches and queries provenance. It hosts no service and
  runs no task.

## 3. How the architecture answers "What breaks" (§6 of the document)

| # | Break | Answer in the architecture |
|---|---|---|
| 1 | laptop is the controller; VPN drops kill it | the controller is the always-on central host; the laptop is a client that may disconnect; endpoints reconnect on their own |
| 2 | fixed staging names force one frame at a time | each task runs in its own sandbox under a unique id; N pipelines in flight |
| 3 | tag collisions mix runs silently | the Campaign Manager derives the tag from the pipeline id; task arguments come from the spec, not from hand-picked numbers |
| 4 | results visible 2–3 min after Slurm reports COMPLETED | the endpoint reports completion from inside the allocation once declared outputs exist; staging reads from the endpoint, not from the login node |
| 5 | `pgrep` matched its own ssh command | the endpoint starts the process and tracks it by pid; no remote process probing |
| 6 | 12.7 GB intermediates | placement by data locality; ACE output stays on SOE NFS; only declared small outputs move |
| 7 | hand-built environments, stripped login shell | environment declared once per endpoint at join (OpenMPI paths, conda env), plus an optional per-task setup step |
| 8 | silent failures, provenance drift | a task is done only on exit 0 plus declared outputs present; provenance recorded per task by the Campaign Manager |

## 4. Draft answers to the ML team's questions (§8 of the document)

1. **Scripts as executables:** yes. A task is a script plus arguments. The
   only requirement is to make implicit inputs explicit: tag, staging paths,
   model paths. The Campaign Manager passes them in, so the validated logic
   does not change.
2. **Where the manager runs:** on the central host. The laptop is not needed
   after submission. Endpoints drive Slurm on Amarel and sills, and run plain
   processes on soenfs6.
3. **Data movement:** declared inputs and outputs per task. The 12.7 GB stays
   on SOE NFS. Only the 36 MB denoise round trip and the 20–60 MB results move.
4. **Network drop:** the endpoint keeps running its tasks and reconnects.
   Reattach and campaign resume after a central host restart are on the
   development line (§5). Do not promise these as available today.
5. **1,820 small jobs:** yes. That is what an endpoint inside one allocation
   does: many tasks, one Slurm submission.
6. **GPU and MPI tasks:** per-task requirements (gpu, ranks, nodes) are in the
   spec and passed through. Per-task setup steps cover the environment exports.
   Multi-node MPI tasks are a gap today (§5).
7. **State and provenance:** per task: script, arguments, model version, scale
   factor, noise level, inputs, outputs, endpoint, exit code, times. Queryable
   from the CLI and portal. The schema needs agreement with the ML team.
8. **Credentials:** endpoints start under the user's own account, with their
   ssh keys, then connect to the central host over token-gated TLS. The VPN is
   needed only to start an endpoint. Compute nodes must be able to reach the
   central host (§6).

## 5. Development line (proposal)

Each step ends with a run the ML team can check against their numbers.

1. **Bigsim on the demo stack:**
   - Three endpoints: Amarel gpu, sills mpi, soenfs6 host.
   - The chain as a campaign spec, with their scripts unchanged except for the
     explicit inputs.
   - 2 frames first, compared with the ~4 h baseline. Then N frames.
2. **Multi-node MPI tasks:** ACE needs 2 nodes × 4 ranks inside an endpoint's
   allocation. Today Rhapsody `concurrent` runs only on the head node, and
   multi-node use of an adopted allocation is an open gap in ORBIT. Until it is
   fixed, one fallback is an ACE task that submits its own Slurm job from the
   `mpi` endpoint.
3. **Robustness:**
   - task reattach after an endpoint reconnect;
   - campaign resume after a central host restart (today the plugin marks
     unfinished work INTERRUPTED);
   - checked outputs;
   - a dispatcher reaper for tasks stuck in RUNNING.
4. **Provenance schema:** agree the fields with the ML team, record them per
   task, and query them from the CLI and portal.
5. **Campaign Manager integration:** replace the mock-up in `atomic_campaign`
   with the Rutgers Campaign Manager. This is the point where Masha's design
   docs feed in.
6. **Wider fan-out:**
   - GB frames in parallel;
   - the 1,820 training-crystal jobs under one allocation;
   - MLP training as cluster tasks (270 runs).

## 6. Open points for the call

- **Central host location:** r3 sits outside the campus network. A
  Rutgers-hosted VM sits inside it. The deciding question is whether Amarel and
  SOE compute nodes can open outbound connections to r3.
- **Compute approvals:** the status of what Ryan is chasing with Ileny.
- **Access:** accounts on Amarel and SOE for running endpoints. Use the ML
  team's allocation, or ours?
- **Code and data:** the GitHub repository Yating announced for the week of
  09-21. Which revision of the scripts counts as validated?
- **Test data:** Qianqian's MD frames, and the separate thread Ryan opened on
  test files.
- **Who owns the campaign spec:** the ML team writes it with our templates, or
  we write it with them.

