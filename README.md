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

Clone the repo and install the one Python dependency:

```bash
git clone https://github.com/Max25R/alias-mapper.git
cd alias-mapper
pip3 install platformdirs
```

That's it. The first time you run `convert`, the tool downloads the
latest alias data (~33 MB) from the GitHub Releases and builds a
local SQLite database (~260 MB) in your platform cache directory:

- macOS:   `~/Library/Caches/alias-mapper/aliases.db`
- Linux:   `~/.cache/alias-mapper/aliases.db`
- Windows: `%LOCALAPPDATA%\alias-mapper\Cache\aliases.db`

This takes about 1-2 minutes the first time. Subsequent runs use the
cached database directly.

## Quickstart

Translate a GFF from whatever it uses now to UCSC names, with
auto-detection figuring out the rest:

```bash
python3 src/alias_mapper.py convert annotations.gff \
    --to ucsc \
    -o annotations.ucsc.gff
```

Output goes to `annotations.ucsc.gff`. A summary line on stderr reports
how many rows were translated and how many had names not in the alias
table (those are passed through unchanged with a warning).

## Usage

```
alias-mapper convert <input> --to <convention> -o <output> [options]
alias-mapper update
```

### Subcommands

- **`convert`** — translate one file from one convention to another.
- **`update`** — re-download the latest alias data and rebuild the
  cached database. Run this manually when you want newer data.

### Supported file types

GFF (`.gff`, `.gff3`), GTF (`.gtf`), and FASTA (`.fa`, `.fasta`,
`.fna`). The translator is picked by file extension.

### Supported conventions

`genbank`, `refseq`, `ucsc`, `sequence-name`, `assigned-molecule`.

### Examples

```bash
# Translate explicitly from RefSeq to UCSC
python3 src/alias_mapper.py convert annotations.gff \
    --from refseq --to ucsc \
    -o out.gff

# Pin the assembly when auto-detection is ambiguous
python3 src/alias_mapper.py convert annotations.gff \
    --to ucsc \
    --assembly GCF_000001405.40 \
    -o out.gff

# FASTA — same syntax, different file
python3 src/alias_mapper.py convert reference.fa \
    --from genbank --to sequence-name \
    --assembly GCA_963924405.1 \
    -o reference.renamed.fa

# Refresh the cached alias data
python3 src/alias_mapper.py update
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
Five example names are printed at the end of the run so you can see
what didn't translate.

## More

See [`docs/DESIGN.md`](docs/DESIGN.md) for the architecture, design
decisions, and v2 direction.
