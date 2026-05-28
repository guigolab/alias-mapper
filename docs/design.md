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

- File formats: GFF, GTF, FASTA (single input file per invocation)
- Naming conventions: GenBank, RefSeq, UCSC, Sequence-Name,
  Assigned-Molecule
- Source convention and assembly auto-detected from the input;
  overridable via flags
- Pipeline streams NCBI's four `assembly_summary` files weekly,
  publishes `aliases.tsv.gz` + `historical.tsv.gz` + `failures.tsv`
  as GitHub Release assets
- Client builds the local SQLite cache from the TSV on first run
- Schema-versioned cache: stale-schema caches rebuild silently when
  the CLI is upgraded

**On the horizon:**

- Multi-file `align` subcommand: make several files share one
  consistent naming convention, optionally driven by a reference FASTA
- Issue #5 (suppressed-accession error messages) wired into the CLI
  using the already-shipped `historical.tsv.gz`
- Hosted query API as the eventual replacement for the local DB

## Command-line interface

```
alias-mapper convert <input> --to <convention> [-o <output>] [options]
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
```

### Flags

| Flag            | Required | Purpose                                                  |
| --------------- | -------- | -------------------------------------------------------- |
| `--to`          | yes      | Target naming convention                                 |
| `-o / --output` | yes      | Output path                                              |
| `--from`        | no       | Source convention. Auto-detected if absent               |
| `--assembly`    | no       | Restrict lookup to one assembly. Auto-detected if absent |
| `--alias-db`    | no       | Path to the alias SQLite database                        |

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

### Single-file mode today, multi-file on the horizon

Today `convert` takes one input file per invocation. A common
real-world workflow has one FASTA and several GFF files for the same
assembly; running the tool repeatedly accepts the table-load cost
each time. The planned `align` subcommand will take multiple files
and a target convention (specified directly or inferred from a
reference FASTA), load the alias table once, and translate all files
in one pass.

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
  input line.

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
| File is gzipped (`.gff.gz`)                                     | Auto-detect by extension; read transparently                                 |   ◯    |
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
| Mixed conventions within one file                      | Per-line resolution (stretch)                                                   |   ◯    |

### Output

| Case                           | Behaviour                                                   | Status |
| ------------------------------ | ----------------------------------------------------------- | :----: |
| Output directory doesn't exist | Create it                                                   |   ✓    |
| Output file already exists     | Error unless `--force` is supplied                          |   ◯    |
| Tool fails mid-write           | Write to a temp file; rename on success — no partial output |   ◯    |

## Data storage

The alias dataset is rebuilt weekly from NCBI's four
`assembly_summary` files and published to GitHub Releases under
`data-YYYY-MM-DD` tags. Each data release ships three artifacts:

- `aliases.tsv.gz`     — merged-row per-assembly data (~35 MB)
- `historical.tsv.gz`  — dead-accession lookup (~5 MB)
- `failures.tsv`       — per-assembly collection failure log

The CLI maintains a local SQLite cache in the platform cache
directory:

- macOS:   `~/Library/Caches/alias-mapper/aliases.db`
- Linux:   `~/.cache/alias-mapper/aliases.db`
- Windows: `%LOCALAPPDATA%\alias-mapper\Cache\aliases.db`

First-run flow: the CLI queries the GitHub API for the most recent
`data-*` release, downloads its `aliases.tsv.gz` asset, runs
`build_alias_db` to produce the local SQLite, and caches it.
Subsequent invocations use the cached DB directly. If the schema
version in the cache no longer matches the CLI's expected version
(typical scenario: CLI was upgraded), the cache is rebuilt silently.
Users can force a refresh via `alias-mapper update`; there is no
automatic refresh on every run.

The `platformdirs` package handles cross-platform cache-path
resolution. Together with `certifi` (and optionally `truststore`
for TLS-inspecting networks), it's the only runtime dependency
outside the standard library.

### TSV schema (schema v2)

One row per assembly. Per-molecule data is held in comma-separated,
position-aligned list columns: the Nth comma-separated entry in every
list column refers to the same molecule, with entries sorted by
length descending.

```
genbank_acc, refseq_acc, assembly_name, taxid, organism_name,
group, assembly_level,
sequence_names, genbank_seq_accs, refseq_seq_accs, ucsc_names,
assigned_molecules, lengths
```

This format is human-readable and diff-friendly. RefSeq-only or
GenBank-only assemblies just leave the absent column empty; empty
entries within a list (e.g. a UCSC name for a non-vertebrate
chromosome) are preserved between commas so position-alignment is
never broken.

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
    accession           TEXT PRIMARY KEY,    -- GenBank acc (GCA_*)
    assembly_name       TEXT,
    paired_refseq_acc   TEXT,                -- GCF_*, if paired
    taxid               INTEGER,
    organism_name       TEXT,
    group_name          TEXT,                -- "group" is reserved in SQL
    assembly_level      TEXT,
    last_updated        TEXT
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

The weekly GitHub Actions workflow runs `scripts/collect_aliases.py`,
which is driven by NCBI's four `assembly_summary` files rather than
per-assembly enumeration via the `datasets` CLI.

Pipeline phases:

1. **Stream** the four summary files (current + historical, GenBank +
   RefSeq). In CI they're streamed directly with no disk persistence;
   local dev keeps a 24h cache for iteration speed.
2. **Plan**: filter to `version_status=latest`, allowed assembly
   levels (Complete Genome / Chromosome — Scaffold excluded for v2.0,
   see below) and eukaryotic taxonomic groups. Join GenBank↔RefSeq
   pairs via `gbrs_paired_asm`, stem-normalized to handle version
   differences. Current plan size is ~16k assemblies (down from ~48k
   when Scaffold was included).
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

## What's next

### Multi-file `align` subcommand

`convert` (the current single-file flow) stays as-is. `align` is the
planned addition for the multi-file workflow:

```
alias-mapper align --fasta ref.fa annotations.gff peaks.gff
alias-mapper align --to ucsc annotations.gff peaks.gff
```

The first form uses a reference FASTA's convention as the target; the
second specifies the target directly. Both share the existing
translator code under the hood. Reframing: the goal is making a set
of files *consistent*, not specifically performing a translation.

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

### Scaffold-level excluded from v2.0

The pipeline's first two full-scale CI runs failed: the first timed
out at the workflow's 360-minute ceiling, the second hit it again
after processing ~28k of 47,920 planned assemblies. The fetch rate
started at ~15/s and decayed monotonically to ~1.3/s by the 28k
mark, with zero rejections throughout — NCBI's FTP HTTPS endpoint
is silently throttling sustained sweeps. (An NCBI API key won't
help: API keys raise the limit on E-utilities, not the FTP host
this pipeline uses.) 6h is also the GitHub-hosted runner ceiling,
so the timeout itself can't be raised further.

Dropping Scaffold-level cuts the plan from ~48k to ~16k (Scaffold
was 31,594 of 47,920) and removes the assemblies with the highest
per-assembly molecule counts — a double win on cost. Chromosome+
is also the population most users actually want: Scaffold-level
entries are draft genomes rarely used for downstream tools that
need alias mapping. Coverage trade-off is acceptable for v2.0.

Paths to add Scaffold back, in rough order of effort:
1. Matrix-split the workflow (N parallel jobs, merge artifacts).
   Risk: GitHub Actions runners may share IP ranges that NCBI
   throttles together, in which case parallelism buys nothing.
   Worth a small-scale test first.
2. Switch the per-assembly fetch path off the FTP HTTPS endpoint —
   `rsync` bulk pull, a mirror, or `datasets` CLI batched access.
3. Self-hosted runner with no 6h ceiling.

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
