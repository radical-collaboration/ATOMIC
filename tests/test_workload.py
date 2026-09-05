
"""Unit tests for the synthetic ATOMIC workload tools."""

import json
import os
import subprocess
import sys
import time

import pytest

from atomic_wm.workload import common
from atomic_wm.workload import fake_descriptors
from atomic_wm.workload import fake_md
from atomic_wm.workload import fake_train


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def run_md(tmp_path, out='md.json', temperature=300.0, steps=50, seed=1,
           duration=0.0):
    path = str(tmp_path / out)
    rc   = fake_md.main(['--temperature', str(temperature),
                         '--steps', str(steps),
                         '--out', path,
                         '--seed', str(seed),
                         '--duration-sec', str(duration)])
    assert rc == 0
    return path, load(path)


def run_train(tmp_path, inp, out='model.json', epochs=20, seed=1,
              duration=0.0):
    path = str(tmp_path / out)
    rc   = fake_train.main(['--in', inp, '--epochs', str(epochs),
                            '--out', path, '--seed', str(seed),
                            '--duration-sec', str(duration)])
    assert rc == 0
    return path, load(path)


def load(path):
    with open(path, 'r', encoding='utf-8') as fin:
        return json.load(fin)


def stable(doc):
    """The part of a document which must not vary between identical runs."""

    return {k: v for k, v in doc.items() if k != 'produced_by'}


# ---------------------------------------------------------------------------
# envelope
# ---------------------------------------------------------------------------

def test_envelope_shape(tmp_path):

    _, doc = run_md(tmp_path)

    for key in ['type', 'params', 'series', 'summary', 'produced_by']:
        assert key in doc, key

    assert doc['type'] == 'simulation'
    assert set(doc['produced_by']) == {'tool', 'version', 'host', 'ts'}
    assert doc['produced_by']['tool'] == 'atomic-fake-md'
    assert doc['produced_by']['version']
    assert doc['produced_by']['ts'] > 0


def test_envelope_extra_key_collision():

    with pytest.raises(ValueError):
        common.make_envelope('t', 'simulation', {}, {}, {},
                             extra={'summary': {}})


# ---------------------------------------------------------------------------
# fake_md
# ---------------------------------------------------------------------------

def test_md_series_shape(tmp_path):

    _, doc = run_md(tmp_path, steps=37, temperature=450.0)

    series = doc['series']
    assert set(series) == {'step', 'energy', 'temperature'}
    assert all(len(v) == 37 for v in series.values())
    assert series['step'] == list(range(37))

    assert doc['summary']['n_steps'] == 37
    assert doc['params']['temperature'] == 450.0
    # the contract mirrors the temperature at the top level as well
    assert doc['temperature'] == 450.0

    # the thermostat fluctuates around the requested temperature
    assert abs(doc['summary']['mean_temperature'] - 450.0) < 45.0
    assert doc['summary']['std_energy'] > 0.0


def test_default_seed_is_stable_and_parameter_derived():

    # no PYTHONHASHSEED dependency, no cross-machine drift
    assert common.default_seed('t', 300.0, 200) \
        == common.default_seed('t', 300.0, 200)
    assert common.default_seed('t', 300.0, 200) \
        != common.default_seed('t', 600.0, 200)
    assert common.default_seed('t', 300.0, 200) \
        != common.default_seed('t', 300.0, 201)
    assert isinstance(common.default_seed('t'), int)


def test_default_seed_makes_runs_reproducible(tmp_path):

    # identical parameters => identical output *without* --seed
    def md(name, temperature):
        out = str(tmp_path / name)
        assert fake_md.main(['--temperature', str(temperature),
                             '--steps', '20', '--out', out,
                             '--duration-sec', '0']) == 0
        return load(out)

    a = md('a.json', 300.0)
    b = md('b.json', 300.0)
    c = md('c.json', 600.0)

    assert stable(a) == stable(b)
    assert a['params']['seed'] == b['params']['seed']
    assert a['params']['seed'] != c['params']['seed']

    # ... and an explicit seed overrides the derived one
    explicit = str(tmp_path / 'd.json')
    assert fake_md.main(['--temperature', '300', '--steps', '20',
                         '--out', explicit, '--seed', '0',
                         '--duration-sec', '0']) == 0
    assert load(explicit)['params']['seed'] == 0


def test_default_seed_reproducible_for_train_and_descriptors(tmp_path):

    md, _ = run_md(tmp_path)

    docs = {}
    for tool, mod, args in [
            ('train', fake_train, ['--epochs', '10']),
            ('desc',  fake_descriptors, [])]:
        for name in ['a', 'b']:
            out = str(tmp_path / ('%s-%s.json' % (tool, name)))
            assert mod.main(['--in', md, '--out', out,
                             '--duration-sec', '0'] + args) == 0
            docs.setdefault(tool, []).append(load(out))

    for runs in docs.values():
        assert stable(runs[0]) == stable(runs[1])


def test_md_deterministic_under_seed(tmp_path):

    _, a = run_md(tmp_path, out='a.json', seed=7)
    _, b = run_md(tmp_path, out='b.json', seed=7)
    _, c = run_md(tmp_path, out='c.json', seed=8)

    assert stable(a) == stable(b)
    assert a['series']['energy'] != c['series']['energy']


def test_md_energy_relaxes_towards_equilibrium(tmp_path):

    _, doc = run_md(tmp_path, steps=400, temperature=300.0)

    energy = doc['series']['energy']
    head   = common.mean(energy[:20])
    tail   = common.mean(energy[-20:])

    # starts cold, relaxes upwards towards the equilibrium mean
    assert head < tail
    assert abs(tail - (-100.0 + 0.05 * 300.0)) < 1.0


def test_md_equilibrium_rises_with_temperature(tmp_path):

    tails = []
    for temp in [300.0, 600.0, 900.0]:
        _, doc = run_md(tmp_path, out='md-%d.json' % temp, steps=400,
                        temperature=temp)
        tails.append(common.mean(doc['series']['energy'][-50:]))

    assert tails == sorted(tails)


# ---------------------------------------------------------------------------
# fake_train
# ---------------------------------------------------------------------------

def test_train_series_shape(tmp_path):

    md, _   = run_md(tmp_path)
    _, doc  = run_train(tmp_path, md, epochs=25)

    series = doc['series']
    assert doc['type'] == 'ml_training'
    assert set(series) == {'epoch', 'loss', 'accuracy'}
    assert all(len(v) == 25 for v in series.values())
    assert doc['summary']['epochs'] == 25
    assert 0.0 <= doc['summary']['final_accuracy'] <= 1.0
    assert doc['summary']['final_loss'] >= 0.0
    assert doc['params']['input'] == md


def test_train_deterministic_under_seed(tmp_path):

    md, _  = run_md(tmp_path)
    _, a   = run_train(tmp_path, md, out='a.json', seed=3)
    _, b   = run_train(tmp_path, md, out='b.json', seed=3)
    _, c   = run_train(tmp_path, md, out='c.json', seed=4)

    assert stable(a) == stable(b)
    assert a['series']['loss'] != c['series']['loss']


def test_train_loss_decays_accuracy_rises(tmp_path):

    md, _  = run_md(tmp_path)
    _, doc = run_train(tmp_path, md, epochs=50)

    loss = doc['series']['loss']
    acc  = doc['series']['accuracy']

    assert common.mean(loss[:5]) > common.mean(loss[-5:])
    assert common.mean(acc[:5])  < common.mean(acc[-5:])


def test_train_plateau_decreases_with_temperature(tmp_path):

    finals = []
    for temp in [300.0, 600.0, 900.0]:
        md, _  = run_md(tmp_path, out='md-%d.json' % temp, temperature=temp)
        _, doc = run_train(tmp_path, md, out='model-%d.json' % temp,
                           epochs=20)
        finals.append(doc['summary']['final_accuracy'])
        assert doc['summary']['input_temperature'] == temp

    # the whole point: three visibly different curves, high T -> low acc
    assert finals[0] > finals[1] > finals[2]
    assert finals[0] > 0.95
    assert finals[2] < 0.90


def test_train_ordering_holds_with_default_seeds(tmp_path):

    # same as above but with *no* --seed anywhere: the demo sweep runs
    # this way, so the ordering must hold for the derived seeds too
    finals = []
    for temp in [300, 600, 900]:
        md    = str(tmp_path / ('md-%d.json' % temp))
        model = str(tmp_path / ('model-%d.json' % temp))

        assert fake_md.main(['--temperature', str(temp), '--steps', '200',
                             '--out', md, '--duration-sec', '0']) == 0
        assert fake_train.main(['--in', md, '--epochs', '20',
                                '--out', model,
                                '--duration-sec', '0']) == 0
        finals.append(load(model)['summary']['final_accuracy'])

    assert finals[0] > finals[1] > finals[2]


def test_train_ordering_robust_over_many_seeds(tmp_path):

    # the noise must stay far below the plateau gaps (0.06), for *any*
    # seed -- otherwise the demo would occasionally show the wrong story
    for seed in range(25):
        finals = [fake_train.run(float(t), 20, seed, 0.0)
                  ['summary']['final_accuracy']
                  for t in [300, 600, 900]]
        assert finals[0] > finals[1] > finals[2], (seed, finals)


def test_train_accepts_alternative_temperature_locations():

    plain  = {'temperature': 600.0}
    nested = {'params': {'temperature': 700.0}}
    series = {'series': {'temperature': [800.0, 800.0, 800.0]}}
    empty  = {'series': {'energy': [1.0]}}

    assert fake_train.input_temperature(plain)  == 600.0
    assert fake_train.input_temperature(nested) == 700.0
    assert fake_train.input_temperature(series) == 800.0
    assert fake_train.input_temperature(empty)  is None


def test_train_plateau_bounds():

    # the knots pinned by the contract
    assert fake_train.accuracy_plateau(300.0) == pytest.approx(0.99)
    assert fake_train.accuracy_plateau(600.0) == pytest.approx(0.93)
    assert fake_train.accuracy_plateau(900.0) == pytest.approx(0.85)

    # interpolated, extrapolated and clamped
    assert 0.93 < fake_train.accuracy_plateau(450.0) < 0.99
    assert 0.85 < fake_train.accuracy_plateau(750.0) < 0.93
    assert fake_train.accuracy_plateau(100.0)  == pytest.approx(0.99)
    assert fake_train.accuracy_plateau(9000.0) == pytest.approx(0.50)

    # strictly monotone over the demo's temperature range
    plateaus = [fake_train.accuracy_plateau(t)
                for t in range(300, 901, 50)]
    assert all(a > b for a, b in zip(plateaus, plateaus[1:]))


# ---------------------------------------------------------------------------
# fake_descriptors
# ---------------------------------------------------------------------------

def test_descriptors_histogram(tmp_path):

    md, _ = run_md(tmp_path)
    out   = str(tmp_path / 'desc.json')
    rc    = fake_descriptors.main(['--in', md, '--out', out,
                                   '--n-atoms', '128', '--bins', '10',
                                   '--seed', '2', '--duration-sec', '0'])
    assert rc == 0

    doc = load(out)
    assert doc['type'] == 'analysis'
    assert set(doc['series']) == {'bin_center', 'count'}
    assert len(doc['series']['count']) == 10
    assert sum(doc['series']['count']) == 128
    assert doc['summary']['n_atoms'] == 128


def test_descriptors_deterministic(tmp_path):

    md, _ = run_md(tmp_path)
    docs  = []
    for name in ['a.json', 'b.json']:
        out = str(tmp_path / name)
        assert fake_descriptors.main(['--in', md, '--out', out,
                                      '--seed', '5',
                                      '--duration-sec', '0']) == 0
        docs.append(load(out))

    assert stable(docs[0]) == stable(docs[1])


def test_descriptors_histogram_degenerate_input():

    hist = fake_descriptors.histogram([1.0, 1.0, 1.0], 4)

    assert sum(hist['count']) == 3
    assert len(hist['bin_center']) == 4


# ---------------------------------------------------------------------------
# atomic writes
# ---------------------------------------------------------------------------

def test_output_written_atomically(tmp_path):

    out = str(tmp_path / 'md.json')

    # a stale, longer file must be fully replaced, not overwritten in place
    with open(out, 'w', encoding='utf-8') as fout:
        fout.write('x' * 100000)

    run_md(tmp_path, out='md.json')

    assert load(out)['type'] == 'simulation'
    assert os.listdir(str(tmp_path)) == ['md.json']


def test_atomic_write_leaves_no_tmp_on_failure(tmp_path):

    out = str(tmp_path / 'broken.json')

    class Unserializable:
        pass

    with pytest.raises(TypeError):
        common.write_json_atomic(out, {'bad': Unserializable()})

    assert os.listdir(str(tmp_path)) == []


def test_write_creates_missing_directories(tmp_path):

    out = str(tmp_path / 'deep' / 'deeper' / 'md.json')
    rc  = fake_md.main(['--temperature', '300', '--steps', '5',
                        '--out', out, '--duration-sec', '0'])

    assert rc == 0
    assert load(out)['summary']['n_steps'] == 5


# ---------------------------------------------------------------------------
# bad input
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('argv', [
    ['--temperature', '0',    '--steps', '10', '--out', 'md.json'],
    ['--temperature', '-5',   '--steps', '10', '--out', 'md.json'],
    ['--temperature', '300',  '--steps', '0',  '--out', 'md.json'],
    ['--temperature', '300',  '--steps', '10', '--out', 'md.json',
     '--duration-sec', '-1'],
    ['--temperature', '{temperature}', '--steps', '10', '--out', 'md.json'],
    ['--steps', '10', '--out', 'md.json'],
])
def test_md_bad_input_exits_2(argv, tmp_path, monkeypatch):

    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc:
        fake_md.main(argv)

    assert exc.value.code == 2
    assert not os.path.exists(str(tmp_path / 'md.json'))


def test_train_missing_input_exits_2(tmp_path):

    with pytest.raises(SystemExit) as exc:
        fake_train.main(['--in', str(tmp_path / 'nope.json'),
                         '--epochs', '5', '--out', str(tmp_path / 'm.json')])

    assert exc.value.code == 2


def test_train_broken_input_exits_2(tmp_path):

    bad = tmp_path / 'bad.json'
    bad.write_text('not json at all', encoding='utf-8')

    with pytest.raises(SystemExit) as exc:
        fake_train.main(['--in', str(bad), '--epochs', '5',
                         '--out', str(tmp_path / 'm.json')])

    assert exc.value.code == 2


def test_train_input_without_temperature_exits_2(tmp_path):

    bad = tmp_path / 'no-temp.json'
    bad.write_text(json.dumps({'type': 'simulation', 'series': {}}),
                   encoding='utf-8')

    with pytest.raises(SystemExit) as exc:
        fake_train.main(['--in', str(bad), '--epochs', '5',
                         '--out', str(tmp_path / 'm.json')])

    assert exc.value.code == 2


def test_descriptors_input_without_energy_exits_2(tmp_path):

    bad = tmp_path / 'no-energy.json'
    bad.write_text(json.dumps({'type': 'simulation', 'summary': {}}),
                   encoding='utf-8')

    with pytest.raises(SystemExit) as exc:
        fake_descriptors.main(['--in', str(bad),
                               '--out', str(tmp_path / 'd.json')])

    assert exc.value.code == 2


def test_input_json_must_be_object(tmp_path):

    lst = tmp_path / 'list.json'
    lst.write_text('[1, 2, 3]', encoding='utf-8')

    with pytest.raises(SystemExit) as exc:
        common.read_input_json('t', str(lst))

    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# pacing
# ---------------------------------------------------------------------------

def test_duration_paces_the_run(tmp_path):

    start = time.monotonic()
    run_md(tmp_path, steps=5, duration=0.3)
    elapsed = time.monotonic() - start

    assert elapsed >= 0.3
    assert elapsed <  5.0


def test_zero_duration_does_not_sleep(tmp_path):

    start = time.monotonic()
    run_md(tmp_path, steps=5, duration=0.0)

    assert time.monotonic() - start < 1.0


# ---------------------------------------------------------------------------
# end to end, as a task would run it
# ---------------------------------------------------------------------------

def test_pipeline_via_subprocess(tmp_path):

    env  = dict(os.environ)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env['PYTHONPATH'] = os.pathsep.join([root, env.get('PYTHONPATH', '')])

    md    = str(tmp_path / 'md.json')
    model = str(tmp_path / 'model.json')
    desc  = str(tmp_path / 'desc.json')

    steps = [
        [sys.executable, '-m', 'atomic_wm.workload.fake_md',
         '--temperature', '600', '--steps', '50', '--out', md,
         '--duration-sec', '0.2'],
        [sys.executable, '-m', 'atomic_wm.workload.fake_train',
         '--in', md, '--epochs', '10', '--out', model,
         '--duration-sec', '0.2'],
        [sys.executable, '-m', 'atomic_wm.workload.fake_descriptors',
         '--in', md, '--out', desc, '--duration-sec', '0.2'],
    ]

    for cmd in steps:
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        # each tool prints exactly one summary line to stdout
        assert len(proc.stdout.strip().splitlines()) == 1

    assert load(md)['type']    == 'simulation'
    assert load(model)['type'] == 'ml_training'
    assert load(desc)['type']  == 'analysis'
    assert load(model)['summary']['input_temperature'] == 600.0
