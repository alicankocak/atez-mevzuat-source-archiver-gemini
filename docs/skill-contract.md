# ATEZ Skill Compatibility Contract

This repository supplies immutable source archives to the installed `atez-mevzuar-rapor-alcn` skill. The shared Google Drive root is `ATEZ-Gemini-Mevzuat-Radari`, ID `1xrSozns-2sMBJRUuY3JvglVdSnKX1hDc`.

## Request interface

When `YYYY-MM-DD/sources/rg-*/_READY.json` has no valid file-complete gate, the skill checks the root `requests/` folder for the same date in these states:

- pending: `SOURCE_REQUEST__<date>__<uuid>.json`
- claimed: `PROCESSING_SOURCE_REQUEST__<date>__<uuid>.json`
- complete: `DONE_SOURCE_REQUEST__<date>__<uuid>.json`
- failed: `FAILED_SOURCE_REQUEST__<date>__<uuid>.json`

READY, pending, processing, or done suppresses a duplicate request. Otherwise the skill uploads exactly one `application/json` file named `SOURCE_REQUEST__<date>__<uuid>.json`. Its bytes are UTF-8 JSON with no fields beyond:

```json
{
  "schema_version": 1,
  "request_id": "<RFC-4122-uuid>",
  "report_date": "YYYY-MM-DD",
  "requested_at": "YYYY-MM-DDTHH:MM:SSZ",
  "requested_by": "atez-mevzuar-rapor-alcn"
}
```

The watcher parses the bytes rather than inferring values from the filename. It claims by renaming to `PROCESSING_` and replaces the same file with a `DONE_` or `FAILED_` result.

## Archive interface

A consumable archive is `YYYY-MM-DD/sources/rg-<number>/` with `_READY.json` uploaded last. The gate has `schema_version: 1`, `status: "READY"`, the matching `report_date` and `resmi_gazete_sayisi`, `verified: true`, and a non-empty `files[]` whose length equals `total_files_count`. Every item contains:

```json
{
  "relative_path": "doc-01/source.html",
  "drive_file_id": "<drive-id>",
  "size_bytes": 123,
  "sha256": "<64-lowercase-hex>"
}
```

The skill downloads every listed file by Drive ID and verifies its path under the same `rg-*` tree, byte length, and SHA-256 before analysis. Any mismatch is `SOURCE_INTEGRITY_ERROR`.

`source-manifest.json` contains `index_file` and `documents[]`. Each document supplies `main_document` plus `attachments[]`; these records carry the official URLs, content type, role, parent ID, `local_relative_path`, byte length, and SHA-256. Removing the leading `rg-<number>/` from `local_relative_path` yields the READY-relative path.

After integrity succeeds, HTML is parsed from raw bytes even when connector `content` is blank. PDFs use embedded text first and page OCR when needed; JPG/JPEG/PNG and text-bearing GIF attachments use OCR. Extraction-tool failure is `SOURCE_EXTRACTION_BLOCKED` and does not invalidate matching source bytes.
