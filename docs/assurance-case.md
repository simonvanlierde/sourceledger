---
title: What sourceledger's checks establish
description: The trust boundaries, safeguards, evidence and limits behind a sourceledger dataset.
---

# What sourceledger's checks establish

sourceledger helps a researcher connect a built file to a recorded download and notice
changes. Its checks establish consistency between files and records you trust. They do
not establish that a publisher's data is correct, that a record is authentic, or that you
have permission to publish the data.

This page explains the case for those checks. [Safe use](secure-use.md) gives the
instructions, and [architecture](architecture.md) explains the files and stages.

## What you must trust

Review the manifest and the code that runs it. The manifest chooses URLs, local files,
credentials by environment-variable name, reuse terms and optional checksum pins. A
custom reader is Python code with the same access as the process. Neither is sandboxed.

The working directory and the machine must be under your control. Another process that
can rewrite both data and records can replace the evidence. A checksum is not a signature.
Keep the manifest and fetch log in version control, and review changes to them.

Dataset recipients must obtain `datapackage.json` from a source they trust. An attacker
who replaces both that descriptor and its files can produce matching checksums.
[Software release attestations](releases.md) authenticate the sourceledger distribution;
they do not sign your dataset or its fetch records.

## Claims and evidence

The evidence below is executable regression coverage in
[`tests/test_ledger.py`](https://github.com/simonvanlierde/sourceledger/blob/main/tests/test_ledger.py).
It supports the stated behavior; it is not an independent security certification.

| Claim | Safeguard | Regression evidence |
| --- | --- | --- |
| A fixed source can be tied to expected bytes. | Fetch, build and validate check a declared SHA-256 pin; build also re-hashes the raw file against its record. | `test_fetch_refuses_bytes_that_miss_the_pinned_sha256`, `test_build_checks_the_pin_too`, `test_validate_rehashes_the_raw_file` |
| A revision can be detected without silently accepting an old output. | Fetch logs changed bytes. Validate compares output provenance with the current fetch record and checks package hashes. | `test_validate_catches_a_stale_data_package`, `test_the_whole_pipeline_runs_from_a_local_file` |
| A public server cannot redirect a direct connection into a private service. | The connection checks the actual peer before sending the redirected request. | `test_a_redirect_to_a_name_that_rebinds_inward_is_refused`, `test_the_peer_check_refuses_a_loopback_peer_over_tls` |
| Redirects do not forward manifest credentials to another origin. | A change of scheme, host or port drops the extra headers. Header values come from the environment and are omitted from records. | `test_a_redirect_to_another_host_drops_the_credentials`, `test_a_redirect_to_plain_http_on_the_same_host_drops_the_credentials`, `test_a_header_comes_from_the_environment` |
| Downloads have bounded body size and a checked time budget. | Streaming reads enforce `max_bytes`; socket waits and retry waits use the remaining `timeout_s`. Redirect bodies are closed without draining. | `test_fetch_refuses_a_download_over_the_size_cap`, `test_a_server_trickling_bytes_is_stopped_near_the_budget`, `test_source_timeout_includes_retry_waits`, `test_redirects_do_not_need_the_redirect_response_body` |
| A failed write does not expose a partially written replacement file. | A fresh temporary file is flushed and renamed into place. Fetch serializes writers with a lock, logs before writing the record and recovers a cut-off last log line. | `test_a_failed_write_raises_its_own_error_and_leaves_no_temp_file`, `test_a_cut_off_last_log_line_is_dropped_not_fatal`, `test_a_whole_last_log_line_without_its_newline_is_kept` |
| A recipient can check a package without fetching its sources. | Verify checks listed files against size and SHA-256, and refuses paths that escape the dataset folder, including through symlinks. | `test_verify_checks_a_published_dataset_against_its_package`, `test_verify_reports_a_package_it_cannot_check` |
| The same input records give repeatable build outputs. | Build writes no current timestamps into outputs or package metadata. | `test_the_same_records_build_the_same_package`, `test_the_package_is_written_exactly` |

## Where these claims stop

- **Network:** HTTPS uses Python's certificate checks. HTTP has no transport authenticity.
  The inward-redirect peer check is skipped through proxies; apply network restrictions
  at the proxy. Initial manifest URLs may deliberately point to internal or local files.
- **Resources:** DNS resolution and HTTP header parsing can exceed the fetch budget.
  The size cap is not a process-memory limit: JSON, Excel, gzipped CSV and custom readers
  can hold the data and parsed rows in memory. See [download behavior](fetching.md).
- **Writes:** Atomic replacement covers one file, not a whole fetch or build. A stopped
  run can leave files from different stages; run `validate` before using its output.
  Do not run build and fetch concurrently. These checks do not defend against a hostile
  local process changing files while they are being checked.
- **Meaning:** Validate checks recorded provenance and file integrity. It does not
  recompute the scientific result or prove that a custom reader used the input correctly.
  Domain checks belong in the consuming project's tests.
- **Permission:** Reuse terms and `redistribute` are declarations supplied by the manifest's
  author. Local builds may contain restricted files. Passing validation or verification
  grants no publication permission. Review the terms for both raw files and derived values.

## Check the evidence

Run `just check` for the tests, types, lint and dependency audit. Run `just py-mutation`
for the full mutation check against the recorded survivor baseline; coverage alone does
not show that every incorrect implementation would be detected.

The library uses the standard library at runtime; the optional Excel reader adds
openpyxl. Development tools are locked and audited. See the
[dependency policy](https://github.com/simonvanlierde/sourceledger/blob/main/.github/CONTRIBUTING.md#dependencies).
The release workflow tests the source and the built wheel before publishing.
[Release verification](releases.md) explains the attestations and reproducible-build check.

Report a suspected vulnerability through the
[private reporting process](https://github.com/simonvanlierde/sourceledger/security/policy).
