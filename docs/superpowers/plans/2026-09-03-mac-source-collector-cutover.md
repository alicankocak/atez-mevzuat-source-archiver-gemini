# Mac Source Collector Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route ATEZ report requests through the Mac Drive watcher, produce a fully verifiable Gemini source archive, teach the installed skill to consume it, and retire the Windows workflow after a live acceptance test.

**Architecture:** The skill uploads one strict request JSON to the new Drive root. The Mac watcher atomically claims it, runs the official-domain-only collector, uploads and verifies every archive byte, then publishes `_READY.json` last. The skill validates this gate and the Gemini `documents` manifest before HTML/PDF/OCR analysis.

**Tech Stack:** Python 3.13, pytest, Pydantic 2, Playwright Chromium, Requests, Google Drive API, GitHub Actions, Codex skills.

**Spec:** `docs/superpowers/specs/2026-09-03-mac-source-collector-cutover-design.md`

## Global Constraints

- Canonical Drive root ID is `1xrSozns-2sMBJRUuY3JvglVdSnKX1hDc`.
- Only `https://resmigazete.gov.tr` and `https://www.resmigazete.gov.tr` are valid source origins.
- `_READY.json` is uploaded only after every other archive file passes Drive size and SHA-256 verification.
- Chat-triggered source requests use Drive `requests/`; they do not create GitHub Issues.
- Existing Windows workflow remains active until the Mac acceptance test passes, then is disabled.
- Preserve the unrelated modified file in `atez-mevzuat-radari-fetcher/docs/superpowers/specs/2026-09-01-hybrid-source-archive-report-pipeline-design.md`.

---

### Task 1: Strict request contract and atomic watcher lifecycle

**Files:**
- Create: `tests/test_drive_watcher.py`
- Modify: `src/models.py`
- Modify: `src/drive_watcher.py`
- Modify: `requirements.txt`

**Interfaces:**
- Produces: `SourceRequest.model_validate_json(bytes) -> SourceRequest`
- Produces: `DriveRequestWatcher.claim_request(file_id, old_name) -> str`
- Produces: `DriveRequestWatcher.complete_request(file_id, request, result) -> None`
- Produces: `DriveRequestWatcher.fail_request(file_id, request, error) -> None`

- [ ] **Step 1: Add failing request-validation tests**

```python
def test_request_requires_exact_schema_and_iso_date():
    request = SourceRequest.model_validate({
        "schema_version": 1,
        "request_id": "req-123",
        "report_date": "2026-08-14",
        "requested_at": "2026-09-03T00:00:00Z",
        "requested_by": "atez-mevzuar-rapor-alcn",
    })
    assert request.report_date.isoformat() == "2026-08-14"

def test_request_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        SourceRequest.model_validate({
            "schema_version": 1,
            "request_id": "req-123",
            "report_date": "2026-08-14",
            "requested_at": "2026-09-03T00:00:00Z",
            "requested_by": "atez-mevzuar-rapor-alcn",
            "target_url": "https://example.com",
        })
```

- [ ] **Step 2: Run the tests and confirm RED**

Run: `python -m pytest tests/test_drive_watcher.py -v`

Expected: collection fails because `SourceRequest` does not exist.

- [ ] **Step 3: Implement the request model and byte-based parsing**

Add `SourceRequest` and `SourceRequestResult` Pydantic models with `extra="forbid"`, `schema_version: Literal[1]`, UUID-like non-empty `request_id`, strict `date`, timezone-aware `requested_at`, and fixed `requested_by`. Remove filename and free-text date inference from the watcher.

- [ ] **Step 4: Add failing lifecycle tests**

Cover these observable behaviors with a fake Drive service:

```python
def test_claim_renames_before_fetch(watcher, pending_file):
    claimed_name = watcher.claim_request(pending_file["id"], pending_file["name"])
    assert claimed_name.startswith("PROCESSING_")

def test_success_replaces_json_and_renames_done(watcher, request):
    watcher.complete_request("file-1", request, rg_number="33299", ready_file_id="ready-1")
    assert watcher.last_update["name"].startswith("DONE_")
    assert json.loads(watcher.last_media)["status"] == "DONE"

def test_failure_records_error_and_does_not_reenter_pending(watcher, request):
    watcher.fail_request("file-1", request, "network unavailable")
    assert watcher.last_update["name"].startswith("FAILED_")
    assert json.loads(watcher.last_media)["status"] == "FAILED"
```

- [ ] **Step 5: Run lifecycle tests and confirm RED**

Run: `python -m pytest tests/test_drive_watcher.py -v`

Expected: failures show the watcher still uses `processed_` and does not write result JSON.

- [ ] **Step 6: Implement claim, completion, failure, and date-level deduplication**

Use Drive metadata rename as the claim boundary. Before collecting, query both `PROCESSING_*<date>*` files and a valid date READY gate. A second request must reuse the finished result or remain pending; it must not start another collector.

- [ ] **Step 7: Run focused and full tests**

Run: `python -m pytest tests/test_drive_watcher.py -v`

Run: `python -m pytest -q`

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add requirements.txt src/models.py src/drive_watcher.py tests/test_drive_watcher.py
git commit -m "feat: enforce Drive source request lifecycle"
```

### Task 2: Official-origin browser transport and response validation

**Files:**
- Create: `src/browser_transport.py`
- Create: `tests/test_browser_transport.py`
- Modify: `src/fetcher.py`

**Interfaces:**
- Produces: `OfficialBrowserTransport.fetch(url: str) -> BrowserResponse`
- Produces: `validate_official_url(url: str) -> None`
- Consumes: `BrowserResponse.status`, `.final_url`, `.content_type`, `.body`

- [ ] **Step 1: Add failing allowlist and redirect tests**

```python
@pytest.mark.parametrize("url", [
    "http://resmigazete.gov.tr/14.08.2026",
    "https://example.com/file.pdf",
    "https://resmigazete.gov.tr.evil.test/file.pdf",
])
def test_rejects_non_official_urls(url):
    with pytest.raises(UnsafeSourceUrl):
        validate_official_url(url)

def test_rejects_redirect_leaving_official_hosts(local_server):
    with pytest.raises(UnsafeSourceUrl):
        OfficialBrowserTransport(test_origins=local_server.origins).fetch(local_server.escape_url)
```

- [ ] **Step 2: Run and confirm RED**

Run: `python -m pytest tests/test_browser_transport.py -v`

Expected: import fails because the transport module does not exist.

- [ ] **Step 3: Implement transport with constrained TLS compatibility**

Create a Chromium context with service workers blocked. Intercept all requests and allow only the official host set. Validate each redirect and final URL. Use page navigation for HTML and browser-context byte fetching for PDF/images so headless Chromium PDF navigation is not required. Catch Playwright timeout and navigation errors as retryable transport failures. Keep Requests/Drive TLS verification enabled globally.

- [ ] **Step 4: Add failing byte/content validation tests**

Test rejection of empty bodies, HTML error pages returned for PDF URLs, non-2xx responses, mismatched PDF/image signatures, and oversized responses. Include a loopback integration test that returns one HTML document, PDF, and image attachment.

- [ ] **Step 5: Run and confirm RED**

Run: `RUN_PLAYWRIGHT_INTEGRATION=1 python -m pytest tests/test_browser_transport.py -v`

Expected: content/signature cases fail until validation is connected.

- [ ] **Step 6: Route all fihrist, document, and attachment downloads through the transport**

Remove `self.session.verify = False` and `urllib3.disable_warnings`. Preserve raw response bytes and content type in `FileManifest`. Ensure attachments outside the two official hosts never reach the network.

- [ ] **Step 7: Run focused and full tests**

Run: `RUN_PLAYWRIGHT_INTEGRATION=1 python -m pytest tests/test_browser_transport.py -v`

Run: `python -m pytest -q`

Expected: all pass; no TLS-warning suppression remains.

- [ ] **Step 8: Commit**

```bash
git add src/browser_transport.py src/fetcher.py tests/test_browser_transport.py
git commit -m "feat: harden official source browser downloads"
```

### Task 3: File-complete READY gate

**Files:**
- Create: `tests/test_drive_uploader.py`
- Modify: `src/models.py`
- Modify: `src/drive_uploader.py`

**Interfaces:**
- Produces: `ReadyFile(relative_path, drive_file_id, size_bytes, sha256)`
- Produces: `ReadyGate.files: list[ReadyFile]`
- Produces: `DriveUploader.upload_rg_source_tree(local_rg_dir: Path, sources_folder_id: str) -> tuple[ReadyGate, str]`

- [ ] **Step 1: Add failing READY ordering and file-list tests**

```python
def test_ready_contains_every_verified_file(uploader, archive_tree):
    gate = uploader.upload_rg_source_tree(archive_tree.rg_dir, "sources-id")
    assert {item.relative_path for item in gate.files} == archive_tree.relative_paths
    assert all(item.drive_file_id for item in gate.files)

def test_ready_is_last_upload(uploader, archive_tree):
    uploader.upload_rg_source_tree(archive_tree.rg_dir, "sources-id")
    assert uploader.upload_names[-1] == "_READY.json"
```

- [ ] **Step 2: Run and confirm RED**

Run: `python -m pytest tests/test_drive_uploader.py -v`

Expected: `ReadyGate` has no `files` field and uploader returns `bool`.

- [ ] **Step 3: Implement complete recursive upload records**

Upload recursively at arbitrary depth, retain each relative path and Drive ID, verify downloaded bytes, then serialize and upload the gate last. Exclude stale local `_READY.json` before collection. Return the created gate and READY Drive ID to the watcher.

- [ ] **Step 4: Add failing partial-upload test**

```python
def test_verification_failure_never_uploads_ready(uploader, archive_tree):
    uploader.fail_verification_for = "doc-01/source.pdf"
    with pytest.raises(DriveVerificationError):
        uploader.upload_rg_source_tree(archive_tree.rg_dir, "sources-id")
    assert "_READY.json" not in uploader.upload_names
```

- [ ] **Step 5: Run and confirm RED, then implement minimal failure behavior**

Run: `python -m pytest tests/test_drive_uploader.py -v`

Expected before implementation: READY is uploaded despite incomplete record handling. Expected after implementation: all tests pass.

- [ ] **Step 6: Run the full suite and commit**

Run: `python -m pytest -q`

```bash
git add src/models.py src/drive_uploader.py tests/test_drive_uploader.py
git commit -m "feat: publish file-complete READY gates"
```

### Task 4: Mac workflow regression coverage

**Files:**
- Create: `.github/workflows/tests.yml`
- Modify: `.github/workflows/fetch-mevzuat.yml`
- Modify: `requirements.txt`

**Interfaces:**
- Consumes: test suite from Tasks 1–3.
- Produces: GitHub-hosted unit/loopback CI and Mac-only production archive job.

- [ ] **Step 1: Add a workflow contract test**

Create `tests/test_workflow_contract.py` that loads YAML and asserts the production job uses `[self-hosted, macOS]`, the test job uses GitHub-hosted Ubuntu, and no production secret exists in the loopback test job.

- [ ] **Step 2: Run and confirm RED**

Run: `python -m pytest tests/test_workflow_contract.py -v`

Expected: test workflow is missing.

- [ ] **Step 3: Add deterministic CI**

Create one GitHub-hosted test workflow that installs Python, dependencies and Chromium, then runs ordinary tests plus only local-loopback Playwright integration tests. Keep Resmî Gazete and Drive calls out of CI. Pin action majors consistently.

- [ ] **Step 4: Run local suite and validate YAML**

Run: `python -m pytest -q`

Run: `python -c "import yaml, pathlib; [yaml.safe_load(p.read_text()) for p in pathlib.Path('.github/workflows').glob('*.yml')]"`

Expected: all tests and YAML parsing pass.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows requirements.txt tests/test_workflow_contract.py
git commit -m "ci: verify Mac source collector contracts"
```

### Task 5: Update and validate the installed ATEZ skill

**Files:**
- Modify: `/Users/alican/.codex/skills/atez-mevzuar-rapor-alcn/SKILL.md`
- Modify: `/Users/alican/.codex/skills/atez-mevzuar-rapor-alcn/references/runtime-config.md`
- Modify: `/Users/alican/.codex/skills/atez-mevzuar-rapor-alcn/references/sources.md`
- Create: `/Users/alican/Documents/Yasal Süreçler/skill-packages/atez-mevzuar-rapor-alcn-v13.zip`

**Interfaces:**
- Consumes: Drive root ID, request schema, Gemini `documents` manifest, file-complete READY gate.
- Produces: user-visible behavior `rapor oluştur` → Drive request → verified source analysis → HTML/PDF report.

- [ ] **Step 1: Run a RED baseline skill evaluation**

Give an independent evaluator the current v12 skill plus this scenario: “14 Ağustos 2026 raporunu oluştur; new Gemini Drive root has no READY archive.” Record whether it incorrectly creates an Issue in the Windows repo or fails to create a strict Drive request.

- [ ] **Step 2: Make the minimal skill changes**

Replace the old root/repository contract with the new root and root-level `requests/` folder. Define exact request filename/content, deduplication checks, polling states, `documents` traversal, READY `files[]` verification, raw HTML handling, PDF text/OCR fallback, and image OCR. Remove all instructions to create GitHub Issues or use the Windows collector.

- [ ] **Step 3: Validate structure**

Run: `python /Users/alican/.codex/skills/.system/skill-creator/scripts/quick_validate.py /Users/alican/.codex/skills/atez-mevzuar-rapor-alcn`

Expected: validation succeeds.

- [ ] **Step 4: Run GREEN independent evaluations**

Verify at least these scenarios:

- Missing source creates exactly one strict Drive request and waits.
- Existing `DONE_` or valid READY does not create another request.
- `documents` HTML with blank connector `content` uses raw bytes.
- Scanned PDF/image invokes OCR rather than declaring the archive unreadable.
- Invalid hash stops with `SOURCE_INTEGRITY_ERROR`.

- [ ] **Step 5: Package and inspect v13**

Create a deterministic ZIP containing only the skill directory, then list the archive and confirm `SKILL.md`, all references, assets, and `agents/openai.yaml` are present with no cache files.

- [ ] **Step 6: Commit the repository-side compatibility documentation**

Add a short `docs/skill-contract.md` in the Gemini repo containing the request and archive interface, then commit it with any generated schema examples.

### Task 6: Live acceptance, push, and Windows retirement

**Files:**
- Modify external state: Gemini GitHub repository `main`
- Modify external state: new Drive root `requests/` and `2026-08-14/sources/`
- Modify external state: old GitHub workflow state
- No change: old repository's unrelated dirty design document

**Interfaces:**
- Consumes: all previous tasks.
- Produces: one successful live source archive and disabled Windows workflow.

- [ ] **Step 1: Review and verify before external writes**

Run: `python -m pytest -q`

Run: `git status --short`

Run: skill quick validation and ZIP listing again.

Expected: tests pass; only intended commits/files exist.

- [ ] **Step 2: Push Gemini `main`**

Run: `git push origin main`

Verify the remote SHA equals local HEAD and the GitHub-hosted test workflow succeeds.

- [ ] **Step 3: Create one live acceptance request**

Immediately before this external Drive write, obtain user confirmation. Upload `SOURCE_REQUEST__2026-08-14__<uuid>.json` to the new root `requests/` folder and monitor the existing Mac watcher.

- [ ] **Step 4: Verify acceptance output**

Confirm the request becomes `PROCESSING_` then `DONE_`; locate `2026-08-14/sources/rg-*/_READY.json`; verify every `files[]` entry exists and matches raw size/SHA; open at least one archived HTML/PDF/image through the same raw-read path used by the skill.

- [ ] **Step 5: Disable the old Windows workflow**

Disable `.github/workflows/archive-resmi-gazete-sources.yml` in `alicankocak/atez-mevzuat-radari-fetcher` through GitHub Actions configuration. Verify it reports disabled and cannot accept new Issue-triggered jobs.

- [ ] **Step 6: Stop and unregister the Windows service**

Provide these commands for an Administrator PowerShell on the office PC after GitHub disablement:

```powershell
cd C:\Windows\System32\actions-runner
Get-Service "actions.runner.alicankocak-atez-mevzuat-radari-fetcher*" | Stop-Service
.\config.cmd remove
```

If `config.cmd remove` requests a removal token, generate one from the old repository’s Settings → Actions → Runners page at execution time. Do not remove the Mac runner.

- [ ] **Step 7: Final handoff**

Report the Gemini commit SHA, passing CI/live-run URLs, new Drive READY link, v13 skill ZIP path, disabled Windows workflow state, and the one remaining manual Windows service command if it could not be executed remotely.
