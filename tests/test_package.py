
"""Skeleton tests: entry points resolve, placeholders behave."""

import importlib

import pytest

import atomic_wm

from atomic_wm.cli import campaign, join, leave, resources


# ---------------------------------------------------------------------------
def test_version():

    assert atomic_wm.__version__


# ---------------------------------------------------------------------------
def test_plugins_package_imports_without_orbit():

    # the `radical.orbit.plugins` entry point targets this package; it must
    # import cleanly even though the campaign plugin (piece 04) is absent
    # and even where radical.orbit is not installed at all
    mod = importlib.import_module('atomic_wm.plugins')

    assert mod is not None


# ---------------------------------------------------------------------------
@pytest.mark.parametrize('mod', [join, leave, resources, campaign])
def test_cli_modules_expose_a_working_parser(mod, capsys):

    # the placeholders these modules started out as are gone (P2/P4); what
    # stays contractual is that every console script has a `main` and a
    # parser whose --help works
    parser = mod.build_parser()

    assert parser.prog.startswith('atomic-')
    assert callable(mod.main)

    with pytest.raises(SystemExit) as exc:
        mod.main(['--help'])

    assert exc.value.code == 0
    assert parser.prog in capsys.readouterr().out
