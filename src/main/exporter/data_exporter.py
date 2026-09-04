"""Extraction of test artifacts from the source qTest instance (HS).

`DataExporter` is a consumer of `RestClient`: it owns the decisions about which
endpoints to call and how to read the responses, while the client only carries
the request.

`export_project` retrieves a project from the source instance and persists it
as JSON under `migration/imported/`, where it waits to be injected into the
target instance. The steps are kept as separate public methods so they can be
moved into dedicated collaborator objects as the migration grows.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from main.client import RestClient
from main.writer import IMPORTED_DIR, FileWriter

logger = logging.getLogger(__name__)

#: qTest Manager endpoint exposing projects.
PROJECTS_ENDPOINT = "/api/v3/projects"

#: File `export_project` writes to when no name is given.
DEFAULT_PROJECT_FILE = "project.json"

SUCCESS_STATUS_CODES = (200,)


class ProjectExportError(RuntimeError):
    """Raised when a project cannot be retrieved from the source instance."""


class DataExporter:
    """Entry point for extracting artifacts from the source instance."""

    def __init__(self, client: RestClient, output_dir: Path | str = IMPORTED_DIR) -> None:
        self._client = client
        self._output_dir = Path(output_dir)
        logger.debug(
            "Exporter ready against %s, writing to %s", client.base_url, self._output_dir
        )

    @property
    def client(self) -> RestClient:
        """The REST client used to reach the source instance."""
        return self._client

    @property
    def output_dir(self) -> Path:
        """Folder the extracted payloads are written to."""
        return self._output_dir

    # -- orchestration -------------------------------------------------------

    def export_project(
        self,
        project_id: int,
        file_name: str = DEFAULT_PROJECT_FILE,
    ) -> Path:
        """Export a project from the source instance and return the file written.

        Args:
            project_id: id of the project in the source instance.
            file_name: name of the JSON file to write under `migration/imported/`.

        Raises:
            ProjectExportError: if the source instance does not return the
                project.
        """
        logger.info("Starting export of project %s from %s", project_id, self._client.base_url)

        project = self.fetch_project(project_id)
        written = self.write_payload(project, file_name)

        logger.info("Project export finished, payload written to %s", written)
        return written

    # -- steps ---------------------------------------------------------------

    def fetch_project(self, project_id: int) -> dict[str, Any]:
        """Retrieve a single project from the source instance."""
        path = f"{PROJECTS_ENDPOINT}/{project_id}"
        logger.info("Fetching project %s", project_id)

        response = self._client.get(path)
        project = self._payload_of(response, f"project {project_id}")

        if not isinstance(project, dict):
            logger.error(
                "Expected a JSON object for project %s, got %s",
                project_id,
                type(project).__name__,
            )
            raise ProjectExportError(
                f"Project {project_id} came back as {type(project).__name__}, expected an object"
            )

        logger.info(
            "Fetched project %s: %r (%s fields)",
            project_id,
            project.get("name"),
            len(project),
        )
        return project

    def list_projects(self) -> list[dict[str, Any]]:
        """List the projects the token can see: useful to confirm access and ids."""
        logger.info("Listing projects available in %s", self._client.base_url)

        response = self._client.get(PROJECTS_ENDPOINT)
        projects = self._payload_of(response, "the project list")

        if not isinstance(projects, list):
            logger.error("Expected a JSON array of projects, got %s", type(projects).__name__)
            raise ProjectExportError(
                f"Project list came back as {type(projects).__name__}, expected an array"
            )

        logger.info("Found %s projects", len(projects))
        return projects

    def write_payload(self, payload: Any, file_name: str = DEFAULT_PROJECT_FILE) -> Path:
        """Persist an extracted payload as JSON, pending injection."""
        logger.debug("Handing the payload over to the writer as %s", file_name)
        return FileWriter(payload, output_dir=self._output_dir).write(file_name)

    # -- response handling ---------------------------------------------------

    def _payload_of(self, response: Any, subject: str) -> Any:
        """Validate the response and return its decoded body."""
        if response.status_code not in SUCCESS_STATUS_CODES:
            logger.error(
                "Source instance refused %s with status %s: %s",
                subject,
                response.status_code,
                response.text,
            )
            raise ProjectExportError(
                f"Could not retrieve {subject}: {response.status_code} {response.text}"
            )

        try:
            return response.json()
        except ValueError as error:
            logger.error("Source instance returned a non-JSON body for %s", subject)
            raise ProjectExportError(
                f"Source instance returned a non-JSON response for {subject}"
            ) from error
