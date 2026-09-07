"""Consolidated traceability report as an Excel workbook.

`ExcelWriter` takes the report built by `DataExporter.build_traceability` and
lays it out in four sheets: a per-release summary, the traceability rows at
test-run grain, the requirements with the releases covering them, and the Test
Design tree.

It computes nothing about the instance: every relation comes from the report.
Temporary, like the inventory it presents.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

logger = logging.getLogger(__name__)

#: Project root, where the report is written.
PROJECT_ROOT = Path(__file__).resolve().parents[3]

DEFAULT_REPORT_FILE = "qtest_traceability"
XLSX_EXTENSION = ".xlsx"

FONT_NAME = "Arial"
HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(name=FONT_NAME, size=10, bold=True, color="FFFFFF")
BODY_FONT = Font(name=FONT_NAME, size=10)
TITLE_FONT = Font(name=FONT_NAME, size=12, bold=True)
NOTE_FONT = Font(name=FONT_NAME, size=9, italic=True)

TRACEABILITY_SHEET = "Traceability"

#: Columns of the traceability sheet: header, key in the report row, width.
TRACEABILITY_COLUMNS = (
    ("Release ID", "release_id", 12),
    ("Release", "release", 28),
    ("Test Cycle", "cycle", 30),
    ("Test Suite", "suite", 28),
    ("Test Run ID", "run_id", 12),
    ("Test Run", "run", 34),
    ("Test Case ID", "case_id", 13),
    ("Test Case", "case", 40),
    ("Module", "module", 38),
    ("Test Case Jira", "case_jira", 22),
    ("Requirement IDs", "requirement_ids", 18),
    ("Requirements", "requirements", 40),
    ("Requirement Jira", "requirement_jira", 26),
)


class ExcelWriter:
    """Writes a consolidated traceability report to an `.xlsx` file."""

    def __init__(self, report: dict[str, Any], output_dir: Path | str = PROJECT_ROOT) -> None:
        self.report = report
        self._output_dir = Path(output_dir)
        logger.debug("Excel writer ready, output folder: %s", self._output_dir)

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
        """Folder the workbook is written to."""
        return self._output_dir

    # -- workbook ------------------------------------------------------------

    def write(self, file_name: str = DEFAULT_REPORT_FILE) -> Path:
        """Build the workbook and return the file written."""
        target = self._resolve_path(file_name)
        logger.info("Writing the traceability report to %s", target)

        workbook = Workbook()
        workbook.remove(workbook.active)

        self._write_summary(workbook)
        self._write_traceability(workbook)
        self._write_requirements(workbook)
        self._write_test_design(workbook)

        self._output_dir.mkdir(parents=True, exist_ok=True)
        workbook.save(target)

        logger.info("Wrote %s (%s bytes)", target.name, target.stat().st_size)
        return target

    def _resolve_path(self, file_name: str) -> Path:
        name = (file_name or "").strip()
        if not name:
            logger.error("Cannot resolve the target path: file_name is missing")
            raise ValueError("file_name is required")
        if not name.lower().endswith(XLSX_EXTENSION):
            name = f"{name}{XLSX_EXTENSION}"
        return self._output_dir / name

    # -- sheets --------------------------------------------------------------

    def _write_summary(self, workbook: Workbook) -> None:
        """One row per release, with what hangs from it."""
        sheet = workbook.create_sheet("Summary")
        rows = self._report["rows"]

        sheet["A1"] = f"qTest consolidated traceability - project {self._report['project_id']}"
        sheet["A1"].font = TITLE_FONT
        for offset, note in enumerate(self._notes(), start=2):
            sheet.cell(row=offset, column=1, value=note).font = NOTE_FONT

        header_row = len(self._notes()) + 3
        headers = (
            ("Release ID", 12),
            ("Release", 30),
            ("Test runs", 11),
            ("Test cases", 12),
            ("Requirements", 14),
            ("Requirements with Jira", 22),
        )
        self._write_header(sheet, header_row, headers)

        releases = self._release_names(rows)
        for index, release in enumerate(releases):
            row = header_row + 1 + index
            held = [entry for entry in rows if entry["release"] == release]
            sheet.cell(row=row, column=1, value=self._release_id(held))
            sheet.cell(row=row, column=2, value=release)
            # Counted by formula, so the number follows the rows if they are filtered or edited.
            sheet.cell(
                row=row,
                column=3,
                value=f"=COUNTIF({TRACEABILITY_SHEET}!$B:$B,$B{row})",
            )
            # De-duplicated in Python: a distinct count is not expressible as a
            # plain formula, and the same case or requirement repeats across runs.
            sheet.cell(row=row, column=4, value=self._distinct(held, "case_id"))
            sheet.cell(row=row, column=5, value=self._distinct_requirements(held))
            sheet.cell(row=row, column=6, value=self._distinct_requirements(held, with_jira=True))

        total_row = header_row + 1 + len(releases)
        sheet.cell(row=total_row, column=2, value="Total").font = Font(
            name=FONT_NAME, size=10, bold=True
        )
        for column in range(3, 7):
            letter = get_column_letter(column)
            cell = sheet.cell(
                row=total_row,
                column=column,
                value=f"=SUM({letter}{header_row + 1}:{letter}{total_row - 1})",
            )
            cell.font = Font(name=FONT_NAME, size=10, bold=True)

        self._style_body(sheet, header_row + 1, total_row, len(headers))
        sheet.freeze_panes = sheet.cell(row=header_row + 1, column=1)

    def _write_traceability(self, workbook: Workbook) -> None:
        """Every test run, with the chain leading from its release to a requirement."""
        sheet = workbook.create_sheet(TRACEABILITY_SHEET)
        self._write_header(sheet, 1, [(header, width) for header, _, width in TRACEABILITY_COLUMNS])

        for index, row in enumerate(self._report["rows"], start=2):
            for column, (_, key, _width) in enumerate(TRACEABILITY_COLUMNS, start=1):
                sheet.cell(row=index, column=column, value=row.get(key))

        last_row = len(self._report["rows"]) + 1
        self._style_body(sheet, 2, last_row, len(TRACEABILITY_COLUMNS))
        sheet.freeze_panes = "A2"
        if last_row > 1:
            sheet.auto_filter.ref = f"A1:{get_column_letter(len(TRACEABILITY_COLUMNS))}{last_row}"

    def _write_requirements(self, workbook: Workbook) -> None:
        """Requirements, their Jira link, and the releases reaching them."""
        sheet = workbook.create_sheet("Requirements")
        headers = (
            ("Requirement ID", 15),
            ("Requirement", 45),
            ("Jira", 26),
            ("Linked test cases", 17),
            ("Releases covering it", 45),
        )
        self._write_header(sheet, 1, headers)

        links = self._report["links"]
        rows = self._report["rows"]

        for index, requirement in enumerate(self._report["requirements"], start=2):
            requirement_id = requirement.get("id")
            covering = sorted(
                {
                    row["release"]
                    for row in rows
                    if str(requirement_id) in self._split(row["requirement_ids"])
                }
            )
            linked_cases = sum(
                1 for linked in links.values() if requirement_id in linked
            )

            sheet.cell(row=index, column=1, value=requirement_id)
            sheet.cell(row=index, column=2, value=requirement.get("name"))
            sheet.cell(row=index, column=3, value=requirement.get("jira"))
            sheet.cell(row=index, column=4, value=linked_cases)
            sheet.cell(row=index, column=5, value=", ".join(covering))

        last_row = len(self._report["requirements"]) + 1
        self._style_body(sheet, 2, last_row, len(headers))
        sheet.freeze_panes = "A2"

    def _write_test_design(self, workbook: Workbook) -> None:
        """The module tree with its test cases, empty folders included."""
        sheet = workbook.create_sheet("Test Design")
        headers = (
            ("Module ID", 11),
            ("Module", 45),
            ("Test Case ID", 13),
            ("Test Case", 45),
            ("Jira", 26),
        )
        self._write_header(sheet, 1, headers)

        by_module: dict[Any, list[dict[str, Any]]] = {}
        for case in self._report["test_cases"]:
            by_module.setdefault(case.get("module_id"), []).append(case)

        row = 2
        for module in self._report["modules"]:
            module_id = module.get("id")
            held = by_module.get(module_id, [])

            if not held:
                sheet.cell(row=row, column=1, value=module_id)
                sheet.cell(row=row, column=2, value=module.get("name"))
                sheet.cell(row=row, column=4, value="(empty)")
                row += 1
                continue

            for case in held:
                sheet.cell(row=row, column=1, value=module_id)
                sheet.cell(row=row, column=2, value=module.get("name"))
                sheet.cell(row=row, column=3, value=case.get("id"))
                sheet.cell(row=row, column=4, value=case.get("name"))
                sheet.cell(row=row, column=5, value=case.get("jira"))
                row += 1

        self._style_body(sheet, 2, row - 1, len(headers))
        sheet.freeze_panes = "A2"

    # -- layout helpers ------------------------------------------------------

    @staticmethod
    def _write_header(sheet: Any, row: int, headers: Any) -> None:
        for column, (header, width) in enumerate(headers, start=1):
            cell = sheet.cell(row=row, column=column, value=header)
            cell.font = HEADER_FONT
            cell.fill = HEADER_FILL
            cell.alignment = Alignment(vertical="center")
            sheet.column_dimensions[get_column_letter(column)].width = width

    @staticmethod
    def _style_body(sheet: Any, first_row: int, last_row: int, columns: int) -> None:
        for row in range(first_row, last_row + 1):
            for column in range(1, columns + 1):
                cell = sheet.cell(row=row, column=column)
                if cell.font is not BODY_FONT and not cell.font.bold:
                    cell.font = BODY_FONT

    def _notes(self) -> list[str]:
        """What the reader needs to know to trust the numbers."""
        links_note = (
            "Requirement columns come from the test case to requirement links."
            if self._report["links_available"]
            else "WARNING: the case-to-requirement links could not be read, so the "
            "requirement columns are empty."
        )
        return [
            f"Source: {self._report['base_url']}, "
            f"generated {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            "qTest has no release-to-requirement relation: coverage is derived along "
            "Release > Test Cycle > Test Suite > Test Run > Test Case > Requirement.",
            links_note,
            "Test runs are counted by formula; test cases and requirements are distinct "
            "counts de-duplicated before writing, as the same one repeats across runs.",
        ]

    # -- report readers ------------------------------------------------------

    @staticmethod
    def _release_names(rows: list[dict[str, Any]]) -> list[str]:
        return sorted({row["release"] for row in rows})

    @staticmethod
    def _release_id(held: list[dict[str, Any]]) -> Any:
        return held[0]["release_id"] if held else ""

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
