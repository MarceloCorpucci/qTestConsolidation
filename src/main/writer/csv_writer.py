"""Consolidated traceability report as CSV files.

`CsvWriter` takes the report built by `DataExporter.build_traceability` and
writes one file per table: the traceability rows at test-run grain, a
per-release summary, the requirements with the releases covering them, and the
Test Design tree. A CSV holds a single table, so what would be four sheets of
a workbook becomes four files, plus a text file carrying the notes a reader
needs to trust the numbers.

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

#: Prefix shared by every file of the report.
DEFAULT_PREFIX = "qtest"

CSV_EXTENSION = ".csv"

#: Written with a BOM so Excel opens the files as UTF-8 and keeps accents.
CSV_ENCODING = "utf-8-sig"

#: Columns of the traceability table: header and key in the report row.
TRACEABILITY_COLUMNS = (
    ("Release ID", "release_id"),
    ("Release", "release"),
    ("Test Cycle", "cycle"),
    ("Test Suite", "suite"),
    ("Test Run ID", "run_id"),
    ("Test Run", "run"),
    ("Test Case ID", "case_id"),
    ("Test Case", "case"),
    ("Module", "module"),
    ("Test Case Jira", "case_jira"),
    ("Requirement IDs", "requirement_ids"),
    ("Requirements", "requirements"),
    ("Requirement Jira", "requirement_jira"),
)

SUMMARY_HEADERS = (
    "Release ID",
    "Release",
    "Test runs",
    "Test cases",
    "Requirements",
    "Requirements with Jira",
)

REQUIREMENT_HEADERS = (
    "Requirement ID",
    "Requirement",
    "Jira",
    "Linked test cases",
    "Releases covering it",
)

TEST_DESIGN_HEADERS = ("Module ID", "Module", "Test Case ID", "Test Case", "Jira")


class CsvWriter:
    """Writes a consolidated traceability report as a set of CSV files."""

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

    def write(self, prefix: str = DEFAULT_PREFIX) -> dict[str, Path]:
        """Write every table and return the files, by table name."""
        logger.info("Writing the traceability report to %s", self._output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

        written = {
            "traceability": self.write_traceability(prefix),
            "summary": self.write_summary(prefix),
            "requirements": self.write_requirements(prefix),
            "test_design": self.write_test_design(prefix),
            "notes": self.write_notes(prefix),
        }

        logger.info("Report complete: %s files written", len(written))
        return written

    def write_traceability(self, prefix: str = DEFAULT_PREFIX) -> Path:
        """Every test run, with the chain leading from its release to a requirement."""
        rows = [
            [row.get(key, "") for _header, key in TRACEABILITY_COLUMNS]
            for row in self._report["rows"]
        ]
        return self._write_table(
            f"{prefix}_traceability",
            [header for header, _key in TRACEABILITY_COLUMNS],
            rows,
        )

    def write_summary(self, prefix: str = DEFAULT_PREFIX) -> Path:
        """One row per release, with what hangs from it.

        The counts are values, not formulas: a CSV cannot carry a formula, so
        they are computed here and do not follow later edits of the other files.
        """
        rows = self._report["rows"]
        table = []

        for release in sorted({row["release"] for row in rows}):
            held = [row for row in rows if row["release"] == release]
            table.append([
                held[0]["release_id"] if held else "",
                release,
                len(held),
                self._distinct(held, "case_id"),
                self._distinct_requirements(held),
                self._distinct_requirements(held, with_jira=True),
            ])

        # Totals of the distinct columns are counted over every row, not summed
        # down the column: a case or requirement reached by two releases would
        # otherwise be counted twice.
        table.append([
            "",
            "Total (distinct)",
            len(rows),
            self._distinct(rows, "case_id"),
            self._distinct_requirements(rows),
            self._distinct_requirements(rows, with_jira=True),
        ])
        return self._write_table(f"{prefix}_summary", SUMMARY_HEADERS, table)

    def write_requirements(self, prefix: str = DEFAULT_PREFIX) -> Path:
        """Requirements, their Jira link, and the releases reaching them."""
        links = self._report["links"]
        rows = self._report["rows"]
        table = []

        for requirement in self._report["requirements"]:
            requirement_id = requirement.get("id")
            covering = sorted(
                {
                    row["release"]
                    for row in rows
                    if str(requirement_id) in self._split(row["requirement_ids"])
                }
            )
            table.append([
                requirement_id,
                requirement.get("name") or "",
                requirement.get("jira") or "",
                sum(1 for linked in links.values() if requirement_id in linked),
                ", ".join(covering),
            ])

        return self._write_table(f"{prefix}_requirements_coverage", REQUIREMENT_HEADERS, table)

    def write_test_design(self, prefix: str = DEFAULT_PREFIX) -> Path:
        """The module tree with its test cases, empty folders included."""
        by_module: dict[Any, list[dict[str, Any]]] = {}
        for case in self._report["test_cases"]:
            by_module.setdefault(case.get("module_id"), []).append(case)

        table = []
        for module in self._report["modules"]:
            module_id = module.get("id")
            held = by_module.get(module_id, [])

            if not held:
                table.append([module_id, module.get("name") or "", "", "(empty)", ""])
                continue

            table.extend(
                [
                    module_id,
                    module.get("name") or "",
                    case.get("id"),
                    case.get("name") or "",
                    case.get("jira") or "",
                ]
                for case in held
            )

        return self._write_table(f"{prefix}_test_design", TEST_DESIGN_HEADERS, table)

    def write_notes(self, prefix: str = DEFAULT_PREFIX) -> Path:
        """What the reader needs to know to trust the numbers.

        Kept out of the CSV files on purpose: a comment header would break the
        tools the CSV format was chosen for.
        """
        target = self._output_dir / f"{prefix}_report_notes.txt"
        logger.info("Writing the report notes to %s", target)

        target.write_text("\n".join(self._notes()) + "\n", encoding="utf-8")
        return target

    # -- writing -------------------------------------------------------------

    def _write_table(self, file_name: str, headers: Any, rows: list[list[Any]]) -> Path:
        """Write one table, quoting whatever needs it."""
        target = self._resolve_path(file_name)
        logger.info("Writing %s rows to %s", len(rows), target)

        with target.open("w", encoding=CSV_ENCODING, newline="") as csv_file:
            writer = csv.writer(csv_file, delimiter=self._delimiter, quoting=csv.QUOTE_MINIMAL)
            writer.writerow(headers)
            writer.writerows(rows)

        logger.info("Wrote %s (%s bytes)", target.name, target.stat().st_size)
        return target

    def _resolve_path(self, file_name: str) -> Path:
        name = (file_name or "").strip()
        if not name:
            logger.error("Cannot resolve the target path: file_name is missing")
            raise ValueError("file_name is required")
        if not name.lower().endswith(CSV_EXTENSION):
            name = f"{name}{CSV_EXTENSION}"
        return self._output_dir / name

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
            "qTest has no release-to-requirement relation: coverage is derived along",
            "Release > Test Cycle > Test Suite > Test Run > Test Case > Requirement.",
            "A requirement is therefore tied to a release only when a test run",
            "executes it through that chain.",
            links_note,
            "",
            "Files:",
            "  _traceability.csv        one row per test run, the whole chain",
            "  _summary.csv             per release: runs, and distinct cases and requirements",
            "  _requirements_coverage.csv  per requirement: Jira link and releases reaching it",
            "  _test_design.csv         the module tree with its cases, empty folders included",
            "",
            "Counts in the summary are computed values, not formulas: a CSV carries no",
            "formulas, so they do not follow later edits of the other files. Test cases",
            "and requirements are counted distinct, as the same one repeats across runs.",
            "The total row counts distinct over the whole project, so it is smaller than",
            "the sum of the column whenever a case or requirement belongs to two releases.",
        ]

    # -- report readers ------------------------------------------------------

    @staticmethod
    def _distinct(rows: list[dict[str, Any]], key: str) -> int:
        return len({row[key] for row in rows if row[key] not in ("", None)})

    @staticmethod
    def _split(value: str) -> set[str]:
        return {part.strip() for part in (value or "").split(",") if part.strip()}

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
