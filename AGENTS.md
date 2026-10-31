# AGENTS.md - Agent Guide for image-captioning

Agent rules only. Overview, setup, run commands, VPS loading, timeline, reviews
and costs: @README.md (canonical). Reference DDL in `doc/sql/` is read-only
unless the user asks for schema-doc edits. Keep `README.md` aligned with any
behaviour change.

## Role in Agent BBB

Reads the TMDb backdrops `download-images` mirrored onto the NAS, captions them
with a Claude vision model grounded in `fastapi-text2sql` `GET /movies/{id}`,
and writes `T_WC_TMDB_IMAGE_CAPTION`; `embedding-update` embeds the captions
next (APP-IDEAS-010). The NAS path layout and resumable-SQLite pattern are
copied from download-images, not imported. Repo roster:
`%USERPROFILE%/Nestor/projets/t2s-backlog/topics/related-repositories.txt`.

## The captioning contract (read before changing the prompt)

- **A caption ranks, it never grounds.** It retrieves an image by meaning;
  verifiable statements always come from the DB. Do not let captions feed claims.
- **Anchored, not generic**: the anchor comes from the FILM CONTEXT block; a film
  without context is skipped unless `--allow-ungrounded`.
- **One film = one cached prefix.** Keep the context block first and
  byte-stable across a film's images, or cache hits vanish (check
  `usage.cache_read_input_tokens`).
- The timeline orders a gallery, it never dates a scene.

## DB writes (hard constraint)

The PC sees the NAS but not MariaDB: scripts only emit `.sql` files
(`captions.sql`, `timeline.sql`, `reviews.sql`). Loading them is voie 1 (both
caption tables are on the agent whitelist): commit and push first, then the
`ssh vps-agent` gate, see README § Loading on the VPS. DDL and files over 600 s
are voie 2 (rule `sql-vps.md`). Never hand-build a `docker exec ... mariadb`
pipeline. Archive each `.sql` with its dated result once loaded (README §
Timeline): the scripts append to these files.

## API usage and cost

- `anthropic` SDK, default `claude-sonnet-5-5`. Never pass `temperature`/`top_p`
  (400); depth via `output_config.effort`, no `budget_tokens`.
- Run with the repo `.venv`: the global `anthropic` 0.72 rejects `output_config`.
- Key from **Philippe's Individual Org** (Max plan credit); do not assume the
  repo's `ANTHROPIC_API_KEY` is from it, have it checked.
- **Every run spends real money**: `--dry-run`, then `--limit` on one film,
  before a full pass.
- Batch is the default. Rows with `status='submitted'` and a `batch_id` are in
  flight: never resubmit them by hand, or the images are paid twice.

## Code conventions

Match the sibling repos (Hungarian notation, legacy style): `str`, `lng`, `dbl`,
`arr`, `int` (boolean-like) prefixes; entry points `f_*`, private helpers `_*`;
Google-style docstrings. A failed image is logged and skipped, never fatal.

SQL naming: UPPERCASE snake_case; tables `T_WC_*` (`T_WC_TMDB_*` for TMDb
source); PK `ID_{ENTITY}` or surrogate `ID_*`; `UK_*`, `IDX_*`; FKs reuse the
referenced PK name; timestamps `TIM_*`; media paths `*_PATH`.

UTF-8 everywhere (captions carry non-ASCII names); avoid tools that rewrite
files with mojibake.

---

**Last Updated**: 2026-10-31
**Current Version**: 0.1.19

## Backlog (Nestor second-brain)

Design note `%USERPROFILE%/Nestor/projets/t2s-backlog/app-ideas-010-image-captioning.md`,
parent `app-ideas-010-generated-podcast-video.md`, dashboard `index.md` (same
folder). Local PC paths, not on the VPS or cloud agents.
