
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
def test_cli_placeholders_exit_2(mod, capsys):

    assert mod.main([]) == 2
    assert 'not implemented yet' in capsys.readouterr().err
