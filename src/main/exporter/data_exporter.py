"""Extraction of test artifacts from the source qTest instance (HS).

`DataExporter` is a consumer of `RestClient`: it owns the decisions about which
endpoints to call and how to read the responses, while the client only carries
the request.

`export_project` retrieves a project from the source instance and persists it
as JSON under `migration/imported/`, where it waits to be injected into the
target instance. The steps are kept as separate public methods so they can be
moved into dedicated collaborator objects as the migration grows.

The `export_inventory` family is temporary: it lists the ids and names of the
project's modules (the Test Design folders), test plans, test cases and test
runs into text files at the project root, to size and track the migration. It
is not part of the migration flow and should be removed once the inventory has
been taken.
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

#: Endpoints backing the inventory, by entity, with how each must be read.
#: qTest Manager has no "test plans" endpoint: the Test Plan module is made of
#: releases, so that is what this reads.
INVENTORY_ENTITIES = {
    # Modules are the folders of the Test Design tree, empty ones included.
    # The endpoint returns only the root level unless descendants are expanded,
    # and then the tree arrives nested under "children" -- so it is read in a
    # single call and flattened into full paths.
    "test_modules": {
        "path": PROJECTS_ENDPOINT + "/{project_id}/modules",
        "params": {"expand": "descendants"},
        "paginated": False,
        "tree": True,
    },
    "test_plans": {"path": PROJECTS_ENDPOINT + "/{project_id}/releases"},
    "test_cases": {"path": PROJECTS_ENDPOINT + "/{project_id}/test-cases"},
    # Test runs hang from the containers of the Test Execution tree, never from
    # the project itself: asking without a parent yields nothing. They are
    # collected by walking that tree instead of reading one endpoint.
    "test_runs": {"path": PROJECTS_ENDPOINT + "/{project_id}/test-runs", "walk": True},
}

#: Endpoints of the Test Execution tree, walked to reach every test run.
TEST_CYCLES_PATH = PROJECTS_ENDPOINT + "/{project_id}/test-cycles"
TEST_SUITES_PATH = PROJECTS_ENDPOINT + "/{project_id}/test-suites"

#: Containers that may hold cycles and suites. A suite only holds runs.
BRANCHING_CONTAINERS = ("root", "release", "test-cycle")

#: Entities the inventory writes on their own. Modules and test cases are left
#: out: they are reported together in the Test Design tree, which is what tells
#: apart a folder holding cases from an empty one.
STANDALONE_ENTITIES = ("test_plans", "test_runs")

#: File holding the modules and their test cases.
TEST_DESIGN_FILE = "test_design"

#: Keys under which a test case may carry the id of the module holding it.
MODULE_ID_KEYS = ("parent_id", "parentId", "module_id", "moduleId")

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
        """Write the inventory files and return them.

        The Test Design tree comes first -- modules with the test cases they
        hold -- followed by one file per standalone entity.
        """
        logger.info("Taking inventory of project %s in %s", project_id, self._client.base_url)

        written = {TEST_DESIGN_FILE: self.export_test_design(project_id)}
        written.update(
            {entity: self.export_entity(entity, project_id) for entity in STANDALONE_ENTITIES}
        )

        logger.info("Inventory complete: %s files written", len(written))
        return written

    def export_test_design(self, project_id: int) -> Path:
        """Write the Test Design tree: every module with its test cases.

        Modules with nothing inside are listed as empty, so the file shows both
        where the cases live and which folders are unused.
        """
        logger.info("Building the Test Design tree of project %s", project_id)

        modules = self.fetch_entities("test_modules", project_id)
        test_cases = self.fetch_entities("test_cases", project_id)

        return self.write_test_design(modules, test_cases, project_id)

    def export_test_modules(self, project_id: int) -> Path:
        """List the Test Design folders of a project into `test_modules.txt`.

        Every folder is listed, empty ones included, by its full path.
        """
        return self.export_entity("test_modules", project_id)

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
        entities = self.fetch_entities(entity, project_id)
        return self.write_inventory(entities, entity, project_id)

    def fetch_entities(self, entity: str, project_id: int) -> list[dict[str, Any]]:
        """Fetch every entity of a kind, reading it the way its endpoint needs."""
        try:
            spec = INVENTORY_ENTITIES[entity]
        except KeyError:
            logger.error("Unknown inventory entity %r", entity)
            raise ProjectExportError(
                f"Unknown entity {entity!r}, expected one of {sorted(INVENTORY_ENTITIES)}"
            ) from None

        if spec.get("walk"):
            return self.fetch_test_runs(project_id)

        entities = self.fetch_all(
            spec["path"].format(project_id=project_id),
            entity,
            params=spec.get("params"),
            paginated=spec.get("paginated", True),
        )

        if spec.get("tree"):
            entities = self.flatten_tree(entities)
            logger.info("Flattened %s into %s entries", entity, len(entities))

        return entities

    def fetch_all(
        self,
        path: str,
        subject: str,
        params: dict[str, Any] | None = None,
        paginated: bool = True,
    ) -> list[dict[str, Any]]:
        """Read an endpoint whole, walking its pages when it has them.

        The loop stops on an empty page, or when a page brings nothing new --
        which is what happens when an endpoint ignores the paging parameters
        and keeps answering with the same block. It deliberately does NOT stop
        on a page shorter than requested: qTest caps some endpoints at its own
        page size and ignores ours, so a short page is not the last one.
        """
        base_params = dict(params or {})

        if not paginated:
            logger.info("Fetching %s in a single call", subject)
            response = self._client.get(path, params=base_params or None)
            items = self._items_of(self._payload_of(response, subject), subject)
            logger.info("Collected %s %s", len(items), subject)
            return items

        collected: list[dict[str, Any]] = []
        seen: set[Any] = set()

        for page in range(1, MAX_PAGES + 1):
            logger.info("Fetching %s, page %s", subject, page)
            response = self._client.get(
                path, params={**base_params, "page": page, "pageSize": PAGE_SIZE}
            )
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
        else:
            logger.warning(
                "Stopped after %s pages of %s: raise MAX_PAGES if more are expected",
                MAX_PAGES,
                subject,
            )

        logger.info("Collected %s %s", len(collected), subject)
        return collected

    def fetch_test_runs(self, project_id: int) -> list[dict[str, Any]]:
        """Walk the Test Execution tree and collect every test run in it.

        Runs hang from test cycles and test suites, which in turn hang from the
        project root or from a release, and cycles can nest. Each run is named
        by the path of containers leading to it, and reported once even when
        its container is reachable by more than one route.
        """
        logger.info("Walking the Test Execution tree of project %s", project_id)

        pending: list[tuple[str, Any, str]] = [("root", 0, "")]
        for release in self.fetch_entities("test_plans", project_id):
            pending.append(("release", self._key_of(release), release.get("name") or "release"))

        collected: dict[Any, dict[str, Any]] = {}
        visited: set[tuple[str, Any]] = set()

        while pending:
            parent_type, parent_id, path = pending.pop()
            if (parent_type, parent_id) in visited:
                continue
            visited.add((parent_type, parent_id))

            for run in self._children(project_id, "test-runs", parent_type, parent_id):
                key = self._key_of(run)
                if key not in collected:
                    collected[key] = {**run, "name": self._path_join(path, run.get("name"))}

            for suite in self._children(project_id, "test-suites", parent_type, parent_id):
                pending.append(
                    ("test-suite", self._key_of(suite), self._path_join(path, suite.get("name")))
                )

            if parent_type in BRANCHING_CONTAINERS:
                for cycle in self._children(project_id, "test-cycles", parent_type, parent_id):
                    pending.append(
                        ("test-cycle", self._key_of(cycle), self._path_join(path, cycle.get("name")))
                    )

        logger.info(
            "Collected %s test runs across %s containers", len(collected), len(visited)
        )
        # Sorted by path, so runs of the same cycle or suite read together.
        return sorted(collected.values(), key=lambda run: str(run.get("name")))

    def _children(
        self,
        project_id: int,
        child: str,
        parent_type: str,
        parent_id: Any,
    ) -> list[dict[str, Any]]:
        """Entities of a kind hanging from one container of the execution tree.

        A container that rejects the query is reported and skipped: not every
        combination of parent and child is valid, and one refusal must not stop
        the walk.
        """
        paths = {
            "test-runs": INVENTORY_ENTITIES["test_runs"]["path"],
            "test-suites": TEST_SUITES_PATH,
            "test-cycles": TEST_CYCLES_PATH,
        }
        subject = f"{child} under {parent_type} {parent_id}"

        try:
            return self.fetch_all(
                paths[child].format(project_id=project_id),
                subject,
                params={"parentId": parent_id, "parentType": parent_type},
            )
        except ProjectExportError as error:
            logger.warning("Skipping %s: %s", subject, error)
            return []

    @staticmethod
    def _path_join(prefix: str, name: str | None) -> str:
        """Append a container or entity name to a path."""
        name = name or "<no name>"
        return f"{prefix} / {name}" if prefix else name

    def flatten_tree(self, entities: list[dict[str, Any]], prefix: str = "") -> list[dict[str, Any]]:
        """Flatten a nested module tree, naming each entry by its full path."""
        flattened: list[dict[str, Any]] = []

        for entity in entities:
            name = entity.get("name") or "<no name>"
            full_path = f"{prefix} / {name}" if prefix else name
            flattened.append({**entity, "name": full_path})
            flattened.extend(self.flatten_tree(entity.get("children") or [], full_path))

        return flattened

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

    def write_test_design(
        self,
        modules: list[dict[str, Any]],
        test_cases: list[dict[str, Any]],
        project_id: int,
    ) -> Path:
        """Write each module with the test cases it holds, empty ones included."""
        target = PROJECT_ROOT / f"{TEST_DESIGN_FILE}.txt"

        by_module: dict[Any, list[dict[str, Any]]] = {}
        for test_case in test_cases:
            by_module.setdefault(self._module_id_of(test_case), []).append(test_case)

        empty = [module for module in modules if not by_module.get(self._key_of(module))]
        logger.info(
            "Test Design tree: %s modules (%s empty), %s test cases",
            len(modules),
            len(empty),
            len(test_cases),
        )

        lines = [
            f"# Test Design tree of project {project_id} in {self._client.base_url}",
            f"# {len(modules)} modules, {len(empty)} of them empty, {len(test_cases)} test cases",
            "# Each module lists the test cases directly inside it, indented.",
            "",
        ]
        for module in modules:
            held = by_module.get(self._key_of(module), [])
            summary = f"{len(held)} test cases" if held else "empty"
            lines.append(f"{self._key_of(module)} | {module.get('name')}  ({summary})")
            lines.extend(
                f"    {self._key_of(case)} | {case.get('name') or '<no name>'}" for case in held
            )
            lines.append("")

        lines.extend(self._orphan_lines(modules, by_module))

        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info("Wrote %s (%s bytes)", target.name, target.stat().st_size)
        return target

    def _orphan_lines(
        self,
        modules: list[dict[str, Any]],
        by_module: dict[Any, list[dict[str, Any]]],
    ) -> list[str]:
        """Report test cases whose module is not in the tree, if there are any."""
        known = {self._key_of(module) for module in modules}
        orphans = [
            test_case
            for module_id, held in by_module.items()
            if module_id not in known
            for test_case in held
        ]
        if not orphans:
            return []

        logger.warning(
            "%s test cases point at a module missing from the tree", len(orphans)
        )
        return [
            f"# {len(orphans)} test cases whose module is not in the tree above",
            "",
            *(
                f"    {self._key_of(case)} | {case.get('name') or '<no name>'}"
                f"  (module {self._module_id_of(case)})"
                for case in orphans
            ),
            "",
        ]

    @staticmethod
    def _module_id_of(test_case: dict[str, Any]) -> Any:
        """Id of the module holding a test case, whatever key carries it."""
        for key in MODULE_ID_KEYS:
            module_id = test_case.get(key)
            if module_id is not None:
                return module_id
        return None

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
