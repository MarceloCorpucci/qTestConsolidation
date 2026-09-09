"""Export one artifact of the source instance on its own.

The first half of the migration, at its smallest: read a single artifact and
write it to `migration/exported/`, following nothing it points at. What comes
out is the file the injection will later read.

    pytest src/tests/test_export_entity.py -s

Which artifacts to export is data, not code: `src/tests/input/export_entity.json`
lists them, and the test runs once per entry. Add an entry there to export
another artifact -- of any kind the exporter knows, not only requirements.

Skipped when `config/qtest.env` has no credentials, like every test that
talks to a live instance.
"""

import json
from pathlib import Path

import pytest

from main.exporter import DataExporter
from main.exporter.data_exporter import STANDALONE_SUFFIX

INPUT_FILE = Path(__file__).resolve().parent / "input" / "export_entity.json"


def artifacts_to_export():
    """The artifacts listed in the input file, read at collection time."""
    if not INPUT_FILE.is_file():
        return []
    return json.loads(INPUT_FILE.read_text(encoding="utf-8")).get("artifacts", [])


@pytest.fixture(
    params=artifacts_to_export(),
    ids=lambda entry: f"{entry['artifact_type']}-{entry['id']}",
)
def artifact_to_export(request):
    """One entry of the input file, feeding a run of the test."""
    return request.param


@pytest.mark.integration
def test_export_artifact_on_its_own(source_client, project_id, artifact_to_export):
    """The artifact comes back from HS and lands in migration/exported."""
    artifact_type = artifact_to_export["artifact_type"]
    artifact_id = artifact_to_export["id"]

    exporter = DataExporter(source_client)
    artifact = exporter.fetch_standalone(artifact_type, artifact_id, project_id)
    written = exporter.write_standalone(artifact, artifact_type, artifact_id)

    assert int(artifact["id"]) == int(artifact_id), "The response is for another artifact"
    assert artifact.get("name"), "The artifact came back with no name"

    assert written.is_file(), f"{written} was not written"
    assert written.name.endswith(f"_{artifact_id}_{STANDALONE_SUFFIX}.json")
    assert json.loads(written.read_text(encoding="utf-8")) == artifact, "The file lost data"

    expected = artifact_to_export.get("name")
    if expected and artifact["name"] != expected:
        print(f"\n  NOTE: named {artifact['name']!r} in HS, {expected!r} in the input file")

    print(f"\nExported {artifact_type} {artifact_id} -> {written}")
    print(f"  name  : {artifact['name']}")
    print(f"  fields: {sorted(artifact)}")
