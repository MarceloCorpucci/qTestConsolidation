# qTestConsolidation

Project to migrate artifacts (test plans, test cases, test runs, requirements,
etc.) between two Tricentis qTest instances, via the REST API.

## Structure

```
qTestConsolidation/
├── requirements.txt
├── config/           # environment, user and credential definitions
├── src/
│   ├── main/         # production code (API clients, extraction, injection)
│   └── tests/        # validation tests (pytest)
│       ├── inventory/    # the tests that took stock of the source instance
│       ├── input/        # what the tests read: which artifacts to work on
│       └── output/       # what they produce
│           └── inventory/    # the reference the migration is tracked
│                             # against, committed on purpose
└── migration/        # migration payloads in transit
    ├── exported/     # artifacts read out of the source instance
    ├── imported/     # payloads pending injection into the target
    └── id_map.json   # what each source artifact became in the target
```

## Setup

```bash
python -m venv .venv
source .venv/bin/activate        # on Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Configuration

Connection settings live in `config/` as `.env` files (flat `KEY=VALUE` pairs).
Copy the template and fill in the real values:

```bash
cp config/qtest.example.env config/qtest.env
```

`config/qtest.env` holds credentials and is git-ignored; only the
`*.example.env` templates are committed. Both instances share a single file,
distinguished by the `SOURCE_` and `TARGET_` prefixes: `SOURCE_*` is the HS
instance (migration origin) and `TARGET_*` is the HCSC instance (destination).

## Logging

Every module logs through `logging.getLogger(__name__)` and configures nothing
by itself. Entry-point scripts turn logging on once:

```python
import logging
from main.logging_config import configure_logging

configure_logging(logging.INFO)                      # console
configure_logging(logging.DEBUG, "migration.log")    # console + file
```

`INFO` reports the migration steps (requests issued, responses received, files
written). `DEBUG` adds the internal traces. Credentials are never logged.

## Running the tests

```bash
pytest src/tests
```
