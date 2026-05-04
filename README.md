# alias-mapper

A command-line tool to map alias names of biological molecules
(chromosomes, scaffolds, contigs) across naming conventions used in
public genome assemblies (Sequence-Name, GenBank, RefSeq, UCSC).

## Project structure

- `data/` — collected alias data (TSV files)
- `scripts/` — helper scripts for collecting and processing data
- `src/` — source code for the alias mapper CLI

## Status

Work in progress. Currently collecting alias data for the 50 longest
molecules of every eukaryotic assembly available on NCBI.
