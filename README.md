# terragrunt-toolkit

A Python toolkit for running Terragrunt operations in parallel across multiple providers and environments.

## What is this?

If you manage Terraform infrastructure with Terragrunt across multiple environments (dev, stage, prod) and many modules, you've probably hit this problem: running `terragrunt plan` or `terragrunt apply` one provider at a time is slow and error-prone, especially when a single change touches 10–20 providers.

This toolkit wraps Terragrunt commands to:
- Run plans and applies **in parallel** across all matching providers
- Organize output by **JIRA ticket** so you can track what changed and when
- **Detect affected providers automatically** from a git branch diff
- Respect **dependency ordering** on apply (dependencies applied before dependents)
- Enforce **production safety checks** before applying

## When would I use this?

- You have a standard Terragrunt project with a `providers/` directory tree (dev/stage/prod × N modules)
- You want to plan all affected providers in one command and store the output for review
- You want to apply plans in the right order without manually tracking which modules depend on which
- You need a record of what was planned/applied per JIRA ticket

## Scripts

| Script | Description |
|--------|-------------|
| `terragrunt_plan_all` | Run terragrunt plans in parallel across providers |
| `terragrunt_apply_all` | Apply saved plans from a ticket directory |
| `terragrunt_unlock_all` | Remove stale S3 state lock files |
| `terragrunt_format` | Filter and clean up terragrunt plan output |

## Requirements

- Python 3.8 or higher
- Terragrunt installed and in PATH
- Terraform installed and in PATH
- Git (for project detection and branch-based planning)
- boto3 (for S3 unlock functionality only)

## Installation

```bash
# Install dependencies
pip install -r requirements.txt

# Make scripts executable (Unix/macOS)
chmod +x terragrunt_plan_all terragrunt_apply_all terragrunt_unlock_all terragrunt_format

# Run directly
./terragrunt_plan_all --help
```

## Quick Start

```bash
# Plan all providers affected by your changes (auto-detects ticket from branch name)
./terragrunt_plan_all INFRA-1234 modules/my-service

# Plan based on git diff from main branch
./terragrunt_plan_all --from-branch main

# Apply all plans for a ticket
./terragrunt_apply_all INFRA-1234

# List stale S3 lock files (dry run)
./terragrunt_unlock_all -b my-tfstate-bucket --dry-run

# Filter/clean plan output
cat plan.txt | ./terragrunt_format
```

---

## Project Structure

These scripts expect a standard Terragrunt layout:

```
project-root/
├── modules/              # Terraform modules
│   └── my-service/
│       └── *.tf
├── providers/            # Terragrunt provider configurations
│   ├── dev/
│   │   └── us-east-1/
│   │       └── my-service/
│   │           └── terragrunt.hcl
│   └── prod/
│       └── us-east-1/
│           └── my-service/
│               └── terragrunt.hcl
└── tickets/              # Plan outputs (created automatically)
```

## Output Structure

Plans and applies are stored by ticket:

```
tickets/
└── INFRA-1234/
    ├── plan_all_report.txt           # Plan summary
    └── providers/
        └── prod/
            └── us-east-1/
                └── my-service/
                    ├── my-service.txt        # Plan output
                    └── my-service.txt.apply  # Apply output
```

## Auto-Detection

All scripts automatically detect:

1. **Ticket from git branch**: Branch named `INFRA-1234-my-feature` → ticket is `INFRA-1234`
2. **Ticket from directory**: If you're inside `tickets/INFRA-1234/`, ticket is auto-detected
3. **Module/provider from directory**: If you're inside a provider or module directory, it's auto-detected

## JIRA Ticket Format

Tickets must follow the format `INFRA-<number>` (e.g., `INFRA-1234`). The `test` prefix is also accepted for local testing (e.g., `test-1`).

To use a different prefix, update `JIRA_PREFIXES` in [lib/project.py](lib/project.py#L87).

---

## terragrunt_plan_all

Run terragrunt plans in parallel across multiple providers/modules.

### Usage

```
terragrunt_plan_all [OPTIONS] [TICKET] [paths...]
```

### Arguments

| Argument | Description |
|----------|-------------|
| `TICKET` | JIRA ticket number (e.g., `INFRA-1234`) |
| `paths...` | Provider, module, or variable file paths to plan |

### Options

#### Execution Control

| Option | Description |
|--------|-------------|
| `--dry-run` | Preview what plans would run without executing |
| `--clean` | Remove previous plan directory before starting |
| `--keep-empty` | Keep plans even if they show no changes |
| `-j N, --concurrency N` | Number of parallel plans (default: 5) |
| `--destroy` | Generate destroy plans |
| `--stop-on-error` | Stop after first plan failure |
| `--unmask-output` | Show sensitive info in plan output |

#### Terraform/Terragrunt Control

| Option | Description |
|--------|-------------|
| `--refresh` | Force refresh before plan |
| `--no-refresh` | Skip refresh before plan (default) |
| `--cache` | Keep terragrunt cache (default) |
| `--no-cache` | Clear terragrunt cache before plan |

#### Replan Options

| Option | Description |
|--------|-------------|
| `--replan all` | Rerun all previous plans |
| `--replan changes` | Rerun only plans that had changes |
| `--replan errors` | Rerun only failed plans |
| `--replan none` | Just regenerate summary |

#### Git Integration

| Option | Description |
|--------|-------------|
| `--from-branch BRANCH` | Plan only providers affected by git diff from BRANCH |

#### Filtering

| Option | Description |
|--------|-------------|
| `-f, --filter REGEX` | Include only providers matching pattern |
| `-F, --exclude-filter REGEX` | Exclude providers matching pattern |
| `--dev` | Shortcut for `-f /dev/` |
| `--prod` | Shortcut for `-F /dev/` |
| `--limit-per-module N` | Max plans per top-level module |
| `--tags-only` | Keep only plans with tag changes |

#### Variables and Targets

| Option | Description |
|--------|-------------|
| `-v, --var KEY=VALUE` | Pass variable to terraform plan |
| `-t, --target VALUE` | Target specific resource |

### Examples

```bash
# Plan a specific module across all environments
./terragrunt_plan_all INFRA-1234 modules/my-service

# Plan only dev environment
./terragrunt_plan_all INFRA-1234 modules/my-service --dev

# Plan based on git changes vs main
./terragrunt_plan_all --from-branch main

# Replan only failed plans
./terragrunt_plan_all INFRA-1234 --replan errors

# Run 10 plans in parallel
./terragrunt_plan_all INFRA-1234 modules/my-service -j 10

# Destroy plan for a specific provider
./terragrunt_plan_all INFRA-1234 providers/dev/us-east-1/old-service --destroy
```

---

## terragrunt_apply_all

Apply saved terraform plans from a ticket directory with safety checks and dependency-aware ordering.

### Usage

```
terragrunt_apply_all [OPTIONS] [TICKET]
```

### Options

| Option | Description |
|--------|-------------|
| `-j N, --concurrency N` | Number of parallel applies (default: 2) |
| `--stop-on-error` | Stop after first apply failure |
| `--retry TYPE` | Retry previous applies: `all`, `errors`, or `none` |
| `--run-twice` | Run apply twice (useful for eventual consistency) |
| `--refresh` | Force refresh before apply |
| `--no-refresh` | Skip refresh before apply (default) |
| `--cache` | Keep terragrunt cache (default) |
| `--no-cache` | Clear terragrunt cache before apply |
| `--force` | Allow production applies while not on main branch |
| `--allow-destroy` | Allow destroy plans to run |
| `--dry-run` | Preview what would be applied |
| `--verbose` | Enable verbose output |

### Safety Features

- **Production branch check**: Warns if applying prod changes while not on main/master
- **Uncommitted changes check**: Warns if there are uncommitted git changes
- **Destroy confirmation**: Requires `--allow-destroy` flag plus interactive confirmation
- **Dependency ordering**: Reads `dependency` blocks in terragrunt.hcl and applies providers in waves (dependencies first)

### Examples

```bash
# Apply all plans for a ticket
./terragrunt_apply_all INFRA-1234

# Retry only failed applies
./terragrunt_apply_all INFRA-1234 --retry errors

# Allow destroy plans (requires confirmation)
./terragrunt_apply_all INFRA-1234 --allow-destroy

# Force prod apply from a feature branch
./terragrunt_apply_all INFRA-1234 --force

# Preview what would be applied
./terragrunt_apply_all INFRA-1234 --dry-run
```

---

## terragrunt_unlock_all

Remove stale Terraform S3 state lock files (`.tflock` files created by `use_lockfile = true`).

### Usage

```
terragrunt_unlock_all [OPTIONS]
```

### Options

| Option | Description |
|--------|-------------|
| `-b, --bucket BUCKET` | S3 bucket name (required) |
| `--prefix PREFIX` | S3 key prefix to filter by |
| `-p, --profile PROFILE` | AWS profile to use |
| `-r, --region REGION` | AWS region |
| `-u, --user USER` | Filter by lock owner (default: current user, use `all` for all users) |
| `-a, --age DURATION` | Filter locks older than specified age (e.g., `6h`, `24h`, `7d`) |
| `-j N, --concurrency N` | Parallel delete operations (default: 5) |
| `--confirm` | Delete locks without prompting |
| `--dry-run` | Show locks but do not delete them |
| `--verbose` | Enable verbose output |

### Duration Format

Durations can use: `w` (weeks), `d` (days), `h` (hours), `m` (minutes), `s` (seconds)

Examples: `6h`, `24h`, `7d`, `1d12h`

### Examples

```bash
# List all your lock files (dry run)
./terragrunt_unlock_all -b my-tfstate-bucket --dry-run

# List locks for a specific environment
./terragrunt_unlock_all -b my-tfstate-bucket --prefix env/prod/ --dry-run

# List locks from all users
./terragrunt_unlock_all -b my-tfstate-bucket -u all --dry-run

# Remove locks older than 24 hours
./terragrunt_unlock_all -b my-tfstate-bucket --age 24h --confirm

# Remove only your own locks with a specific AWS profile
./terragrunt_unlock_all -b my-tfstate-bucket -p myprofile --confirm
```

---

## terragrunt_format

Filter and clean up terragrunt plan output by removing noise lines.

### Usage

```
terragrunt_format [FILE]
cat plan.txt | terragrunt_format
```

### Options

| Option | Description |
|--------|-------------|
| `FILE` | Input file (default: stdin) |
| `-o, --output FILE` | Output file (default: stdout) |

### What It Does

Strips terraform/terragrunt noise: lock acquire/release messages, "Refreshing state", "This plan was saved to:", and other boilerplate. Collapses multiple blank lines and duplicate `---` separators. Counts "no change" plans and shows a summary at the end.

### Examples

```bash
# Filter a saved plan file
./terragrunt_format plan_output.txt

# Filter from stdin
cat plan.txt | ./terragrunt_format

# Filter and save to file
./terragrunt_format plan.txt -o filtered.txt

# Filter live terragrunt output
terragrunt plan-all 2>&1 | ./terragrunt_format
```

---

## Troubleshooting

### "Ticket required" error

The ticket must be either:
- Passed as an argument: `./terragrunt_plan_all INFRA-1234 ...`
- Present in the branch name: `INFRA-1234-my-feature`
- Present in the current directory path: `tickets/INFRA-1234/`

### Plans running slowly

- Increase concurrency: `-j 10`
- Use `--no-refresh` to skip state refresh
- Use filters to narrow scope: `-f us-east-1`

### "text file busy" errors on macOS

Add to `~/.terraformrc`:
```hcl
plugin_cache_may_break_dependency_lock_file = true
```

### boto3 not found

```bash
pip install boto3
```

---

## License

MIT
