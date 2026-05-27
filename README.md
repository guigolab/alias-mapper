# alias-mapper

Translate chromosome and scaffold names in bioinformatics files
between naming conventions (GenBank, RefSeq, UCSC, and others).

## What it does

Research files from different sources use different names for the same
sequences: `chr1`, `NC_000001.11`, `CM000663.2`, and `1` can all refer
to the same human chromosome. Files using different conventions can't
be combined without translation.

`alias-mapper` rewrites the sequence names in GFF, GTF, and FASTA
files from one convention to another using a precomputed alias table
built from NCBI assembly reports. Source convention and genome
assembly are auto-detected from the input by default.

## Install

```bash
pip install git+https://github.com/Max25R/alias-mapper.git
```

On networks that perform TLS inspection (corporate / institutional,
e.g. CRG), also install the `trusted` extra so the tool uses the
system keychain for cert verification:

```bash
pip install "alias-mapper[trusted] @ git+https://github.com/Max25R/alias-mapper.git"
```

The first time you run `convert`, the tool downloads the latest alias
data (~35 MB) from GitHub Releases and builds a local SQLite database
in your platform cache directory:

- macOS:   `~/Library/Caches/alias-mapper/aliases.db`
- Linux:   `~/.cache/alias-mapper/aliases.db`
- Windows: `%LOCALAPPDATA%\alias-mapper\Cache\aliases.db`

First-run setup takes about a minute. Subsequent runs use the cached
database directly. If the database schema changes in a newer release,
the cache is rebuilt automatically.

## Quickstart

```bash
alias-mapper convert annotations.gff --to ucsc -o annotations.ucsc.gff
```

A summary on stderr reports how many rows were translated and how many
had sequence names not in the alias database (those rows are passed
through unchanged with a warning).

## Usage

```
alias-mapper convert <input> --to <convention> -o <output> [options]
alias-mapper update
```

### Subcommands

- **`convert`** — translate one file from one convention to another.
- **`update`** — re-download the latest alias data and rebuild the
  cached database. Run manually when you want newer data.

### Supported file types

GFF (`.gff`, `.gff3`), GTF (`.gtf`), and FASTA (`.fa`, `.fasta`,
`.fna`). The translator is picked by file extension.

### Supported conventions

`genbank`, `refseq`, `ucsc`, `sequence-name`, `assigned-molecule`.

### Examples

```bash
# Translate from RefSeq to UCSC explicitly
alias-mapper convert annotations.gff \
    --from refseq --to ucsc \
    -o out.gff

# Pin the assembly when auto-detection is ambiguous
alias-mapper convert annotations.gff \
    --to ucsc \
    --assembly GCF_000001405.40 \
    -o out.gff

# FASTA — same syntax, different file
alias-mapper convert reference.fa \
    --from genbank --to sequence-name \
    --assembly GCA_963924405.1 \
    -o reference.renamed.fa

# Refresh the cached alias data
alias-mapper update
```

### Flags (`convert`)

| Flag         | Required | Purpose                                                  |
| ------------ | -------- | -------------------------------------------------------- |
| `--to`       | yes      | Target naming convention                                 |
| `-o`         | yes      | Output path                                              |
| `--from`     | no       | Source convention. Auto-detected if absent               |
| `--assembly` | no       | Assembly accession. Auto-detected if absent              |
| `--alias-db` | no       | Path to a specific alias SQLite database (overrides cache) |

### Auto-detection

When `--from` or `--assembly` is omitted, the tool reads up to 50
unique sequence names from the input and scores them against the
database. It commits to a result only when the top candidate has at
least 5 matches and beats the runner-up by 2× or more. Otherwise it
errors out and asks for the flag explicitly.

### Unmapped names

If a sequence name in the input isn't in the alias database, the line
is written to the output unchanged and counted in the unmapped total.
Up to five example names are printed at the end of the run so you can
see what didn't translate.

## Data updates

A weekly GitHub Actions workflow rebuilds the alias dataset from
NCBI's published assembly summaries and publishes it as a
`data-YYYY-MM-DD` GitHub Release. Each release ships three artifacts:

- `aliases.tsv.gz` — the merged-row alias data the CLI consumes.
- `historical.tsv.gz` — dead-accession lookup with suppression dates
  and best-effort replacements.
- `failures.tsv` — per-assembly collection failure log.

## More

See [`docs/design.md`](docs/design.md) for architecture, design
decisions, and direction.
