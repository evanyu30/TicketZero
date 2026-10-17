"""The frozen acceptance tests call Crusoe Managed Inference for real, so they are live tests."""
import pytest


def pytest_collection_modifyitems(items):
    for item in items:
        item.add_marker(pytest.mark.live)
