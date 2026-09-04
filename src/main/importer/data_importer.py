"""Injection of test artifacts into the target qTest instance (HCSC).

`DataImporter` is a consumer of `RestClient`: it owns the decisions about which
endpoints to call and how to build the payloads, while the client only carries
the request. Concrete work is delegated to collaborator objects invoked from
`import_project`.
"""

from __future__ import annotations

import logging

from main.client import RestClient

logger = logging.getLogger(__name__)


class DataImporter:
    """Entry point for injecting artifacts into the target instance."""

    def __init__(self, client: RestClient) -> None:
        self._client = client
        logger.debug("Importer ready against %s", client.base_url)

    @property
    def client(self) -> RestClient:
        """The REST client used to reach the target instance."""
        return self._client

    def import_project(self) -> int:
        """Import a project into the target instance.

        Placeholder: returns a fixed value until the collaborator objects that
        do the actual injection are wired in.
        """
        logger.info("Starting project import into %s", self._client.base_url)
        result = 1
        logger.info("Project import finished, result: %s", result)
        return result
