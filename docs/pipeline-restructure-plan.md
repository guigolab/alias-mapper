# Pipeline restructure: orchestrator plan + Actions artifacts

**Status: proposed, not yet built.** Implement in the `guigolab` fork once
Actions is enabled there. Do not implement in `Max25R/alias-mapper`; the
fork is the canonical repo going forward.

This is Emilio's suggestion from the v2 discussion: use GitHub Actions
artifacts to handle the summary-retrieval logic once instead of in every
shard. It is a CI/pipeline change only. The published Release artifacts
(`aliases.tsv.gz`, `historical.tsv.gz`, `failures.tsv`) and the CLI are
unaffected.

## Motivation

The sharded workflow currently streams and filters NCBI's four
`assembly_summary` files more than necessary. `scripts/collect_aliases.py`
runs the whole Stream + Plan phase (download the four summaries, filter to
`version_status=latest` + allowed levels + eukaryotic groups, join
GenBank/RefSeq pairs) inside *every* shard so each can compute the plan and
take its `plan[shard::num_shards]` slice, and again in the catalog job for
historical output. With 8 shards that is the summary retrieval and planning
happening ~9 times per run. Streaming + planning once and sharing the result
removes that, and is exactly what Actions artifacts are for.

## Current shape

Job graph in `full-alias-update-sharded.yml`:

- **setup** turns `num_shards` (default 8, with a `|| '8'` fallback) into a
  matrix index list.
- **catalog** runs `collect_aliases.py --historical-only`: builds the plan,
  writes `historical.tsv.gz`, exits before any per-assembly fetch.
- **shard** (matrix, N jobs) each run `collect_aliases.py --shard i
  --num-shards N --skip-historical`: build the full plan, stride to their
  slice, fetch per-assembly via `ftp_path`, apply the adaptive cap, write a
  shard TSV.
- **merge** downloads the shard TSVs, concatenates, runs the Scaffold-presence
  and build-verify gates, and publishes the Release.

Relevant `collect_aliases.py` flags already present: `--num-shards`,
`--shard` (strided `plan[shard::num_shards]`), `--skip-historical`,
`--levels`, `--historical-only`.

Worth noting: the shard-to-merge handoff already uses
`upload-artifact`/`download-artifact` (matrix jobs do not share a
filesystem). So artifacts are already the transport for pipeline *outputs*;
this change extends the same mechanism to the *input* side.

## Proposed shape

Add one orchestrator step and have the shards consume its output.

- **plan** (extends the current catalog job): stream the four summaries once,
  build the plan, and upload two artifacts — the **work manifest** and
  `historical.tsv.gz` (already planner output, so it belongs here).
- **shard** (matrix, N): `needs: plan`. Download the manifest artifact,
  stride to the `i::N` slice, fetch per-assembly, apply the adaptive cap,
  upload the shard TSV. No summary access at all.
- **merge**: unchanged in spirit — download the shard TSVs and the historical
  artifact, concatenate, run the existing gates, publish.

## The manifest

The manifest is the serialized plan: one row per planned assembly, carrying
exactly the fields the fetch and row-writing phases consume. That is the
`AssemblyPlanEntry` dataclass, verbatim:

```
genbank_acc            assembly-level GenBank accession (GCA_*)
refseq_acc             paired RefSeq accession, or empty
assembly_name
taxid
organism_name
group
assembly_level
ftp_path               used to build the assembly_report.txt URL
genome_size            for the adaptive coverage cap, or empty
genome_size_ungapped   for genome_coverage_ungapped_pct, or empty
```

A TSV with a header row, to match the rest of the pipeline. Empty cell means
`None` on read-back for the three nullable fields (`refseq_acc`,
`genome_size`, `genome_size_ungapped`), parsed with the existing
`_to_int_or_none` helper for the two sizes. ~48k rows of metadata compress
to a few MB, well within artifact limits.

The manifest is already level-filtered (the `--levels` selection is applied
when it is built), so shards do **not** pass `--levels`. That removes a real
failure mode: today a shard launched with a mismatched `--levels` would
silently plan a different population than its siblings.

## Code changes in `collect_aliases.py`

Split the Plan phase from the Fetch phase behind two new flags, leaving the
existing all-in-one path intact:

- `--emit-plan <path>`: build the plan once, serialize it to the manifest at
  `<path>`, write `historical.tsv.gz` (unless `--skip-historical`), and exit
  before any per-assembly fetch. This is the orchestrator entry point; it
  supersedes `--historical-only` (which can either be folded into this flag
  or kept for the manual fallback workflow).
- `--plan <path>`: read the manifest into the `AssemblyPlanEntry` list,
  **skip** `build_assembly_plan` entirely, stride to `--shard i
  --num-shards N`, fetch, and write the shard TSV. Since no summaries are
  read, the summary-related options (`--levels`, `--no-cache`, `--cache-*`)
  do not apply in this mode and should be rejected or ignored with a note.

The all-in-one mode (no `--plan`/`--emit-plan`) stays for local dev and the
manual Chromosome+ fallback (`alias-tsv-update.yml`), so nothing existing
breaks.

## Workflow YAML changes (`full-alias-update-sharded.yml`)

- **catalog → plan**: run `--emit-plan plan.tsv.gz --historical-output
  historical.tsv.gz --levels "Complete Genome,Chromosome,Scaffold"
  --no-cache`. Upload `plan.tsv.gz` and `historical.tsv.gz` as artifacts.
- **shard**: add `needs: plan` and a `download-artifact` for `plan.tsv.gz`.
  Run `--plan plan.tsv.gz --shard ${{ matrix.index }} --num-shards N
  --skip-historical -o shard-${{ matrix.index }}.tsv.gz`. Upload the shard
  TSV artifact as today.
- **merge**: download the shard artifacts plus the historical artifact;
  otherwise unchanged.
- Preserve the `|| '8'` `num_shards` fallback from the earlier fix.

## Why this is worth doing

- **NCBI summary load drops ~9x to 1x.** One stream + plan per run instead of
  one per shard plus catalog.
- **Snapshot consistency (a correctness fix, not just efficiency).** Today
  each shard streams the summaries at a slightly different wall-clock moment.
  If NCBI updates the summary files mid-run, shards can plan against
  different snapshots and produce gaps or overlaps. One shared plan snapshot
  removes that race.
- **Fail fast.** A planning failure now fails the run before 8 fetch jobs are
  spawned, instead of failing one shard and leaving a silent gap.
- **Less per-shard compute.** Shards no longer re-run the filter/pair work.

## Tradeoffs

- The plan job is a sequential step before the shards start (streaming +
  planning, a few minutes), but it removes that same work from all N shards,
  so wall-clock is roughly neutral.
- All shards depend on `plan`. That is the fail-fast property above, framed as
  a cost.
- Actions-artifact retention and auth are not a concern here: the manifest is
  consumed inside the same run by downstream jobs, which is precisely what
  run-scoped artifacts are for. The public dataset still goes out as a
  Release, unchanged.

## On Emilio's "prebuilt image of docker (or artifacts)"

These solve different problems. A prebuilt container image pins the job
environment and skips per-job dependency install; Actions artifacts share the
retrieved data between jobs. The summary-retrieval-sharing win is the artifact
path, so that is what this plan uses. A prebuilt image is a separate, later
optimization, worth it only if shard cold-start (dependency install) becomes a
measurable share of run time.

## Future-API framing

This splits the cheap catalog tier (Stream + Plan, the manifest) from the
expensive per-molecule tier (Fetch) into distinct jobs with distinct
artifacts. That is the same two-cadence split the future hosted API wants
(catalog vs. alias queries against separate backing stores), so the
restructure is a step toward that seam, not only a CI cleanup.

## Open questions to confirm with Emilio / at implementation

- Manifest format: TSV (lean) vs JSON. TSV matches the pipeline and reuses the
  existing helpers.
- Whether to also upload the raw summaries as a debug artifact. Lean: no, the
  manifest is sufficient; add only if a debugging need appears.
- Whether `--emit-plan` fully replaces `--historical-only` or the two
  coexist (keep `--historical-only` if the manual fallback still wants
  historical-without-manifest).
