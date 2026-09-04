"""Integration check: `fetch_project` brings real data back from HS.

This test talks to the live source instance, so it only runs where
`config/qtest.env` carries real credentials -- the client VDI. On a machine
without access it is skipped, never failed.

Run it with the request log visible:

    pytest -m integration -s
"""

import logging
import os

import pytest
from dotenv import dotenv_values

from main.client import DEFAULT_ENV_FILE, MissingConfigurationError, RestClient
from main.exporter import DataExporter
from main.logging_config import configure_logging

PROJECT_ID_VAR = "SOURCE_QTEST_PROJECT_ID"


@pytest.fixture(scope="module")
def source_client():
    """A client for the HS instance, or a skip when it is not configured."""
    configure_logging(logging.INFO)
    try:
        return RestClient.for_source()
    except MissingConfigurationError as error:
        pytest.skip(f"Source instance not configured: {error}")


@pytest.fixture(scope="module")
def project_id():
    """The HS project to read, taken from the `.env` settings."""
    values = {**dotenv_values(DEFAULT_ENV_FILE), **os.environ}
    configured = (values.get(PROJECT_ID_VAR) or "").strip()
    if not configured:
        pytest.skip(f"{PROJECT_ID_VAR} is not set in {DEFAULT_ENV_FILE}")
    return int(configured)


@pytest.mark.integration
def test_fetch_project_returns_data(source_client, project_id):
    """The source instance answers with the requested project."""
    project = DataExporter(source_client).fetch_project(project_id)

    assert project, f"Project {project_id} came back empty"
    assert int(project["id"]) == project_id, "The response is for a different project"
    assert project.get("name"), "The project carries no name"

    print(f"\nProject {project_id} fetched from {source_client.base_url}")
    print(f"  name  : {project['name']}")
    print(f"  fields: {sorted(project)}")


@pytest.mark.integration
def test_list_projects_returns_data(source_client):
    """The token can see at least one project in the source instance."""
    projects = DataExporter(source_client).list_projects()

    assert projects, "The source instance returned no projects for this token"

    print(f"\n{len(projects)} projects visible in {source_client.base_url}")
    for project in projects:
        print(f"  {project.get('id')}: {project.get('name')}")
