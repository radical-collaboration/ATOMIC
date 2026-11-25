"""The science half of ``atomic-gb-frame`` -- run by the *science* python.

Executed as a plain script by :mod:`atomic_wm.workload.gb_frame`, in the
environment which holds the ATOMIC classifier package (``atomic``), torch,
OVITO and scikit-learn.  It must not import ``atomic_wm``: that package lives
in another environment.  Writes one small JSON document, nothing else.
"""

import argparse
import json
import os
import subprocess
import sys
import time

from pathlib import Path


def _denoise(args):
    from atomic.denoise import denoise_frame, load_denoiser
    from atomic.gb.frames import cfg_to_dump
    from atomic.io import read_dump, write_dump

    work = Path(args.work)
    raw  = work / 'raw.dump'
    n, _ = cfg_to_dump(args.input, raw, args.step)
    model, hparams = load_denoiser(args.ckpt, 'cpu')
    frame = denoise_frame(read_dump(raw), model, args.steps,
                          cutoff=float(hparams.get('cutoff', 3.2)))
    write_dump(args.out, frame)
    raw.unlink()
    return {'n_atoms': int(n), 'timestep': int(args.step)}


def _train(args):
    import joblib
    import numpy as np

    from atomic.gb import train_gb_classifier

    with np.load(args.train, allow_pickle=False) as z:
        model = train_gb_classifier(z['X'], z['y'], random_state=args.seed)
    joblib.dump(model, args.out)
    return {'iterations': int(model.named_steps['mlp'].n_iter_)}


def _classify(args):
    import joblib

    from atomic import ace
    from atomic.dataset import AtomicDataset
    from atomic.gb import predict_frames
    from atomic.io import read_dump
    from atomic.ptm import run_ptm

    timing = {}
    work   = Path(args.work)
    frame, _ = read_dump(args.input).sorted_by_id()

    # 1. ACE descriptors (LAMMPS, about 90 MB of text: it stays in `work`)
    start = time.time()
    ace.write_lammps_data(work / 'snapshot.data', frame, masses=[args.mass])
    # the run directory is self-contained, as `atomic.ace prepare` makes it
    (work / ace.POTENTIAL.name).write_bytes(ace.POTENTIAL.read_bytes())
    ace.write_lammps_input('snapshot.data', work / 'in.ace',
                           potential=ace.POTENTIAL.name, mass=args.mass)
    with open(work / 'lmp.out', 'w', encoding='utf-8') as fd:
        rc = subprocess.call(json.loads(args.lmp) + ['-in', 'in.ace'],
                             cwd=work, stdout=fd, stderr=subprocess.STDOUT)
    if rc:
        tail = (work / 'lmp.out').read_text(encoding='utf-8')[-800:]
        raise SystemExit('LAMMPS failed (rc=%d): %s' % (rc, tail))
    X = ace.parse_pace_output(work / 'pace_row_output.txt',
                              n_atoms=frame.n_atoms)
    timing['ace'] = time.time() - start

    # 2. boundary atoms, 3. classifier.  The model is trained once and
    # loaded here: its training depends on the BLAS threads and CPU kernels,
    # so a model trained per task scores differently per resource.
    ptm = run_ptm(args.input)
    ds  = AtomicDataset.from_frame(frame, X=X, ptm=ptm)
    start = time.time()
    model = joblib.load(args.model)
    res = predict_frames(model, [ds])[0]
    timing['predict'] = time.time() - start
    for name in ('pace_row_output.txt', 'snapshot.data'):
        (work / name).unlink()
    return {'n_atoms'         : int(frame.n_atoms),
            'timestep'        : int(frame.timestep),
            'n_boundary'      : int(res['n_boundary']),
            'fraction_phase_b': float(res['fraction_phase_b']),
            'timing'          : {k: round(v, 2) for k, v in timing.items()}}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('cmd', choices=('denoise', 'train', 'classify'))
    for name in ('--work', '--result'):
        parser.add_argument(name, required=True)
    parser.add_argument('--input')
    parser.add_argument('--out')
    parser.add_argument('--ckpt')
    parser.add_argument('--train')
    parser.add_argument('--model')
    parser.add_argument('--lmp')
    parser.add_argument('--step',  type=int,   default=0)
    parser.add_argument('--steps', type=int,   default=8)
    parser.add_argument('--seed',  type=int,   default=42)
    parser.add_argument('--mass',  type=float, default=55.845)
    args = parser.parse_args(argv)

    os.makedirs(args.work, exist_ok=True)
    doc = {'denoise': _denoise, 'train': _train,
           'classify': _classify}[args.cmd](args)
    with open(args.result, 'w', encoding='utf-8') as fd:
        json.dump(doc, fd)
    return 0


if __name__ == '__main__':
    sys.exit(main())
