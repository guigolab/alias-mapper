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

Clone the repo:

```bash
git clone https://github.com/Max25R/alias-mapper.git
cd alias-mapper
```

Download the latest alias TSV from the data Release and build the
local SQLite database from it. Data Releases are tagged
`data-YYYY-MM-DD` (one per weekly build); find the most recent at
[github.com/Max25R/alias-mapper/releases](https://github.com/Max25R/alias-mapper/releases)
and substitute the tag below:

```bash
mkdir -p data
curl -L -o data/aliases.tsv.gz \
    https://github.com/Max25R/alias-mapper/releases/download/data-YYYY-MM-DD/aliases.tsv.gz
python3 scripts/build_alias_db.py --tsv data/aliases.tsv.gz --db data/aliases.db
```

The build step takes about a minute and produces a ~260 MB SQLite
file at `data/aliases.db`. The CLI looks there by default; override
with `--alias-db` if you've placed it elsewhere.

The CLI itself is `src/alias_mapper.py`. No installation step yet —
run it directly with `python3`.

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
```

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
```

### Flags

| Flag         | Required | Purpose                                              |
| ------------ | -------- | ---------------------------------------------------- |
| `--to`       | yes      | Target naming convention                             |
| `-o`         | yes      | Output path                                          |
| `--from`     | no       | Source convention. Auto-detected if absent           |
| `--assembly` | no       | Assembly accession. Auto-detected if absent          |
| `--alias-db` | no       | Path to the alias SQLite database                    |

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

## Updating the alias database

The alias data is rebuilt weekly from NCBI and published as a new
`data-YYYY-MM-DD` Release. To pick up a newer version, re-run the
install steps with a more recent tag:

```bash
curl -L -o data/aliases.tsv.gz \
    https://github.com/Max25R/alias-mapper/releases/download/data-YYYY-MM-DD/aliases.tsv.gz
python3 scripts/build_alias_db.py --tsv data/aliases.tsv.gz --db data/aliases.db
```

`build_alias_db.py` overwrites the existing DB, so no manual cleanup
is needed.

## More

See [`docs/DESIGN.md`](docs/DESIGN.md) for the architecture, design
decisions, and v2 direction.
