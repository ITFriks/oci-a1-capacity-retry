# Security

## Credentials stay outside this repository

- Authentication uses only the standard OCI configuration: `~/.oci/config`
  (Windows: `%USERPROFILE%\.oci\config`) and the API signing key it points to.
- This program never asks for, copies, prints or logs private key contents. It
  stores no OCI secrets in its own `config.toml`, which holds only non-secret
  settings such as the stack OCID.
- **Never commit** `~/.oci/config`, private keys (`*.pem`), `config.toml`, `.env`
  files or log files. `.gitignore` blocks the common names, but check
  `git status` before every commit anyway.
- Keep the key private: `chmod 600 ~/.oci/oci_api_key.pem` on Linux. `oci-a1-retry
  doctor` warns when the key file is readable by other users.
- Give the API user only what it needs, e.g. in the stack's compartment:
  `read orm-stacks`, `manage orm-jobs`, plus whatever the Terraform stack itself
  creates (instances, VNICs, ...).
- If a key may have leaked: delete it in OCI Console > Profile > API keys, create a
  new one, and update `~/.oci/config`.

## What the program can do in OCI

Only: read the stack, list/read its jobs and job logs, and create **Apply** jobs
for that one stack. It contains no code to create or update stacks, cancel jobs,
run Destroy, or touch instances or networking directly; a test enforces this.

Apply jobs are submitted with `AUTO_APPROVED`, i.e. without a manual plan review:
whatever the stack's current Terraform configuration says is applied. Review the
stack (e.g. run a Plan job in the console) before starting `run`.

## Local configuration is trusted

`config.toml` can set `success_command`, which runs through your shell. The file in
the *current directory* is read first, so do not run the tool from directories you
do not trust; the startup header shows the success command whenever one is set.

## Reporting a problem

This is a private personal project. Report issues directly to the repository owner.
