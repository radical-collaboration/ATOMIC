"""Unit tests for the ``atomic-campaign`` CLI (the Client is mocked)."""

import json

from unittest.mock import MagicMock

import pytest

from atomic_wm.cli import campaign as cli
from atomic_wm.client import ClientError


# ---------------------------------------------------------------------------
@pytest.fixture
def client(monkeypatch):
    """A mocked ``Client``; the CLI never opens a socket in these tests."""

    mock = MagicMock()
    monkeypatch.setattr(cli, 'client_from_args', lambda args: mock)
    return mock


@pytest.fixture
def spec_file(tmp_path):
    path = tmp_path / 'wf.json'
    path.write_text(json.dumps({
        'name'  : 'demo',
        'params': {'temperature': 300},
        'stages': [{'name': 'md', 'cmd': ['atomic-fake-md', '--out',
                                          'md.json'],
                    'inputs': [], 'outputs': ['md.json']}]}))
    return str(path)


def _campaign(state='DONE'):
    return {'campaign_id': 'cmp-1234', 'name': 'demo', 'state': state,
            'reason': None, 'created_at': 1757100000.0,
            'started_at': 1757100000.0, 'finished_at': 1757100060.0,
            'workflows': [
                {'id': 'wf-000', 'params': {'temperature': 300},
                 'state': state,
                 'stages': [{'name': 'md', 'state': state,
                             'resource': 'res-a', 'member': 'cpu',
                             'task_id': 't1'},
                            {'name': 'train', 'state': state,
                             'resource': 'res-b', 'member': None,
                             'task_id': 't2'}]}]}


def _results():
    return {'campaign_id': 'cmp-1234', 'state': 'DONE', 'workflows': [
        {'id': 'wf-000', 'params': {'temperature': 300}, 'state': 'DONE',
         'metrics': {'train': {'type': 'ml_training',
                               'summary': {'final_accuracy': 0.9912}}},
         'files': {'train': [{'name': 'model.json', 'size': 12,
                              'json': True}]}}]}


# ---------------------------------------------------------------------------
class TestSweepParsing:

    def test_numbers_stay_numbers(self):
        assert cli.parse_sweep(['temperature=300,600,900']) == \
               {'temperature': [300, 600, 900]}

    def test_strings_and_floats(self):
        assert cli.parse_sweep(['x=a,b', 'y=1.5']) == \
               {'x': ['a', 'b'], 'y': [1.5]}

    def test_repeated_flags_build_a_cartesian_sweep(self):
        assert cli.parse_sweep(['t=1,2', 's=10']) == {'t': [1, 2], 's': [10]}

    def test_missing_equals_is_an_error(self):
        with pytest.raises(ValueError):
            cli.parse_sweep(['temperature'])

    def test_empty_values_are_an_error(self):
        with pytest.raises(ValueError):
            cli.parse_sweep(['temperature='])

    def test_none_is_an_empty_sweep(self):
        assert cli.parse_sweep(None) == {}


# ---------------------------------------------------------------------------
class TestSpecLoading:

    def test_bare_workflow_spec(self, spec_file):
        spec = cli.load_spec(spec_file)
        assert spec['workflow']['name'] == 'demo'
        assert spec['sweep'] == {}

    def test_full_campaign_request(self, tmp_path):
        path = tmp_path / 'req.json'
        path.write_text(json.dumps({'workflow': {'name': 'demo',
                                                 'stages': []},
                                    'sweep': {'temperature': [300]}}))
        spec = cli.load_spec(str(path))
        assert spec['sweep'] == {'temperature': [300]}

    def test_non_object_is_an_error(self, tmp_path):
        path = tmp_path / 'bad.json'
        path.write_text('[1, 2]')
        with pytest.raises(ValueError):
            cli.load_spec(str(path))


# ---------------------------------------------------------------------------
class TestSubmit:

    def test_submit_passes_workflow_and_sweep(self, client, spec_file,
                                              capsys):
        client.campaign_submit.return_value = {'campaign_id': 'cmp-1234',
                                               'state': 'RUNNING',
                                               'workflows': [{}, {}, {}]}
        rc = cli.main(['--broker', 'https://x', 'submit', spec_file,
                       '--sweep', 'temperature=300,600,900'])
        assert rc == 0
        workflow, sweep = client.campaign_submit.call_args[0]
        assert workflow['name'] == 'demo'
        assert sweep == {'temperature': [300, 600, 900]}
        out = capsys.readouterr().out
        assert 'cmp-1234' in out and '3 workflow' in out

    def test_sweep_from_the_file_is_used_when_no_flag(self, client, tmp_path):
        path = tmp_path / 'req.json'
        path.write_text(json.dumps({
            'workflow': {'name': 'demo', 'stages': []},
            'sweep'   : {'temperature': [300, 600]}}))
        client.campaign_submit.return_value = {'campaign_id': 'c',
                                               'workflows': []}
        assert cli.main(['--broker', 'https://x', 'submit', str(path)]) == 0
        assert client.campaign_submit.call_args[0][1] == \
               {'temperature': [300, 600]}

    def test_wait_polls_until_terminal_and_exits_zero(self, client, spec_file,
                                                      monkeypatch, capsys):
        monkeypatch.setattr(cli.time, 'sleep', lambda _s: None)
        client.campaign_submit.return_value = {'campaign_id': 'cmp-1234',
                                               'workflows': [{}]}
        client.campaign.side_effect = [_campaign('RUNNING'),
                                       _campaign('DONE')]
        rc = cli.main(['--broker', 'https://x', 'submit', spec_file,
                       '--sweep', 'temperature=300', '--wait'])
        assert rc == 0
        assert client.campaign.call_count == 2
        assert 'wf-000' in capsys.readouterr().out

    def test_wait_exits_nonzero_on_failure(self, client, spec_file,
                                           monkeypatch):
        monkeypatch.setattr(cli.time, 'sleep', lambda _s: None)
        client.campaign_submit.return_value = {'campaign_id': 'cmp-1234',
                                               'workflows': [{}]}
        client.campaign.return_value = _campaign('FAILED')
        rc = cli.main(['--broker', 'https://x', 'submit', spec_file,
                       '--sweep', 'temperature=300', '--wait'])
        assert rc == 1

    def test_wait_gives_up_after_the_timeout(self, client, spec_file,
                                             monkeypatch, capsys):
        ticks = iter([0.0] + [float(i) for i in range(1, 100)])
        monkeypatch.setattr(cli.time, 'sleep', lambda _s: None)
        monkeypatch.setattr(cli.time, 'time', lambda: next(ticks))
        client.campaign_submit.return_value = {'campaign_id': 'cmp-1234',
                                               'workflows': [{}]}
        client.campaign.return_value = _campaign('RUNNING')
        rc = cli.main(['--broker', 'https://x', 'submit', spec_file,
                       '--sweep', 'temperature=300', '--wait',
                       '--timeout', '5'])
        assert rc == 3
        assert 'still RUNNING' in capsys.readouterr().err

    def test_interrupt_exits_130(self, client, spec_file, capsys):
        client.campaign_submit.side_effect = KeyboardInterrupt
        rc = cli.main(['--broker', 'https://x', 'submit', spec_file])
        assert rc == 130
        assert 'interrupted' in capsys.readouterr().err

    def test_bad_sweep_is_an_error(self, client, spec_file, capsys):
        rc = cli.main(['--broker', 'https://x', 'submit', spec_file,
                       '--sweep', 'nonsense'])
        assert rc == 1
        assert 'error' in capsys.readouterr().err

    def test_missing_spec_file_is_an_error(self, client, capsys):
        rc = cli.main(['--broker', 'https://x', 'submit', '/no/such.json'])
        assert rc == 1
        assert 'error' in capsys.readouterr().err

    def test_server_error_is_reported(self, client, spec_file, capsys):
        client.campaign_submit.side_effect = ClientError(503, 'no federation')
        rc = cli.main(['--broker', 'https://x', 'submit', spec_file])
        assert rc == 1
        assert 'no federation' in capsys.readouterr().err


# ---------------------------------------------------------------------------
class TestStatus:

    def test_status_prints_a_compact_table(self, client, capsys):
        client.campaign.return_value = _campaign('DONE')
        assert cli.main(['--broker', 'https://x', 'status', 'cmp-1234']) == 0
        out = capsys.readouterr().out
        assert 'WORKFLOW' in out and 'PARAMS' in out and 'STAGES' in out
        assert 'temperature=300' in out
        # the placement chip names the member once the class pool bound
        # one, and just the resource before that
        assert 'md:DONE@res-a/cpu' in out
        assert 'train:DONE@res-b' in out

    def test_an_unplaced_stage_shows_no_resource(self, client, capsys):
        # a task queued in a capability class pool has no member yet, and
        # may have no resource either -- that is not an error
        camp = _campaign('RUNNING')
        camp['workflows'][0]['stages'][0].update(resource=None, member=None)
        client.campaign.return_value = camp

        cli.main(['--broker', 'https://x', 'status', 'cmp-1234'])
        out = capsys.readouterr().out
        assert 'md:RUNNING' in out and 'md:RUNNING@' not in out

    def test_status_json(self, client, capsys):
        client.campaign.return_value = _campaign('DONE')
        assert cli.main(['--broker', 'https://x', 'status', 'cmp-1234',
                         '--json']) == 0
        assert json.loads(capsys.readouterr().out)['campaign_id'] == 'cmp-1234'

    def test_failed_campaign_exits_nonzero(self, client):
        client.campaign.return_value = _campaign('FAILED')
        assert cli.main(['--broker', 'https://x', 'status', 'cmp-1234']) == 1

    def test_unknown_campaign_is_reported(self, client, capsys):
        client.campaign.side_effect = ClientError(404, 'unknown campaign')
        assert cli.main(['--broker', 'https://x', 'status', 'nope']) == 1
        assert 'unknown campaign' in capsys.readouterr().err


# ---------------------------------------------------------------------------
class TestResults:

    def test_results_table_shows_the_summary_scalars(self, client, capsys):
        client.campaign_results.return_value = _results()
        assert cli.main(['--broker', 'https://x', 'results', 'cmp-1234']) == 0
        out = capsys.readouterr().out
        assert 'final_accuracy=0.9912' in out
        assert 'train' in out

    def test_results_json_is_verbatim(self, client, capsys):
        client.campaign_results.return_value = _results()
        assert cli.main(['--broker', 'https://x', 'results', 'cmp-1234',
                         '--json']) == 0
        assert json.loads(capsys.readouterr().out) == _results()

    def test_no_results_yet(self, client, capsys):
        client.campaign_results.return_value = {'workflows': []}
        assert cli.main(['--broker', 'https://x', 'results', 'c']) == 0
        assert 'no results' in capsys.readouterr().out


# ---------------------------------------------------------------------------
class TestListAndCancel:

    def test_list_table(self, client, capsys):
        client.campaigns.return_value = [
            {'campaign_id': 'cmp-1234', 'name': 'demo', 'state': 'DONE',
             'n_workflows': 3, 'created_at': 1757100000.0}]
        assert cli.main(['--broker', 'https://x', 'list']) == 0
        out = capsys.readouterr().out
        assert 'cmp-1234' in out and 'CAMPAIGN' in out

    def test_list_accepts_the_wrapped_shape(self, client, capsys):
        client.campaigns.return_value = {'campaigns': [
            {'campaign_id': 'cmp-1', 'state': 'RUNNING', 'n_workflows': 1}]}
        assert cli.main(['--broker', 'https://x', 'list']) == 0
        assert 'cmp-1' in capsys.readouterr().out

    def test_empty_list(self, client, capsys):
        client.campaigns.return_value = []
        assert cli.main(['--broker', 'https://x', 'list']) == 0
        assert 'no campaigns' in capsys.readouterr().out

    def test_cancel(self, client, capsys):
        client.campaign_cancel.return_value = {'campaign_id': 'cmp-1234',
                                               'canceled': True}
        assert cli.main(['--broker', 'https://x', 'cancel', 'cmp-1234']) == 0
        assert 'cancel requested' in capsys.readouterr().out

    def test_cancel_of_a_finished_campaign(self, client, capsys):
        client.campaign_cancel.return_value = {'canceled': False,
                                               'reason': 'already terminal'}
        assert cli.main(['--broker', 'https://x', 'cancel', 'cmp-1234']) == 0
        assert 'already terminal' in capsys.readouterr().out


# ---------------------------------------------------------------------------
class TestUsage:

    def test_no_command_prints_help(self, capsys):
        assert cli.main([]) == 2
        assert 'usage' in capsys.readouterr().out

    def test_help_mentions_every_subcommand(self, capsys):
        with pytest.raises(SystemExit):
            cli.main(['--help'])
        out = capsys.readouterr().out
        for word in ('submit', 'status', 'results', 'list', 'cancel'):
            assert word in out

    def test_connection_args_are_present(self):
        parser = cli.build_parser()
        opts = {a.dest for a in parser._actions}
        assert {'broker', 'token', 'cert'} <= opts

    def test_missing_broker_is_reported(self, monkeypatch, capsys):
        monkeypatch.delenv('RADICAL_ORBIT_BROKER_URL', raising=False)
        assert cli.main(['list']) == 1
        assert 'broker URL required' in capsys.readouterr().err
