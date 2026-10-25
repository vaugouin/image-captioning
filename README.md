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
notepad .env   # ANTHROPIC_API_KEY, API_BASE, NAS_ROOT
# the film context needs the T_WC_TMDB_MOVIE_IMAGE.csv export:
copy ..\download-images\csv\T_WC_TMDB_MOVIE_IMAGE.csv .\csv\
```

Use an API key from **Philippe's Individual Org** so the calls draw on the Max
plan's monthly API credit (rule `credits-api-anthropic`).

## Run

```powershell
# the 8 films of the APP-IDEAS-010 corpus (401 backdrops, < $1 in Sonnet)
python caption_images.py --movie-ids 539,391,346,11878,75233,966,11252,2593

# smoke test first, no API call:
python caption_images.py --movie-ids 391 --limit 5 --dry-run

# one film, 5 real captions, to eyeball quality:
python caption_images.py --movie-ids 391 --limit 5
```

Resumable: a rerun skips rows already captioned for the same model (SQLite
`state.sqlite`). The caption rows accumulate in `captions.sql`; hand that to the
VPS when the run looks good.

## Model and cost

| Model | ~ / image (Batch) | 401 backdrops | all TMDb backdrops (1.64M) |
|---|---|---|---|
| **claude-sonnet-5-5** (default) | ~0.22 ¢ | ~$0.90 | ~$3,700 |
| claude-haiku-5-5 | ~0.01 ¢ | ~$0.05 | ~$180 |
| claude-opus-5-5 | ~0.45 ¢ | ~$1.80 | ~$7,400 |

Levers in the code: the per-film context block is **prompt-cached** (one cached
prefix per film), the image is **downsized locally** to `--max-dim` px (originals
on the NAS reach 3840×2160, ~3× the vision tokens), and `--effort low` (describing
a scene is not a reasoning task). The script prints a measured cost estimate per
run. For a full-TMDb pass, move to the **Batch API** (half price) and Haiku.

## Backlog

Design note: `%USERPROFILE%\Nestor\projets\t2s-backlog\app-ideas-010-image-captioning.md`.
Parent dossier: `app-ideas-010-generated-podcast-video.md`. These are local paths
on the PC and do not resolve on the VPS or on cloud agents.
