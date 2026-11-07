# oci-a1-capacity-retry

Keeps re-submitting an **existing** OCI Resource Manager **Apply** job until it succeeds,
then stops. Built for `VM.Standard.A1.Flex` stacks that keep failing with:

```
Error: 500-InternalError, Out of host capacity.
```

Runs the same way on **Windows (PowerShell)** and **Linux**, written in Python with
Oracle's official [`oci`](https://pypi.org/project/oci/) SDK. No Cloud Shell, no bash
wrappers around the OCI CLI.

---

## Contents

1. [What it does](#what-it-does)
2. [Why it exists](#why-it-exists)
3. [Safety behaviour](#safety-behaviour)
4. [Requirements](#requirements)
5. [Create the stack](#create-the-stack)
6. [OCI authentication](#oci-authentication)
7. [Install on Windows](#install-on-windows)
8. [Install on Linux](#install-on-linux)
9. [Configuration](#configuration)
10. [Commands: doctor, status, once, run](#commands)
11. [Retry logic](#retry-logic)
12. [How success is detected](#how-success-is-detected)
13. [Stopping with Ctrl+C](#stopping-with-ctrlc)
14. [Windows: run at logon (Task Scheduler)](#windows-run-at-logon-task-scheduler)
15. [Linux: systemd user service](#linux-systemd-user-service)
16. [Troubleshooting](#troubleshooting)
17. [Security notes](#security-notes)
18. [Logs](#logs)
19. [Exit codes](#exit-codes)
20. [Limitations](#limitations)
21. [Development](#development)

---

## What it does

```
            +--------------------------------------------------------------+
            v                                                              |
 list the stack's jobs --> an Apply is already running? --yes--> monitor it |
            |                                                       |      |
            no                                                      v      |
            v                                               terminal state |
 latest Apply SUCCEEDED? --yes--> "Nothing to do", exit 0           |      |
            |                                                       |      |
            no                                                      |      |
            v                                                       |      |
 CreateJob (APPLY, AUTO_APPROVED) --429--> wait 5,10,20,40,60.. s   |      |
            |                                                       |      |
            +--------------------> poll every 15 s --------------->-+      |
                                                                    |      |
   SUCCEEDED  --> SUCCESS banner, notification, exit 0              |      |
   FAILED + "Out of host capacity" --> wait 300 s ------------------+------+
   FAILED for any other reason --> STOPPED banner + last 30 log lines, exit 1
```

1. Loads settings, authenticates with your normal OCI config (`~/.oci/config`).
2. Checks that the stack exists, is `ACTIVE` and accessible, and prints a summary.
3. Lists the stack's jobs. If an Apply job is `ACCEPTED`, `IN_PROGRESS` or `CANCELING`
   it **monitors that job** instead of creating another one.
4. Otherwise it submits an Apply job with `execution_plan_strategy = AUTO_APPROVED`.
5. Polls the job every 15 s until it finishes.
6. On `SUCCEEDED` it prints a big success banner and exits 0 - it never submits
   another Apply after success.
7. On `FAILED` it downloads the job log. If the log (or the job's failure details)
   contains `Out of host capacity` / `Out of capacity` (case-insensitive), it waits
   300 s and goes back to step 3. Any other failure stops the program.

## Why it exists

Always Free / PAYG A1 (Ampere) capacity is frequently exhausted in a region. Resource
Manager then fails the Apply with `500-InternalError, Out of host capacity`. Capacity
appears at random moments, so the only practical approach is to retry patiently -
without hammering the API, without creating duplicate jobs and without ever touching
the Terraform configuration.

## Safety behaviour

- **Only** reads the stack, lists/reads its jobs and job logs, and creates **Apply**
  jobs for that one existing stack. `doctor` also downloads the stack's Terraform
  configuration (read-only) to check the VM size. The SDK wrapper exposes nothing else;
  a unit test fails if destructive or stack-changing API calls appear anywhere in the source.
- Never creates or modifies a stack, never changes the Terraform configuration,
  shape, OCPUs or memory, never runs Destroy, never cancels or deletes anything.
  The Terraform stack is the single source of truth.
- Never creates a second Apply while one is running - not after a restart, not when
  started on a second computer (it attaches to the running job instead).
- Stops after success, and on the next start says "Nothing to do" (also when the
  success happened on another computer).
- Stops on any non-capacity failure and shows the end of the Terraform log.
- If the newest job on the stack is a **Destroy**, it refuses to re-create resources
  automatically (use `--force` if you really want that).
- Ctrl+C never cancels the OCI job.
- Even if two machines raced and both submitted an Apply, Terraform state prevents a
  second VM: the later Apply just finds nothing left to create.

## Requirements

- Python **3.11+** (Windows: from python.org with the `py` launcher; Linux: distro python3)
- An OCI API signing key configured in `~/.oci/config`
- IAM permissions for that user, in the stack's compartment:
  `read orm-stacks`, `manage orm-jobs`, plus whatever the stack creates
  (e.g. `manage instance-family`, `use virtual-network-family`).
- Optional: [pipx](https://pipx.pypa.io/) (recommended way to install CLI tools)

Only one runtime dependency: `oci`.

## Create the stack

`oci-a1-retry` does not build infrastructure. It re-runs **Apply** on a Resource
Manager stack that you create once. The simplest way needs no Terraform knowledge and
happens entirely in the OCI Console.

Before you start, select your **home region** (top right of the Console): Always Free
A1 instances can only be created there. Always Free covers **1,500 OCPU hours and
9,000 GB hours per month**, i.e. **2 OCPUs and 12 GB of memory in total** across all
your A1 instances (Oracle halved this in 2026 from 4 OCPUs / 24 GB). Check Oracle's
[Always Free Resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
page for the current numbers before choosing the VM size.

1. **Network (once).** Networking > Virtual cloud networks > **Start VCN Wizard** >
   *Create VCN with Internet Connectivity* > give it a name > Next > Create. This gives
   you a VCN with a public subnet. Skip this step if you already have one.
   Creating the network separately keeps the stack small: it then contains only the VM.
2. **Fill in the instance form, but do not click Create.** Compute > Instances >
   **Create instance**:
   - Name: anything, e.g. `a1-vm`.
   - Image and shape: **Change shape** > *Ampere* > `VM.Standard.A1.Flex`, choose the
     OCPUs and memory (2 OCPUs / 12 GB uses the whole Always Free allowance),
     then pick an image (Ubuntu, Oracle Linux, ...) that the Console offers for Ampere.
   - Networking: select the VCN and its **public** subnet from step 1, and keep
     *Assign a public IPv4 address* enabled.
   - SSH keys: paste or upload your public key, or let the Console generate a key pair
     and download both keys. You need this to log in to the VM later.
3. **Save as stack.** Instead of *Create*, click **Save as stack**. On the *Create
   stack* page give the stack any name (e.g. `my-a1-stack`), choose the compartment,
   click **Next** twice, then **Create**. If the last page offers *Run apply*, leave it
   unchecked: `oci-a1-retry` takes care of the applies.
4. **Copy the stack OCID.** Developer Services > Resource Manager > Stacks > your stack
   > *Stack information* > OCID > **Copy**, and put it in `config.toml`
   (see [Configuration](#configuration)).

Notes:

- Oracle may reclaim idle Always Free instances: an instance counts as idle when, over
  7 days, its 95th-percentile CPU, its network and (for A1) its memory utilization all
  stay below 20%.
- The program never edits the stack. To change the VM (shape, OCPUs, image), edit the
  stack in the Console, or create a new one. If the stack's last Apply had already
  succeeded, start the next run with `--force`.
- Already have Terraform files? Resource Manager > Stacks > **Create stack** >
  *My configuration*, then upload them as a folder or `.zip`.
- Oracle's guide: [Creating a Stack from a Resource Creation Page](https://docs.oracle.com/en-us/iaas/Content/ResourceManager/Tasks/create-stack-resource.htm).

## OCI authentication

The program uses the **standard** OCI SDK configuration and never stores credentials:

| OS      | OCI config file                 |
|---------|---------------------------------|
| Linux   | `~/.oci/config`                 |
| Windows | `%USERPROFILE%\.oci\config`     |

If you already use the OCI CLI on that computer, you are done. Otherwise:

1. OCI Console > profile icon (top right) > **My profile** > **API keys** > **Add API key**.
2. **Download private key**, save it as `~/.oci/oci_api_key.pem`
   (Windows: `%USERPROFILE%\.oci\oci_api_key.pem`). Linux: `chmod 600 ~/.oci/oci_api_key.pem`.
3. Click **Add**, copy the *Configuration file preview* into `~/.oci/config` and set
   `key_file` to the path from step 2:

   ```ini
   [DEFAULT]
   user=ocid1.user.oc1..<your user>
   fingerprint=<aa:bb:...>
   tenancy=ocid1.tenancy.oc1..<your tenancy>
   region=<your region, e.g. eu-frankfurt-1>
   key_file=~/.oci/oci_api_key.pem
   ```

   Do not leave comments such as `# TODO` at the end of a line: the OCI SDK reads
   them as part of the value.

   (With the OCI CLI installed, `oci setup config` does the same interactively.)
4. Check: `oci-a1-retry doctor`.

Use another profile with `--profile NAME` (or `profile = "NAME"` in config.toml),
another file with `--oci-config-file PATH`. If `~/.oci/config` does not exist, the SDK
falls back to the file named by `OCI_CONFIG_FILE`.

To use the same setup on several computers, copy `~/.oci/config` and the key file to
each one (or better: create one API key per computer - a user can have up to 3).

## Install on Windows

In **Windows PowerShell** or PowerShell 7:

```powershell
git clone https://github.com/ITFriks/oci-a1-capacity-retry.git
cd oci-a1-capacity-retry
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

`install.ps1` checks for Python 3.11+, installs with pipx if present (otherwise into a
private venv in `%LOCALAPPDATA%\oci-a1-capacity-retry\venv`), creates
`%APPDATA%\oci-a1-capacity-retry\config.toml` from the example, explains the OCI
setup and runs `doctor`. It needs no administrator rights and does not touch PATH.

Manual alternatives:

```powershell
py -m pip install --user pipx; py -m pipx ensurepath   # once, then reopen the terminal
pipx install .
# or plainly:
py -m pip install .
```

Start it (venv install; with pipx just type `oci-a1-retry run`):

```powershell
& "$env:LOCALAPPDATA\oci-a1-capacity-retry\venv\Scripts\oci-a1-retry.exe" run
```

## Install on Linux

```bash
git clone https://github.com/ITFriks/oci-a1-capacity-retry.git
cd oci-a1-capacity-retry
bash scripts/install.sh
```

`install.sh` finds Python 3.11+, uses pipx if available (otherwise a private venv in
`~/.local/share/oci-a1-capacity-retry/venv`), links `~/.local/bin/oci-a1-retry`,
creates `~/.config/oci-a1-capacity-retry/config.toml` from the example and runs
`doctor`. No sudo. If the venv step fails on Debian/Ubuntu: `sudo apt install python3-venv`.

Manual alternatives:

```bash
pipx install .            # recommended
python3 -m pip install .  # inside a virtualenv
```

## Configuration

Settings come from (highest priority first):

1. command-line options: `--stack-id`, `--region`, `--profile`, `--oci-config-file`, `--config`
2. environment variables `OCI_A1_<SETTING>`, e.g. `OCI_A1_STACK_OCID`, `OCI_A1_REGION`,
   `OCI_A1_PROFILE`, `OCI_A1_CAPACITY_RETRY_SECONDS`
3. `config.toml`
4. built-in defaults

`config.toml` is looked up in this order: `--config PATH`, `$OCI_A1_CONFIG`,
`./config.toml`, then the per-user location:

| OS      | config.toml                                          |
|---------|------------------------------------------------------|
| Linux   | `~/.config/oci-a1-capacity-retry/config.toml`        |
| Windows | `%APPDATA%\oci-a1-capacity-retry\config.toml`        |

**Set your stack OCID** (OCI Console > Developer Services > Resource Manager > Stacks >
your stack > copy OCID) in that file:

```toml
stack_ocid = "ocid1.ormstack.oc1.<region>.aaaa..."
region = ""            # empty = region of the OCI profile; or e.g. "eu-frankfurt-1"
profile = "DEFAULT"
capacity_retry_seconds = 300
job_poll_seconds = 15
throttle_initial_seconds = 5
throttle_max_seconds = 60
```

See [`config.example.toml`](config.example.toml) for all settings (log directory,
notifications, success command). Unknown keys and wrong types are rejected with a
clear message. `config.toml` is git-ignored - never commit it.

A `config.toml` in the current directory wins over the per-user one and can set
`success_command` (a shell command), so treat it like a script: do not run the tool
from untrusted directories. The startup header shows the success command if one is set.

Windows note: inside double quotes TOML treats `\` as an escape, so write paths in
single quotes: `log_dir = 'C:\Users\me\oci-logs'`. Notepad's UTF-8 BOM is fine.

### Phone notification on success (servers)

`success_command` runs once after a successful Apply, with `OCI_A1_JOB_ID` and
`OCI_A1_STACK_NAME` in its environment. Example with [ntfy](https://ntfy.sh) (free, no
account): install the ntfy app, subscribe to a hard-to-guess topic, and set

```toml
success_command = 'curl -s -m 20 -H "Title: OCI A1 VM created" -d "Apply succeeded for stack $OCI_A1_STACK_NAME" https://ntfy.sh/oci-a1-<random>'
```

Generate the random part with `openssl rand -hex 6`. Anyone who knows a ntfy topic name
can read it, so keep it unguessable. Test it once by running the `curl` command by hand.

## Commands

```
oci-a1-retry doctor   # read-only checks: Python, SDK, config, OCI profile/key, API auth,
                      # stack access, job listing, A1 size vs Always Free, log directory
oci-a1-retry status   # read-only: stack, recent jobs, what 'run' would do  (--json available)
oci-a1-retry once     # at most ONE Apply attempt; no waiting after "out of capacity"
oci-a1-retry run      # retry until the Apply succeeds
```

`doctor` reads the VM size from the stack's Terraform (the `shape_config` of each
`VM.Standard.A1.Flex` instance, including simple `var.x` values) and compares it with the
Always Free allowance of **2 OCPUs and 12 GB in total** (since June 2026). Above it, you get
a `[WARN]`: a free account's Apply would fail with a service limit error, and a Pay As You
Go account would be billed for the part above the allowance. It never stops you, because a
paid account may want a bigger VM. When the size can't be read (modules, `count`, computed
values) or the download is refused, `doctor` says so and you check the size yourself.
Other A1 instances in your tenancy use the same allowance.

Common options (after the command): `--config PATH`, `--stack-id OCID`, `--region`,
`--profile`, `--oci-config-file PATH`, `-v/--verbose` (debug output, full OCIDs).
`run` and `once` accept `--force` (see below). Running `oci-a1-retry` without a command
only shows help - starting a cloud retry loop should always be an explicit choice.

Sample `run`:

```
OCI A1 Capacity Retry
---------------------
Region:       eu-frankfurt-1
Stack:        my-a1-stack
Stack OCID:   ocid1.ormstack...q7x2la
Profile:      DEFAULT
Retry delay:  300 seconds
Mode:         run (retry until Apply succeeds)
Log file:     /home/me/.local/state/oci-a1-capacity-retry/oci-a1-retry.log

14:32:05 Creating Resource Manager Apply job...
14:32:06 Job accepted: ocid1.ormjob...k2l9qa (attempt #1)
14:32:21 Job IN_PROGRESS
14:33:36 Job FAILED
14:33:37 No A1 host capacity available.
14:33:37 Next capacity attempt in 300 seconds.
14:38:37 Creating Resource Manager Apply job...
14:38:38 Resource Manager returned HTTP 429 TooManyRequests.
14:38:38 Retrying CreateJob in 5 seconds.
14:38:43 Creating Resource Manager Apply job...
14:38:44 Job accepted: ocid1.ormjob...81bdzc (attempt #2)
14:38:59 Job IN_PROGRESS
14:41:14 Job SUCCEEDED
========================================
SUCCESS
OCI Resource Manager Apply succeeded.
The VM should now be created.
Retry loop stopped.
========================================
14:41:14 Successful job: ocid1.ormjob...81bdzc
```

Non-capacity failure:

```
14:32:36 Job FAILED
========================================
STOPPED
Apply failed for a reason other than host capacity.
========================================
14:32:37 Failure details: TERRAFORM_EXECUTION_ERROR Terraform apply failed
14:32:37 Last 4 log lines of job ocid1.ormjob...000001:
  | oci_core_instance.vm: Creating...
  | Error: 400-InvalidParameter, Invalid shape config: ocpus must be at least 1
  |   with oci_core_instance.vm,
  |   on main.tf line 12, in resource "oci_core_instance" "vm":
```

`status`:

```
OCI A1 Capacity Retry - status
------------------------------
Region:       eu-frankfurt-1
Profile:      DEFAULT
Stack:        my-a1-stack (ACTIVE)
Stack OCID:   ocid1.ormstack...q7x2la

Recent jobs (newest first):
  2026-10-06 12:00:00  APPLY     FAILED      ocid1.ormjob...k2l9qa
  2026-10-06 11:54:00  APPLY     FAILED      ocid1.ormjob...81bdzc

Next action:  'run' would submit a new Apply job.
```

## Retry logic

There is exactly **one** retry mechanism: the program's own. The OCI SDK's default
retry strategy (up to 8 attempts) and its circuit breaker are explicitly disabled
(`NoneRetryStrategy`, `NoCircuitBreakerStrategy`), so delays never stack up and timing
is predictable. A test asserts that one HTTP 429 results in exactly one HTTP request.

| Situation | Reaction |
|-----------|----------|
| Apply job FAILED with `Out of host capacity` / `Out of capacity` | wait `capacity_retry_seconds` (**300 s**), then a new Apply |
| CreateJob returns **HTTP 429** `TooManyRequests` | **5 -> 10 -> 20 -> 40 -> 60 -> 60 ...** s (cap `throttle_max_seconds` = 60), never fatal; resets to 5 s after a CreateJob succeeds |
| Timeouts, connection errors, HTTP 5xx, 429 on read calls | same 5 -> 60 s backoff; gives up (exit 5) after 60 consecutive failures (about 1 hour) |
| HTTP 409 on CreateJob (stack locked by another job) | re-check the jobs after 15 s and attach to the running job |
| HTTP 401 / 404 / other 4xx, bad config, missing key | stop immediately with an explanation (exit 2 or 3) |
| Apply FAILED for any other reason, or CANCELED | stop, show the last 30 log lines (exit 1) |
| A Plan/Destroy/... job is running on the stack | wait for it, then decide again |
| Job log not readable yet after FAILED | re-read up to 3 times, 15 s apart; if it stays unreadable and the failure details do not mention capacity, stop (safe default) |
| Unexpected internal error | clear one-line message (traceback in the log file), exit 1 |

A 429 is Resource Manager rate limiting, **not** a capacity signal, so it never triggers
the 300-second wait. Each CreateJob carries an `opc-retry-token`: if a request times
out after OCI already accepted it, the re-sent request returns the same job instead of
creating a second one. Before every CreateJob the job list is checked again, so a job
started meanwhile (by you, or by another computer) is attached to instead of duplicated.

## How success is detected

- **During a run:** the Apply job reaches `SUCCEEDED` -> success banner, terminal bell,
  optional desktop notification / `success_command`, exit 0.
- **At start-up and before every new attempt:** the stack's newest Apply/Destroy job is
  inspected. If it is a `SUCCEEDED` Apply - no matter which computer started it - the
  program prints `The latest Apply job succeeded. Nothing to do.` and exits 0.
- `--force` skips that check once and submits an Apply anyway (useful after you changed
  the stack's Terraform). A running job is still never duplicated, even with `--force`.

## Stopping with Ctrl+C

Press **Ctrl+C** at any time. The program prints `Retry loop stopped by user.` and
exits (code 130) without submitting anything else. A job that is already running in
OCI is **not** canceled - it finishes on its own, and the next start attaches to it
(or reports its result).

## Windows: run at logon (Task Scheduler)

Optional. Nothing is installed unless you run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-windows-task.ps1
```

It shows what it will do and asks for confirmation. The task belongs to your user
(no admin rights, no stored password), triggers at logon, and opens a **minimized**
console window running `oci-a1-retry run` - restore it from the taskbar to watch, or
press Ctrl+C in it to stop. After success, later logons just print "Nothing to do".

```powershell
Start-ScheduledTask -TaskName oci-a1-capacity-retry      # start now
Get-ScheduledTask   -TaskName oci-a1-capacity-retry      # inspect
powershell -ExecutionPolicy Bypass -File scripts\install-windows-task.ps1 -Uninstall
```

Options: `-ExePath`, `-ConfigPath`, `-Force`. If Task Scheduler refuses, the script
prints a Startup-folder shortcut alternative. Without any of this you can simply keep a
PowerShell window open with `oci-a1-retry run`.

## Linux: systemd user service

Optional, runs as your user (`systemctl --user`, no root):

```bash
bash scripts/install-systemd-user.sh          # asks first; installs + daemon-reload only
systemctl --user start oci-a1-capacity-retry  # start
systemctl --user status oci-a1-capacity-retry # state
journalctl --user -u oci-a1-capacity-retry -f # follow output
systemctl --user stop oci-a1-capacity-retry   # stop (the OCI job is not canceled)
systemctl --user enable oci-a1-capacity-retry # start at login
loginctl enable-linger "$USER"                # optional: keep running while logged out
```

Unit file: [`packaging/systemd/oci-a1-capacity-retry.service`](packaging/systemd/oci-a1-capacity-retry.service).
Exit 0 (success / nothing to do) leaves the service stopped; exits 1-3 (non-capacity
failure, configuration, authentication) are not restarted because they need you;
exit 5 (network trouble for an hour) is restarted after 5 minutes.

Manual install: copy the unit to `~/.config/systemd/user/`, adjust `ExecStart` if the
command is not in `~/.local/bin`, then `systemctl --user daemon-reload`.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `stack_ocid is not set` | edit `config.toml` (see [Configuration](#configuration)) or pass `--stack-id` |
| `OCI config file not found` | create `~/.oci/config` ([OCI authentication](#oci-authentication)) |
| `Profile [X] not found` | use `--profile` with a section name from `~/.oci/config` |
| `Cannot load the API signing key` | wrong `key_file` path, wrong file, or encrypted key without `pass_phrase` |
| `OCI rejected the API credentials (HTTP 401 ...)` | fingerprint/user/tenancy mismatch, public key not uploaded, or wrong system clock |
| `Stack or job not found, or not authorized (HTTP 404 ...)` | wrong stack OCID, wrong `region` (must be the stack's region), or missing IAM policy |
| Many `HTTP 429 TooManyRequests` | expected under load; it backs off to one request per 60 s. Do not run many copies |
| `STOPPED - Apply failed for a reason other than host capacity` | read the log tail, fix the stack in OCI, then `oci-a1-retry once` |
| `latest job on this stack is a DESTROY` | intentional safety stop; use `run --force` if you want the resources back |
| `Nothing to do` but you changed the Terraform | `oci-a1-retry run --force` |
| PowerShell: "running scripts is disabled" | use `powershell -ExecutionPolicy Bypass -File ...` as shown |
| `python -m venv` fails on Ubuntu | `sudo apt install python3-venv` (or use pipx) |

`oci-a1-retry doctor -v` and the log file give details (including the OCI
`opc-request-id`, useful for Oracle support).

## Security notes

- OCI credentials live only in the standard OCI config and key file - never in this
  repository or in `config.toml`. The program never prints or logs key contents, and a
  log filter additionally scrubs anything that looks like a private key or passphrase.
- `.gitignore` excludes `config.toml`, keys, `.env` files, logs and virtualenvs.
- Normal output shortens OCIDs (`ocid1.ormjob...k2l9qa`); `-v` and the log file show
  them in full for debugging.
- `success_command` runs through your shell; it comes only from your own config file.

More in [SECURITY.md](SECURITY.md).

## Logs

Console output plus a rotating log file (2 MB x 5 backups, about 12 MB at most):

| OS      | Log file |
|---------|----------|
| Linux   | `~/.local/state/oci-a1-capacity-retry/oci-a1-retry.log` (or `$XDG_STATE_HOME/...`) |
| Windows | `%LOCALAPPDATA%\oci-a1-capacity-retry\oci-a1-retry.log` |

Change it with `log_dir` / `OCI_A1_LOG_DIR`. The file always contains debug details
(timestamps, full job OCIDs, state changes, capacity failures, 429 events and backoff
delays, errors with request IDs). Under systemd the console output also goes to the
journal, without the program's own time prefix (the journal adds its own).

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Apply succeeded, or nothing to do (latest Apply already succeeded) |
| 1 | Stopped: Apply failed for a non-capacity reason, job canceled, Destroy guard, or unexpected internal error (`doctor`: problems found) |
| 2 | Configuration error / usage |
| 3 | OCI authentication, permission or other permanent API error |
| 4 | `once` only: the attempt failed with "out of capacity" |
| 5 | Gave up after ~1 hour of consecutive network / 5xx errors |
| 130 | Stopped by Ctrl+C |

## Limitations

- A failure whose log contains *both* a capacity error and another error is treated
  as a capacity failure (retried).
- Two computers that finish waiting at the same second can both submit an Apply.
  Resource Manager's stack lock and Terraform state keep this harmless (no second VM),
  but it is one extra job.
- Authentication: API-key profiles from the OCI config file only (no session tokens,
  instance principals or resource principals).
- On Windows, Ctrl+C during an HTTP request can take until that request finishes
  (at most about 60 s). `systemctl --user stop` / ending the task simply terminates
  the process; no job is affected either way.
- Previous-success detection looks at the stack's job history only; if you changed the
  stack's Terraform after a successful Apply, use `--force`.

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate      # Windows: py -m venv .venv; .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest --cov            # all OCI calls are faked; tests never touch a real tenancy
ruff check . && ruff format --check .
```

Layout:

```
src/oci_a1_retry/
  cli.py            argument parsing, exit codes, header output
  config.py         settings: defaults < config.toml < env < CLI, validation
  paths.py          per-user config/log directories (Windows + XDG)
  oci_client.py     SDK wrapper (read + create Apply only), error classification
  jobs.py           job/stack views and the pure "what next" decision
  runner.py         the retry loop
  retry.py          deterministic exponential backoff
  analysis.py       capacity detection and log tail
  doctor.py         'doctor' checks
  status.py         'status' output
  notify.py         optional bell / desktop notification / success command
  logging_setup.py  console + rotating file logging with key redaction
```

CI (GitHub Actions) runs lint and tests on Ubuntu and Windows with Python 3.11-3.13 and
exercises both installer scripts.
