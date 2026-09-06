"""Shared fixtures for the tests that talk to a live qTest instance.

Everything here resolves from `config/qtest.env`. When the settings are
missing -- any machine without access to the customer instances -- the tests
that depend on these fixtures are skipped, never failed.
"""

import logging
import os

import pytest
from dotenv import dotenv_values

from main.client import DEFAULT_ENV_FILE, MissingConfigurationError, RestClient
from main.logging_config import configure_logging

PROJECT_ID_ENV_VAR = "SOURCE_QTEST_PROJECT_ID"


@pytest.fixture(scope="session")
def source_client():
    """A client for the HS instance, or a skip when it is not configured."""
    configure_logging(logging.INFO)
    try:
        return RestClient.for_source()
    except MissingConfigurationError as error:
        pytest.skip(f"Source instance not configured: {error}")


@pytest.fixture(scope="session")
def project_id():
    """The HS project to read, taken from the `.env` settings."""
    values = {**dotenv_values(DEFAULT_ENV_FILE), **os.environ}
    configured = (values.get(PROJECT_ID_ENV_VAR) or "").strip()
    if not configured:
        pytest.skip(f"{PROJECT_ID_ENV_VAR} is not set in {DEFAULT_ENV_FILE}")
    return int(configured)
