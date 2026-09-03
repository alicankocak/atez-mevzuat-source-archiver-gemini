import re
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator


class SourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal[1]
    request_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    report_date: date
    requested_at: AwareDatetime
    requested_by: Literal["atez-mevzuar-rapor-alcn"]

    @field_validator("report_date", mode="before")
    @classmethod
    def require_iso_report_date(cls, value):
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("report_date must use YYYY-MM-DD")
        return date.fromisoformat(value)

    @field_validator("requested_at")
    @classmethod
    def require_utc_requested_at(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("requested_at must be timezone-aware UTC")
        return value


class SourceRequestResult(SourceRequest):
    status: Literal["DONE", "FAILED"]
    completed_at: AwareDatetime
    rg_number: Optional[str] = None
    ready_file_id: Optional[str] = None
    error: Optional[str] = None


class FileManifest(BaseModel):
    source_url: str
    final_url: str
    http_status: int
    content_type: str
    size_bytes: int
    sha256: str
    downloaded_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    role: Literal["daily_index", "main_document", "attachment"]
    parent_document_id: Optional[str] = None
    drive_file_id: Optional[str] = None
    drive_web_view_link: Optional[str] = None
    local_relative_path: Optional[str] = None


class DocumentItem(BaseModel):
    document_id: str
    title: str
    source_url: str
    decision: str = "unclassified" # Default as per doc section 6
    main_document: Optional[FileManifest] = None
    attachments: List[FileManifest] = Field(default_factory=list)


class SourceManifest(BaseModel):
    schema_version: int = 1
    report_date: str # YYYY-MM-DD
    resmi_gazete_sayisi: Optional[str] = None
    fihrist_url: str
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    index_file: Optional[FileManifest] = None
    documents: List[DocumentItem] = Field(default_factory=list)


class ReadyGate(BaseModel):
    schema_version: int = 1
    status: Literal["READY", "FAILED"] = "READY"
    report_date: str # YYYY-MM-DD
    resmi_gazete_sayisi: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    total_files_count: int
    verified: bool = True
