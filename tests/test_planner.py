"""Unit tests for the campaign planner (``atomic_wm.campaign.planner``)."""

import pytest

from atomic_wm.campaign.planner import (PlannerError, SweepPlanner,
                                        format_value, placeholders,
                                        required_parameters, substitute,
                                        validate_sweep, validate_workflow)


# ---------------------------------------------------------------------------
def _spec(**over):
    spec = {
        'name'  : 'vacancy-classifier',
        'params': {'temperature': 300},
        'stages': [
            {'name': 'md', 'type': 'simulation',
             'cmd': ['atomic-fake-md', '--temperature', '{temperature}',
                     '--out', 'md.json'],
             'requirements': {'cores': 2, 'software': ['lammps']},
             'inputs': [], 'outputs': ['md.json']},
            {'name': 'train', 'type': 'ml_training',
             'cmd': ['atomic-fake-train', '--in', 'md.json',
                     '--out', 'model.json'],
             'requirements': {'cores': 2}, 'inputs': ['md.json'],
             'outputs': ['model.json']},
        ],
    }
    spec.update(over)
    return spec


# ---------------------------------------------------------------------------
class TestSubstitution:

    def test_substitute_replaces_every_placeholder(self):
        assert substitute('{a}-{b}-{a}', {'a': 1, 'b': 'x'}) == '1-x-1'

    def test_substitute_leaves_plain_text(self):
        assert substitute('--steps', {}) == '--steps'

    def test_substitute_ignores_non_identifier_braces(self):
        # a JSON snippet in a command must survive untouched
        text = '{"k": 1}'
        assert substitute(text, {}) == text

    def test_substitute_raises_on_unknown_parameter(self):
        with pytest.raises(PlannerError) as exc:
            substitute('--t {temperature}', {'steps': 2})
        assert 'temperature' in str(exc.value)

    def test_format_value_keeps_ints_int(self):
        assert format_value(300)   == '300'
        assert format_value(1.5)   == '1.5'
        assert format_value(True)  == 'true'
        assert format_value('abc') == 'abc'

    def test_placeholders_and_required_parameters(self):
        assert placeholders('{a} {b} {a}') == ['a', 'b', 'a']
        assert list(required_parameters(_spec())) == ['temperature']


# ---------------------------------------------------------------------------
class TestValidation:

    def test_accepts_the_demo_spec(self):
        validate_workflow(_spec())

    def test_rejects_non_object(self):
        with pytest.raises(PlannerError):
            validate_workflow([1, 2, 3])

    def test_rejects_missing_stages(self):
        with pytest.raises(PlannerError):
            validate_workflow({'name': 'x', 'stages': []})

    def test_rejects_duplicate_stage_names(self):
        spec = _spec()
        spec['stages'][1]['name'] = 'md'
        with pytest.raises(PlannerError) as exc:
            validate_workflow(spec)
        assert 'duplicate' in str(exc.value)

    def test_rejects_empty_cmd(self):
        spec = _spec()
        spec['stages'][0]['cmd'] = []
        with pytest.raises(PlannerError):
            validate_workflow(spec)

    def test_rejects_non_string_cmd_element(self):
        spec = _spec()
        spec['stages'][0]['cmd'] = ['atomic-fake-md', 300]
        with pytest.raises(PlannerError):
            validate_workflow(spec)

    def test_rejects_path_in_outputs(self):
        spec = _spec()
        spec['stages'][0]['outputs'] = ['sub/md.json']
        with pytest.raises(PlannerError) as exc:
            validate_workflow(spec)
        assert 'plain file name' in str(exc.value)

    def test_rejects_stage_name_with_slash(self):
        spec = _spec()
        spec['stages'][0]['name'] = 'a/b'
        with pytest.raises(PlannerError):
            validate_workflow(spec)

    def test_sweep_validation(self):
        assert validate_sweep(None) == {}
        assert validate_sweep({'t': [1, 2]}) == {'t': [1, 2]}
        with pytest.raises(PlannerError):
            validate_sweep({'t': []})
        with pytest.raises(PlannerError):
            validate_sweep({'t': 300})
        with pytest.raises(PlannerError):
            validate_sweep([1])


# ---------------------------------------------------------------------------
class TestSweepPlanner:

    def test_one_instance_per_sweep_point(self):
        wfs = SweepPlanner().expand(_spec(), {'temperature': [300, 600, 900]})
        assert [w.id for w in wfs] == ['wf-000', 'wf-001', 'wf-002']
        assert [w.params['temperature'] for w in wfs] == [300, 600, 900]
        assert all(w.name == 'vacancy-classifier' for w in wfs)

    def test_substitutes_into_the_commands(self):
        wfs = SweepPlanner().expand(_spec(), {'temperature': [600]})
        assert wfs[0].stages[0].cmd == ['atomic-fake-md', '--temperature',
                                        '600', '--out', 'md.json']

    def test_carries_stage_metadata(self):
        wf = SweepPlanner().expand(_spec(), {'temperature': [300]})[0]
        md, train = wf.stages
        assert md.type             == 'simulation'
        assert md.declared_outputs == ['md.json']
        assert md.requirements     == {'cores': 2, 'software': ['lammps']}
        assert train.inputs        == ['md.json']
        assert md.state            == 'PENDING'

    def test_cartesian_product_last_key_fastest(self):
        wfs = SweepPlanner().expand(_spec(),
                                    {'temperature': [300, 600],
                                     'steps': [10, 20]})
        assert [(w.params['temperature'], w.params['steps']) for w in wfs] \
            == [(300, 10), (300, 20), (600, 10), (600, 20)]

    def test_no_sweep_yields_one_instance_with_spec_params(self):
        wfs = SweepPlanner().expand(_spec(), {})
        assert len(wfs) == 1
        assert wfs[0].params == {'temperature': 300}

    def test_sweep_overrides_spec_params(self):
        wfs = SweepPlanner().expand(_spec(), {'temperature': [900]})
        assert wfs[0].params['temperature'] == 900

    def test_unknown_placeholder_is_rejected(self):
        spec = _spec()
        spec['params'] = {}
        spec['stages'][0]['cmd'] = ['atomic-fake-md', '{pressure}']
        with pytest.raises(PlannerError) as exc:
            SweepPlanner().expand(spec, {'temperature': [300]})
        assert 'pressure' in str(exc.value)

    def test_instances_do_not_share_stage_objects(self):
        wfs = SweepPlanner().expand(_spec(), {'temperature': [300, 600]})
        wfs[0].stages[0].state = 'DONE'
        assert wfs[1].stages[0].state == 'PENDING'
