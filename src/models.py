from typing import List, Optional, Literal
from pydantic import BaseModel, Field
from datetime import datetime, timezone


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
