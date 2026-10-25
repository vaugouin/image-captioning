# AGENTS.md - Agent Guide for image-captioning

This file gives you the agentic context to work on this repo safely. For project
overview, install, run and cost material, read @README.md - it is canonical and
not duplicated here.

Single canonical guide for coding agents here. @CLAUDE.md and any future
tool-specific guide (`GEMINI.md`, ...) only point here.

Deeper specs live in their own files:
- @doc/sql/T_WC_TMDB_IMAGE_CAPTION.sql - reference DDL for the caption table;
  treat as read-only unless the user explicitly asks for schema-doc edits.

Keep documentation aligned on any change: `README.md` for user-facing behaviour,
this file only when agent workflow or safety context changes.

---

## Related repositories (project ecosystem)

`image-captioning` is one stage of **Agent BBB**, a multi-repo movie/TV database
system owned by GitHub user `vaugouin`. Sibling repos live under
`%USERPROFILE%/Code/<repo>` and at `github.com/vaugouin/<repo>`, converging on a
shared MariaDB (`T_WC_*`) and a ChromaDB vector store. Canonical roster:
`%USERPROFILE%/Nestor/projets/t2s-backlog/topics/related-repositories.txt`.

**This repo's role:** Asset enrichment. It reads the TMDb backdrop images that
`download-images` mirrored onto the NAS, asks a Claude vision model for one
anchored caption per image grounded in the film's data (from the
`fastapi-text2sql` API), and writes the captions back to the DB in a new table
`T_WC_TMDB_IMAGE_CAPTION`. Those captions are then embedded into ChromaDB by
`embedding-update` so a generated podcast episode (APP-IDEAS-010, in `voice-agent`)
can retrieve the right image for a given line.

Direct upstream/downstream:
- **download-images** - mirrors the images this repo reads; shares the sharded
  NAS path layout and the resumable-SQLite pattern (copied, not imported).
- **fastapi-text2sql** - `GET /movies/{id}` supplies the per-film grounding context.
- **embedding-update** - embeds the captions into ChromaDB (next stage, not here).

---

## The captioning contract (read before changing the prompt)

- **A caption ranks, it never grounds.** It exists to retrieve the image by
  meaning. The vision model can misread a scene; that is acceptable for picking
  an ambience shot and unacceptable for a verifiable statement, which always
  comes from the DB. Do not let captions feed claims.
- **Anchored, not generic.** "the final duel between the Yojimbo and Unosuke",
  not "two men in a desert". The anchor comes from the FILM CONTEXT block passed
  with every image; without context the script skips the film unless
  `--allow-ungrounded` is set.
- **One film = one cached prefix.** The context block carries `cache_control`;
  all backdrops of a film reuse it. Keep the context block first and byte-stable
  across a film's images, or cache hits vanish (check
  `usage.cache_read_input_tokens`).

## Two-stage topology (hard constraint)

The Windows PC sees the NAS but cannot reach MariaDB (same reason
`download-images` is split). So this script runs PC-side and never writes the DB
directly: it emits `captions.sql`, which Philippe loads on the VPS.

- **Creating the table and loading captions is DDL/off-whitelist, so voie 2**
  (Philippe runs it), rule `sql-vps.md`. The caption table is not among the five
  tables the agent may write via `ssh vps-agent`. Once stable, it could be added.
- Never hand-build a `docker exec ... mariadb` pipeline. The load command is the
  three-line `runsqlvaugouindb.sh` form in `README.md` / the design note.

## API usage (rule claude-api + credits-api-anthropic)

- Model via `anthropic` official SDK, `client.messages.create`. Default
  `claude-sonnet-5-5`. **Do not pass `temperature`/`top_p`** (400 on current
  models). Thinking is left adaptive; depth is controlled by
  `output_config.effort` (`low` for captioning). `budget_tokens` is removed.
- Use an API key from **Philippe's Individual Org** so calls draw on the Max
  plan's monthly API credit (100 USD, not carried over). Do not assume a repo's
  `ANTHROPIC_API_KEY` is from that org; have it checked before counting on the credit.
- **Every run spends real money.** Smoke-test with `--dry-run`, then `--limit`
  on one film, before a full pass. The script prints a measured cost estimate.
- **Batch API is the default mode** (Philippe, 2026-10-25): a run submits Message
  Batches, `--collect` fetches them; `--sync` is the full-price opt-in. Rows in a
  submitted batch are `status='submitted'` with their `batch_id` in state; never
  resubmit them by hand, or the same images are paid twice.
- **Timeline stage** (`timeline_captions.py`, 2026-10-25): one text request per
  film places its captions on the film's timeline (JSON schema output), writes
  `timeline.sql` (UPDATEs on the `TIMELINE_*` columns from
  `doc/sql/archive/alter-001-timeline.sql`, applied 2026-10-25). Read by tmdb-front `movie-captions.php`.
  Same guardrail as captions: it orders a gallery, it never dates a scene.
- Run with the repo `.venv`: the global Python's `anthropic` 0.72 rejects
  `output_config` (TypeError before any request, so nothing billed).

---

## Code conventions

Match the sibling repos (Hungarian notation, legacy style):
- `str` strings, `lng` integers, `dbl` floats, `arr` lists, `int` boolean-like flags.
- Public pipeline entry points `f_*`; private helpers `_*`. Google-style docstrings.
- Broad try/except with console logging on the API loop; a failed image is
  recorded and skipped, never fatal.

## SQL object naming

UPPERCASE snake_case; persistent tables `T_WC_*`, TMDb source/reference `T_WC_TMDB_*`;
PK `ID_{ENTITY}` or a surrogate `ID_*`; unique keys `UK_*`, indexes `IDX_*`,
foreign keys reuse the referenced PK name; timestamps `TIM_*`; media paths `*_PATH`.

## Encoding

UTF-8 everywhere (Markdown, SQL, captions carry non-ASCII titles and names).
Avoid editor/terminal operations that rewrite files with mojibake.

---

**Last Updated**: 2026-10-25
**Current Version**: 0.1.3

## Backlog (Nestor second-brain)

- Design note: `%USERPROFILE%/Nestor/projets/t2s-backlog/app-ideas-010-image-captioning.md`
- Parent dossier: `%USERPROFILE%/Nestor/projets/t2s-backlog/app-ideas-010-generated-podcast-video.md`
- Cross-repo dashboard: `%USERPROFILE%/Nestor/projets/t2s-backlog/index.md`

These are local PC paths; they do not resolve on the VPS or on cloud agents.
