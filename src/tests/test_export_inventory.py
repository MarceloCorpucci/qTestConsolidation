"""One-shot inventory of the source project: test plans, test cases, test runs.

Running this test is the simplest way to produce the three tracking files at
the project root -- it exercises every inventory function in one go:

    pytest src/tests/test_export_inventory.py -s

Like the other integration tests it is skipped when `config/qtest.env` has no
credentials. Temporary, same as the code it drives: both go away once the
inventory has been taken.
"""

import pytest

from main.exporter import DataExporter
from main.exporter.data_exporter import STANDALONE_ENTITIES, TEST_DESIGN_FILE


def count_entries(path):
    """Entities listed in an inventory file, ignoring the header lines."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return len([line for line in lines if line and not line.startswith("#")])


@pytest.mark.integration
def test_export_inventory_writes_every_entity(source_client, project_id):
    """Every inventory endpoint answers and lands in its own file."""
    written = DataExporter(source_client).export_inventory(project_id)

    assert set(written) == {TEST_DESIGN_FILE, *STANDALONE_ENTITIES}, "Some entity was not exported"

    print(f"\nInventory of project {project_id} in {source_client.base_url}")
    for entity, path in written.items():
        assert path.is_file(), f"{path} was not written"
        entries = count_entries(path)
        flag = "  <- empty, check the endpoint against the qTest UI" if not entries else ""
        print(f"  {entity:<12} {entries:>6} entries  -> {path}{flag}")
