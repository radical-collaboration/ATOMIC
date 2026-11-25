# Grain-boundary frames

`atomic-gb-frame` runs the real grain-boundary classifier on one MD frame, as
two stages: `denoise`, then `classify` (ACE descriptors, PTM, frame assembly,
classifier). The ACE output is about 90 MB per frame, so it never leaves the
`classify` task. The score is `summary.fraction_phase_b` in `gb.json`.

The classifier package, its weights and the frames are not part of this
repository. A resource needs them, and two environments, below
`$ATOMIC_GB_ROOT`, which must be set in the environment of the endpoint
*before* `atomic-join`:

| path | content |
|---|---|
| `pkg/` | the classifier package (`atomic/`, `weights/`, `data/gb/`) |
| `frames/` | the raw `.cfg` frames |
| `model.joblib` | the trained classifier, the same file on every resource |
| `ve/` | Python 3.12: numpy 1.26.4, scipy 1.13, pandas 2.2, scikit-learn 1.6, matplotlib 3.9, joblib 1.4, torch 2.5 (CPU), torch_geometric >= 2.5, `ovito>=3.10,<3.13` (newer ones need numpy 2), graphite @ `4e09a285378fd7af50f204c0f8f5e2642fedc5f0`, and `pip install --no-deps pkg/` |
| `lmpenv/` | conda-forge `lammps=2025.07.22=cpu_*mpi_mpich*` (the PyPI wheel has no ML-PACE) |

`$ATOMIC_GB_PYTHON`, `$ATOMIC_GB_LMP`, `$ATOMIC_GB_PKG`, `$ATOMIC_GB_FRAMES`
and `$ATOMIC_GB_MODEL` override single entries. `$ATOMIC_GB_LMP` is the LAMMPS
command line up to its arguments, e.g. `srun -n {ranks} lmp`.

Train the model once, check it against reference scores, and copy the file to
every resource. The training depends on the number of BLAS threads and on the
CPU kernels: the same seed gives another model, and scores that differ by a few
points, under other settings.

```sh
OMP_NUM_THREADS=4 OPENBLAS_CORETYPE=Haswell \
    atomic-gb-frame train --out $ATOMIC_GB_ROOT/model.joblib
```

```sh
atomic-gb-frame spec --pattern 'dump.{step}.cfg' --steps 1000,2000 > gb.json
atomic-campaign submit gb.json --wait
```

A frame whose fraction lies in the transition band 0.15 .. 0.85 is *good*
(`aucam.good_band`); `--goal N` ends the campaign after N of them. The score
appears with the last stage, so there is no frame to cancel early.
