from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent


def pytest_collect_directory(path, parent):
    # The repository root is a py4web package. Collecting it imports that
    # package and fails before any test runs.
    if Path(path).resolve() == ROOT:
        return pytest.Dir.from_parent(parent, path=path)
    return None
