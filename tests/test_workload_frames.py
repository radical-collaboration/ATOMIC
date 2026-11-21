"""The synthetic frame workload for the AuCaM demo (plan 11, section 5)."""

import json

import pytest

from atomic_wm.campaign.planner import SweepPlanner
from atomic_wm.workload import fake_train
from atomic_wm.workload.frames import (GOOD_SCORE, POOR_SCORE,
                                       expected_score, frame_workflow,
                                       synthetic_frames,
                                       synthetic_submit_body)


def test_frames_are_reproducible():
    assert synthetic_frames(10, 7) == synthetic_frames(10, 7)
    assert synthetic_frames(10, 7) != synthetic_frames(10, 8)


@pytest.mark.parametrize('seed', range(6))
def test_scores_spread_with_good_and_clearly_poor_frames(seed):
    scores = [f['expected_score'] for f in synthetic_frames(10, seed)]
    assert sum(s >= GOOD_SCORE for s in scores) >= 2
    assert sum(s < POOR_SCORE for s in scores) >= 2
    assert max(scores) - min(scores) > 0.15


def test_costs_spread():
    secs = [f['analysis_sec'] for f in synthetic_frames(10, 0, stage_sec=5)]
    assert min(secs) >= 10 and max(secs) <= 25
    assert len(set(secs)) > 5


def test_expected_score_is_what_the_tool_writes(tmp_path):
    frame = synthetic_frames(3, 4)[1]
    md    = tmp_path / 'md.json'
    out   = tmp_path / 'model.json'
    from atomic_wm.workload import fake_md
    doc = fake_md.run(frame['temperature'], frame['steps'], frame['seed'], 0)
    md.write_text(json.dumps(doc))
    assert fake_train.main(['--in', str(md), '--epochs',
                            str(frame['epochs']), '--seed',
                            str(frame['seed']), '--duration-sec', '0',
                            '--out', str(out)]) == 0
    score = json.loads(out.read_text())['summary']['final_accuracy']
    assert score == pytest.approx(frame['expected_score'])
    assert expected_score(frame['temperature'], frame['epochs'],
                          frame['seed']) == frame['expected_score']


def test_every_frame_expands_into_a_workflow():
    wf = frame_workflow(cores=2)
    for frame in synthetic_frames(5, 0):
        inst = SweepPlanner().expand(wf, {k: [v] for k, v in frame.items()})
        (inst,) = inst
        assert [s.name for s in inst.stages] == ['md', 'train', 'analysis']
        assert str(frame['temperature']) in inst.stages[0].cmd
        assert all(s.requirements == {'cores': 2} for s in inst.stages)


def test_submit_body_selects_the_aucam_planner():
    body = synthetic_submit_body(6, 1, goal_good_frames=2)
    assert body['planner'] == 'aucam'
    assert len(body['aucam']['frames']) == 6
    assert body['aucam']['goal_good_frames'] == 2
    assert 'expected_score' not in body['aucam']['frames'][0]
    json.dumps(body)                      # it is a request body
