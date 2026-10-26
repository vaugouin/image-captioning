"""Caption TMDb backdrop images with a Claude vision model, grounded per film.

Runs on the Windows machine (it needs the NAS at NAS_ROOT and Internet). For
each backdrop listed in the exported T_WC_TMDB_MOVIE_IMAGE.csv, it:

  1. resolves the image on the NAS (same sharded layout as download-images),
  2. fetches the film's context once from the fastapi-text2sql API
     (GET {TEXT2SQL_BASE_URL}/movies/{id}) and caches it, so every backdrop of the
     same film reuses one prompt-cached prefix,
  3. downsizes the image locally (Pillow) to keep vision token cost down,
  4. asks Claude for one anchored caption ("the final duel between the Yojimbo
     and Unosuke", not "two men in a desert"),
  5. records the caption in a resumable SQLite state DB and appends an INSERT
     to an .sql file loaded into the DB on the VPS via voie 1 (ssh vps-agent run;
     the caption tables are whitelisted since 2026-10-26).

The caption only ranks the image for retrieval; it never grounds a claim.

Usage:
    python caption_images.py --movie-ids 539,391,346,11878,75233,966,11252,2593
    python caption_images.py --movie-ids 391 --limit 5 --dry-run
"""
from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from PIL import Image

csv.field_size_limit(10_000_000)

# API prices (USD per 1M tokens), first-party Anthropic, Oct 2026. Used only to
# print a cost estimate at the end; not authoritative for billing.
PRICES = {
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-opus-5-5": (4.00, 20.00),
}

SYSTEM_PROMPT = (
    "You caption film and television backdrop images (screenshots) so they can "
    "later be retrieved by meaning. You are given a FILM CONTEXT block (title, "
    "year, synopsis, characters, cast) and one image from that film. Write ONE "
    "vivid sentence, at most 40 words, describing what the image depicts, anchored "
    "in the film: name the specific characters, actors or scene when the context "
    "lets you identify them; describe the setting, the action and the mood. "
    "Do not open with 'This image shows' or 'The image'. If you cannot tell which "
    "scene it is, describe the visible content plainly and do NOT invent plot. "
    "Always write in English, even when parts of the context are in French. "
    "Output only the sentence, nothing else."
)


# ---------------------------------------------------------------------------
# NAS path layout (shared with download-images)
# ---------------------------------------------------------------------------

def _shard(lngid: int) -> str:
    """3-digit zero-padded shard from an entity ID (id % 1000)."""
    return f"{lngid % 1000:03d}"


def _basename(strimagepath: str) -> str:
    """TMDb IMAGE_PATH starts with '/'; the filename is what follows."""
    return strimagepath.lstrip("/")


def _movie_image_path(pathnas: Path, lngmovie: int, strimagepath: str, strsize: str) -> Path:
    return pathnas / "movie" / _shard(lngmovie) / str(lngmovie) / strsize / _basename(strimagepath)


# ---------------------------------------------------------------------------
# Film context from the fastapi-text2sql API
# ---------------------------------------------------------------------------

def f_fetch_movie_context(strapibase: str, strkeyname: str, strapikey: str,
                          lngmovie: int, lngmaxchars: int,
                          lngmaxcast: int = 25, lngplotchars: int = 0) -> str | None:
    """Fetch GET {api}/movies/{id} and return a curated text block, or None.

    The raw JSON is ~150 KB and front-loads technical fields: a blind trim
    would drop the cast and the plot, the two things a caption anchors on. So
    the block is composed from the fields that name scenes and people (see
    _compose_context). The API rejects unauthenticated calls (401), so the
    auth header is sent when set.

    The plot comes from English Wikipedia; when that page has no plot section
    (often the case for a French film), the French page's synopsis is used.
    """
    strurl = f"{strapibase.rstrip('/')}/movies/{lngmovie}"
    dctheaders = {"Accept": "application/json"}
    if strapikey:
        dctheaders[strkeyname] = strapikey
    try:
        dctmovie = _get_json(strurl, dctheaders)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        print(f"  ! context fetch failed for movie {lngmovie}: {exc}")
        return None
    tplplot = _find_plot(dctmovie.get("wikipedia_content"), PLOT_TITLES_EN, "Wikipedia, English")
    if tplplot is None:
        try:
            dctfr = _get_json(f"{strurl}?ui_language=fr", dctheaders)
            tplplot = _find_plot(dctfr.get("wikipedia_content"), PLOT_TITLES_FR, "Wikipedia, French")
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            print(f"  ! French Wikipedia fetch failed for movie {lngmovie}: {exc}")
    if tplplot and lngplotchars and len(tplplot[1]) > lngplotchars:
        # Cut at the last sentence end before the cap, so the plot stays readable.
        strcut = tplplot[1][:lngplotchars]
        lngdot = strcut.rfind(". ")
        tplplot = (tplplot[0], strcut[: lngdot + 1] if lngdot > lngplotchars // 2 else strcut)
    strtext = _compose_context(dctmovie, tplplot, lngmaxcast)
    if len(strtext) > lngmaxchars:
        strtext = strtext[:lngmaxchars] + "\n... [truncated]"
    return strtext


PLOT_TITLES_EN = ("plot", "synopsis", "story", "premise")
PLOT_TITLES_FR = ("synopsis", "résumé", "intrigue", "histoire", "scénario")


def _get_json(strurl: str, dctheaders: dict) -> dict:
    req = urllib.request.Request(strurl, headers=dctheaders)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _find_plot(arrsections, tpltitles: tuple, strlabel: str) -> tuple[str, str] | None:
    """Return (label, text) of the first non-empty section whose title starts with a plot word."""
    for dctsection in arrsections or []:
        strsect = (dctsection.get("title") or "").strip().lower()
        strcontent = (dctsection.get("content") or "").strip()
        if strsect.startswith(tpltitles) and strcontent:
            return strlabel, strcontent
    return None


def _compose_context(dctmovie: dict, tplplot: tuple[str, str] | None,
                     lngmaxcast: int = 25) -> str:
    """Build the FILM CONTEXT block: identity, people, story, places.

    Order matters for the trim: the Wikipedia plot goes last, so a long plot
    is cut before the cast is.
    """
    arrlines = []
    strtitle = dctmovie.get("MOVIE_TITLE") or ""
    strorig = dctmovie.get("ORIGINAL_TITLE") or ""
    stryear = str(dctmovie.get("RELEASE_YEAR") or "")
    arrlines.append(f"Title: {strtitle}"
                    + (f" (original title: {strorig})" if strorig and strorig != strtitle else "")
                    + (f", {stryear}" if stryear else ""))
    arrdirectors = [c["PERSON_NAME"] for c in dctmovie.get("crew") or []
                    if "Director" in (c.get("CREW_JOB") or "").split(", ")]
    if arrdirectors:
        arrlines.append("Directed by: " + ", ".join(arrdirectors))
    if dctmovie.get("RUNTIME"):
        arrlines.append(f"Runtime: {dctmovie['RUNTIME']} min")
    arrgenres = [g.get("GENRE_NAME") or g.get("NAME") or "" for g in dctmovie.get("genres") or []
                 if isinstance(g, dict)]
    if any(arrgenres):
        arrlines.append("Genres: " + ", ".join(s for s in arrgenres if s))
    if dctmovie.get("TAGLINE"):
        arrlines.append(f"Tagline: {dctmovie['TAGLINE']}")
    if dctmovie.get("OVERVIEW"):
        arrlines.append(f"Overview: {dctmovie['OVERVIEW']}")
    arrcast = sorted(dctmovie.get("cast") or [], key=lambda c: c.get("DISPLAY_ORDER") or 9999)
    arrcastlines = [f"- {c['PERSON_NAME']} as {c['CAST_CHARACTER']}" if c.get("CAST_CHARACTER")
                    else f"- {c['PERSON_NAME']}" for c in arrcast[:lngmaxcast]]
    if arrcastlines:
        arrlines.append("Cast (actor as character):\n" + "\n".join(arrcastlines))
    arrbased = [f"{b['SOURCE_WORK_NAME']} ({b.get('SOURCE_WORK_YEAR') or '?'})"
                for b in dctmovie.get("based_on") or [] if b.get("SOURCE_WORK_NAME")]
    if arrbased:
        arrlines.append("Based on: " + ", ".join(arrbased))
    arrplaces = [l["LOCATION_NAME"] for l in dctmovie.get("locations") or [] if l.get("LOCATION_NAME")]
    if arrplaces:
        arrlines.append("Locations: " + ", ".join(arrplaces))
    if tplplot:
        arrlines.append(f"Plot ({tplplot[0]}):\n{tplplot[1]}")
    return "\n".join(arrlines)


# ---------------------------------------------------------------------------
# Image reduction
# ---------------------------------------------------------------------------

def _reduce_image(pathimg: Path, lngmaxdim: int) -> tuple[str, str]:
    """Load, downscale so the long edge <= lngmaxdim, re-encode JPEG, base64.

    Returns (base64_data, media_type).
    """
    with Image.open(pathimg) as img:
        img = img.convert("RGB")
        lngw, lngh = img.size
        lnglong = max(lngw, lngh)
        if lnglong > lngmaxdim:
            dblscale = lngmaxdim / lnglong
            img = img.resize((round(lngw * dblscale), round(lngh * dblscale)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
    strdata = base64.standard_b64encode(buf.getvalue()).decode("utf-8")
    return strdata, "image/jpeg"


# ---------------------------------------------------------------------------
# Vision call
# ---------------------------------------------------------------------------

@dataclass
class Caption:
    text: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int


def _build_params(
    strmodel: str,
    streffort: str,
    strcontext: str,
    strimgdata: str,
    strmedia: str,
    lngmaxtokens: int,
    strttl: str = "5m",
) -> dict:
    """Messages API params for one image, shared by the sync call and the batch.

    The film-context block carries the cache breakpoint, so all backdrops of a
    film share one cached prefix. A batch runs its requests concurrently and
    in any order, so it uses the 1-hour TTL to keep that prefix alive.
    """
    dctcache = {"type": "ephemeral"} if strttl == "5m" else {"type": "ephemeral", "ttl": strttl}
    return {
        "model": strmodel,
        "max_tokens": lngmaxtokens,
        "output_config": {"effort": streffort},
        "system": SYSTEM_PROMPT,
        "messages": [{
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": f"FILM CONTEXT:\n{strcontext}",
                    "cache_control": dctcache,
                },
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": strmedia, "data": strimgdata},
                },
                {"type": "text", "text": "Caption this backdrop."},
            ],
        }],
    }


def _to_caption(message) -> Caption:
    strcaption = next((b.text for b in message.content if b.type == "text"), "").strip()
    usage = message.usage
    return Caption(
        text=strcaption,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
    )


def f_caption_one(
    client: anthropic.Anthropic,
    strmodel: str,
    streffort: str,
    strcontext: str,
    strimgdata: str,
    strmedia: str,
    lngmaxtokens: int,
) -> Caption:
    """One anchored caption for one image (sync call). The film-context block is cached."""
    return _to_caption(client.messages.create(
        **_build_params(strmodel, streffort, strcontext, strimgdata, strmedia, lngmaxtokens)
    ))


# ---------------------------------------------------------------------------
# Resumable state
# ---------------------------------------------------------------------------

class State:
    """Per-(table, id_row, model) caption store, so a rerun skips done rows."""

    def __init__(self, pathdb: Path) -> None:
        self._conn = sqlite3.connect(str(pathdb), timeout=30.0)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS caption_state (
                table_name  TEXT NOT NULL,
                id_row      INTEGER NOT NULL,
                id_entity   INTEGER NOT NULL,
                image_path  TEXT NOT NULL,
                model       TEXT NOT NULL,
                caption     TEXT,
                status      TEXT NOT NULL,
                finished_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (table_name, id_row, model)
            )
            """
        )
        # batch_id: set while a row sits in a submitted Message Batch (status 'submitted').
        arrcols = [r[1] for r in self._conn.execute("PRAGMA table_info(caption_state)")]
        if "batch_id" not in arrcols:
            self._conn.execute("ALTER TABLE caption_state ADD COLUMN batch_id TEXT")
        self._conn.commit()

    def status(self, strtable: str, lngrow: int, strmodel: str) -> str | None:
        cur = self._conn.execute(
            "SELECT status FROM caption_state WHERE table_name=? AND id_row=? AND model=?",
            (strtable, lngrow, strmodel),
        )
        row = cur.fetchone()
        return row[0] if row else None

    def done(self, strtable: str, lngrow: int, strmodel: str) -> bool:
        return self.status(strtable, lngrow, strmodel) == "done"

    def record(
        self, strtable: str, lngrow: int, lngentity: int, strpath: str,
        strmodel: str, strcaption: str | None, strstatus: str, strbatch: str | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO caption_state
                (table_name, id_row, id_entity, image_path, model, caption, status, batch_id, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(table_name, id_row, model) DO UPDATE SET
                caption=excluded.caption, status=excluded.status, batch_id=excluded.batch_id,
                finished_at=CURRENT_TIMESTAMP
            """,
            (strtable, lngrow, lngentity, strpath, strmodel, strcaption, strstatus, strbatch),
        )
        self._conn.commit()

    def submitted_batches(self) -> list[str]:
        cur = self._conn.execute(
            "SELECT DISTINCT batch_id FROM caption_state WHERE status='submitted' AND batch_id IS NOT NULL"
        )
        return [r[0] for r in cur.fetchall()]

    def row_for(self, strbatch: str, lngrow: int) -> tuple[int, str, str] | None:
        """(id_entity, image_path, model) of a row submitted in that batch."""
        cur = self._conn.execute(
            "SELECT id_entity, image_path, model FROM caption_state "
            "WHERE batch_id=? AND id_row=? AND table_name='movie'",
            (strbatch, lngrow),
        )
        return cur.fetchone()

    def close(self) -> None:
        self._conn.close()


# ---------------------------------------------------------------------------
# SQL emit (loaded on the VPS, voie 1: ssh vps-agent run)
# ---------------------------------------------------------------------------

def _sql_escape(strvalue: str) -> str:
    return strvalue.replace("\\", "\\\\").replace("'", "''")


def _sql_insert(lngrow: int, lngentity: int, strpath: str, strcaption: str, strmodel: str) -> str:
    return (
        "INSERT INTO T_WC_TMDB_IMAGE_CAPTION "
        "(TABLE_SOURCE, ID_ROW, ID_ENTITY, IMAGE_PATH, CAPTION, CAPTION_MODEL) VALUES "
        f"('movie', {lngrow}, {lngentity}, '{_sql_escape(strpath)}', "
        f"'{_sql_escape(strcaption)}', '{_sql_escape(strmodel)}')\n"
        "ON DUPLICATE KEY UPDATE CAPTION=VALUES(CAPTION), TIM_UPDATED=CURRENT_TIMESTAMP;"
    )


# ---------------------------------------------------------------------------
# CSV iteration
# ---------------------------------------------------------------------------

def _iter_backdrops(pathcsv: Path, setmovies: set[int]):
    """Yield (id_row, id_movie, image_path) for textless backdrops of the wanted films.

    A backdrop with a LANG carries a title or text overlay (TMDb tags textless
    images with no language); a podcast wants clean stills, so those are skipped.
    """
    with pathcsv.open("r", encoding="utf-8", newline="") as fp:
        for row in csv.DictReader(fp):
            if row.get("TYPE_IMAGE") != "backdrop" or not row.get("IMAGE_PATH"):
                continue
            if (row.get("LANG") or "").strip():
                continue
            lngmovie = int(row["ID_MOVIE"])
            if setmovies and lngmovie not in setmovies:
                continue
            yield int(row["ID_ROW"]), lngmovie, row["IMAGE_PATH"]


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def _cost(strmodel: str, lngin: int, lngcacheread: int, lngcachewrite: int, lngout: int,
          dblwritemult: float, dbldiscount: float) -> float:
    """Estimated USD: cache writes at dblwritemult x input (1.25 for 5 min, 2 for 1 h),
    cache reads at 0.1 x input, everything times dbldiscount (0.5 for the Batch API)."""
    dblin, dblout = PRICES.get(strmodel, (0.0, 0.0))
    return dbldiscount * (
        lngin * dblin + lngcachewrite * dblin * dblwritemult
        + lngcacheread * dblin * 0.1 + lngout * dblout
    ) / 1_000_000


def _resolve_context(args, dctcontext: dict, lngmovie: int) -> str | None:
    if lngmovie not in dctcontext:
        dctcontext[lngmovie] = (
            None if args.dry_run and not args.api_base
            else f_fetch_movie_context(args.api_base, args.api_key_name, args.api_key,
                                       lngmovie, args.context_chars, args.max_cast, args.plot_chars)
        )
    return dctcontext[lngmovie]


def f_submit_batches(args, state: "State", arrbackdrops: list, pathnas: Path) -> int:
    """Submit pending backdrops as Message Batches (half price), grouped by film.

    Each batch stays under --batch-max-mb (the API caps a batch at 256 MB; a
    base64 backdrop is ~130 KB) and --batch-size requests. Submitted rows are
    marked 'submitted' with their batch id; --collect picks the results up.
    """
    arrpending = [t for t in arrbackdrops
                  if state.status("movie", t[0], args.model) not in ("done", "submitted")]
    arrpending.sort(key=lambda t: (t[1], t[0]))  # by film, so a batch shares cached prefixes
    if args.limit:
        arrpending = arrpending[: args.limit]
    print(f"-> {len(arrpending)} backdrops pending for batch submission")

    client = None if args.dry_run else anthropic.Anthropic()
    lngmaxbytes = args.batch_max_mb * 1_000_000
    dctcontext: dict[int, str | None] = {}
    arrreqs: list[dict] = []
    arrrows: list[tuple[int, int, str]] = []
    lngbytes = lngsubmitted = lngfail = lngbatches = 0

    def _flush() -> None:
        nonlocal lngsubmitted, lngbatches
        if args.dry_run:
            print(f"  [dry-run] would submit a batch of {len(arrreqs)} requests ({lngbytes / 1e6:.0f} MB)")
        else:
            batch = client.messages.batches.create(requests=arrreqs)
            for lngrow, lngmovie, strpath in arrrows:
                state.record("movie", lngrow, lngmovie, strpath, args.model, None, "submitted", batch.id)
            print(f"  submitted batch {batch.id}: {len(arrreqs)} requests ({lngbytes / 1e6:.0f} MB)")
        lngsubmitted += len(arrreqs)
        lngbatches += 1

    for lngrow, lngmovie, strpath in arrpending:
        pathimg = _movie_image_path(pathnas, lngmovie, strpath, args.size)
        if not pathimg.exists():
            print(f"  ! missing on NAS: {pathimg}")
            if not args.dry_run:
                state.record("movie", lngrow, lngmovie, strpath, args.model, None, "missing_file")
            lngfail += 1
            continue
        strcontext = _resolve_context(args, dctcontext, lngmovie)
        if strcontext is None and not args.allow_ungrounded:
            print(f"  ! no context for movie {lngmovie}; skipping (use --allow-ungrounded to force)")
            if not args.dry_run:
                state.record("movie", lngrow, lngmovie, strpath, args.model, None, "no_context")
            lngfail += 1
            continue
        strdata, strmedia = _reduce_image(pathimg, args.max_dim)
        dctreq = {
            "custom_id": f"movie-{lngrow}",
            "params": _build_params(args.model, args.effort, strcontext or "(no context available)",
                                    strdata, strmedia, args.max_tokens, strttl="1h"),
        }
        lngsize = len(json.dumps(dctreq))
        if arrreqs and (lngbytes + lngsize > lngmaxbytes or len(arrreqs) >= args.batch_size):
            _flush()
            arrreqs, arrrows, lngbytes = [], [], 0
        arrreqs.append(dctreq)
        arrrows.append((lngrow, lngmovie, strpath))
        lngbytes += lngsize
    if arrreqs:
        _flush()

    print(f"\nsummary: {lngsubmitted} submitted in {lngbatches} batch(es), {lngfail} skipped (missing / no context)")
    if lngsubmitted and not args.dry_run:
        print("results: run again with --collect (add --wait to poll until every batch has ended)")
    return 0


def f_collect_batches(args) -> int:
    """Fetch the results of submitted batches; write captions to state and SQL.

    A failed, expired or canceled request goes back to 'error', so the next
    submission retries it. With --wait, polls every 60 s until no
    submitted batch is left.
    """
    client = anthropic.Anthropic()
    state = State(Path(args.state))
    pathsql = Path(args.out_sql)
    fpsql = pathsql.open("a", encoding="utf-8")
    lngdone = lngfail = 0
    lngin = lngout = lngcacheread = lngcachewrite = 0
    dblcost = 0.0  # priced per row with the model it was submitted with, not --model
    try:
        while True:
            lngopen = 0
            for strbatch in state.submitted_batches():
                batch = client.messages.batches.retrieve(strbatch)
                if batch.processing_status != "ended":
                    rc = batch.request_counts
                    print(f"  batch {strbatch}: {batch.processing_status}, "
                          f"{rc.processing} processing, {rc.succeeded} succeeded so far")
                    lngopen += 1
                    continue
                for result in client.messages.batches.results(strbatch):
                    lngrow = int(result.custom_id.split("-", 1)[1])
                    tplrow = state.row_for(strbatch, lngrow)
                    if tplrow is None:
                        continue
                    lngmovie, strpath, strmodel = tplrow
                    if result.result.type == "succeeded":
                        cap = _to_caption(result.result.message)
                        if cap.text:
                            state.record("movie", lngrow, lngmovie, strpath, strmodel, cap.text, "done", strbatch)
                            fpsql.write(_sql_insert(lngrow, lngmovie, strpath, cap.text, strmodel) + "\n")
                            lngin += cap.input_tokens
                            lngout += cap.output_tokens
                            lngcacheread += cap.cache_read_tokens
                            lngcachewrite += cap.cache_write_tokens
                            dblcost += _cost(strmodel, cap.input_tokens, cap.cache_read_tokens,
                                             cap.cache_write_tokens, cap.output_tokens, 2.0, 0.5)
                            lngdone += 1
                            continue
                    print(f"  ! batch {strbatch} row {lngrow}: {result.result.type}")
                    state.record("movie", lngrow, lngmovie, strpath, strmodel, None, "error", strbatch)
                    lngfail += 1
                fpsql.flush()
                print(f"  batch {strbatch}: ended, collected")
            if not args.wait or lngopen == 0:
                break
            print(f"  {lngopen} batch(es) still running; next check in 60 s")
            time.sleep(60)
    finally:
        fpsql.close()
        state.close()

    print(f"\nsummary: {lngdone} captions collected, {lngfail} failed (resubmitted by the next submission)")
    if lngdone:
        print(
            f"tokens: in={lngin:,} cache_read={lngcacheread:,} cache_write={lngcachewrite:,} out={lngout:,}\n"
            f"estimated cost (Batch API, 1 h cache): ${dblcost:.4f}  = {100 * dblcost / lngdone:.3f} cents/image\n"
            f"SQL appended to {pathsql} -> load on the VPS (voie 1: commit+push, then ssh vps-agent run)"
        )
    return 1 if lngfail else 0


def f_run(args) -> int:
    if args.collect:
        return f_collect_batches(args)
    pathcsv = Path(args.csv_dir) / "T_WC_TMDB_MOVIE_IMAGE.csv"
    if not pathcsv.exists():
        sys.exit(f"missing CSV: {pathcsv} (copy it from download-images/csv/)")
    setmovies = {int(s) for s in args.movie_ids.split(",") if s.strip()} if args.movie_ids else set()
    if args.movie_ids_file:
        # One id per line, bare or as a mariadb table cell ("|  391 |"); any other
        # line (header, borders, run banner) is ignored.
        for strline in Path(args.movie_ids_file).read_text(encoding="utf-8").splitlines():
            strcell = strline.strip().strip("|").strip()
            if strcell.isdigit():
                setmovies.add(int(strcell))
    if not setmovies and not args.all_movies:
        sys.exit("no --movie-ids / --movie-ids-file given; pass --all-movies to caption the whole CSV on purpose")
    pathnas = Path(args.nas_root)

    arrbackdrops = list(_iter_backdrops(pathcsv, setmovies))
    print(f"-> {len(arrbackdrops)} backdrops for {len(setmovies) or 'all'} film(s), model {args.model}")

    if not args.sync:
        # Default: Message Batches. A dry run reads the real state (to count what
        # is pending) but writes nothing.
        state = State(Path(args.state))
        try:
            return f_submit_batches(args, state, arrbackdrops, pathnas)
        finally:
            state.close()

    if args.limit:
        arrbackdrops = arrbackdrops[: args.limit]
    client = None if args.dry_run else anthropic.Anthropic()
    # A dry run touches nothing on disk: in-memory state, SQL to the null device.
    state = State(Path(":memory:") if args.dry_run else Path(args.state))
    pathsql = Path(os.devnull) if args.dry_run else Path(args.out_sql)
    fpsql = pathsql.open("a", encoding="utf-8")

    dctcontext: dict[int, str | None] = {}
    lngdone = lngskip = lngfail = 0
    lngin = lngout = lngcacheread = lngcachewrite = 0

    try:
        for lngrow, lngmovie, strpath in arrbackdrops:
            if state.done("movie", lngrow, args.model):
                lngskip += 1
                continue

            pathimg = _movie_image_path(pathnas, lngmovie, strpath, args.size)
            if not pathimg.exists():
                print(f"  ! missing on NAS: {pathimg}")
                state.record("movie", lngrow, lngmovie, strpath, args.model, None, "missing_file")
                lngfail += 1
                continue

            if lngmovie not in dctcontext:
                dctcontext[lngmovie] = (
                    None if args.dry_run and not args.api_base
                    else f_fetch_movie_context(args.api_base, args.api_key_name, args.api_key,
                                               lngmovie, args.context_chars, args.max_cast,
                                               args.plot_chars)
                )
            strcontext = dctcontext[lngmovie]
            if strcontext is None and not args.allow_ungrounded:
                print(f"  ! no context for movie {lngmovie}; skipping (use --allow-ungrounded to force)")
                state.record("movie", lngrow, lngmovie, strpath, args.model, None, "no_context")
                lngfail += 1
                continue

            strdata, strmedia = _reduce_image(pathimg, args.max_dim)

            if args.dry_run:
                print(f"  [dry-run] would caption movie {lngmovie} row {lngrow} ({_basename(strpath)})")
                lngdone += 1
                continue

            try:
                cap = f_caption_one(
                    client, args.model, args.effort, strcontext or "(no context available)",
                    strdata, strmedia, args.max_tokens,
                )
            except anthropic.APIError as exc:
                print(f"  ! API error on row {lngrow}: {exc}")
                state.record("movie", lngrow, lngmovie, strpath, args.model, None, "error")
                lngfail += 1
                continue

            state.record("movie", lngrow, lngmovie, strpath, args.model, cap.text, "done")
            fpsql.write(_sql_insert(lngrow, lngmovie, strpath, cap.text, args.model) + "\n")
            fpsql.flush()
            lngin += cap.input_tokens
            lngout += cap.output_tokens
            lngcacheread += cap.cache_read_tokens
            lngcachewrite += cap.cache_write_tokens
            lngdone += 1
            print(f"  movie {lngmovie} row {lngrow}: {cap.text}")
    finally:
        fpsql.close()
        state.close()

    print(f"\nsummary: {lngdone} done, {lngskip} skipped, {lngfail} failed")
    if not args.dry_run and lngdone:
        dblcost = _cost(args.model, lngin, lngcacheread, lngcachewrite, lngout, 1.25, 1.0)
        print(
            f"tokens: in={lngin:,} cache_read={lngcacheread:,} cache_write={lngcachewrite:,} out={lngout:,}\n"
            f"estimated cost (sync, this run): ${dblcost:.4f}  = {100 * dblcost / lngdone:.3f} cents/image"
            "  (the default batch mode would halve it)\n"
            f"SQL written to {pathsql} -> load on the VPS (voie 1: commit+push, then ssh vps-agent run)"
        )
    return 1 if lngfail else 0


def main() -> int:
    load_dotenv()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--csv-dir", default=os.environ.get("CSV_DIR", "./csv"))
    p.add_argument("--nas-root", default=os.environ.get("NAS_ROOT", r"T:\prive\TMDb-images"))
    p.add_argument("--size", default=os.environ.get("NAS_SIZE", "original"),
                   help="NAS size folder to read (default: original, the only one downloaded)")
    p.add_argument("--api-base", default=os.environ.get("TEXT2SQL_BASE_URL", ""),
                   help="fastapi-text2sql base URL for GET /movies/{id}")
    p.add_argument("--api-key-name", default=os.environ.get("TEXT2SQL_API_KEY_NAME", "X-API-Key"),
                   help="auth header name expected by fastapi-text2sql")
    p.add_argument("--api-key", default=os.environ.get("TEXT2SQL_API_KEY_VALUE", ""),
                   help="auth header value for fastapi-text2sql")
    p.add_argument("--movie-ids", default="", help="comma-separated TMDb movie ids (required unless --all-movies)")
    p.add_argument("--movie-ids-file", default="",
                   help="file with one TMDb movie id per line (e.g. a SELECT result), merged with --movie-ids")
    p.add_argument("--all-movies", action="store_true",
                   help="caption every backdrop in the CSV (1.6M rows, thousands of dollars): explicit opt-in")
    p.add_argument("--model", default=os.environ.get("CAPTION_MODEL", "claude-sonnet-5-5"))
    p.add_argument("--effort", default="low", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--max-dim", type=int, default=1024, help="downscale long edge to this many px")
    p.add_argument("--max-tokens", type=int, default=400)
    p.add_argument("--context-chars", type=int, default=8000, help="trim the film context block to this many chars")
    p.add_argument("--max-cast", type=int, default=25, help="cast lines (actor as character) in the context")
    p.add_argument("--plot-chars", type=int, default=0, help="cap the Wikipedia plot to this many chars (0 = whole)")
    p.add_argument("--state", default=os.environ.get("STATE_DB", "./state.sqlite"))
    p.add_argument("--out-sql", default=os.environ.get("OUT_SQL", "./captions.sql"))
    p.add_argument("--limit", type=int, default=0, help="cap the number of backdrops (smoke test)")
    p.add_argument("--allow-ungrounded", action="store_true", help="caption even when context is missing")
    p.add_argument("--dry-run", action="store_true", help="no API call, no files written")
    p.add_argument("--sync", action="store_true",
                   help="caption one image at a time, full price, immediate results (default is the "
                        "Message Batches API: half price, results within 24 h, fetched by --collect)")
    p.add_argument("--batch-size", type=int, default=10_000, help="max requests per batch (API cap 100,000)")
    p.add_argument("--batch-max-mb", type=int, default=200, help="max payload per batch in MB (API cap 256)")
    p.add_argument("--collect", action="store_true",
                   help="fetch results of submitted batches into state and captions.sql")
    p.add_argument("--wait", action="store_true", help="with --collect, poll every 60 s until all batches end")
    args = p.parse_args()

    if args.model not in PRICES:
        print(f"note: unknown model {args.model}, cost estimate will be zero")
    return f_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
