"""Synthetic frames for an AuCaM-steered campaign (plan 11, section 5).

The campaign manager needs something to steer: frames whose scores spread,
some of them clearly poor, each costing node-hours.  A frame here is one
workflow of three stages built from the existing tools:

    md        atomic-fake-md          the simulation (cheap)
    train     atomic-fake-train       the model fit; its ``final_accuracy``
                                      is the frame's SCORE
    analysis  atomic-fake-descriptors the expensive production step

The score is known after ``train``, so a controller that cancels poor frames
saves their ``analysis`` stage -- that is what the demo shows.  How good a
frame is depends on its MD temperature (``fake_train``'s accuracy plateau
falls with temperature); how expensive it is, on its ``analysis`` duration.

Everything is drawn from one RNG seeded per campaign, and every frame
carries its own ``seed`` for the tools, so a rehearsal is reproducible:
same ``(n_frames, seed)``, same frames, same scores.

Stdlib only, like the tools themselves.
"""

import random

from typing import Any, Dict, List

from . import fake_train

# a frame whose temperature lands here is "clearly poor" (plateau <= ~0.79)
POOR_T = (1150.0, 1600.0)
# ... and here it spreads between excellent and fair (plateau ~0.99..0.85)
FAIR_T = (250.0, 900.0)

# the stage which yields the score, its output and the key in that output
SCORE_STAGE = 'train'
SCORE_FILE  = 'model.json'
SCORE_KEY   = 'summary.final_accuracy'

# thresholds that fit the scores above (a controller may use others)
GOOD_SCORE = 0.90
POOR_SCORE = 0.80


# ---------------------------------------------------------------------------
def frame_workflow(cores: int = 1) -> Dict[str, Any]:
    """The workflow template of one frame, ``{param}`` placeholders included.

    Valid input for :class:`~atomic_wm.campaign.planner.SweepPlanner`; the
    parameters come from :func:`synthetic_frames`.
    """

    req = {'cores': int(cores)}
    return {
        'name'  : 'synthetic-frame',
        'params': {},
        'stages': [
            {'name': 'md', 'type': 'simulation',
             'cmd': ['atomic-fake-md', '--temperature', '{temperature}',
                     '--steps', '{steps}', '--seed', '{seed}',
                     '--duration-sec', '{md_sec}', '--out', 'md.json'],
             'requirements': dict(req), 'inputs': [],
             'outputs': ['md.json']},
            {'name': 'train', 'type': 'ml_training',
             'cmd': ['atomic-fake-train', '--in', 'md.json',
                     '--epochs', '{epochs}', '--seed', '{seed}',
                     '--duration-sec', '{train_sec}', '--out', 'model.json'],
             'requirements': dict(req), 'inputs': ['md.json'],
             'outputs': ['model.json']},
            {'name': 'analysis', 'type': 'analysis',
             'cmd': ['atomic-fake-descriptors', '--in', 'md.json',
                     '--seed', '{seed}', '--duration-sec', '{analysis_sec}',
                     '--out', 'descriptors.json'],
             'requirements': dict(req), 'inputs': ['md.json'],
             'outputs': ['descriptors.json']},
        ],
    }


# ---------------------------------------------------------------------------
def synthetic_frames(n_frames: int = 10, seed: int = 0, *,
                     poor_fraction: float = 0.3,
                     stage_sec: float = 5.0) -> List[Dict[str, Any]]:
    """The parameters of *n_frames* frames, reproducible for *seed*.

    About *poor_fraction* of the frames are clearly poor (and at least one
    is, for ``n_frames >= 3``).  ``md`` and ``train`` take *stage_sec*
    each; ``analysis`` takes two to five times that, so the frames' costs
    spread as well.  Each frame dict has ``frame`` (its index), the tool
    parameters, and ``expected_score`` -- what its ``train`` stage will
    score.
    """

    if n_frames < 1:
        raise ValueError('n_frames must be >= 1')
    rng    = random.Random(seed)
    n_poor = max(1 if n_frames >= 3 else 0,
                 int(round(n_frames * poor_fraction)))
    poor   = set(rng.sample(range(n_frames), n_poor))

    frames = []
    for idx in range(n_frames):
        lo, hi = POOR_T if idx in poor else FAIR_T
        temp   = round(rng.uniform(lo, hi), 1)
        fseed  = rng.randrange(1, 2 ** 31)
        frames.append({
            'frame'         : idx,
            'temperature'   : temp,
            'steps'         : 200,
            'epochs'        : 20,
            'seed'          : fseed,
            'md_sec'        : stage_sec,
            'train_sec'     : stage_sec,
            'analysis_sec'  : round(stage_sec * rng.uniform(2.0, 5.0), 1),
            'expected_score': expected_score(temp, 20, fseed),
        })
    return frames


# ---------------------------------------------------------------------------
def expected_score(temperature: float, epochs: int, seed: int) -> float:
    """The score the ``train`` stage of a frame will report -- exactly.

    The tool is a pure function of its arguments, so running its model
    in-process (without the padding to ``--duration-sec``) gives the very
    number the stage writes.
    """

    doc = fake_train.run(float(temperature), int(epochs), int(seed), 0.0)
    return float(doc['summary']['final_accuracy'])


# ---------------------------------------------------------------------------
def synthetic_submit_body(n_frames: int = 10, seed: int = 0, *,
                          stage_sec: float = 5.0, cores: int = 1,
                          **aucam: Any) -> Dict[str, Any]:
    """A complete ``planner: "aucam"`` campaign submit body for the demo.

    Extra keyword arguments land in the body's ``aucam`` section (see
    :mod:`atomic_wm.campaign.aucam_adapter` for the keys).  The defaults
    fit the frames above: 10 frames, the goal at 4 good frames, a budget
    that runs out before every frame could have run its ``analysis``.
    """

    frames = synthetic_frames(n_frames, seed, stage_sec=stage_sec)
    params: Dict[str, Any] = {
        'frames'          : [{k: v for k, v in f.items()
                              if k != 'expected_score'} for f in frames],
        'score'           : {'stage': SCORE_STAGE, 'file': SCORE_FILE,
                             'key': SCORE_KEY},
        'good_score'      : GOOD_SCORE,
        'poor_score'      : POOR_SCORE,
        'goal_good_frames': 3,
        'max_concurrent'  : 4,
        'resources'       : {'cpus': cores},
        # with node_hour_scale 60 one wall second reads as one node-minute,
        # so a 5 s stage costs ~0.08 node-hours -- legible on screen
        'node_hour_scale' : 60.0,
        'frame_node_hours': 1.0,
        'budget_node_hours': 6.0,
    }
    params.update(aucam)
    return {'name'    : 'synthetic-frames',
            'planner' : 'aucam',
            'workflow': frame_workflow(cores),
            'aucam'   : params}
