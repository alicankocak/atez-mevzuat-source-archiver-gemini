import re
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class SourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal[1]
    request_id: str = Field(
        min_length=7,
        max_length=128,
        pattern=(
            r"^(?:req-[A-Za-z0-9][A-Za-z0-9._-]{2,63}|"
            r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})$"
        ),
    )
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

    @field_validator("requested_at", mode="before")
    @classmethod
    def require_rfc3339_requested_at(cls, value):
        if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?"
            r"(?:Z|[+-]\d{2}:\d{2})",
            value,
        ):
            raise ValueError("requested_at must be an RFC3339 UTC string")
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


class ReadyFile(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    relative_path: str = Field(min_length=1)
    drive_file_id: str = Field(min_length=1)
    size_bytes: int = Field(ge=0, strict=True)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("relative_path")
    @classmethod
    def require_safe_relative_path(cls, value: str) -> str:
        path = value.replace("\\", "/")
        if path.startswith("/") or any(part in {"", ".", ".."} for part in path.split("/")):
            raise ValueError("relative_path must be a normalized relative path")
        return path


class ReadyGate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: int = 1
    status: Literal["READY", "FAILED"] = "READY"
    report_date: str # YYYY-MM-DD
    resmi_gazete_sayisi: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    total_files_count: int = Field(ge=0, strict=True)
    verified: bool = True
    files: List[ReadyFile]

    @model_validator(mode="after")
    def require_complete_inventory(self):
        if self.total_files_count != len(self.files):
            raise ValueError("total_files_count must equal len(files)")
        if self.status == "READY" and not self.files:
            raise ValueError("READY gate files must not be empty")
        relative_paths = [item.relative_path for item in self.files]
        if len(relative_paths) != len(set(relative_paths)):
            raise ValueError("READY gate file paths must be unique")
        return self
