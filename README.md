# image-captioning

Caption TMDb backdrop images (screenshots of films and series) with a Claude
vision model, grounded in each film's own data, so the captions can be embedded
and the right image surfaced at the right beat of a generated podcast video
(APP-IDEAS-010).

A caption **ranks** an image for retrieval ("the final duel between the Yojimbo
and Unosuke"). It never **grounds** a claim: a verifiable statement comes from
the database, not from a caption the vision model might get wrong.

## Why this is its own repo

It is the inverse data flow of `download-images`: that one reads the DB and
writes the NAS; this one reads the NAS and writes the DB. It also needs an
Anthropic API key and cost control, which have no place in a downloader. It
reuses download-images' sharded NAS path layout and resumable SQLite state.

## Two-stage topology (same reason as download-images)

The PC that sees the NAS cannot reach MariaDB, so:

1. **Stage 1 (this script, on the Windows PC).** Reads backdrops off the NAS,
   fetches each film's context from the `fastapi-text2sql` API, downsizes the
   image, calls Claude, writes captions to a local SQLite and to a `captions.sql`.
2. **Stage 2 (on the VPS).** Philippe loads `captions.sql` into the DB (voie 2,
   rule `sql-vps.md`), after creating the table once from
   [doc/sql/T_WC_TMDB_IMAGE_CAPTION.sql](doc/sql/T_WC_TMDB_IMAGE_CAPTION.sql).

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
notepad .env   # ANTHROPIC_API_KEY, TEXT2SQL_* (as in voice-agent), NAS_ROOT, CSV_DIR
```

`CSV_DIR` points at the folder holding the `T_WC_TMDB_MOVIE_IMAGE.csv` export
(the NAS copy of the VPS `download-images/csv`); nothing to copy.

Use an API key from **Philippe's Individual Org** so the calls draw on the Max
plan's monthly API credit (rule `credits-api-anthropic`).

## Run

Use the repo venv (`.venv\Scripts\python`): the global Python may carry an
`anthropic` SDK too old for `output_config` (0.72 failed; the venv has 2.x).

**Batch is the default mode** (Message Batches API, half price, results within
24 h, usually within the hour). It runs in two steps:

```powershell
# 1. submit the pending backdrops (grouped by film, split into <= 200 MB batches)
python caption_images.py --movie-ids 539,391,346,11878,75233,966,11252,2593

# 2. collect the results into state.sqlite and captions.sql (rerun until all ended,
#    or --wait to poll every 60 s)
python caption_images.py --collect --wait

# count what would be submitted, no API call, nothing written:
python caption_images.py --movie-ids-file doc/sql/criterion-movie-ids-20261025.txt --dry-run

# full-price sync mode, immediate captions, for a quick look at 5 images:
python caption_images.py --movie-ids 391 --limit 5 --sync
```

A batch request uses the 1-hour cache TTL on the film context: batch requests
run concurrently and in any order, so the 5-minute default would expire
between two backdrops of the same film. Cache hits inside a batch are best-effort.

Only **textless** backdrops are captioned (empty `LANG`): a backdrop tagged with a
language carries a title or text overlay, which a podcast does not want. Captions
are in English; the plot comes from English Wikipedia, or from the French page's
synopsis when the English one has none.

For a large set, pass `--movie-ids-file` (one id per line). The Criterion
Collection list comes from
[doc/sql/criterion-movie-ids.sql](doc/sql/criterion-movie-ids.sql) (a plain
SELECT; its dated result file next to it is accepted as is, header included).

The film list is `--movie-ids` (TMDb ids). It is required: without it the script
refuses to run, since an empty list would mean every backdrop in the CSV (1.6M,
thousands of dollars). That whole-CSV pass needs an explicit `--all-movies`.

Incremental: a rerun skips rows already captioned (or already in a submitted
batch) for the same model, tracked in SQLite `state.sqlite`. Rows missing on the
NAS, without context, or failed in a batch are retried by the next run, so after
`download-images` fetches new images, the same command only pays for those. The
caption rows accumulate in `captions.sql`; hand that to the VPS when the run
looks good.

## Timeline (second stage)

`timeline_captions.py` places each captioned still on its film's timeline: one
text request per film holding the context (runtime, plot, cast) and all of the
film's captions, answered as JSON (schema-enforced): a start-end interval in
minutes, or "not placed" for promotional art and composites. Placed stills get a
chronological rank. Batch by default; a film is redone only when its caption
count changed. It writes `timeline.sql` (UPDATEs). The `TIMELINE_*` columns
exist since 2026-10-25 ([doc/sql/archive/alter-001-timeline.sql](doc/sql/archive/alter-001-timeline.sql));
each new lot loads captions first, then their timeline:

```bash
cd ~/docker/image-captioning
git pull
~/docker/tools/runsqlvaugouindb.sh ~/docker/image-captioning/captions.sql
~/docker/tools/runsqlvaugouindb.sh ~/docker/image-captioning/timeline.sql
```

Once a lot is loaded, `captions.sql`, `timeline.sql` and their dated results go
to `archive/` (rule archive.md): the scripts append to these files, so an
archived pair means the next run starts empty and the next load carries only
the new rows. The first lot (8 corpus films, 342 captions) is in `archive/`.

Measured: ~$0.03 per film in sync (A Fistful of Dollars, 61 stills), half in batch.
The interval width carries the model's uncertainty; it orders a gallery, it does
not date a scene.

## Model and cost

| Model | ~ / image (Batch) | 401 backdrops | all TMDb backdrops (1.64M) |
|---|---|---|---|
| **claude-sonnet-5-5** (default) | ~0.22 ¢ | ~$0.90 | ~$3,700 |
| claude-haiku-5-5 | ~0.01 ¢ | ~$0.05 | ~$180 |
| claude-opus-5-5 | ~0.45 ¢ | ~$1.80 | ~$7,400 |

Levers in the code: the per-film context block is **prompt-cached** (one cached
prefix per film), the image is **downsized locally** to `--max-dim` px (originals
on the NAS reach 3840×2160, ~3× the vision tokens), and `--effort low` (describing
a scene is not a reasoning task), and the **Batch API** by default (half price).
The script prints a measured cost estimate per run (sync) or per collection (batch).

## Backlog

Design note: `%USERPROFILE%\Nestor\projets\t2s-backlog\app-ideas-010-image-captioning.md`.
Parent dossier: `app-ideas-010-generated-podcast-video.md`. These are local paths
on the PC and do not resolve on the VPS or on cloud agents.
