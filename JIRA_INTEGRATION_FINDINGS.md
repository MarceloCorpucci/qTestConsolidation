# Jira References in the Source Project — Findings

**Instance:** `healthspring.qtestnet.com` · **Project:** 24901 · **Date:** 18 September 2026

Everything below was measured through the qTest REST API, artifact by
artifact. No figure is an estimate.

## Verdict

**No artifact of this project is tied to a Jira issue by an integration.**
Test cases, test runs, test cycles, test suites, releases, modules and
defects carry nothing a Jira connection would have written.

The only Jira references that exist are on **requirements**, and even there
they are not integration metadata: they are URLs a person pasted into a
description, and names that echo an issue key.

## What was evaluated

| Element | How it was checked | Jira reference found |
|---|---|---|
| **Test cases** (266) | Integration fields — `external_id`, `external_system`, `jira_id`, `jira_key` — plus every custom field and `web_url` | **None** |
| **Test cases** — free text | Name, description, precondition and custom field values, searched for Jira URLs and for tokens shaped like an issue key | **None**: 0 URLs, 0 issue keys |
| **Test cases** — what they link to | `linked-artifacts` for all 266: 43 links to requirements, 75 to test runs | **No defect, no Jira object** |
| **Test cases** — hidden fields | One case fetched on its own, compared with the listing | Only `agent_ids` is withheld, and it holds no reference |
| **Test steps** | The steps of 10 of the 266 cases | **None** — one false positive, see below |
| **Defects** | The project's defect listing | **The project holds no defect at all** |
| **Test runs** (75) | Walked from every release, cycle and suite | No Jira field on any of them |
| **Test cycles, suites, releases, modules** | Read in full during the inventory | No Jira field on any of them |
| **Requirements** (510) | Integration fields, custom fields, descriptions | **The one exception — see below** |

### The one false positive worth knowing

One test step links to
`http://www.qasymphony.com/platform/jira-integration.html`. That is qTest's
own marketing page about its Jira integration — qaSymphony is the vendor of
qTest. It matched only because the address contains the word "jira". It is
not a reference to an issue.

## The exception: requirements

Of the 510 requirements:

| | Count |
|---|---|
| Carrying a **structured** integration field | **0** |
| Carrying a Jira URL inside the **description text** | 11 |
| …of those, pointing at a real Jira host | 8 |
| …of those, pointing at documentation rather than an issue | 3 |
| **Named** like an issue key, all `MSTQ-####` | 42 |
| Both named like a key and carrying a URL | 2 |

So even the requirements carry no integration metadata. What they carry is
free text: someone pasted a link, and some requirements were named after the
issue they came from. Neither will resynchronise in another instance.

The eight real links point at **two different Jira instances**:

| Jira host | Links |
|---|---|
| `jira.healthspring-jira-prod.aws.zilverton.com` | 6 |
| `jira.express-scripts.com` | 2 |

## What this proves, and what it does not

**It proves** that nothing in this project depends on a Jira connection to be
readable, and that migrating it does not require Jira to be migrated first.
Every artifact stands on its own.

**It does not prove that the integration is switched off.** A connection that
was configured but never used would leave exactly this: no references, and no
defects. Whether Jira is connected to this project, and whether it should be
connected to the target, is a question for whoever administers the instance —
it is a setting, not data, and it is not visible through the API.

## What was not examined

| Not examined | Why it matters, or does not |
|---|---|
| Test steps of the remaining 256 test cases | 10 were read as a sample. Reading all of them is one setting away |
| Links of the 75 test runs, individually | A defect hangs off an execution, not off a test case. The project holding **zero** defects already covers this; the per-run check is written and pending a run |
| Attachments | A document could mention a ticket, but that is not a reference the migration has to preserve |
| The project's integration settings | Not reachable through the API, and not accessible with the permissions available |

## How these figures can be reproduced

```bash
pytest src/tests/inventory/test_diagnose_jira_references.py -s   # the table above
pytest src/tests/inventory/test_export_inventory.py -s           # the counts per artifact
```

The first prints six sections, one per row of the table. Each of its
detectors was first shown to fire against planted data — a check that never
fires proves nothing — and only then run against the instance.
