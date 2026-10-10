"""Expose the FastAPI application and PDF processing endpoints."""

import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterator
from uuid import uuid4

from azure.core.exceptions import AzureError
from azure.data.tables import TableServiceClient
from azure.storage.blob import BlobServiceClient
from fastapi import FastAPI, File, HTTPException, Request, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import Settings
from app.services.blob_service import BlobService
from app.services.table_service import PARTITION_KEY, TableService

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MAX_UPLOAD_SIZE = 10 * 1024 * 1024
PDF_CONTENT_TYPE = "application/pdf"
STATIC_DIRECTORY = Path(__file__).parent / "static"


class Status(str, Enum):
    """Define supported PDF processing lifecycle values."""

    UPLOADED = "Uploaded"
    PROCESSING = "Processing"
    COMPLETED = "Completed"
    FAILED = "Failed"


class UploadResponse(BaseModel):
    """Describe a successful PDF upload response."""

    file_id: str = Field(
        description="Identify the uploaded PDF and its processing record.",
    )


class FileRecord(BaseModel):
    """Describe a PDF status entity returned by the API."""

    partition_key: str = Field(
        alias="PartitionKey",
        description="Group PDF status records in Table Storage.",
    )
    row_key: str = Field(
        alias="RowKey",
        description="Identify the PDF with the generated file ID.",
    )
    filename: str = Field(
        description="Preserve the sanitized original PDF filename.",
    )
    file_type: str = Field(
        description="Record the validated PDF media type.",
    )
    uploaded_at: str = Field(
        description="Record the upload time as a UTC ISO 8601 value.",
    )
    status: Status = Field(
        description="Report the current PDF processing lifecycle state.",
    )
    blob_name: str = Field(
        description="Locate the original PDF in the uploads container.",
    )
    text_blob_name: str | None = Field(
        default=None,
        description="Locate extracted text after processing completes.",
    )
    error: str | None = Field(
        default=None,
        description="Explain a processing failure without exposing credentials.",
    )

    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class FileDetail(FileRecord):
    """Describe a PDF status entity with optional extracted text."""

    extracted_text: str | None = Field(
        description="Contain extracted UTF-8 text only for completed files.",
    )


class HealthResponse(BaseModel):
    """Describe the application process health response."""

    status: str = Field(
        description="Report whether the FastAPI process is responding.",
    )


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Create and reuse Azure Storage clients for the application lifetime.

    Args:
        application: FastAPI instance receiving initialized service wrappers.

    Yields:
        Control to FastAPI while the shared clients remain available.

    Raises:
        RuntimeError: If the storage connection string is missing or invalid.
        AzureError: If required containers or the status table cannot be created.
    """
    try:
        settings = Settings()
    except ValidationError as error:
        logger.critical("AZURE_STORAGE_CONNECTION_STRING is required at startup")
        raise RuntimeError(
            "AZURE_STORAGE_CONNECTION_STRING is required and must not be empty"
        ) from error

    try:
        blob_client = BlobServiceClient.from_connection_string(
            settings.azure_storage_connection_string
        )
        table_client = TableServiceClient.from_connection_string(
            settings.azure_storage_connection_string
        )
    except (TypeError, ValueError) as error:
        logger.critical("AZURE_STORAGE_CONNECTION_STRING is invalid")
        raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING is invalid") from None

    blob_service = BlobService(
        blob_client,
        settings.upload_container,
        settings.extracted_container,
    )
    table_service = TableService(table_client, settings.table_name)

    try:
        await run_in_threadpool(blob_service.ensure_containers)
        await run_in_threadpool(table_service.ensure_table)
        application.state.blob_service = blob_service
        application.state.table_service = table_service
        yield
    finally:
        blob_client.close()
        table_client.close()


app = FastAPI(
    title="Azure Storage PDF Processing POC",
    description="Upload PDFs, monitor extraction status, and download results.",
    lifespan=lifespan,
)


def get_blob_service(request: Request) -> BlobService:
    """Return the shared Blob Storage service for a request.

    Args:
        request: Current HTTP request containing application state.

    Returns:
        Application-scoped Blob Storage service.

    Raises:
        AttributeError: If application startup did not initialize the service.
    """
    return request.app.state.blob_service


def get_table_service(request: Request) -> TableService:
    """Return the shared Table Storage service for a request.

    Args:
        request: Current HTTP request containing application state.

    Returns:
        Application-scoped Table Storage service.

    Raises:
        AttributeError: If application startup did not initialize the service.
    """
    return request.app.state.table_service


def _storage_error(operation: str, error: AzureError) -> HTTPException:
    """Convert an Azure SDK failure into a safe HTTP response.

    Args:
        operation: Human-readable operation name used only in server logs.
        error: Azure SDK exception retained as the causal exception.

    Returns:
        An HTTP 503 exception that does not expose storage credentials.

    Raises:
        None.
    """
    logger.exception("Azure Storage operation failed: %s", operation)
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Storage service unavailable",
    )


def _safe_filename(filename: str) -> str:
    """Remove path components from an uploaded filename.

    Args:
        filename: Browser-provided upload filename.

    Returns:
        A basename safe to use beneath a generated blob prefix.

    Raises:
        HTTPException: If no usable basename remains after sanitization.
    """
    sanitized = PurePosixPath(filename.replace("\\", "/")).name
    if not sanitized or sanitized in {".", ".."}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A valid PDF filename is required",
        )
    return sanitized


@app.post(
    "/api/upload",
    summary="Upload a PDF",
    description=(
        "Validate a PDF up to 10 MB, store it in Blob Storage, and create an "
        "Uploaded status row. Returns 400 for invalid files and 503 for storage errors."
    ),
    response_model=UploadResponse,
    status_code=status.HTTP_200_OK,
    responses={
        400: {"description": "The uploaded file is not a valid supported PDF."},
        503: {"description": "Azure Storage is unavailable."},
    },
)
async def upload_pdf(
    request: Request,
    file: UploadFile = File(...),
) -> UploadResponse:
    """Upload a validated PDF and create its status record.

    Accept only `application/pdf` files with a `.pdf` extension and a maximum
    size of 10 MB. Return HTTP 400 for invalid input and HTTP 503 when Blob or
    Table Storage fails. Roll back the blob if creating the Table row fails.

    Args:
        request: Current request used to access shared storage services.
        file: Multipart PDF uploaded by the caller.

    Returns:
        Generated file identifier used by all subsequent endpoints.

    Raises:
        HTTPException: If validation fails or Azure Storage is unavailable.
    """
    filename = _safe_filename(file.filename or "")
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only .pdf files are accepted",
        )
    if file.content_type != PDF_CONTENT_TYPE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Content type must be application/pdf",
        )

    content = await file.read(MAX_UPLOAD_SIZE + 1)
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="PDF files must be 10 MB or smaller",
        )
    if not content.startswith(b"%PDF-"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File content is not a valid PDF",
        )

    file_id = str(uuid4())
    blob_name = f"{file_id}/{filename}"
    entity: dict[str, Any] = {
        "PartitionKey": PARTITION_KEY,
        "RowKey": file_id,
        "filename": filename,
        "file_type": PDF_CONTENT_TYPE,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "status": Status.UPLOADED.value,
        "blob_name": blob_name,
    }

    blob_service = get_blob_service(request)
    table_service = get_table_service(request)
    blob_uploaded = False
    try:
        await run_in_threadpool(blob_service.upload_pdf, blob_name, content)
        blob_uploaded = True
        await run_in_threadpool(table_service.insert_file, entity)
    except AzureError as error:
        if blob_uploaded:
            try:
                await run_in_threadpool(blob_service.delete_pdf, blob_name)
            except AzureError:
                logger.exception("Could not roll back uploaded blob %s", blob_name)
        raise _storage_error("upload PDF", error) from error

    return UploadResponse(file_id=file_id)


@app.get(
    "/api/files",
    summary="List uploaded PDFs",
    description=(
        "Return all PDF status rows with the newest upload first. Returns 503 "
        "when Table Storage is unavailable."
    ),
    response_model=list[FileRecord],
    responses={503: {"description": "Azure Table Storage is unavailable."}},
)
def list_files(request: Request) -> list[dict[str, Any]]:
    """List all PDF status records in descending upload order.

    Return HTTP 503 if Table Storage cannot be queried.

    Args:
        request: Current request used to access the shared table service.

    Returns:
        PDF entities ordered from newest to oldest.

    Raises:
        HTTPException: If Azure Table Storage is unavailable.
    """
    try:
        return get_table_service(request).list_files()
    except AzureError as error:
        raise _storage_error("list files", error) from error


@app.get(
    "/api/files/{file_id}",
    summary="Get PDF details",
    description=(
        "Return one status row and its extracted text when processing is complete. "
        "Returns 404 for an unknown ID and 503 for storage errors."
    ),
    response_model=FileDetail,
    responses={
        404: {"description": "No PDF exists for the supplied file ID."},
        503: {"description": "Azure Storage is unavailable."},
    },
)
def get_file(file_id: str, request: Request) -> dict[str, Any]:
    """Return one PDF record and its extracted text when available.

    Return HTTP 404 when the row does not exist and HTTP 503 when Blob or Table
    Storage cannot complete the request.

    Args:
        file_id: Generated row key identifying the uploaded PDF.
        request: Current request used to access shared storage services.

    Returns:
        PDF status entity with extracted text or a null extracted-text value.

    Raises:
        HTTPException: If the file is unknown or Azure Storage is unavailable.
    """
    try:
        table_service = get_table_service(request)
        blob_service = get_blob_service(request)
        entity = table_service.get_file(file_id)
        if entity is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="File not found",
            )

        extracted_text = None
        if entity.get("status") == Status.COMPLETED.value:
            text_blob_name = str(entity.get("text_blob_name") or f"{file_id}.txt")
            extracted_text = blob_service.read_extracted_text(text_blob_name)
        return {**entity, "extracted_text": extracted_text}
    except AzureError as error:
        raise _storage_error("get file", error) from error


@app.get(
    "/api/files/{file_id}/download",
    summary="Download the original PDF",
    description=(
        "Stream the original PDF as an attachment. Returns 404 for an unknown "
        "or invalid ID and 503 for storage errors."
    ),
    response_model=None,
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "Original PDF stream.",
            "content": {PDF_CONTENT_TYPE: {}},
        },
        404: {"description": "No PDF exists for the supplied file ID."},
        503: {"description": "Azure Storage is unavailable."},
    },
)
def download_file(file_id: str, request: Request) -> StreamingResponse:
    """Stream the original PDF with an attachment filename.

    Return HTTP 404 for an unknown or malformed identifier and HTTP 503 when
    Blob or Table Storage cannot begin the download.

    Args:
        file_id: Generated row key identifying the uploaded PDF.
        request: Current request used to access shared storage services.

    Returns:
        Streaming response containing the original PDF bytes.

    Raises:
        HTTPException: If the file is unknown or Azure Storage is unavailable.
    """
    try:
        entity = get_table_service(request).get_file(file_id)
        if entity is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="File not found",
            )

        filename = _safe_filename(str(entity.get("filename", "")))
        blob_name = str(entity.get("blob_name", ""))
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", file_id) or not blob_name:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="File not found",
            )
        chunks = get_blob_service(request).stream_original(blob_name)
        return StreamingResponse(
            chunks,
            media_type=PDF_CONTENT_TYPE,
            headers={"Content-Disposition": BlobService.content_disposition(filename)},
        )
    except AzureError as error:
        raise _storage_error("download file", error) from error


@app.get(
    "/health",
    summary="Check process health",
    description="Confirm that the FastAPI process can answer requests.",
    response_model=HealthResponse,
)
def health() -> HealthResponse:
    """Report that the FastAPI process is healthy.

    Returns HTTP 200 whenever the process is running. This endpoint does not
    probe Azure Storage.

    Returns:
        Static process health response.

    Raises:
        None.
    """
    return HealthResponse(status="healthy")


app.mount("/", StaticFiles(directory=STATIC_DIRECTORY, html=True), name="static")
