# `analysis/` — QC notebooks

Two Jupyter notebooks that measure this project's **quality-control apparatus** against the
artifacts on disk, plus their rendered HTML so the numbers can be read without running
anything.

Everything here is **read-only**. The notebooks open both SQLite databases with
`readonly=True`, read parquet files (mostly just their footers), and run no pipeline stage.
They import nothing heavy: `prompt_factory.annotate.entrega` loads in ~0.4 s and does not
pull in torch or sentence-transformers.

| file | what it measures |
| --- | --- |
| `01_annotation_qc.ipynb` | The Bancada annotation platform: deliverable set vs operational ledger, human/synthetic composition, reviewer calibration against hidden targets, the triage-leak metric, the second-pass edit trail, active time per task type, and one item walked end to end. |
| `02_labeling_campaign_qc.ipynb` | The seed-labeling campaign (stage s07): quota allocation with deficit redistribution, gold-slot reuse, batch state machine, seed composition; and the near-duplicate validation of stage s06, checked as an exact invariant against `dedup_near_map.parquet`. |
| `*.html` | The same notebooks, executed and rendered. Committed so a reader gets the numbers without a Python environment. |

## Running them

```powershell
# uv NÃO está no PATH desta máquina até abrir um terminal novo — use o caminho completo.
& "$env:USERPROFILE\.local\bin\uv.exe" sync --group analysis

& "$env:USERPROFILE\.local\bin\uv.exe" run --group analysis jupyter nbconvert --execute --to html --ExecutePreprocessor.kernel_name=python3 analysis\01_annotation_qc.ipynb
& "$env:USERPROFILE\.local\bin\uv.exe" run --group analysis jupyter nbconvert --execute --to html --ExecutePreprocessor.kernel_name=python3 analysis\02_labeling_campaign_qc.ipynb
```

`matplotlib`, `jupyter` and `nbconvert` live in the **`analysis` dependency group**, not in
the project's runtime dependencies: nothing under `src/` imports matplotlib, and whoever
only runs the pipeline or the interface should not have to download a notebook stack.

### Afterwards: clear the orphan `-shm`

A read-only SQLite connection in WAL mode **creates** `prompts.sqlite-shm` and cannot remove
it on close, because removing it is a write. That orphan file is exactly the signal the
`pf load-db` swap pre-flight reads as *"someone has this database open"* — so a kernel that
has already exited would make the next corpus reload refuse to swap. The last cell of
notebook 01 reports any file it left behind. Clear it with:

```powershell
& "$env:USERPROFILE\.local\bin\uv.exe" run pf annotate status
```

Never delete the `-wal` by hand: it can hold committed transactions that have not yet been
folded into the main file.

### Running from a clean clone

Both notebooks **render without the data**. Every section is guarded: a missing database or
parquet prints what is absent and the command that builds it, and execution continues. A
notebook that raised on a missing input would produce a half-written HTML, which is worse
than a page that honestly says what it could not read.

## Two panels currently say "not measured"

Neither is a zero. Both are absences, and the distinction is the point — reporting `0.0`
where nothing was measured would read as a reviewer who gets everything wrong, or a campaign
that fails its gate. Two levers, both the owner's:

**1. Reviewer calibration (notebook 01).** Four synthetic annotations are queued, each
carrying a hidden target rating and a planted defect family, none reviewed yet. Open the
Bancada (`pf annotate`, http://127.0.0.1:8766), triage them blind like any other item, then
rate them in Rate and Review. Re-render notebook 01: the "not measured" panel is replaced by
a confusion matrix of reviewer-vs-target, plus exact agreement, within-one agreement and
mean absolute error on the ordinal scale.

**2. Campaign agreement and the label distribution (notebook 02).** All 155 batches are
`pending`, `manifest.gold` is `null`, and `data/final/seed_labels.parquet` does not exist.
Run `pf labels gold --file <hand-revised calibration>.jsonl`, then the campaign itself (the
`rotular-prompts` skill, or `pf labels next` / `pf labels submit`), then `pf merge-labels`.
Re-render notebook 02: per-batch agreement appears against the 0.80 gate, and the taxonomy
class distribution replaces the all-zero placeholder.

## Licence and disclosure policy of the rendered HTML

The two `.html` files are committed to the repository, so what goes into them is a
deliberate decision, not a side effect of what happened to print.

**No prompt text.** The prompts come from public conversation corpora under mixed licences
(ODC-BY-1.0, CC-BY-4.0, CC-BY-SA-3.0, CC-BY-NC-4.0, Apache-2.0, MIT). `labeling/seed/`,
`labeling/batches/` and `data/` are gitignored precisely because they carry full prompt
bodies. Neither notebook prints a prompt body — from the corpus, the seed, a batch file, or
even the hand-written demonstration pack. Notebook 02 reads the seed's metadata columns
only and never loads its `text` column at all. The single worked example in notebook 01
shows the provenance block, the *shape* of the payload and the QC chain; the full text is
available locally through `pf annotate export --perfil audit --anotacao <id>`.

**No hidden target of an unreviewed item.** Synthetic annotations exist to calibrate the
reviewer, and the reviewer is the person reading this repository. Notebook 01 prints how
many synthetic items are queued and what unlocks the measurement, and **withholds** the
per-target and per-defect-family breakdowns for as long as any synthetic item is still
awaiting review — publishing the multiset of pending targets would let a reviewer allocate
ratings by elimination. Those breakdowns are printed only over items that have already been
rated, which is the same rule the platform's own audit artifact follows.

Both properties are verified against the rendered HTML, not assumed: the check greps the
pages for literal sentences taken from `labeling/batches/batch_0001.json`, from
`seed.parquet`, from the corpus and from the demo pack, and for the planted defect-family
names and hidden-target notes of every unreviewed synthetic item — with a per-page canary
string to prove the search itself works.

## Language

The notebooks are written in **English**, like every other delivered artifact
(`export.IDIOMA_DOS_ARTEFATOS`): dataset card, quality report, item audit. Code comments
stay in **pt-BR**, which is this repository's convention for internal text. Where a notebook
reproduces an internal artifact verbatim — `labeling/seed/strata.txt`, the `_note` fields of
`labeling/mappings/*.json` — it stays in Portuguese, because translating a file on the way
into a report would make the report quote something that does not exist on disk under that
wording.
