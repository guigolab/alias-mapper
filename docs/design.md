# alias-mapper — Design

A command-line tool that rewrites the chromosome / scaffold names in
bioinformatics input files (GFF, GTF, FASTA) from one naming convention
to another. The translation uses a precomputed alias table built from
NCBI assembly reports.

The problem it solves: research files from different sources use
different naming conventions for the same sequences (e.g. `chr1`,
`NC_000001.11`, `CM000663.2`, and `1` can all refer to the same human
chromosome). Files using different conventions can't be used together
without translation.

## Scope

**Shipped today:**

- File formats: GFF, GTF, FASTA, plain or gzipped (`.gz` detected by
  content on read, by extension on write)
- Single-file convert, plus multi-file convert driven by a reference
  FASTA (`convert --fasta REF [ANN ...]`): the alias table is loaded
  once for the whole batch
- Naming conventions: GenBank, RefSeq, UCSC, Sequence-Name,
  Assigned-Molecule
- Source convention and assembly auto-detected from the input;
  overridable via flags
- Lookup falls back to conservative name normalizations on an exact
  miss: UCSC `.N`↔`vN` version-separator swap, and ENA `ENA|…|ACC`
  header prefix strip
- Pipeline streams NCBI's four `assembly_summary` files, publishes
  `aliases.tsv.gz` + `historical.tsv.gz` + `failures.tsv` as GitHub
  Release assets. The weekly run produces the full eukaryotic set
  (~48k assemblies, all levels) via a sharded matrix workflow; a
  single-job Chromosome+ workflow (~16k) is kept as a manual fallback
- Client builds the local SQLite cache from the TSV on first run,
  keeping the TSV for offline schema rebuilds
- Schema-versioned cache: stale-schema caches rebuild silently when
  the CLI is upgraded

**On the horizon:**

- Issue #5 (suppressed-accession error messages) wired into the CLI
  using the already-shipped `historical.tsv.gz`
- Hosted query API as the eventual replacement for the local DB

## Command-line interface

```
# single file
alias-mapper convert <input> --to <convention> -o <output> [options]

# multi-file: conform annotations to the reference FASTA's convention
alias-mapper convert --fasta <ref> [<ann> ...] --out-dir <dir> [options]

# multi-file: force the FASTA and annotations to one convention
alias-mapper convert --fasta <ref> [<ann> ...] --overwrite-to <convention> --out-dir <dir>
```

### Examples

```
# Auto-detect source convention and assembly
alias-mapper convert annotations.gff --to ucsc -o annotations.ucsc.gff

# Source convention specified explicitly
alias-mapper convert annotations.gff --from refseq --to ucsc -o out.gff

# Restrict lookup to a specific assembly
alias-mapper convert annotations.gff --assembly GCF_000001405.40 \
    --to ucsc -o out.gff

# Multi-file conform: rewrite the annotations to match ref.fa's own
# convention; ref.fa is left untouched
alias-mapper convert --fasta ref.fa peaks.gff genes.gtf --out-dir out/

# Multi-file overwrite: force ref.fa and both annotations to UCSC
alias-mapper convert --fasta ref.fa peaks.gff genes.gtf \
    --overwrite-to ucsc --out-dir out/
```

### Flags

| Flag             | Mode        | Purpose                                                        |
| ---------------- | ----------- | -------------------------------------------------------------- |
| `--to`           | single-file | Target naming convention (required in single-file mode)        |
| `-o / --output`  | single-file | Output path                                                    |
| `--fasta`        | multi-file  | Reference FASTA; enables multi-file mode                       |
| `--overwrite-to` | multi-file  | Force the FASTA and all annotations to this convention         |
| `--out-dir`      | multi-file  | Output directory; each input written as `<stem>.<conv>.<ext>`  |
| `--from`         | both        | Source convention. Auto-detected if absent. Not used in conform mode |
| `--assembly`     | both        | Restrict lookup to one assembly. Auto-detected if absent       |
| `--alias-db`     | both        | Path to the alias SQLite database                              |

In multi-file mode, omitting `--overwrite-to` selects **conform** mode
(annotations conform to the FASTA's own convention; the FASTA is left
unchanged). `--to` is single-file only; in multi-file mode it errors
with a pointer to `--overwrite-to`.

## Design decisions

### Source convention: auto-detect, overridable

Inputs may come from collaborators or public databases where the user
doesn't know the source convention with certainty. Auto-detection
samples up to 50 unique sequence names from the input and scores them
against each convention column in the alias DB; the highest-scoring
match wins.

If no clear winner emerges (fewer than 5 absolute matches, or winner
fails to beat the runner-up by ≥2×), detection refuses to commit and
the user must supply `--from` explicitly.

Not supported: per-line resolution for files that mix conventions
across rows. One convention per file.

### Assembly scope: auto-detect, overridable

A sequence name like `1` is ambiguous across species. The tool scopes
its lookup to a single assembly, chosen by the same scoring trick used
for convention detection: for each assembly, count how many sample
names match any of its naming columns, take the assembly with the most
matches. Same confidence rule applies.

### Unmapped sequence names: warn and pass through

When a sequence name isn't in the alias table, the row is kept
unchanged in the output and a warning is emitted to stderr.

Silent skipping risks data loss in downstream pipelines. Strict
erroring is too aggressive for common cases (a handful of unknown
contigs shouldn't kill the run). Pass-through preserves data while
making issues visible.

`--strict` (error on any unmapped name) and `--skip-unknown` (drop
unmapped rows) are not currently implemented but could be added
without changing the default behaviour.

### Fallback name resolution

A bare exact lookup misses names that are the *same* identifier in a
different surface form. On a miss (and only on a miss, so the common
path stays a single dict lookup), `formats/_resolve.resolve_alias`
tries a small set of conservative normalizations and retries:

- **Version separator** (`.N` ↔ `vN`): UCSC writes unplaced scaffolds
  with a `v` separator (`NW_013982187v1`) where GenBank/RefSeq use a dot
  (`NW_013982187.1`). The two forms are swapped and retried.
- **ENA header prefix** (`ENA|<unversioned>|<versioned>`): ENA FASTA
  headers wrap the accession; the bare accession (the inner field) is
  what matches our columns, so the wrapper is peeled and retried.

These are surface-form rewrites of one accession, not fuzzy matching, so
a fallback hit cannot map to an unrelated sequence. A genuine miss still
warns and passes through unchanged. This handles header-parsing variants
seen in the wild without loosening the exact-match guarantee for the
common case.

### Multi-file mode: conform vs. overwrite

The common real-world workflow has one reference FASTA and several
annotation files (GFF/GTF) for the same assembly. Multi-file mode
(`convert --fasta REF [ANN ...]`) detects the assembly once from the
FASTA, loads the alias table once, and processes the batch.

The guiding idea is *consistency*, not translation to a fixed target:
the usual goal is to make a genome and its annotations agree on one
naming convention. That drives two modes:

- **Conform** (no `--overwrite-to`): detect the FASTA's own convention
  C, rewrite each annotation into C, and leave the FASTA untouched. The
  FASTA is the reference the user already has, so it is not copied into
  `--out-dir` — only the conformed annotations are written. This is the
  default because it matches the "make my annotations match my genome"
  case without forcing the user to name a convention.
- **Overwrite** (`--overwrite-to TGT`): detect the shared source
  convention from the FASTA and force the FASTA and every annotation to
  TGT. This is the "normalize everything to X" case.

`--to` stays single-file-only. In multi-file mode it is rejected (with
a pointer to `--overwrite-to`) rather than silently reinterpreted, so
the two flags never blur together.

Conform mode builds its `{any-convention-name -> C}` map by merging the
existing one-source/one-target `get_map` over the non-C convention
columns, so it needs no change to the `AliasSource` interface or the
SQLite schema. One consequence: a name that is *already* in C is not a
key in that map, so it passes through unchanged (the correct output)
but lands in the unmapped tally. Conform mode therefore reports
passthrough as a neutral note rather than a warning. A first-class
`get_conform_map` on `AliasSource` (one query folding every convention
column to the target, including C's own values) would make the count
exact; it is deferred because it extends the future-API seam and is
worth raising with that design rather than adding unilaterally.

Not supported: per-line resolution for files that mix conventions
across rows. One source convention per annotation file.

## Architecture

The CLI has three responsibilities split across three areas of code,
so each can change independently:

```
                 user types command
                         |
                         v
              +----------------------+
              |        cli.py        |   parses args, orchestrates,
              |                      |   prints user-facing messages
              +------+----------+----+
                     |          |
            +--------v--+    +--v--------------+
            | formats/  |    | alias_source.py |
            |           |    |                 |
            | - GffT.   |    | - get_map       |
            | - FastaT. |    | - detect_convention
            | - dispatch|    | - detect_assembly
            +-----+-----+    +---------+-------+
                  |                    |
                  | (per-line          | (SQL queries)
                  |  translation)      |
                  v                    v
              input file           SQLite DB
```

**`cli.py`** is the entry point. It parses arguments, decides
whether to sample the input for auto-detection, calls the right pieces
in the right order, and translates internal errors into user-facing
messages.

**`alias_source.py`** wraps the data. The `AliasSource` abstract base
class defines the interface (`get_map`, `detect_convention`,
`detect_assembly`, `assembly_exists`); `SqliteAliasSource` is the
current implementation. When the hosted API ships, `HttpAliasSource`
will implement the same interface and the CLI will pick one based on
configuration. No code outside this module knows SQLite is involved.

**`formats/`** holds one translator class per file format. Each
implements `FileTranslator`'s two methods: `translate_line` (swap the
name in one line) and `sample_names` (read up to N unique names from
the start of a file). A dict in `formats/__init__.py` maps file
extensions to translator classes; adding a new format is one new class
plus one line in the dict.

### Performance properties

- **Constant memory:** input files are streamed line-by-line; memory
  usage is independent of file size.
- **Linear time:** single pass over the input; one dict lookup per row.
- **One alias-table load per invocation:** the per-assembly subset of
  the DB (~50 rows) is loaded into memory once and reused for every
  input line; in multi-file mode it is loaded once for the whole batch.

## Edge cases

Status column: ✓ currently implemented, ◯ planned but not yet built.

### Input files

| Case                                                            | Behaviour                                                                    | Status |
| --------------------------------------------------------------- | ---------------------------------------------------------------------------- | :----: |
| File doesn't exist                                              | Error with clear message                                                     |   ✓    |
| Sequence name with whitespace / special chars                   | Look up as-is; if unmapped, warn-and-pass-through                            |   ✓    |
| Multi-GB input                                                  | Streaming handles it                                                         |   ✓    |
| FASTA header with description (`>chr1 Homo sapiens...`)         | Translate the first token only; preserve description (including whitespace) |   ✓    |
| Unknown file extension                                          | Error listing supported extensions                                           |   ✓    |
| File is empty                                                   | Warn; write empty output; exit 0                                             |   ◯    |
| File is gzipped (`.gff.gz` or bare gzip)                        | Detected by content on read, by extension on write; handled transparently    |   ✓    |
| Mixed line endings (`\r\n` vs `\n`)                             | Normalise on read                                                            |   ◯    |
| Malformed line (wrong column count)                             | Warn; pass through unchanged                                                 |   ◯    |
| GFF metadata lines with sequence names (`##sequence-region`)    | Translate names in those header lines too                                    |   ◯    |

### Naming conventions

| Case                                                   | Behaviour                                                                       | Status |
| ------------------------------------------------------ | ------------------------------------------------------------------------------- | :----: |
| Name not in alias table                                | Warn-and-pass-through                                                           |   ✓    |
| Auto-detection has no clear winner                     | Error; require explicit `--from` or `--assembly`                                |   ✓    |
| Mitochondrial DNA aliases (`MT`, `chrM`, `chrMT`)      | Handled normally — these are entries in the alias table                         |   ✓    |
| Accession with version mismatch (`CM000663.1` vs `.2`) | Strict version matching by default. `--ignore-version` strips the suffix        |   ◯    |
| UCSC version separator (`NW_...v1` vs `NW_....1`)      | Swapped and retried on an exact-lookup miss                                     |   ✓    |
| ENA pipe-prefixed header (`ENA\|ACC\|ACC.v`)           | Stripped to the bare accession and retried on an exact-lookup miss              |   ✓    |
| Mixed conventions within one file                      | Per-line resolution (stretch)                                                   |   ◯    |

### Output

| Case                           | Behaviour                                                   | Status |
| ------------------------------ | ----------------------------------------------------------- | :----: |
| Output directory doesn't exist | Create it                                                   |   ✓    |
| Output file already exists     | Error unless `--force` is supplied                          |   ◯    |
| Tool fails mid-write           | Write to a temp file; rename on success — no partial output |   ◯    |

## Data storage

The alias dataset is rebuilt from NCBI's four `assembly_summary`
files and published to GitHub Releases under `data-YYYY-MM-DD` tags.
Each data release ships three artifacts (sizes from the full ~48k
set):

- `aliases.tsv.gz`     — merged-row per-assembly data (~100 MB, full ~48k eukaryotic set)
- `historical.tsv.gz`  — dead-accession lookup (~1.4 MB)
- `failures.tsv`       — per-assembly collection failure log

The CLI maintains a local SQLite cache in the platform cache
directory:

- macOS:   `~/Library/Caches/alias-mapper/aliases.db`
- Linux:   `~/.cache/alias-mapper/aliases.db`
- Windows: `%LOCALAPPDATA%\alias-mapper\Cache\aliases.db`

First-run flow: the CLI queries the GitHub API for the most recent
`data-*` release, downloads its `aliases.tsv.gz` asset, runs
`build_alias_db` to produce the local SQLite, and caches it. The
downloaded TSV is kept alongside the DB. Subsequent invocations use
the cached DB directly. If the schema version in the cache no longer
matches the CLI's expected version (typical scenario: CLI was
upgraded), the cache is rebuilt silently — reusing the cached TSV
without a network round trip, so a schema-bump rebuild works offline.
Users can force a fresh-data refresh via `alias-mapper update`, which
always re-downloads (fetching newer data is its point); there is no
automatic refresh on every run.

The `platformdirs` package handles cross-platform cache-path
resolution. Together with `certifi` (and optionally `truststore`
for TLS-inspecting networks), it's the only runtime dependency
outside the standard library.

### TSV schema (schema v3)

One row per assembly. Per-molecule data is held in pipe-separated
(`|`), position-aligned list columns: the Nth pipe-separated entry in
every list column refers to the same molecule, with entries sorted by
length descending.

```
genbank_acc, refseq_acc, assembly_name, taxid, organism_name,
group, assembly_level,
sequence_names, genbank_seq_accs, refseq_seq_accs, ucsc_names,
assigned_molecules, lengths,
genome_coverage_pct, genome_coverage_ungapped_pct
```

The two `genome_coverage_*` columns are per-assembly scalars (not
position-aligned lists): the summed length of the molecules kept for
that assembly as a percentage of the assembly's total size, from the
summary row's `genome_size` and `genome_size_ungapped` respectively.
They answer "what fraction of the genome do the chromosomes we carry
account for?" — near 100% for chromosome-level assemblies, lower
where the adaptive cap trimmed a scaffold tail. Empty when the
summary row carries no genome size to divide by.

This format is human-readable and diff-friendly. RefSeq-only or
GenBank-only assemblies just leave the absent column empty; empty
entries within a list (e.g. a UCSC name for a non-vertebrate
chromosome) are preserved between delimiters so position-alignment is
never broken. The delimiter is a pipe rather than a comma because NCBI
Sequence-Name and Assigned-Molecule values can themselves contain
commas; a pipe is effectively absent from these fields, and any
assembly that does contain one is skipped at collection time (logged
to failures.tsv) rather than emitted as a misaligned row.

### SQLite schema

The build step (`build_alias_db.build_db`) explodes the TSV's list
columns back into per-molecule rows. The TSV is the human-readable
source of truth; the SQLite is whatever shape is fastest for queries.

```sql
CREATE TABLE _meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
-- schema_version, build_date

CREATE TABLE assemblies (
    accession                    TEXT PRIMARY KEY,    -- GenBank acc (GCA_*)
    assembly_name                TEXT,
    paired_refseq_acc            TEXT,                -- GCF_*, if paired
    taxid                        INTEGER,
    organism_name                TEXT,
    group_name                   TEXT,                -- "group" is reserved in SQL
    assembly_level               TEXT,
    genome_coverage_pct          REAL,                -- kept length / genome_size, %
    genome_coverage_ungapped_pct REAL,                -- kept length / genome_size_ungapped, %
    last_updated                 TEXT
);

CREATE TABLE aliases (
    accession         TEXT NOT NULL,         -- FK to assemblies.accession
    position          INTEGER NOT NULL,      -- 0-based, longest first
    sequence_name     TEXT,
    assigned_molecule TEXT,
    genbank_acc       TEXT,                  -- per-sequence (e.g. CM000663.2)
    refseq_acc        TEXT,                  -- per-sequence (e.g. NC_000001.11)
    ucsc_name         TEXT,
    length            INTEGER
);

CREATE INDEX idx_accession ON aliases(accession);
```

Single index on `aliases(accession)`. The CLI's only filter when
loading a per-assembly map is by accession; indexing every name column
would inflate the DB by hundreds of MB for queries the CLI never runs.

Detection queries (`detect_convention`, `detect_assembly`) do full
table scans, but a single scan in SQLite is sub-second and runs at
most once per invocation.

### Why SQLite over Parquet

Parquet does not support row-level appends; adding rows requires
rewriting the file. SQLite supports `INSERT` natively, has indexed
point lookups that match the CLI's access pattern, and ships in
Python's standard library. The size overhead vs. gzipped TSV is
acceptable.

## Pipeline

The data workflow runs `scripts/collect_aliases.py`, which is driven
by NCBI's four `assembly_summary` files rather than per-assembly
enumeration via the `datasets` CLI.

Pipeline phases:

1. **Stream** the four summary files (current + historical, GenBank +
   RefSeq). In CI they're streamed directly with no disk persistence;
   local dev keeps a 24h cache for iteration speed.
2. **Plan**: filter to `version_status=latest`, allowed assembly
   levels and eukaryotic taxonomic groups. Join GenBank↔RefSeq pairs
   via `gbrs_paired_asm`, stem-normalized to handle version
   differences. The full plan is ~48k assemblies (all of Complete
   Genome / Chromosome / Scaffold). The allowed levels are selectable
   via `--levels`: the canonical weekly workflow passes the full set;
   the manual fallback workflow uses the default (Chromosome+, ~16k).
3. **Historical writer**: write `historical.tsv.gz` from the
   historical summary files. Pure planner output — doesn't depend on
   per-assembly fetches, so it survives partial sweeps.
4. **Per-assembly fetch**: for each planned assembly, fetch its
   `assembly_report.txt` via the `ftp_path` column from the summary
   file (no URL reconstruction needed). Parallelized via
   `ThreadPoolExecutor`; structured failures logged to `failures.tsv`.
5. **Adaptive coverage cap**: take the top 50 longest molecules per
   assembly; expand toward 90% of `genome_size` if needed; hard
   ceiling at 500 to guard against pathologically fragmented
   scaffold assemblies.
6. **Write merged TSV rows** to `aliases.tsv.gz`.

The cost split is the key architectural property: the cheap
catalog-level work (steps 1-3) finishes in minutes; the expensive
per-molecule data (steps 4-6) takes most of the wall clock. This
maps directly onto a future hosted-API design where the two could
refresh at different cadences.

### Sharded full-set workflow

The full ~48k set can't be fetched in a single sweep: NCBI throttles
a sustained per-assembly fetch (the rate decays from ~16/s to ~1/s),
and a single job can't finish inside GitHub's 6h runner ceiling.
`full-alias-update-sharded.yml` gets around this with a map-reduce
shape, and is the canonical workflow that owns the weekly Sunday
schedule:

- **setup** turns a `num_shards` input (default 8) into a matrix
  index list.
- **catalog** writes `historical.tsv.gz` once from the full
  population (it's shard-independent).
- **shard** (matrix, 8 jobs) each fetch a strided 1/N slice of the
  plan, on separate GitHub runners with distinct outbound IPs —
  which sidesteps the per-IP throttle. A standalone throughput test
  confirmed parallel shards each hold full rate rather than
  collectively sagging, so the runners do not share a throttled IP
  pool.
- **merge** concatenates the shard TSVs, prints the assembly-level
  distribution and fails if no Scaffold rows are present (a guard
  against a misconfigured run silently shipping Chromosome+ data
  under the full-set tag), verifies the merged file builds into
  SQLite, then publishes the Release.

First full-set run completed in ~26 min at 8 shards (~6k assemblies
per shard). <!-- TODO: confirm current wall time + failure count from the scheduled run's merge log -->

The older `alias-tsv-update.yml` (single-job, Chromosome+ only) has
had its schedule removed and is retained as a manually-runnable
fallback — e.g. for a quick partial dataset if a sharded run is
failing. Only the sharded workflow is scheduled, so the two can't
collide on a `data-*` release tag.

## What's next

### Suppressed-accession messages (issue #5)

`historical.tsv.gz` already ships. The remaining work is on the CLI
side: download it on first run alongside the alias data, materialize
it as an `assemblies_historical` SQLite table, and use it to give
useful errors when a user references a dead accession ("this was
suppressed on YYYY-MM-DD, replaced by X").

### Hosted API as the eventual replacement

The local DB is a stepping stone. The eventual target is a hosted
endpoint that answers the same queries the CLI runs today. The
`AliasSource` abstraction was introduced specifically so this can
land as a new implementation (`HttpAliasSource`) without touching
the translator or CLI code.

A useful framing emerged from the pipeline rewrite: NCBI's data has
two natural cost tiers — cheap catalog metadata (the four summary
files, refreshable in minutes) and expensive per-molecule alias data
(per-assembly fetches, hours of wall clock). The hosted API would
expose both: catalog and alias queries hit different backing stores
that refresh at different cadences. The pipeline rewrite is half of
that future API already.

## Open questions

1. Should an `alias-mapper inspect <file>` subcommand report the
   auto-detected source convention and assembly without rewriting the
   file? Useful for debugging input data.
2. Should mapping be lossless-reversible (A→B→A returns the original)?
   This constrains how unmapped rows are handled.
3. Cache invalidation policy for the user-side DB: manual (`alias-mapper
   update`), automatic on a TTL, or check Release etag on every run?

## Notes from investigation

### Scaffold-level: excluded early, restored via sharding

The pipeline's first two full-scale single-job runs failed: the
first timed out at the workflow's 360-minute ceiling, the second hit
it again after processing ~28k of ~48k planned assemblies. The fetch
rate started at ~15/s and decayed monotonically to ~1.3/s by the 28k
mark, with zero rejections throughout — NCBI silently throttles
sustained per-assembly sweeps. (An NCBI API key won't help: API keys
raise the limit on E-utilities, not the FTP host this pipeline uses.)
6h is also the GitHub-hosted runner ceiling, so the timeout itself
can't be raised further.

The stopgap was dropping Scaffold-level, cutting the plan from ~48k
to ~16k (Scaffold was 31,594 of 47,920) — which also removed the
assemblies with the highest per-assembly molecule counts. That
shipped a first working release while the throughput problem was
solved separately.

The throughput problem was then solved by the sharded matrix
workflow (see "Sharded full-set workflow" above), which restores the
full set including Scaffold and is now the canonical weekly job. Of
the three paths originally considered — matrix split, switching off
the FTP HTTPS endpoint (rsync/mirror/`datasets`), and a self-hosted
runner — the matrix split was tried first and worked: a throughput
test confirmed parallel runners have distinct IPs that NCBI doesn't
throttle collectively, and the full set completed in ~26 min.

Two bugs surfaced while getting the full-scale run to land, both
caught at the merge stage rather than reaching users:

- A `gzip | head` pipeline under `set -o pipefail` exited 141
  (SIGPIPE) on a large merged file, failing the merge step after the
  data was already correctly merged. Fixed by using `awk NR==1`
  (reads to EOF, no early pipe close) for the column-count check.
- The within-cell list delimiter was a comma, but some NCBI
  Sequence-Name / Assigned-Molecule values contain literal commas
  (e.g. GCA_014751505.1), which broke position alignment — caught by
  the merge step's build-verification gate. Fixed by switching the
  delimiter to a pipe, with a write-time guard that skips and logs
  any assembly whose values contain a pipe.

A related lesson: the build-verification gate catches *corrupt* data
(a file that won't parse or build), but valid-but-incomplete data
(e.g. Chromosome+ rows where the full set was intended) builds fine
and sails through. The merge step's Scaffold-presence check was
added specifically to close that gap for the full-set workflow —
"the run went green" and "the output is the right dataset" are
different claims.

### Single-row assemblies are legitimate

184 assemblies in an earlier production DB had only one row (one
molecule). Reviewed them against NCBI; not a parser bug. They fall
into a few categories, each recognizable from the `assembly_name`
or sequence name:

- Single-chromosome or chromosome-arm deposits (a researcher
  sequenced one chromosome separately from the rest of the genome)
- Y-chromosome-only assemblies (Y is often deposited apart from the
  autosomes)
- Bacterial and endosymbiont genomes (one circular chromosome by
  biology). Note that the new pipeline filters bacteria out by
  default, so these no longer appear in the dataset.
- Mitochondrial genomes (~16-20 kb, often suffixed
  `_mitochondrial_genome_(circular)` in the sequence name)
- Viral / phage genomes

In the merged-row schema these are single-entry lists — still valid,
just position 0 only.
