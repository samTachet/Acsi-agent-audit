import pytest

from acsi_agent.agent import Runtime


@pytest.fixture
def runtime(tmp_path):
    return Runtime(tmp_path / "journal")
