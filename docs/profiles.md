# Project graph profiles

One graph rarely fits a real repository. A monorepo with a large vendored
dependency tree can produce a graph where >90% of nodes describe code you do
not own — structurally valid, useless for product-architecture questions, and
the failure is silent. Profiles make the fix a first-class, committed part of
the repository: split the corpus into named graphs (for example `product` and
`vendor`), give each its own output directory, and gate trust with
`graphify fitness`.

## Quick start (consumer repository)

Commit a `graphify.toml` at the repository root:

```toml
default_profile = "product"

# optional: split packages below a shared top-level dir for the
# cross-package-edges metric ("packages/web" vs "packages/api").
# Top-level key: it must appear BEFORE any [table] section.
package_roots = ["packages"]

[profiles.product]
out = "graphify-product"
description = "first-party code only"
exclude = ["third_party/", "extern/"]

[profiles.product.fitness]
max_vendor_share_fail = 0.30   # this graph exists to exclude vendor code

[profiles.vendor]
out = "graphify-vendor"         # must be a RELATIVE path inside the repo:
                                # absolute paths and ".." are rejected with a
                                # warning (graphify.toml is repo-author input)
kind = "vendor"                 # exempt from vendor-dominance FAIL
description = "vendored dependencies, opt-in for upstream investigations"

[ownership]
# EXTENDS the built-in generic patterns (node_modules, vendor, dist, tests,
# ...) with repo-specific ones - defaults are always preserved.
vendor = ["third_party/**", "extern/**"]
```

Then:

```bash
graphify profiles          # list profiles; shows default/active markers
graphify fitness           # score the default profile's graph
graphify fitness --profile vendor
graphify fitness --json    # machine-readable, for CI or agent policies
graphify fitness --strict  # LOW confidence also exits 1
```

## How the output directory is resolved

Highest to lowest precedence:

1. explicit `--graph` / `--out` on the command
2. explicit `--profile <name>` (a typo here is a hard error, never a silent
   fallback)
3. `GRAPHIFY_OUT` environment variable (existing contract, unchanged)
4. `GRAPHIFY_PROFILE` environment variable naming a profile
5. `default_profile` in `graphify.toml`
6. the legacy literal `graphify-out`

A repository with no `graphify.toml` sees zero behavior change.

Because most build commands resolve the directory at process start, select a
profile for BUILDS via the environment (`GRAPHIFY_PROFILE=product graphify
update .`) or by committing `default_profile`. AI-assistant hook installs can
pin it per project, e.g. Claude Code `.claude/settings.json`:

```json
{ "env": { "GRAPHIFY_PROFILE": "product" } }
```

## Fitness verdicts

`graphify fitness` reports: total nodes/edges, ownership shares (vendor /
generated / test / first-party, computed over classified nodes; nodes without
a `source_file` are counted separately as `unclassified` and never inflate
first-party share), community-label coverage, cross-package edges, graph file
size, directed status (`true` / `false` / `unknown` for older graphs), and
interpreter-sidecar health.

- **FAIL** (exit 1): graph missing/unparseable, or vendor share above
  `max_vendor_share_fail` on a profile not declared `kind = "vendor"`.
- **LOW** (exit 0; exit 1 with `--strict`): vendor share above
  `max_vendor_share_low`, a configured `min_*` threshold unmet, or an unknown
  directed flag.
- **PASS**: everything else. Informational notes (placeholder community
  labels, unclassified nodes, interpreter sidecar state) never change the
  verdict.

After every clustered build **in a repository that has a `graphify.toml`**,
graphify also prints a one-line vendor-dominance advisory when the fresh
graph crosses the LOW threshold — the noise problem surfaces the moment it is
created, not the first time an answer looks wrong. The advisory is a cheap
sampled estimate (it never re-reads oversized graphs and never runs in repos
without a config file, keeping legacy builds byte-identical); the exact
numbers come from `graphify fitness`.

## Hook-guard integration

The PreToolUse hook nudges (Claude Code and compatible assistants) name the
**resolved** graph path — with the config above they say
`graphify-product/graph.json exists...`, so agents are steered to the graph
the project actually uses.

## v1 limitations (documented deliberately)

- `include` patterns parse and validate but are **not yet wired into the
  scan**; use `exclude` (wired through the same anchored channel as
  `--exclude`, winning over ignore files) or `.graphifyinclude`.
- Build commands accept profile selection via environment or
  `default_profile`; `--profile` as a per-command flag is currently
  implemented on `fitness` only.
- Install-time assistant templates (the generated CLAUDE.md/AGENTS.md
  sections) still print the literal `graphify-out/` paths; the runtime hook
  nudges are profile-aware. Pin the env var per project as shown above.
- Ownership classification is computed post-hoc from node `source_file`
  paths; nodes are not tagged at build time.
