"""``atomic-gb-frame`` -- one grain-boundary frame, with the real classifier.

    atomic-gb-frame denoise  --frame NAME --step N --out den.dump
    atomic-gb-frame train    --out model.joblib     # once, see below
    atomic-gb-frame classify --in den.dump --out gb.json [--ranks 4]
    atomic-gb-frame spec     --pattern 'dump.{step}.cfg' --steps 1,2,3

Two stages, cut by data size: the raw frame and the denoised dump are a few
100 kB and may travel through the broker; the ACE descriptors (about 90 MB of
text per frame) may not, so everything that reads them runs in ``classify``,
which returns a small JSON document (the envelope of :mod:`.common`; the
score is ``summary.fraction_phase_b``).

Like the synthetic tools this module is stdlib only: it runs in the endpoint's
environment.  The science runs in its own python, see :mod:`.gb_science`.
Everything site specific is below ``$ATOMIC_GB_ROOT``::

    ve/bin/python           the science environment ($ATOMIC_GB_PYTHON)
    lmpenv/bin/{mpiexec,lmp} LAMMPS with ML-PACE     ($ATOMIC_GB_LMP, a command
                            line; ``{ranks}`` is replaced)
    pkg/                    the classifier package   ($ATOMIC_GB_PKG)
    frames/                 the raw frames           ($ATOMIC_GB_FRAMES)
    model.joblib            the trained classifier   ($ATOMIC_GB_MODEL)

The model is trained once and copied to every resource.  Its training depends
on the number of BLAS threads and on the CPU kernels, so a model trained per
task or per resource gives different scores for the same frame.

See ``docs/gb_frames.md`` for the recipe.
"""

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile

from typing import Any, Dict, List, Optional, Sequence

from . import common

TOOL     = 'atomic-gb-frame'
SYSTEM   = 'gb_sigma11'
ENV_ROOT = 'ATOMIC_GB_ROOT'

SCORE_STAGE = 'classify'
SCORE_FILE  = 'gb.json'
SCORE_KEY   = 'summary.fraction_phase_b'

# a frame whose phase-B fraction lies in here is in transition
TRANSITION = [0.15, 0.85]


# ---------------------------------------------------------------------------
def site() -> Dict[str, str]:
    """The site paths, from the environment."""

    root = os.environ.get(ENV_ROOT, '')
    common.require(TOOL, bool(root), '$%s is not set' % ENV_ROOT)

    def _get(key: str, default: str) -> str:
        return os.environ.get('ATOMIC_GB_' + key) or default

    return {'python': _get('PYTHON', os.path.join(root, 've/bin/python')),
            'lmp'   : _get('LMP', '%s -launcher fork -n {ranks} %s'
                           % (shlex.quote(os.path.join(root,
                                                       'lmpenv/bin/mpiexec')),
                              shlex.quote(os.path.join(root,
                                                       'lmpenv/bin/lmp')))),
            'pkg'   : _get('PKG', os.path.join(root, 'pkg')),
            'frames': _get('FRAMES', os.path.join(root, 'frames')),
            'model' : _get('MODEL', os.path.join(root, 'model.joblib'))}


def science_env() -> Dict[str, str]:
    """The environment of the science python: nothing of the endpoint's."""

    env = {k: v for k, v in os.environ.items()
           if k not in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV')}
    env['OMP_NUM_THREADS'] = os.environ.get('OMP_NUM_THREADS', '1')
    return env


def run_science(cmd: str, args: List[str], work: str) -> Dict[str, Any]:
    """Run one command of ``gb_science.py``; return its result document."""

    result = os.path.join(work, 'result.json')
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'gb_science.py')
    argv   = [site()['python'], '-W', 'ignore', script, cmd, '--work', work,
              '--result', result] + args
    try:
        rc = subprocess.call(argv, env=science_env())
    except OSError as e:
        common.die(TOOL, 'cannot run %s (%s)' % (argv[0], e))
    if rc:
        common.die(TOOL, '%s failed (rc=%d)' % (cmd, rc), code=1)
    return common.read_input_json(TOOL, result)


# ---------------------------------------------------------------------------
def denoise(args: argparse.Namespace, work: str) -> str:
    cfg  = site()
    path = os.path.join(cfg['frames'], args.frame)
    common.require(TOOL, os.path.isfile(path), 'no such frame: %s' % path)
    out  = os.path.join(work, 'den.dump')
    doc  = run_science('denoise', [
        '--input', path, '--out', out, '--step', str(args.step),
        '--ckpt', os.path.join(cfg['pkg'], 'weights/denoiser',
                               SYSTEM + '.ckpt')], work)
    # into place at once: collection never sees half a dump
    tmp = os.path.abspath(args.out) + '.tmp'
    shutil.move(out, tmp)
    os.replace(tmp, args.out)
    return 'atoms=%d' % doc['n_atoms']


def train(args: argparse.Namespace, work: str) -> str:
    out = os.path.join(work, 'model.joblib')
    doc = run_science('train', [
        '--train', os.path.join(site()['pkg'], 'data/gb',
                                SYSTEM + '_train.npz'),
        '--out', out, '--seed', str(args.seed)], work)
    shutil.move(out, args.out)
    return 'iterations=%d' % doc['iterations']


def classify(args: argparse.Namespace, work: str) -> str:
    cfg = site()
    common.require(TOOL, os.path.isfile(cfg['model']),
                   'no model: %s (atomic-gb-frame train)' % cfg['model'])
    with open(cfg['model'], 'rb') as fd:
        digest = hashlib.sha256(fd.read()).hexdigest()[:12]
    lmp = shlex.split(cfg['lmp'].replace('{ranks}', str(args.ranks)))
    res = run_science('classify', [
        '--input', os.path.abspath(args.inp), '--lmp', json.dumps(lmp),
        '--model', cfg['model']], work)
    frac = float(res['fraction_phase_b'])
    doc  = common.make_envelope(
        TOOL, 'analysis',
        {'system': SYSTEM, 'timestep': res['timestep'], 'model': digest,
         'ranks': args.ranks}, {},
        {'fraction_phase_b': frac, 'n_boundary': res['n_boundary'],
         'n_atoms': res['n_atoms'], 'timing': res['timing']})
    common.write_output(TOOL, args.out, doc)
    return 'step=%s fraction_phase_b=%.4f n_boundary=%d' \
        % (res['timestep'], frac, res['n_boundary'])


# ---------------------------------------------------------------------------
def frame_workflow(pattern: str, cores: int = 4) -> Dict[str, Any]:
    """The workflow template of one frame; ``{step}`` is its parameter."""

    req = {'cores': int(cores)}
    return {'name': 'gb-frame', 'params': {}, 'stages': [
        {'name': 'denoise', 'type': 'simulation',
         'cmd': [TOOL, 'denoise', '--frame', pattern, '--step', '{step}',
                 '--out', 'den.dump'],
         'requirements': dict(req), 'inputs': [], 'outputs': ['den.dump']},
        {'name': 'classify', 'type': 'analysis',
         'cmd': [TOOL, 'classify', '--in', 'den.dump', '--out', SCORE_FILE,
                 '--ranks', str(cores)],
         'requirements': dict(req), 'inputs': ['den.dump'],
         'outputs': [SCORE_FILE]}]}


def submit_body(pattern: str, steps: Sequence[int], *, cores: int = 4,
                goal: Optional[int] = None, **aucam: Any) -> Dict[str, Any]:
    """A ``planner: "aucam"`` submit body over the frames *steps*: a frame in
    transition is good; with *goal* the campaign ends after that many."""

    params: Dict[str, Any] = {
        'frames'        : [{'frame': i, 'step': int(s)}
                           for i, s in enumerate(steps)],
        'score'         : {'stage': SCORE_STAGE, 'file': SCORE_FILE,
                           'key': SCORE_KEY},
        'good_band'     : list(TRANSITION),
        'resources'     : {'cpus': cores},
        'max_concurrent': 2}
    if goal is not None:
        params['goal_good_frames'] = int(goal)
    params.update(aucam)
    return {'name': 'gb-frames', 'planner': 'aucam',
            'workflow': frame_workflow(pattern, cores), 'aucam': params}


# ---------------------------------------------------------------------------
def main(argv: Optional[Sequence[str]] = None) -> int:
    """Console script entry point."""

    parser = argparse.ArgumentParser(prog=TOOL,
                                     description='grain-boundary frames')
    sub = parser.add_subparsers(dest='cmd', required=True)
    p   = sub.add_parser('denoise')
    p.add_argument('--frame', required=True, help='file name in frames/')
    p.add_argument('--step',  required=True, type=int)
    p.add_argument('--out',   required=True)
    p   = sub.add_parser('train', help='train the classifier, once')
    p.add_argument('--out',   required=True)
    p.add_argument('--seed',  type=int, default=42)
    p   = sub.add_parser('classify')
    p.add_argument('--in', dest='inp', required=True)
    p.add_argument('--out',   required=True)
    p.add_argument('--ranks', type=int, default=4)
    p   = sub.add_parser('spec', help='print a campaign submit body')
    p.add_argument('--pattern', required=True,
                   help='frame file name with a {step} placeholder')
    p.add_argument('--steps', required=True, help='comma separated')
    p.add_argument('--cores', type=int, default=4)
    p.add_argument('--goal',  type=int, default=None)
    args = parser.parse_args(argv)

    if args.cmd == 'spec':
        try:
            steps = [int(s) for s in args.steps.split(',')]
        except ValueError:
            common.die(TOOL, '--steps must be integers')
        json.dump(submit_body(args.pattern, steps, cores=args.cores,
                              goal=args.goal), sys.stdout, indent=1)
        sys.stdout.write('\n')
        return 0

    work = tempfile.mkdtemp(prefix='gb-work-', dir=os.getcwd())
    try:
        msg = {'denoise': denoise, 'train': train,
               'classify': classify}[args.cmd](args, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    sys.stdout.write('%s: %s -> %s\n' % (TOOL, msg, args.out))
    return 0


if __name__ == '__main__':
    sys.exit(main())
