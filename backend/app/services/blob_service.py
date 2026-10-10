"""Provide Blob Storage operations for PDF files and extracted text."""

import logging
from collections.abc import Iterator
from urllib.parse import quote

from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import BlobServiceClient, ContainerClient
from azure.storage.blob import StorageStreamDownloader

logger = logging.getLogger(__name__)


class BlobService:
    """Manage original PDF and extracted-text blobs with reusable clients."""

    def __init__(
        self,
        client: BlobServiceClient,
        upload_container: str,
        extracted_container: str,
    ) -> None:
        """Initialize container clients for the application lifetime.

        Args:
            client: Shared service client authenticated for the storage account.
            upload_container: Container that stores original PDFs.
            extracted_container: Container that stores extracted text.

        Returns:
            None.

        Raises:
            ValueError: If an Azure SDK client rejects a container name.
        """
        self._upload_container: ContainerClient = client.get_container_client(
            upload_container
        )
        self._extracted_container: ContainerClient = client.get_container_client(
            extracted_container
        )

    def ensure_containers(self) -> None:
        """Create required containers when they do not already exist.

        Returns:
            None.

        Raises:
            AzureError: If Blob Storage cannot inspect or create a container.
        """
        for container in (self._upload_container, self._extracted_container):
            try:
                container.create_container()
            except ResourceExistsError:
                logger.debug("Blob container already exists: %s", container.container_name)

    def upload_pdf(self, blob_name: str, content: bytes) -> None:
        """Upload a PDF to Blob Storage without overwriting existing data.

        Args:
            blob_name: Destination path beneath the uploads container.
            content: Validated PDF bytes to store.

        Returns:
            None.

        Raises:
            AzureError: If Blob Storage rejects or cannot complete the upload.
        """
        self._upload_container.upload_blob(blob_name, content, overwrite=False)
        logger.info("Uploaded PDF blob %s", blob_name)

    def delete_pdf(self, blob_name: str) -> None:
        """Delete an uploaded PDF during transaction rollback.

        Args:
            blob_name: Path of the original PDF to remove.

        Returns:
            None.

        Raises:
            AzureError: If Blob Storage cannot delete the blob.
        """
        self._upload_container.delete_blob(blob_name)
        logger.info("Deleted PDF blob %s", blob_name)

    def read_extracted_text(self, blob_name: str) -> str:
        """Read and decode an extracted-text blob.

        Args:
            blob_name: Name of the UTF-8 text blob to download.

        Returns:
            Extracted document text.

        Raises:
            AzureError: If Blob Storage cannot download the blob.
            UnicodeDecodeError: If the stored content is not valid UTF-8.
        """
        content = self._extracted_container.download_blob(blob_name).readall()
        return content.decode("utf-8")

    def stream_original(self, blob_name: str) -> Iterator[bytes]:
        """Stream an original PDF in SDK-provided chunks.

        Args:
            blob_name: Path of the original PDF to download.

        Returns:
            An iterator that yields PDF byte chunks.

        Raises:
            AzureError: If Blob Storage cannot open or stream the blob.
        """
        downloader: StorageStreamDownloader[bytes] = self._upload_container.download_blob(
            blob_name
        )
        return downloader.chunks()

    @staticmethod
    def content_disposition(filename: str) -> str:
        """Build a UTF-8 attachment header for a safe display filename.

        Args:
            filename: Sanitized filename presented to the browser.

        Returns:
            RFC 5987-compatible Content-Disposition header value.

        Raises:
            None.
        """
        return f"attachment; filename*=UTF-8''{quote(filename, safe='')}"
