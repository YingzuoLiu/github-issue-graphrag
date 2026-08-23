from importlib.metadata import version

import issue_graphrag


def test_runtime_and_package_metadata_versions_match() -> None:
    assert issue_graphrag.__version__ == version("github-issue-graphrag")
