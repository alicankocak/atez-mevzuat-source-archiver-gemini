from pathlib import Path

import yaml


WORKFLOWS_DIR = Path(__file__).parents[1] / ".github" / "workflows"


def load_workflow(name: str) -> dict:
    with (WORKFLOWS_DIR / name).open(encoding="utf-8") as workflow_file:
        return yaml.safe_load(workflow_file)


def test_ci_keeps_production_archiving_on_mac_and_loopback_tests_on_github_hosted():
    production_job = load_workflow("fetch-mevzuat.yml")["jobs"]["archive-sources"]
    test_job = load_workflow("tests.yml")["jobs"]["test"]

    assert production_job["runs-on"] == ["self-hosted", "macOS"]
    assert test_job["runs-on"] == "ubuntu-latest"
    assert "secrets." not in str(test_job)
