"""Inject one exported artifact into the target instance.

The second half of the migration, at its smallest: read what the export
wrote, create it in HCSC, and record the id the target gave it.

    pytest src/tests/test_import_entity.py -s

Which artifacts to inject is data, not code: `src/tests/input/import_entity.json`
lists them, and the test runs once per entry. Each one must already have been
exported -- run `test_export_entity.py` first, or the test says which file is
missing.

Skipped until the target instance is configured, which it is not while the
proof-of-concept project is still being created.
"""

import json
from pathlib import Path

import pytest

from main.artifacts import standalone_file_name
from main.importer import CREATABLE_FIELDS, DataImporter

INPUT_FILE = Path(__file__).resolve().parent / "input" / "import_entity.json"


def artifacts_to_import():
    """The artifacts listed in the input file, read at collection time."""
    if not INPUT_FILE.is_file():
        return []
    return json.loads(INPUT_FILE.read_text(encoding="utf-8")).get("artifacts", [])


@pytest.fixture(
    params=artifacts_to_import(),
    ids=lambda entry: f"{entry['artifact_type']}-{entry['id']}",
)
def artifact_to_import(request):
    """One entry of the input file, feeding a run of the test."""
    return request.param


@pytest.mark.integration
def test_import_artifact_on_its_own(target_client, target_project_id, artifact_to_import):
    """The exported artifact is created in HCSC and its new id recorded."""
    artifact_type = artifact_to_import["artifact_type"]
    source_id = artifact_to_import["id"]

    importer = DataImporter(target_client)
    kind = importer.resolve_artifact_type(artifact_type)
    exported_file = importer.exported_dir / f"{standalone_file_name(kind, source_id)}.json"

    if not exported_file.is_file():
        pytest.skip(f"{exported_file} does not exist: export the {kind} first")

    exported = json.loads(exported_file.read_text(encoding="utf-8"))
    created = importer.import_standalone(artifact_type, source_id, target_project_id)

    assert created.get("id"), "The target returned no id"
    assert created.get("name") == exported.get("name"), "The target renamed the artifact"

    # What the source numbered is never forwarded: the target numbers its own.
    recorded = importer.target_id_of(artifact_type, source_id)
    assert recorded == created["id"], "The id map does not point at what was created"

    print(f"\nImported {kind} {source_id} -> {created['id']} in {target_client.base_url}")
    print(f"  name         : {created.get('name')}")
    print(f"  carried over : {sorted(CREATABLE_FIELDS[kind])}")
    print(f"  id map       : {importer.id_map_file}")


@pytest.mark.integration
def test_import_reports_what_was_not_exported(target_client, target_project_id):
    """Asking for an artifact that was never exported says so, and says which file."""
    importer = DataImporter(target_client)

    with pytest.raises(FileNotFoundError, match="stand-alone"):
        importer.import_standalone("requirement", 0, target_project_id)
