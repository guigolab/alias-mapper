# alias-mapper — Design

A command-line tool that rewrites the chromosome / scaffold names in
bioinformatics input files (GFF, FASTA, etc.) from one naming convention
to another. The translation uses a precomputed alias table built from
NCBI assembly reports.

The problem it solves: research files from different sources use
different naming conventions for the same sequences (e.g. `chr1`,
`NC_000001.11`, `CM000663.2`, and `1` can all refer to the same human
chromosome). Files using different conventions can't be used together
without translation.

## Scope

**v1:**

- File formats: GFF, GTF, FASTA
- Single input file per invocation
- Naming conventions: GenBank, RefSeq, UCSC, Sequence-Name,
  Assigned-Molecule
- Source convention and assembly auto-detected by default; overridable
  via flags

**Out of v1:**

- Multi-file mode (`--out-dir`)
- Parallel processing

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

| Flag            | Required | Purpose                                                     |
| --------------- | -------- | ----------------------------------------------------------- |
| `--to`          | yes      | Target naming convention                                    |
| `-o / --output` | no       | Output path. Defaults to `<input>.converted.<ext>`          |
| `--from`        | no       | Source convention. Auto-detected if absent                  |
| `--assembly`    | no       | Restrict lookup to one assembly. Auto-detected if absent    |
| `--alias-table` | no       | Path to alias table file (default: standard cache location) |

## Design decisions

### Source convention: auto-detect, overridable

Inputs may come from collaborators or public databases where the user
doesn't know the source convention with certainty. Some files mix
conventions internally.

Auto-detection samples the first ~50 unique sequence names in the input,
checks which convention best matches the alias table, and uses the
highest-scoring match. The `--from` flag overrides auto-detection when
the user knows the source.

Stretch: per-line resolution for files that mix conventions across rows.

### Assembly scope: auto-detect, overridable

A sequence name like `1` is ambiguous across species. The tool must
scope its lookup to a single assembly.

Auto-detection scores candidate assemblies by how many of the input's
first ~50 unique sequence names exist in each one. The highest match
wins. If the top two scores are close, auto-detection fails and
`--assembly` must be supplied explicitly.

### Unmapped sequence names: warn and pass through

When a sequence name isn't in the alias table, the row is kept
unchanged in the output and a warning is emitted to stderr.

Silent skipping risks data loss in downstream pipelines. Strict
erroring is too aggressive for common cases (a handful of unknown
contigs shouldn't kill the run). Pass-through preserves data while
making issues visible.

If future use cases need it, `--strict` (error on any unmapped name)
and `--skip-unknown` (drop unmapped rows) could be added without
changing the default behaviour.

### Single-file mode in v1, multi-file in v2

A real-world invocation often has one FASTA and several GFF files for
the same assembly. The efficient pattern is to load the alias table
once and reuse it across all input files via a shared in-memory lookup.

v1 keeps the interface simple — one input per invocation. Users with
multiple files run the tool multiple times, accepting the table-load
cost each time. v2 adds `--out-dir` and processes multiple files in
one invocation.

## Architecture

```
[ alias table (SQLite or TSV) ]
         │ (loaded once at startup)
         ▼
[ in-memory dict: source_name → target_name ]
         │
         ▼
[ optional: auto-detect source convention and assembly ]
         │
         ▼
[ stream input file line-by-line ]
         │
         ▼ (for each line)
[ extract sequence name → dict lookup → substitute → write to output ]
         │
         ▼
[ on EOF: print summary (rows processed, rows unmapped) ]
```

### Performance properties

- **Constant memory:** input files are streamed line-by-line; memory
  usage is independent of file size. Handles arbitrarily large files.
- **Linear time:** single pass over the input; one dict lookup per row.
- **One alias-table load per invocation:** loaded into memory once and
  reused for every row.

## Edge cases

### Input files

| Case                                                                            | Behaviour                                                                    |
| ------------------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| File doesn't exist                                                              | Error with clear message                                                     |
| File is empty                                                                   | Warn; write empty output; exit 0                                             |
| File is gzipped (`.gff.gz`)                                                     | Auto-detected by extension; read transparently                               |
| Mixed line endings (`\r\n` vs `\n`)                                             | Normalised on read                                                           |
| Malformed line (wrong column count)                                             | Warn; pass through unchanged                                                 |
| Sequence name with whitespace / special chars                                   | Look up as-is; if unmapped, warn-and-pass-through                            |
| Multi-GB input                                                                  | Streaming handles it                                                         |
| FASTA header with description (`>chr1 Homo sapiens...`)                         | Split on first whitespace; translate the first token only; preserve the rest |
| GFF/GTF metadata lines containing sequence names (`##sequence-region chr1 ...`) | Translate names in those header lines too                                    |

### Naming conventions

| Case                                                   | Behaviour                                                                          |
| ------------------------------------------------------ | ---------------------------------------------------------------------------------- |
| Name not in alias table                                | Warn-and-pass-through                                                              |
| Name ambiguous across assemblies                       | Auto-detection fails; require `--assembly`                                         |
| Accession with version mismatch (`CM000663.1` vs `.2`) | Strict version matching by default. `--ignore-version` strips the suffix and warns |
| Mitochondrial DNA aliases (`MT`, `chrM`, `chrMT`)      | Handled normally — these are entries in the alias table                            |
| Mixed conventions within one file                      | Stretch: per-line resolution                                                       |

### Output

| Case                           | Behaviour                                                   |
| ------------------------------ | ----------------------------------------------------------- |
| Output file already exists     | Error unless `--force` is supplied                          |
| Output directory doesn't exist | Create it                                                   |
| Tool fails mid-write           | Write to a temp file; rename on success — no partial output |

## Data storage

### Current state

The weekly workflow rebuilds the alias dataset from NCBI and publishes
the result as a gzipped TSV (~33 MB, ~3M rows, ~66k assemblies)
attached to a GitHub Release.

This works at current scale but has known limitations:

- The full dataset is rebuilt every week even though >99% of assemblies
  are unchanged.
- Publishing a new Release every week accumulates near-identical
  snapshots indefinitely.
- TSV is not queryable; lookups require scanning the whole file.

### Proposed: SQLite with incremental updates

Replace the TSV with a single SQLite database. One table:

```sql
CREATE TABLE aliases (
    accession         TEXT NOT NULL,
    assembly_name     TEXT,
    genbank_acc       TEXT,
    refseq_acc        TEXT,
    sequence_name     TEXT,
    assigned_molecule TEXT,
    ucsc_name         TEXT,
    length            INTEGER,
    last_updated      TEXT
);
```

Indexes on each name column for fast point lookups.

The weekly workflow queries the DB for the most recent `last_updated`
value, fetches only newer assemblies from NCBI, and appends them via
`INSERT`. The DB becomes the canonical source of truth; snapshots are
published less frequently (e.g. monthly).

### Why SQLite over Parquet

Parquet does not support row-level appends. Adding new rows requires
reading the full file into memory and writing a new one, which defeats
the goal of incremental updates.

SQLite supports `INSERT` natively, has indexed point lookups that match
the CLI's access pattern, and ships in Python's standard library. Size
overhead vs. gzipped TSV (~2-3× uncompressed) is acceptable.

## Open questions

1. Default location for the alias table: fetched from the latest
   Release at runtime, or a local file pointed at by `--alias-table`?
2. Should an `alias-mapper inspect <file>` subcommand report the
   auto-detected source convention and assembly without rewriting the
   file?
3. Should mapping be lossless-reversible (A→B→A returns the original)?
   This constrains how unmapped rows are handled.
4. Storage location for the SQLite DB: committed to the repo, attached
   to Releases, or both?
5. Snapshot cadence: weekly, monthly, or quarterly?
