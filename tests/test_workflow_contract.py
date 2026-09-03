import json
import os
import re
import subprocess
from pathlib import Path

import pytest
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


def test_gem_prompt_is_one_balanced_copyable_block_with_nested_examples():
    prompt = (REPOSITORY_ROOT / "GEM_PROMPT.md").read_text(encoding="utf-8")

    outer_block = re.search(
        r"^(?P<fence>`{4,})markdown\n(?P<body>.*)\n(?P=fence)[ \t]*\n?\Z",
        prompt,
        re.DOTALL | re.MULTILINE,
    )
    assert outer_block is not None

    body = outer_block.group("body")
    assert outer_block.group("fence") not in body

    request_example = re.search(
        r"```json\s*\{.*?\}\s*```",
        body,
        re.DOTALL,
    )
    assert request_example is not None

    remaining_prompt = body[request_example.end() :]
    assert "Talep `PROCESSING_` öneki aldığında" in remaining_prompt
    assert "# Sohbette Kullanılacak Standart Yanıt Formatı" in remaining_prompt
    assert "Bu raporu 'test1' grubuna" in remaining_prompt


def test_gem_prompt_requires_complete_integrity_gate_and_raw_extraction_paths():
    prompt = (REPOSITORY_ROOT / "GEM_PROMPT.md").read_text(encoding="utf-8")
    source_contract = prompt.split("# Kaynak Okuma ve Otomatik Arşivleme", 1)[1]

    for required_contract in (
        '`schema_version: 1`',
        '`status: "READY"`',
        "hedef tarihle aynı `report_date`",
        "boş olmayan `resmi_gazete_sayisi`",
        "RFC 3339 `created_at`",
        "`verified: true`",
        "boş olmayan `files[]`",
        "`total_files_count` değeri `files[]` uzunluğuna eşit",
        "`drive_file_id`",
        "aynı `rg-*` ağacı",
        "ham bayt boyutu",
        "SHA-256",
        "`source-manifest.json`",
        "`documents[*].main_document`",
        "`documents[*].attachments[]`",
        "`content` alanı boş",
        "`file_uri`",
        "base64",
        "karakter kodlamasını belirle",
        "DOM",
        "tabloları",
        "PDF metin katmanını",
        "sayfa OCR",
        "GIF/JPG/JPEG/PNG",
        "`SOURCE_INTEGRITY_ERROR`",
        "`SOURCE_EXTRACTION_BLOCKED`",
    ):
        assert required_contract in source_contract

    assert source_contract.index("bütün `files[]`") < source_contract.index(
        "`source-manifest.json`"
    )


def _determine_date_step() -> dict:
    steps = load_workflow("fetch-mevzuat.yml")["jobs"]["archive-sources"]["steps"]
    return next(step for step in steps if step.get("id") == "determine_date")


def _run_determine_date_step(tmp_path: Path, report_date: str):
    github_env = tmp_path / "github-env"
    sentinel = tmp_path / "injected"
    environment = {
        **os.environ,
        "EVENT_NAME": "workflow_dispatch",
        "REPORT_DATE_INPUT": report_date.replace("{sentinel}", str(sentinel)),
        "GITHUB_ENV": str(github_env),
    }
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", _determine_date_step()["run"]],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, github_env, sentinel


def test_manual_date_expression_is_only_passed_through_step_environment():
    workflow = load_workflow("fetch-mevzuat.yml")
    date_step = _determine_date_step()
    run_scripts = "\n".join(
        step["run"]
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if "run" in step
    )

    assert "${{ github.event.inputs.report_date }}" not in run_scripts
    assert "${{ inputs.report_date }}" not in run_scripts
    assert date_step["env"]["REPORT_DATE_INPUT"] == (
        "${{ github.event.inputs.report_date }}"
    )


@pytest.mark.parametrize("report_date", ["2026-08-14", "14.08.2026"])
def test_manual_date_step_accepts_only_supported_calendar_dates(
    tmp_path, report_date
):
    result, github_env, _ = _run_determine_date_step(tmp_path, report_date)

    assert result.returncode == 0, result.stderr
    assert github_env.read_text(encoding="utf-8") == (
        "TARGET_DATE<<ATEZ_TARGET_DATE_EOF\n"
        f"{report_date}\n"
        "ATEZ_TARGET_DATE_EOF\n"
    )


@pytest.mark.parametrize(
    "report_date",
    [
        "2026-02-30",
        "2026-8-14",
        "14/08/2026",
        "2026-08-14\nSECOND_VARIABLE=owned",
        "$(touch {sentinel})",
    ],
)
def test_manual_date_step_rejects_invalid_or_injectable_values(
    tmp_path, report_date
):
    result, github_env, sentinel = _run_determine_date_step(tmp_path, report_date)

    assert result.returncode != 0
    assert not github_env.exists() or github_env.read_bytes() == b""
    assert not sentinel.exists()
