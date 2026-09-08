"""Consolidated traceability report as a single CSV file.

`CsvWriter` takes the report built by `DataExporter.build_traceability` and
writes every artifact into one wide table, joined rather than stacked: each row
carries the whole chain it belongs to -- Release, Test Cycle, Test Suite, Test
Run, Test Case, Requirement -- with the ids and names of all of them side by
side. A requirement linked to a test case appears on that case's row, not on a
row of its own.

The grain is the deepest link of the chain: a test case linked to three
requirements yields three rows. Rows therefore repeat on purpose, and that
redundancy is what makes the file usable for tracking a migration line by line.

An entity that no chain reaches still gets a row -- a test case never
executed, a requirement linked to nothing, an empty folder, a release with
nothing under it -- so the inventory is complete. The `Record Type` column
names the deepest entity a row reaches, which is how those rows are told
apart.

It computes nothing about the instance: every relation comes from the report.
Temporary, like the inventory it presents.
"""

from __future__ import annotations

import csv
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Project root, where the report is written.
PROJECT_ROOT = Path(__file__).resolve().parents[3]

DEFAULT_REPORT_FILE = "qtest_consolidated"
CSV_EXTENSION = ".csv"

#: Written with a BOM so Excel opens the file as UTF-8 and keeps accents.
CSV_ENCODING = "utf-8-sig"

#: What a row is. The value of the first column of every row.
SUMMARY = "SUMMARY"
TEST_PLAN = "TEST_PLAN"
REQUIREMENT = "REQUIREMENT"
MODULE = "MODULE"
TEST_CASE = "TEST_CASE"
TEST_RUN = "TEST_RUN"

#: Every column of every table, in one row shape.
COLUMNS = (
    "Record Type",
    "Release ID",
    "Release",
    "Test Cycle",
    "Test Suite",
    "Test Run ID",
    "Test Run",
    "Test Case ID",
    "Test Case",
    "Module ID",
    "Module",
    "Test Case Jira",
    # One requirement per row, so its id and name sit beside the test case.
    "Requirement ID",
    "Requirement",
    "Requirement Jira",
    "Linked Test Cases",
    "Releases Covering",
    "Test Runs Count",
    "Test Cases Count",
    "Requirements Count",
    "Requirements With Jira Count",
    # Left empty on purpose: filled in by hand while the migration runs.
    "Migration Status",
    "Target ID",
    "Migration Notes",
)


class CsvWriter:
    """Writes the consolidated traceability report as one CSV file."""

    def __init__(
        self,
        report: dict[str, Any],
        output_dir: Path | str = PROJECT_ROOT,
        delimiter: str = ",",
    ) -> None:
        self.report = report
        self._output_dir = Path(output_dir)
        self._delimiter = delimiter
        # Filled once per build: recomputing them per row would be quadratic.
        self._requirement_index: dict[Any, dict[str, Any]] = {}
        self._reach: dict[str, dict[str, Any]] = {}
        logger.debug(
            "CSV writer ready, output folder: %s, delimiter %r", self._output_dir, delimiter
        )

    @property
    def report(self) -> dict[str, Any]:
        """The traceability report to lay out."""
        return self._report

    @report.setter
    def report(self, value: dict[str, Any]) -> None:
        if not value:
            logger.error("Cannot set the report: it is missing")
            raise ValueError("report is required")
        self._report = value

    @property
    def output_dir(self) -> Path:
        """Folder the files are written to."""
        return self._output_dir

    @property
    def delimiter(self) -> str:
        """Field separator. Use ';' where Excel expects it by locale."""
        return self._delimiter

    # -- report --------------------------------------------------------------

    def write(self, file_name: str = DEFAULT_REPORT_FILE) -> dict[str, Path]:
        """Write the consolidated table and its notes, and return both files."""
        target = self._resolve_path(file_name)
        rows = self.build_rows()
        logger.info("Writing %s consolidated rows to %s", len(rows), target)

        self._output_dir.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding=CSV_ENCODING, newline="") as csv_file:
            writer = csv.DictWriter(
                csv_file,
                fieldnames=COLUMNS,
                delimiter=self._delimiter,
                quoting=csv.QUOTE_MINIMAL,
                restval="",
            )
            writer.writeheader()
            writer.writerows(rows)

        logger.info("Wrote %s (%s bytes)", target.name, target.stat().st_size)
        return {"consolidated": target, "notes": self.write_notes(file_name)}

    def build_rows(self) -> list[dict[str, Any]]:
        """Every record of every kind, in one row shape.

        The chain rows come first, each holding a complete Release to
        Requirement path. What no chain reaches follows: requirements linked to
        nothing, empty folders, releases with nothing executed.
        """
        self._requirement_index = {
            requirement.get("id"): requirement for requirement in self._report["requirements"]
        }
        self._reach = self._requirement_reach()

        chain_rows = self._chain_rows()
        rows = [
            *self._summary_rows(),
            *chain_rows,
            *self._unreached_requirement_rows(chain_rows),
            *self._empty_module_rows(),
            *self._unused_release_rows(),
        ]
        logger.info(
            "Consolidated %s rows: %s",
            len(rows),
            ", ".join(
                f"{kind} {sum(1 for row in rows if row['Record Type'] == kind)}"
                for kind in (SUMMARY, TEST_PLAN, REQUIREMENT, MODULE, TEST_CASE, TEST_RUN)
            ),
        )
        return rows

    # -- row builders --------------------------------------------------------

    def _summary_rows(self) -> list[dict[str, Any]]:
        """One row per release with its counts, plus a distinct total."""
        rows = self._report["rows"]
        built = []

        for release in sorted({row["release"] for row in rows}):
            held = [row for row in rows if row["release"] == release]
            built.append({
                "Record Type": SUMMARY,
                "Release ID": held[0]["release_id"] if held else "",
                "Release": release,
                "Test Runs Count": len(held),
                "Test Cases Count": self._distinct(held, "case_id"),
                "Requirements Count": self._distinct_requirements(held),
                "Requirements With Jira Count": self._distinct_requirements(held, with_jira=True),
            })

        # Counted over every row, not summed down the column: a case or a
        # requirement reached by two releases would otherwise count twice.
        built.append({
            "Record Type": SUMMARY,
            "Release": "TOTAL (distinct)",
            "Test Runs Count": len(rows),
            "Test Cases Count": self._distinct(rows, "case_id"),
            "Requirements Count": self._distinct_requirements(rows),
            "Requirements With Jira Count": self._distinct_requirements(rows, with_jira=True),
        })
        return built

    def _chain_rows(self) -> list[dict[str, Any]]:
        """One row per link of the chain, everything it reaches on the same row.

        A run is written once per requirement of the case it executes, so the
        requirement sits beside the case. A case no run executes keeps its own
        rows, with the execution columns empty.
        """
        case_index = {case.get("id"): case for case in self._report["test_cases"]}
        runs_per_case = self._runs_per_case()
        built = []

        executed: set[Any] = set()
        for run in self._report["rows"]:
            executed.add(run["case_id"])
            case = case_index.get(run["case_id"], {})
            for requirement in self._requirements_of(run["case_id"]):
                built.append(self._chain_row(TEST_RUN, run, case, requirement, runs_per_case))

        for case in self._report["test_cases"]:
            if case.get("id") in executed:
                continue
            for requirement in self._requirements_of(case.get("id")):
                built.append(self._chain_row(TEST_CASE, None, case, requirement, runs_per_case))

        return built

    def _chain_row(
        self,
        record_type: str,
        run: dict[str, Any] | None,
        case: dict[str, Any],
        requirement: dict[str, Any] | None,
        runs_per_case: dict[Any, int],
    ) -> dict[str, Any]:
        """One row of the chain, as complete as what it reaches."""
        row = {
            "Record Type": record_type,
            "Test Case ID": case.get("id", ""),
            "Test Case": case.get("name") or "",
            "Module ID": case.get("module_id", ""),
            "Module": case.get("module") or self._missing_module(case),
            "Test Case Jira": case.get("jira") or "",
            # Zero means the case was never scheduled, which is worth knowing
            # before migrating it.
            "Test Runs Count": runs_per_case.get(case.get("id"), 0),
        }

        if run is not None:
            row.update({
                "Release ID": run["release_id"],
                "Release": run["release"],
                "Test Cycle": run["cycle"],
                "Test Suite": run["suite"],
                "Test Run ID": run["run_id"],
                "Test Run": run["run"],
                # Kept from the run when the case itself was not resolved.
                "Test Case ID": case.get("id", run["case_id"]),
                "Test Case": case.get("name") or run["case"],
                "Module": case.get("module") or run["module"],
                "Test Case Jira": case.get("jira") or run["case_jira"],
            })

        if requirement is not None:
            row.update(self._requirement_columns(requirement))

        return row

    def _requirement_columns(self, requirement: dict[str, Any]) -> dict[str, Any]:
        """The requirement side of a row: its id, name, Jira link and reach."""
        requirement_id = requirement.get("id")
        reach = self._reach.get(str(requirement_id), {})

        return {
            "Requirement ID": requirement_id,
            "Requirement": requirement.get("name") or "",
            "Requirement Jira": requirement.get("jira") or "",
            "Linked Test Cases": reach.get("cases", 0),
            "Releases Covering": ", ".join(sorted(reach.get("releases", ()))),
        }

    def _requirement_reach(self) -> dict[str, dict[str, Any]]:
        """Per requirement, how many cases link to it and which releases reach it."""
        reach: dict[str, dict[str, Any]] = {}

        for row in self._report["rows"]:
            for requirement_id in self._split(row["requirement_ids"]):
                entry = reach.setdefault(requirement_id, {"cases": 0, "releases": set()})
                entry["releases"].add(row["release"])

        for linked in self._report["links"].values():
            for requirement_id in linked:
                entry = reach.setdefault(str(requirement_id), {"cases": 0, "releases": set()})
                entry["cases"] += 1

        return reach

    def _unreached_requirement_rows(
        self,
        chain_rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Requirements no test case links to, which no chain row can carry."""
        reached = {str(row.get("Requirement ID")) for row in chain_rows}
        unreached = [
            requirement
            for requirement in self._report["requirements"]
            if str(requirement.get("id")) not in reached
        ]

        if unreached:
            logger.info(
                "%s of %s requirements are linked to no test case",
                len(unreached),
                len(self._report["requirements"]),
            )
        return [
            {"Record Type": REQUIREMENT, **self._requirement_columns(requirement)}
            for requirement in unreached
        ]

    def _empty_module_rows(self) -> list[dict[str, Any]]:
        """Test Design folders holding no test case."""
        holding = {case.get("module_id") for case in self._report["test_cases"]}
        return [
            {
                "Record Type": MODULE,
                "Module ID": module.get("id"),
                "Module": module.get("name") or "",
                "Test Cases Count": 0,
            }
            for module in self._report["modules"]
            if module.get("id") not in holding
        ]

    def _unused_release_rows(self) -> list[dict[str, Any]]:
        """Releases with nothing executed under them, absent from every chain."""
        used = {str(row["release_id"]) for row in self._report["rows"]}
        return [
            {
                "Record Type": TEST_PLAN,
                "Release ID": release.get("id"),
                "Release": release.get("name") or "",
                "Test Runs Count": 0,
            }
            for release in self._report["releases"]
            if str(release.get("id")) not in used
        ]

    def _requirements_of(self, case_id: Any) -> list[dict[str, Any] | None]:
        """Requirements linked to a test case, or `[None]` when there are none.

        The single `None` is what keeps a case without requirements on a row of
        its own instead of dropping it.
        """
        linked = [
            self._requirement_index.get(requirement_id, {"id": requirement_id})
            for requirement_id in self._report["links"].get(case_id, [])
        ]
        return linked or [None]

    def _missing_module(self, case: dict[str, Any]) -> str:
        """Label for a case whose module is not in the tree."""
        module_id = case.get("module_id")
        if module_id is None:
            return ""
        if module_id in {module.get("id") for module in self._report["modules"]}:
            return ""
        return f"(module {module_id} not in the tree)"

    # -- notes ---------------------------------------------------------------

    def write_notes(self, file_name: str = DEFAULT_REPORT_FILE) -> Path:
        """What the reader needs to know to trust the numbers.

        Kept out of the CSV on purpose: a comment header would break the tools
        the format was chosen for.
        """
        target = self._output_dir / f"{file_name}_notes.txt"
        logger.info("Writing the report notes to %s", target)

        target.write_text("\n".join(self._notes()) + "\n", encoding="utf-8")
        return target

    def _notes(self) -> list[str]:
        links_note = (
            "Requirement columns come from the test case to requirement links."
            if self._report["links_available"]
            else "WARNING: the case-to-requirement links could not be read, so the "
            "requirement columns are empty. Empty does not mean no coverage."
        )
        return [
            f"qTest consolidated traceability - project {self._report['project_id']}",
            f"Source: {self._report['base_url']}, "
            f"generated {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            "",
            "One table, every artifact, joined rather than stacked: each row carries",
            "the whole chain it belongs to -- Release, Test Cycle, Test Suite, Test Run,",
            "Test Case, Requirement -- with the ids and names side by side. A",
            "requirement linked to a test case is on that case's row, never on a row",
            "of its own.",
            "",
            "The grain is the deepest link: a case linked to three requirements yields",
            "three rows, and a run executing it yields one row per requirement. Rows",
            "repeat by design, which is what makes the file trackable line by line.",
            "",
            "Record Type names the deepest entity a row reaches:",
            "  TEST_RUN     the full chain, release through requirement",
            "  TEST_CASE    a case no run executes; the execution columns are empty",
            "  REQUIREMENT  a requirement no test case links to",
            "  MODULE       a Test Design folder holding no test case",
            "  TEST_PLAN    a release with nothing executed under it",
            "  SUMMARY      counts per release, then a distinct total for the project",
            "",
            "So an entity appears on its own row only when no chain reaches it. Reading",
            "the file by Record Type therefore never double counts.",
            "",
            "Test Runs Count changes meaning with the row type: on SUMMARY and",
            "TEST_PLAN it counts the runs under that release; on a chain row it counts",
            "the runs executing that test case, and 0 means it was never scheduled.",
            "",
            "qTest has no release-to-requirement relation: coverage is derived along",
            "Release > Test Cycle > Test Suite > Test Run > Test Case > Requirement.",
            "A requirement is therefore tied to a release only when a test run",
            "executes it through that chain.",
            links_note,
            "",
            "Counts are computed values, not formulas: a CSV carries no formulas, so",
            "they do not follow later edits of the rows. Test cases and requirements",
            "are counted distinct, as the same one repeats across runs, and the total",
            "row is distinct over the whole project -- smaller than the sum of the",
            "column whenever a case or requirement belongs to two releases.",
            "",
            "Migration Status, Target ID and Migration Notes are left empty for",
            "tracking the migration by hand.",
        ]

    # -- helpers -------------------------------------------------------------

    def _resolve_path(self, file_name: str) -> Path:
        name = (file_name or "").strip()
        if not name:
            logger.error("Cannot resolve the target path: file_name is missing")
            raise ValueError("file_name is required")
        if not name.lower().endswith(CSV_EXTENSION):
            name = f"{name}{CSV_EXTENSION}"
        return self._output_dir / name

    def _runs_per_case(self) -> dict[Any, int]:
        """How many runs execute each test case."""
        counts: dict[Any, int] = {}
        for row in self._report["rows"]:
            if row["case_id"] != "":
                counts[row["case_id"]] = counts.get(row["case_id"], 0) + 1
        return counts

    @staticmethod
    def _distinct(rows: list[dict[str, Any]], key: str) -> int:
        return len({row[key] for row in rows if row[key] not in ("", None)})

    @staticmethod
    def _split(value: Any) -> set[str]:
        return {part.strip() for part in str(value or "").split(",") if part.strip()}

    def _distinct_requirements(
        self,
        rows: list[dict[str, Any]],
        with_jira: bool = False,
    ) -> int:
        """Requirements reached by a set of rows, optionally only Jira-linked ones."""
        reached: set[str] = set()
        for row in rows:
            reached.update(self._split(row["requirement_ids"]))

        if not with_jira:
            return len(reached)

        jira_ids = {
            str(requirement.get("id"))
            for requirement in self._report["requirements"]
            if requirement.get("jira")
        }
        return len(reached & jira_ids)
