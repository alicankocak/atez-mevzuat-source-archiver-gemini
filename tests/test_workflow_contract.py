import json
import re
from pathlib import Path

import yaml


WORKFLOWS_DIR = Path(__file__).parents[1] / ".github" / "workflows"
REPOSITORY_ROOT = Path(__file__).parents[1]


def load_workflow(name: str) -> dict:
    with (WORKFLOWS_DIR / name).open(encoding="utf-8") as workflow_file:
        return yaml.safe_load(workflow_file)


def workflow_triggers(workflow: dict) -> dict:
    # PyYAML 1.1 parses the unquoted GitHub Actions key `on` as True.
    return workflow.get("on", workflow.get(True, {}))


def test_ci_keeps_production_archiving_on_mac_and_loopback_tests_on_github_hosted():
    production_job = load_workflow("fetch-mevzuat.yml")["jobs"]["archive-sources"]
    test_job = load_workflow("tests.yml")["jobs"]["test"]

    assert production_job["runs-on"] == ["self-hosted", "macOS"]
    assert test_job["runs-on"] == "ubuntu-latest"
    assert "secrets." not in str(test_job)


def test_production_workflow_has_no_github_issue_request_path():
    workflow = load_workflow("fetch-mevzuat.yml")

    assert "issues" not in workflow_triggers(workflow)

    serialized_jobs = json.dumps(workflow["jobs"], ensure_ascii=False)
    assert "github.event.issue" not in serialized_jobs
    assert "github.rest.issues" not in serialized_jobs


def test_gem_prompt_documents_the_strict_drive_request_contract():
    prompt = (REPOSITORY_ROOT / "GEM_PROMPT.md").read_text(encoding="utf-8")

    assert "SOURCE_REQUEST__YYYY-MM-DD__<uuid>.json" in prompt
    assert "kök `requests/`" in prompt
    assert "PROCESSING_SOURCE_REQUEST__YYYY-MM-DD__<uuid>.json" in prompt
    assert "yinelenen talep" in prompt
    assert "`YYYY-MM-DD.json`" not in prompt

    request_blocks = [
        json.loads(block)
        for block in re.findall(r"```json\s*(\{.*?\})\s*```", prompt, re.DOTALL)
        if '"request_id"' in block
    ]
    assert request_blocks == [
        {
            "schema_version": 1,
            "request_id": "<RFC-4122-uuid>",
            "report_date": "YYYY-MM-DD",
            "requested_at": "YYYY-MM-DDTHH:MM:SSZ",
            "requested_by": "atez-mevzuar-rapor-alcn",
        }
    ]
