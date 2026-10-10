"""Provide Table Storage operations for PDF processing records."""

import logging
from typing import Any

from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableClient, TableServiceClient

logger = logging.getLogger(__name__)

PARTITION_KEY = "pdf"


class TableService:
    """Manage PDF status entities through one reusable table client."""

    def __init__(self, service_client: TableServiceClient, table_name: str) -> None:
        """Initialize a client for the configured status table.

        Args:
            service_client: Shared client authenticated for the Table service.
            table_name: Table that stores PDF status entities.

        Returns:
            None.

        Raises:
            ValueError: If the Azure SDK rejects the table name.
        """
        self._service_client = service_client
        self._table_name = table_name
        self._client: TableClient = service_client.get_table_client(table_name)

    def ensure_table(self) -> None:
        """Create the status table when it does not already exist.

        Returns:
            None.

        Raises:
            AzureError: If Table Storage cannot inspect or create the table.
        """
        self._service_client.create_table_if_not_exists(self._table_name)

    def insert_file(self, entity: dict[str, Any]) -> None:
        """Insert a new PDF status entity.

        Args:
            entity: Complete Table entity containing partition and row keys.

        Returns:
            None.

        Raises:
            AzureError: If Table Storage rejects or cannot create the entity.
        """
        self._client.create_entity(entity=entity)
        logger.info("Created file status row %s", entity["RowKey"])

    def list_files(self) -> list[dict[str, Any]]:
        """List PDF entities with the newest uploads first.

        Returns:
            Status entities from the PDF partition in descending upload order.

        Raises:
            AzureError: If Table Storage cannot query the entities.
        """
        entities = [
            dict(entity)
            for entity in self._client.query_entities(
                query_filter=f"PartitionKey eq '{PARTITION_KEY}'"
            )
        ]
        return sorted(
            entities,
            key=lambda entity: str(entity.get("uploaded_at", "")),
            reverse=True,
        )

    def get_file(self, file_id: str) -> dict[str, Any] | None:
        """Retrieve one PDF entity by its generated identifier.

        Args:
            file_id: Row key generated when the PDF was uploaded.

        Returns:
            The matching entity, or None when no row exists.

        Raises:
            AzureError: If Table Storage fails for a reason other than a missing row.
        """
        try:
            return dict(
                self._client.get_entity(
                    partition_key=PARTITION_KEY,
                    row_key=file_id,
                )
            )
        except ResourceNotFoundError:
            return None
