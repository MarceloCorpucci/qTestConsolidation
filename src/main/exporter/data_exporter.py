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
    # Written as qtest_requirements.txt: "requirements.txt" at the project root
    # is pip's dependency file and must not be overwritten.
    "requirements": {
        "path": PROJECTS_ENDPOINT + "/{project_id}/requirements",
        "file": "qtest_requirements",
    },
    # expandProps brings the custom fields along, which is where an
    # integration usually leaves its trace.
    "test_cases": {
        "path": PROJECTS_ENDPOINT + "/{project_id}/test-cases",
        "params": {"expandProps": "true"},
    },
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

#: Endpoint returning what an artifact is linked to. qTest has no direct
#: release-to-requirement relation, so coverage is derived through the test
#: cases a run executes and the requirements those cases are linked to.
LINKED_ARTIFACTS_PATH = PROJECTS_ENDPOINT + "/{project_id}/linked-artifacts"

#: Test case ids asked for per linked-artifacts call.
LINK_BATCH_SIZE = 50

#: Keys under which a test run may carry the test case it executes.
TEST_CASE_KEYS = ("test_case", "testCase")
TEST_CASE_ID_KEYS = ("test_case_id", "testCaseId", "test_case_version_id")

#: Keys under which a cycle, suite or run may name the release it belongs to.
#: Cycles often hang from the project root while still pointing at a release,
#: and reading that pointer is the only way to tie their runs to one.
RELEASE_ID_KEYS = ("release_id", "releaseId")

#: Entities the inventory writes on their own. Modules and test cases are left
#: out: they are reported together in the Test Design tree, which is what tells
#: apart a folder holding cases from an empty one.
STANDALONE_ENTITIES = ("test_plans", "requirements", "test_runs")

#: Top-level keys that betray an entity synced from an external tracker.
INTEGRATION_KEYS = (
    "external_id",
    "external_system",
    "external_system_id",
    "jira_id",
    "jira_key",
)

#: Substrings that mark a custom field or a link as belonging to Jira. Any
#: field naming an external reference is reported too: which one an instance
#: actually uses depends on how its integration was set up, so the evidence
#: found is written next to the entity instead of being assumed.
INTEGRATION_HINTS = ("jira", "external")

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

    def export_requirements(self, project_id: int) -> Path:
        """List the requirements of a project into `requirements.txt`."""
        return self.export_entity("requirements", project_id)

    def export_test_cases(self, project_id: int) -> Path:
        """List the test cases of a project into `test_cases.txt`."""
        return self.export_entity("test_cases", project_id)

    def export_test_runs(self, project_id: int) -> Path:
        """List the test runs of a project into `test_runs.txt`."""
        return self.export_entity("test_runs", project_id)

    def export_entity(self, entity: str, project_id: int) -> Path:
        """Fetch every entity of a kind and write its ids and names to a file."""
        entities = self.fetch_entities(entity, project_id)
        file_name = INVENTORY_ENTITIES[entity].get("file", entity)
        return self.write_inventory(entities, entity, project_id, file_name)

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
        refusal_expected: bool = False,
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
            payload = self._payload_of(response, subject, refusal_expected)
            items = self._items_of(payload, subject)
            logger.info("Collected %s %s", len(items), subject)
            return items

        collected: list[dict[str, Any]] = []
        seen: set[Any] = set()

        for page in range(1, MAX_PAGES + 1):
            logger.info("Fetching %s, page %s", subject, page)
            response = self._client.get(
                path, params={**base_params, "page": page, "pageSize": PAGE_SIZE}
            )
            payload = self._payload_of(response, subject, refusal_expected)
            items = self._items_of(payload, subject)

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
        """Every test run of the project, named by the path leading to it."""
        located = self.walk_execution_tree(project_id)
        runs = [{**item["run"], "name": self._run_path(item)} for item in located]
        # Sorted by path, so runs of the same cycle or suite read together.
        return sorted(runs, key=lambda run: str(run.get("name")))

    def walk_execution_tree(
        self,
        project_id: int,
        releases: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Walk the Test Execution tree, keeping where each run was found.

        Runs hang from test cycles and test suites, which in turn hang from the
        project root or from a release, and cycles can nest. Each entry returned
        carries the run plus its release, cycle and suite, which is what ties a
        run back to a release. A run reachable by more than one route is
        reported once.
        """
        logger.info("Walking the Test Execution tree of project %s", project_id)

        root_context = {"release_id": "", "release": "", "cycle": "", "suite": ""}
        pending: list[tuple[str, Any, dict[str, Any]]] = [("root", 0, root_context)]

        if releases is None:
            releases = self.fetch_entities("test_plans", project_id)
        for release in releases:
            pending.append((
                "release",
                self._key_of(release),
                {
                    **root_context,
                    "release_id": self._key_of(release),
                    "release": release.get("name") or "<no name>",
                },
            ))

        release_names = {self._key_of(release): release.get("name") for release in releases}
        located: dict[Any, dict[str, Any]] = {}
        visited: set[tuple[str, Any]] = set()

        while pending:
            parent_type, parent_id, context = pending.pop()
            if (parent_type, parent_id) in visited:
                continue
            visited.add((parent_type, parent_id))

            for run in self._children(project_id, "test-runs", parent_type, parent_id):
                key = self._key_of(run)
                if key not in located:
                    located[key] = {"run": run, **self._with_release(context, run, release_names)}

            for suite in self._children(project_id, "test-suites", parent_type, parent_id):
                pending.append((
                    "test-suite",
                    self._key_of(suite),
                    {
                        **self._with_release(context, suite, release_names),
                        "suite": suite.get("name") or "<no name>",
                    },
                ))

            if parent_type in BRANCHING_CONTAINERS:
                for cycle in self._children(project_id, "test-cycles", parent_type, parent_id):
                    pending.append((
                        "test-cycle",
                        self._key_of(cycle),
                        {
                            **self._with_release(context, cycle, release_names),
                            "cycle": self._path_join(context["cycle"], cycle.get("name")),
                        },
                    ))

        logger.info("Reached %s test runs across %s containers", len(located), len(visited))
        return list(located.values())

    @staticmethod
    def _with_release(
        context: dict[str, Any],
        entity: dict[str, Any],
        release_names: dict[Any, Any],
    ) -> dict[str, Any]:
        """Fill the release of a container that names one but hangs elsewhere.

        A cycle reached from the project root carries no release in the walk,
        yet its payload usually points at one. Reading that pointer is what
        ties its runs to a release; a release already known from the walk wins.
        """
        if context.get("release_id"):
            return context

        for key in RELEASE_ID_KEYS:
            release_id = entity.get(key)
            if release_id:
                return {
                    **context,
                    "release_id": release_id,
                    "release": release_names.get(release_id) or f"release {release_id}",
                }

        return context

    def _run_path(self, located: dict[str, Any]) -> str:
        """Container path of a located run, ending in the run's own name."""
        path = ""
        for part in (
            located["release"],
            located["cycle"],
            located["suite"],
            located["run"].get("name"),
        ):
            if part:
                path = self._path_join(path, part)
        return path or "<no name>"

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
                refusal_expected=True,
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

    def write_inventory(
        self,
        entities: list[dict[str, Any]],
        subject: str,
        project_id: int,
        file_name: str | None = None,
    ) -> Path:
        """Write the id and name of each entity to a text file in the project root."""
        target = PROJECT_ROOT / f"{file_name or subject}.txt"
        logger.info("Writing %s %s to %s", len(entities), subject, target)

        integrated = self._integration_count(entities)
        nameless = sum(
            1 for entity in entities if not (isinstance(entity, dict) and entity.get("name"))
        )

        lines = [
            f"# {subject} of project {project_id} in {self._client.base_url}",
            f"# {len(entities)} entries, {integrated} with Jira integration",
            "",
            *(self._entry_line(entity) for entity in entities),
        ]

        logger.info("%s of the %s show a Jira link", integrated, subject)
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
        integrated = self._integration_count(test_cases)
        logger.info(
            "Test Design tree: %s modules (%s empty), %s test cases, %s with a Jira link",
            len(modules),
            len(empty),
            len(test_cases),
            integrated,
        )

        lines = [
            f"# Test Design tree of project {project_id} in {self._client.base_url}",
            f"# {len(modules)} modules, {len(empty)} of them empty, {len(test_cases)} test cases",
            f"# {integrated} test cases integrated with Jira, marked [jira: <evidence>]",
            "# Each module lists the test cases directly inside it, indented.",
            "",
        ]
        for module in modules:
            held = by_module.get(self._key_of(module), [])
            with_jira = self._integration_count(held)
            summary = f"{len(held)} test cases" if held else "empty"
            if with_jira:
                summary += f", {with_jira} with Jira"
            lines.append(f"{self._key_of(module)} | {module.get('name')}  ({summary})")
            lines.extend(self._entry_line(case, indent="    ") for case in held)
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
                f"{self._entry_line(case, indent='    ')}  (module {self._module_id_of(case)})"
                for case in orphans
            ),
            "",
        ]

    # -- consolidated traceability -------------------------------------------

    def build_traceability(self, project_id: int) -> dict[str, Any]:
        """Gather every artifact and tie them together, release by release.

        The grain of `rows` is the test run: each row says which release holds
        it, through which cycle and suite, which test case it executes, in which
        module that case lives, and which requirements the case is linked to.

        qTest has no release-to-requirement relation, so the requirements of a
        release are *derived* along that chain. `links_available` says whether
        the case-to-requirement links could be read at all: when they cannot,
        the requirement columns stay empty and only the counts hold.
        """
        logger.info("Building the consolidated traceability of project %s", project_id)

        releases = self.fetch_entities("test_plans", project_id)
        requirements = self.fetch_entities("requirements", project_id)
        modules = self.fetch_entities("test_modules", project_id)
        test_cases = self.fetch_entities("test_cases", project_id)
        located_runs = self.walk_execution_tree(project_id, releases=releases)

        module_paths = {self._key_of(module): module.get("name") for module in modules}
        requirement_index = {self._key_of(item): item for item in requirements}
        case_index = {self._key_of(case): case for case in test_cases}
        links, links_available = self.fetch_requirement_links(
            project_id, list(case_index), list(requirement_index)
        )

        rows = [
            self._traceability_row(located, case_index, module_paths, requirement_index, links)
            for located in located_runs
        ]
        rows.sort(key=lambda row: (str(row["release"]), str(row["cycle"]), str(row["suite"])))

        with_case = sum(1 for row in rows if row["case_id"] != "")
        with_requirement = sum(1 for row in rows if row["requirement_ids"])
        logger.info(
            "Traceability built: %s releases, %s requirements, %s test cases, %s rows "
            "(%s resolved a test case, %s reached a requirement)",
            len(releases),
            len(requirements),
            len(test_cases),
            len(rows),
            with_case,
            with_requirement,
        )
        if rows and not with_case:
            logger.warning(
                "No test run reported the test case it executes, so the chain to the "
                "requirements cannot be built: the run payload carries none of %s",
                list(TEST_CASE_KEYS + TEST_CASE_ID_KEYS),
            )
        elif with_case and not with_requirement:
            logger.warning(
                "%s runs resolved their test case but none of those cases is linked to a "
                "requirement: either the project has no such links, or they are exposed "
                "somewhere other than the linked-artifacts endpoint",
                with_case,
            )
        # Handed over already resolved -- Jira evidence and module path included
        # -- so whoever presents the report needs no knowledge of the API.
        return {
            "project_id": project_id,
            "base_url": self._client.base_url,
            "releases": [
                {"id": self._key_of(release), "name": release.get("name")}
                for release in releases
            ],
            "requirements": [
                {
                    "id": self._key_of(item),
                    "name": item.get("name"),
                    "jira": self.integration_of(item) or "",
                }
                for item in requirements
            ],
            "modules": [
                {"id": self._key_of(module), "name": module.get("name")} for module in modules
            ],
            "test_cases": [
                {
                    "id": self._key_of(case),
                    "name": case.get("name"),
                    "jira": self.integration_of(case) or "",
                    "module_id": self._module_id_of(case),
                    "module": module_paths.get(self._module_id_of(case)) or "",
                }
                for case in test_cases
            ],
            "links": links,
            "links_available": links_available,
            "rows": rows,
        }

    def _traceability_row(
        self,
        located: dict[str, Any],
        case_index: dict[Any, dict[str, Any]],
        module_paths: dict[Any, Any],
        requirement_index: dict[Any, dict[str, Any]],
        links: dict[Any, list[Any]],
    ) -> dict[str, Any]:
        """One row of the traceability sheet, at test-run grain."""
        run = located["run"]
        case_id, case_name = self._test_case_of(run)
        case = case_index.get(case_id, {})
        linked = [requirement_index.get(rid, {"id": rid}) for rid in links.get(case_id, [])]

        return {
            "release_id": located["release_id"],
            "release": located["release"] or "(no release)",
            "cycle": located["cycle"],
            "suite": located["suite"],
            "run_id": self._key_of(run),
            "run": run.get("name") or "<no name>",
            "case_id": case_id or "",
            "case": case.get("name") or case_name or "",
            "module": module_paths.get(self._module_id_of(case)) or "",
            "case_jira": self.integration_of(case) or "",
            "requirement_ids": ", ".join(str(self._key_of(item)) for item in linked),
            "requirements": ", ".join(str(item.get("name") or "") for item in linked),
            "requirement_jira": ", ".join(
                marker for marker in (self.integration_of(item) for item in linked) if marker
            ),
        }

    def fetch_requirement_links(
        self,
        project_id: int,
        test_case_ids: list[Any],
        requirement_ids: list[Any] | None = None,
    ) -> tuple[dict[Any, list[Any]], bool]:
        """Requirement ids linked to each test case, read in batches.

        Asked first from the test case side. When that yields nothing, the same
        question is put from the requirement side before reporting no coverage:
        an empty answer in one direction is not proof that no links exist, and
        the requirements are far fewer to ask about.

        Returns the links and whether the endpoint could be read at all. A
        refusal is reported and treated as "no links known", because the rest
        of the inventory is still worth producing.
        """
        if not test_case_ids:
            return {}, False

        links, available = self._read_links(project_id, "test-cases", test_case_ids, "requirement")
        if links:
            logger.info("%s test cases carry a requirement link", len(links))
            return links, available

        if not requirement_ids:
            return links, available

        logger.info(
            "No links found from the test case side: asking the %s requirements instead",
            len(requirement_ids),
        )
        inverse, inverse_available = self._read_links(
            project_id, "requirements", requirement_ids, "test-case"
        )

        for requirement_id, case_ids in inverse.items():
            for case_id in case_ids:
                links.setdefault(case_id, []).append(requirement_id)

        logger.info(
            "%s test cases carry a requirement link, seen from the requirement side", len(links)
        )
        return links, available or inverse_available

    def _read_links(
        self,
        project_id: int,
        artifact_type: str,
        ids: list[Any],
        linked_prefix: str,
    ) -> tuple[dict[Any, list[Any]], bool]:
        """Read the linked artifacts of a kind, keeping the links of one type."""
        path = LINKED_ARTIFACTS_PATH.format(project_id=project_id)
        links: dict[Any, list[Any]] = {}

        for start in range(0, len(ids), LINK_BATCH_SIZE):
            batch = ids[start:start + LINK_BATCH_SIZE]
            logger.info(
                "Reading %s links of %s %s-%s of %s",
                linked_prefix,
                artifact_type,
                start + 1,
                start + len(batch),
                len(ids),
            )
            params = {"type": artifact_type, "ids": ",".join(str(item) for item in batch)}

            try:
                response = self._client.get(path, params=params)
                entries = self._items_of(self._payload_of(response, "linked artifacts"), "links")
            except ProjectExportError as error:
                logger.warning(
                    "Links of %s unavailable, the requirement columns may be empty: %s",
                    artifact_type,
                    error,
                )
                return links, False

            for entry in entries:
                self._collect_links(entry, links, linked_prefix)

        return links, True

    def _collect_links(
        self,
        entry: Any,
        links: dict[Any, list[Any]],
        linked_prefix: str,
    ) -> None:
        """Read one linked-artifacts entry into the links map."""
        if not isinstance(entry, dict):
            return

        source_id = entry.get("object_id") or entry.get("objectId") or entry.get("id")
        linked = entry.get("objects") or entry.get("linked_objects") or []
        if source_id is None or not isinstance(linked, list):
            return

        for item in linked:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("object_type") or item.get("objectType") or "").lower()
            if kind.startswith(linked_prefix):
                links.setdefault(source_id, []).append(self._key_of(item))

    @staticmethod
    def _test_case_of(run: dict[str, Any]) -> tuple[Any, str | None]:
        """Id and name of the test case a run executes, however it is carried."""
        for key in TEST_CASE_KEYS:
            nested = run.get(key)
            if isinstance(nested, dict):
                return nested.get("id"), nested.get("name")

        for key in TEST_CASE_ID_KEYS:
            case_id = run.get(key)
            if case_id is not None:
                return case_id, None

        return None, None

    # -- Jira integration ----------------------------------------------------

    def integration_of(self, entity: dict[str, Any]) -> str | None:
        """Evidence that an entity is tied to Jira, or None when there is none.

        The field carrying the link differs between instances and integration
        setups, so several places are checked and the one that matched is
        returned verbatim -- the point is to be able to confirm it against the
        qTest UI rather than to trust a guess.
        """
        if not isinstance(entity, dict):
            return None

        for key in INTEGRATION_KEYS:
            value = entity.get(key)
            if value:
                return f"{key}={value}"

        from_properties = self._integration_in_properties(entity)
        if from_properties:
            return from_properties

        for key in ("web_url", "url", "link"):
            value = entity.get(key)
            if isinstance(value, str) and "jira" in value.lower():
                return f"{key} points at Jira"

        return None

    @staticmethod
    def _integration_in_properties(entity: dict[str, Any]) -> str | None:
        """Look for an integration field among the custom properties."""
        properties = entity.get("properties")
        if not isinstance(properties, list):
            return None

        for prop in properties:
            if not isinstance(prop, dict):
                continue
            field = str(prop.get("field_name") or prop.get("field_id") or "")
            value = prop.get("field_value_name") or prop.get("field_value")
            if not value:
                continue
            if any(hint in field.lower() for hint in INTEGRATION_HINTS):
                return f"{field}={value}"
            if isinstance(value, str) and "jira" in value.lower():
                return f"{field}={value}"

        return None

    def _entry_line(self, entity: dict[str, Any], indent: str = "") -> str:
        """One inventory line: id, name, and the Jira evidence when there is any."""
        integration = self.integration_of(entity)
        suffix = f"  [jira: {integration}]" if integration else ""
        name = entity.get("name") if isinstance(entity, dict) else None
        return f"{indent}{self._key_of(entity)} | {name or '<no name>'}{suffix}"

    def _integration_count(self, entities: list[dict[str, Any]]) -> int:
        """How many entities show a Jira link."""
        return sum(1 for entity in entities if self.integration_of(entity))

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

    def _payload_of(self, response: Any, subject: str, refusal_expected: bool = False) -> Any:
        """Validate the response and return its decoded body.

        `refusal_expected` keeps a normal refusal out of the error log: not
        every parent/child combination of the execution tree is valid, and the
        walk asks anyway rather than hardcoding the API's rules.
        """
        if response.status_code not in SUCCESS_STATUS_CODES:
            log = logger.info if refusal_expected else logger.error
            log(
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
