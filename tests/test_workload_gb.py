"""``atomic-gb-frame``: the wrapper, with a stand-in for the science python."""

import json
import os
import stat
import sys

import pytest

from atomic_wm.campaign import aucam_adapter as aa
from atomic_wm.campaign.planner import PlannerError
from atomic_wm.workload import gb_frame

FAKE = '''#!%s
import json, sys
a = sys.argv
if '--out' in a:
    open(a[a.index('--out') + 1], 'w').write('dump')
json.dump({"argv": a, "n_atoms": 5, "timestep": 7, "n_boundary": 3, "iterations": 9,
           "fraction_phase_b": 0.5, "timing": {}},
          open(a[a.index('--result') + 1], 'w'))
json.dump(a, open(%r, 'w'))
sys.exit(%d)
'''


@pytest.fixture
def root(tmp_path, monkeypatch):
    def make(rc=0):
        (tmp_path / 've/bin').mkdir(parents=True, exist_ok=True)
        exe = tmp_path / 've/bin/python'
        exe.write_text(FAKE % (sys.executable, str(tmp_path / 'argv'), rc))
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
        return tmp_path
    (tmp_path / 'frames').mkdir()
    (tmp_path / 'frames/f.7.cfg').write_text('x')
    (tmp_path / 'cwd').mkdir()
    (tmp_path / 'model.joblib').write_text('m')
    monkeypatch.chdir(tmp_path / 'cwd')
    monkeypatch.setenv('ATOMIC_GB_ROOT', str(tmp_path))
    for key in ('PYTHON', 'LMP', 'PKG', 'FRAMES', 'MODEL'):
        monkeypatch.delenv('ATOMIC_GB_' + key, raising=False)
    return make


class TestTool:

    def test_denoise_leaves_only_its_output(self, root):
        root()
        assert gb_frame.main(['denoise', '--frame', 'f.7.cfg', '--step', '7',
                              '--out', 'den.dump']) == 0
        assert os.listdir('.') == ['den.dump']

    def test_classify_writes_the_score_and_runs_the_site_lammps(self, root):
        base = root()
        (base / 'cwd/den.dump').write_text('dump')
        assert gb_frame.main(['classify', '--in', 'den.dump', '--out',
                              'gb.json', '--ranks', '3']) == 0
        doc = json.loads((base / 'cwd/gb.json').read_text())
        assert doc['summary']['fraction_phase_b'] == 0.5
        argv = json.loads((base / 'argv').read_text())
        assert argv[argv.index('--model') + 1] == \
            str(base / 'model.joblib')
        lmp  = json.loads(argv[argv.index('--lmp') + 1])
        assert lmp == [str(base / 'lmpenv/bin/mpiexec'), '-launcher', 'fork',
                       '-n', '3', str(base / 'lmpenv/bin/lmp')]
        assert sorted(os.listdir('.')) == ['den.dump', 'gb.json']

    def test_a_failing_science_run_fails_and_leaves_no_output(self, root):
        root(rc=3)
        with pytest.raises(SystemExit) as exc:
            gb_frame.main(['denoise', '--frame', 'f.7.cfg', '--step', '7',
                           '--out', 'den.dump'])
        assert exc.value.code == 1
        kept = os.listdir('.')
        assert len(kept) == 1 and kept[0].startswith('gb-work-')

    @pytest.mark.parametrize('frame', ['missing.cfg'])
    def test_a_missing_frame_is_a_clean_error(self, root, frame):
        root()
        with pytest.raises(SystemExit) as exc:
            gb_frame.main(['denoise', '--frame', frame, '--step', '7',
                           '--out', 'den.dump'])
        assert exc.value.code == 2

    def test_classify_without_a_model_is_a_clean_error(self, root):
        base = root()
        (base / 'model.joblib').unlink()
        with pytest.raises(SystemExit) as exc:
            gb_frame.main(['classify', '--in', 'den.dump', '--out', 'g.json'])
        assert exc.value.code == 2

    def test_train_writes_the_model(self, root):
        root()
        assert gb_frame.main(['train', '--out', 'model.joblib']) == 0
        assert os.listdir('.') == ['model.joblib']

    def test_no_root_is_a_clean_error(self, root, monkeypatch):
        root()
        monkeypatch.delenv('ATOMIC_GB_ROOT')
        with pytest.raises(SystemExit) as exc:
            gb_frame.main(['denoise', '--frame', 'f.7.cfg', '--step', '7',
                           '--out', 'den.dump'])
        assert exc.value.code == 2

    def test_the_endpoint_python_settings_do_not_leak(self, monkeypatch):
        monkeypatch.setenv('PYTHONPATH', '/endpoint')
        assert 'PYTHONPATH' not in gb_frame.science_env()


@pytest.mark.skipif(aa.AUCAM_ERROR is not None, reason='no AuCaM')
class TestPlan:

    def test_the_spec_is_a_valid_request(self, capsys):
        assert gb_frame.main(['spec', '--pattern', 'f.{step}.cfg', '--steps',
                              '7,8', '--goal', '1']) == 0
        plan = aa.parse_aucam_request(json.loads(capsys.readouterr().out))
        inst = plan.instance(plan.frames[1], 'x.y.abcdef12')
        assert 'f.8.cfg' in inst.stages[0].cmd
        assert plan.goal_good_frames == 1

    @pytest.mark.parametrize('score,good', [
        (0.1, False), (0.15, True), (0.6, True), (0.85, True), (0.9, False),
        (None, False)])
    def test_the_band_marks_good_frames(self, score, good):
        plan = aa.parse_aucam_request(gb_frame.submit_body('f.{step}', [1]))
        assert plan.is_good(score) is good

    def test_the_best_frame_is_the_middle_of_the_band(self):
        plan = aa.parse_aucam_request(gb_frame.submit_body('f.{step}', [1]))
        assert plan.ranks(0.6, 0.95) and not plan.ranks(0.95, 0.6)

    @pytest.mark.parametrize('extra', [
        {'good_band': [0.8, 0.2]}, {'good_band': [1]}, {'good_band': 'x'},
        {'good_score': 0.5}])
    def test_a_bad_band_is_refused(self, extra):
        with pytest.raises(PlannerError):
            aa.parse_aucam_request(
                gb_frame.submit_body('f.{step}', [1], **extra))
