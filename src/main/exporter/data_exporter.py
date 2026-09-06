"""Extraction of test artifacts from the source qTest instance (HS).

`DataExporter` is a consumer of `RestClient`: it owns the decisions about which
endpoints to call and how to read the responses, while the client only carries
the request.

`export_project` retrieves a project from the source instance and persists it
as JSON under `migration/imported/`, where it waits to be injected into the
target instance. The steps are kept as separate public methods so they can be
moved into dedicated collaborator objects as the migration grows.

The `export_inventory` family is temporary: it lists the ids and names of the
project's test plans, test cases and test runs into text files at the project
root, to size and track the migration. It is not part of the migration flow
and should be removed once the inventory has been taken.
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

#: Project root, where the one-off inventory files are written.
PROJECT_ROOT = Path(__file__).resolve().parents[3]

#: Endpoints backing the inventory, by entity. qTest Manager has no
#: "test plans" endpoint: the Test Plan module is made of releases, so that is
#: what this reads.
INVENTORY_ENDPOINTS = {
    "test_plans": PROJECTS_ENDPOINT + "/{project_id}/releases",
    "test_cases": PROJECTS_ENDPOINT + "/{project_id}/test-cases",
    "test_runs": PROJECTS_ENDPOINT + "/{project_id}/test-runs",
}

#: Entities requested per page while walking a paginated endpoint.
PAGE_SIZE = 100

#: Hard stop for the pagination loop, so a misbehaving endpoint cannot spin.
MAX_PAGES = 500

#: Keys under which qTest may nest a page of results.
ITEM_KEYS = ("items", "data", "results")


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

    # -- one-off inventory ---------------------------------------------------
    #
    # Everything below serves a single purpose: taking stock of what lives in
    # the source instance, to track migration progress. It is not part of the
    # migration itself and is meant to be deleted once the inventory is taken.

    def export_inventory(self, project_id: int) -> dict[str, Path]:
        """Write one text file per entity and return the files written."""
        logger.info("Taking inventory of project %s in %s", project_id, self._client.base_url)

        written = {
            entity: self.export_entity(entity, project_id) for entity in INVENTORY_ENDPOINTS
        }

        logger.info("Inventory complete: %s files written", len(written))
        return written

    def export_test_plans(self, project_id: int) -> Path:
        """List the test plans (releases) of a project into `test_plans.txt`."""
        return self.export_entity("test_plans", project_id)

    def export_test_cases(self, project_id: int) -> Path:
        """List the test cases of a project into `test_cases.txt`."""
        return self.export_entity("test_cases", project_id)

    def export_test_runs(self, project_id: int) -> Path:
        """List the test runs of a project into `test_runs.txt`."""
        return self.export_entity("test_runs", project_id)

    def export_entity(self, entity: str, project_id: int) -> Path:
        """Fetch every entity of a kind and write its ids and names to a file."""
        try:
            endpoint = INVENTORY_ENDPOINTS[entity]
        except KeyError:
            logger.error("Unknown inventory entity %r", entity)
            raise ProjectExportError(
                f"Unknown entity {entity!r}, expected one of {sorted(INVENTORY_ENDPOINTS)}"
            ) from None

        entities = self.fetch_all(endpoint.format(project_id=project_id), entity)
        return self.write_inventory(entities, entity, project_id)

    def fetch_all(self, path: str, subject: str) -> list[dict[str, Any]]:
        """Walk a paginated endpoint and return every entity it yields.

        The loop stops on an empty page, or when a page brings nothing new --
        which is what happens when an endpoint ignores the paging parameters
        and keeps answering with the same block.
        """
        collected: list[dict[str, Any]] = []
        seen: set[Any] = set()

        for page in range(1, MAX_PAGES + 1):
            logger.info("Fetching %s, page %s", subject, page)
            response = self._client.get(path, params={"page": page, "pageSize": PAGE_SIZE})
            items = self._items_of(self._payload_of(response, subject), subject)

            if not items:
                logger.debug("Page %s of %s is empty, stopping", page, subject)
                break

            fresh = [item for item in items if self._key_of(item) not in seen]
            if not fresh:
                logger.warning(
                    "Page %s of %s repeated entities already seen: the endpoint seems to "
                    "ignore paging, stopping here",
                    page,
                    subject,
                )
                break

            seen.update(self._key_of(item) for item in fresh)
            collected.extend(fresh)
            logger.debug("%s entities collected so far for %s", len(collected), subject)

            if len(items) < PAGE_SIZE:
                logger.debug("Last page of %s reached", subject)
                break
        else:
            logger.warning(
                "Stopped after %s pages of %s: raise MAX_PAGES if more are expected",
                MAX_PAGES,
                subject,
            )

        logger.info("Collected %s %s", len(collected), subject)
        return collected

    def write_inventory(self, entities: list[dict[str, Any]], subject: str, project_id: int) -> Path:
        """Write the id and name of each entity to a text file in the project root."""
        target = PROJECT_ROOT / f"{subject}.txt"
        logger.info("Writing %s %s to %s", len(entities), subject, target)

        nameless = 0
        lines = [
            f"# {subject} of project {project_id} in {self._client.base_url}",
            f"# {len(entities)} entries",
            "",
        ]
        for entity in entities:
            name = entity.get("name") if isinstance(entity, dict) else None
            if not name:
                nameless += 1
            lines.append(f"{self._key_of(entity)} | {name or '<no name>'}")

        if nameless:
            logger.warning("%s of the %s carry no name", nameless, subject)

        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info("Wrote %s (%s bytes)", target.name, target.stat().st_size)
        return target

    @staticmethod
    def _key_of(entity: Any) -> Any:
        """Identity of an entity, used both for output and to spot repeats."""
        return entity.get("id") if isinstance(entity, dict) else entity

    def _items_of(self, payload: Any, subject: str) -> list[Any]:
        """Pull the list of entities out of a response body.

        Some qTest endpoints answer with a bare array, others wrap the page in
        an object; both shapes are accepted.
        """
        if isinstance(payload, list):
            return payload

        if isinstance(payload, dict):
            for key in ITEM_KEYS:
                items = payload.get(key)
                if isinstance(items, list):
                    logger.debug("Page of %s nested under %r", subject, key)
                    return items

        logger.error("Unexpected shape for %s: %s", subject, type(payload).__name__)
        raise ProjectExportError(
            f"Could not find a list of {subject} in the response "
            f"({type(payload).__name__})"
        )

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
