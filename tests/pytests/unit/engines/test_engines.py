import pytest

import salt.engines
from tests.support.mock import MagicMock, patch


@pytest.mark.parametrize("role", ["minion", "master"])
@pytest.mark.parametrize("config", [{}, {"engines": []}, {"engines": {}}])
def test_start_engines_without_configuration(role, config):
    """Starting without engines must not load modules or create processes."""
    opts = {"__role": role, **config}
    process_manager = MagicMock()
    with patch("salt.engines.salt.loader") as loader:
        salt.engines.start_engines(opts, process_manager)
    assert not loader.mock_calls
    process_manager.add_process.assert_not_called()


@pytest.fixture
def kwargs():
    opts = {"__role": "minion"}
    name = "foobar"
    fun = f"{name}.start"
    config = funcs = runners = proxy = {}
    return dict(
        opts=opts,
        name=name,
        fun=fun,
        config=config,
        funcs=funcs,
        runners=runners,
        proxy=proxy,
    )


def test_engine_module_name(kwargs):
    engine = salt.engines.Engine(**kwargs)
    assert engine.name == kwargs["name"]


def test_engine_title_set(kwargs):
    engine = salt.engines.Engine(**kwargs)
    with patch("salt.utils.process.appendproctitle", MagicMock()) as mm:
        engine.run()
    mm.assert_called_with(kwargs["name"])
