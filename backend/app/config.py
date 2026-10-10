"""Load backend configuration from environment variables."""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Define validated settings for the FastAPI application."""

    azure_storage_connection_string: str = Field(
        description="Authenticate Blob and Table clients for the shared storage account.",
    )
    upload_container: str = Field(
        default="uploads",
        description="Name the container that stores original PDF files.",
    )
    extracted_container: str = Field(
        default="extracted",
        description="Name the container that stores extracted text files.",
    )
    table_name: str = Field(
        default="filestatus",
        description="Name the table that tracks PDF processing status.",
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("azure_storage_connection_string")
    @classmethod
    def validate_connection_string(cls, value: str) -> str:
        """Reject an empty storage connection string.

        Args:
            value: Configured storage connection string.

        Returns:
            The trimmed, nonempty connection string.

        Raises:
            ValueError: If the configured value is empty or whitespace only.
        """
        if not value.strip():
            raise ValueError("AZURE_STORAGE_CONNECTION_STRING must not be empty")
        return value.strip()
