"""Consolidated traceability report as a single CSV file.

`CsvWriter` takes the report built by `DataExporter.build_traceability` and
writes every artifact into one wide table, the way a SQL `UNION ALL` would:
one row per record, a `Record Type` column saying what the row is, and the
columns that do not apply to that type left empty.

Rows repeat on purpose. A requirement covered by twenty test cases appears in
twenty test-run rows, and that redundancy is what makes the file usable for
tracking a migration line by line.

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
    "Requirement IDs",
    "Requirements",
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

        Ordered from the widest view to the narrowest -- summary, releases,
        requirements, the design tree, then the runs -- so the file reads top
        to bottom without sorting it first.
        """
        rows = [
            *self._summary_rows(),
            *self._test_plan_rows(),
            *self._requirement_rows(),
            *self._test_design_rows(),
            *self._test_run_rows(),
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

    def _test_plan_rows(self) -> list[dict[str, Any]]:
        """Every release, including those with nothing executed under them."""
        rows = self._report["rows"]
        return [
            {
                "Record Type": TEST_PLAN,
                "Release ID": release.get("id"),
                "Release": release.get("name") or "",
                "Test Runs Count": sum(
                    1 for row in rows if str(row["release_id"]) == str(release.get("id"))
                ),
            }
            for release in self._report["releases"]
        ]

    def _requirement_rows(self) -> list[dict[str, Any]]:
        """Every requirement, its Jira link and the releases reaching it."""
        links = self._report["links"]
        rows = self._report["rows"]
        built = []

        for requirement in self._report["requirements"]:
            requirement_id = requirement.get("id")
            covering = sorted(
                {
                    row["release"]
                    for row in rows
                    if str(requirement_id) in self._split(row["requirement_ids"])
                }
            )
            built.append({
                "Record Type": REQUIREMENT,
                "Requirement IDs": requirement_id,
                "Requirements": requirement.get("name") or "",
                "Requirement Jira": requirement.get("jira") or "",
                "Linked Test Cases": sum(
                    1 for linked in links.values() if requirement_id in linked
                ),
                "Releases Covering": ", ".join(covering),
            })

        return built

    def _test_design_rows(self) -> list[dict[str, Any]]:
        """The module tree: one row per test case, one per empty folder."""
        by_module: dict[Any, list[dict[str, Any]]] = {}
        for case in self._report["test_cases"]:
            by_module.setdefault(case.get("module_id"), []).append(case)

        runs_per_case = self._runs_per_case()
        built = []

        for module in self._report["modules"]:
            module_id = module.get("id")
            held = by_module.get(module_id, [])

            if not held:
                built.append({
                    "Record Type": MODULE,
                    "Module ID": module_id,
                    "Module": module.get("name") or "",
                    "Test Cases Count": 0,
                })
                continue

            built.extend(
                self._case_row(case, module_id, module.get("name") or "", runs_per_case)
                for case in held
            )

        # A case whose module is missing from the tree still belongs in the
        # file: dropping it would silently shrink the inventory.
        known = {module.get("id") for module in self._report["modules"]}
        for module_id, held in by_module.items():
            if module_id in known:
                continue
            logger.warning(
                "%s test cases hang from module %s, which is not in the tree",
                len(held),
                module_id,
            )
            built.extend(
                self._case_row(
                    case, module_id, f"(module {module_id} not in the tree)", runs_per_case
                )
                for case in held
            )

        return built

    def _case_row(
        self,
        case: dict[str, Any],
        module_id: Any,
        module_name: str,
        runs_per_case: dict[Any, int],
    ) -> dict[str, Any]:
        """One TEST_CASE row."""
        return {
            "Record Type": TEST_CASE,
            "Test Case ID": case.get("id"),
            "Test Case": case.get("name") or "",
            "Module ID": module_id,
            "Module": module_name,
            "Test Case Jira": case.get("jira") or "",
            "Requirement IDs": ", ".join(
                str(item) for item in self._report["links"].get(case.get("id"), [])
            ),
            # How many runs execute this case: zero means it was never
            # scheduled, which is worth knowing before migrating it.
            "Test Runs Count": runs_per_case.get(case.get("id"), 0),
        }

    def _test_run_rows(self) -> list[dict[str, Any]]:
        """Every test run, with the whole chain from its release to a requirement."""
        module_ids = {
            case.get("id"): case.get("module_id") for case in self._report["test_cases"]
        }
        return [
            {
                "Record Type": TEST_RUN,
                "Release ID": row["release_id"],
                "Release": row["release"],
                "Test Cycle": row["cycle"],
                "Test Suite": row["suite"],
                "Test Run ID": row["run_id"],
                "Test Run": row["run"],
                "Test Case ID": row["case_id"],
                "Test Case": row["case"],
                "Module ID": module_ids.get(row["case_id"], ""),
                "Module": row["module"],
                "Test Case Jira": row["case_jira"],
                "Requirement IDs": row["requirement_ids"],
                "Requirements": row["requirements"],
                "Requirement Jira": row["requirement_jira"],
            }
            for row in self._report["rows"]
        ]

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
            "One table, every artifact, the way a SQL UNION ALL would lay them out:",
            "the Record Type column says what a row is, and the columns that do not",
            "apply to that type are empty. Rows repeat by design -- a requirement",
            "covered by twenty cases shows up in twenty TEST_RUN rows.",
            "",
            "Record Type:",
            "  SUMMARY      counts per release, then a distinct total for the project",
            "  TEST_PLAN    every release, including those with nothing executed",
            "  REQUIREMENT  every requirement, its Jira link and the releases reaching it",
            "  MODULE       a Test Design folder holding no test case",
            "  TEST_CASE    every test case, in its module",
            "  TEST_RUN     every run, with release, cycle, suite, case and requirements",
            "",
            "Columns that change meaning with the row type:",
            "  Test Runs Count   SUMMARY and TEST_PLAN: runs under that release.",
            "                    TEST_CASE: runs executing that case; 0 means it was",
            "                    never scheduled.",
            "  Requirement IDs   TEST_CASE and TEST_RUN: the requirements linked.",
            "                    REQUIREMENT: the id of the requirement itself.",
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
