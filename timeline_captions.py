"""Estimate where each captioned still falls in its film's timeline.

Second stage, after caption_images.py. For each film it sends ONE text request
holding the film context (runtime, plot, cast) and every caption of that film,
and asks Claude to place each still on the film's timeline: a start-end
interval in minutes, or "not placed" for promotional art or composites. Seeing
all captions at once keeps the order coherent across the film.

Batch API by default (half price; the script waits for the batch to end), or
--sync for immediate calls. A film is re-estimated only when its number of
captions changed since the last run, since a new still can shift the order.
Writes UPDATE statements to timeline.sql (voie 2, the TIMELINE_* columns exist since doc/sql/archive/alter-001-timeline.sql).

The timeline is a guess from captions and plot, good for ordering a gallery;
it never grounds a claim about when a scene happens.

Usage:
    python timeline_captions.py --movie-ids 539,391,346,11878,75233,966,11252,2593
    python timeline_captions.py --movie-ids 391 --sync
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from caption_images import PRICES, _cost, _sql_escape, f_fetch_movie_context

TIMELINE_PROMPT = (
    "You are given a FILM CONTEXT block (runtime, plot, cast) and numbered captions "
    "describing still images (backdrops) taken from that film. Estimate, for each "
    "still, where it appears in the film: an interval in minutes from the start of "
    "the film, start_min to end_min, within 0 and the runtime. Follow the plot "
    "order; use the width of the interval to express your uncertainty (a precise "
    "scene: a few minutes; a vague one: wider). Stills of the same scene get "
    "overlapping intervals. Set placed=false (and start_min=end_min=0) only for an "
    "image that is not a frame of the film: poster-like or painted promotional art, "
    "a composite, a behind-the-scenes photo. Return every id exactly once."
)

TIMELINE_SCHEMA = {
    "type": "object",
    "properties": {
        "stills": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "placed": {"type": "boolean"},
                    "start_min": {"type": "integer"},
                    "end_min": {"type": "integer"},
                },
                "required": ["id", "placed", "start_min", "end_min"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["stills"],
    "additionalProperties": False,
}


def _open_state(pathdb: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(pathdb), timeout=30.0)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS timeline_state (
            id_movie    INTEGER NOT NULL,
            model       TEXT NOT NULL,
            n_captions  INTEGER NOT NULL,
            finished_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (id_movie, model)
        )
        """
    )
    conn.commit()
    return conn


def _captions_by_film(conn: sqlite3.Connection, setmovies: set[int], strmodel: str) -> dict[int, list]:
    cur = conn.execute(
        "SELECT id_entity, id_row, caption FROM caption_state "
        "WHERE table_name='movie' AND status='done' AND model=? ORDER BY id_entity, id_row",
        (strmodel,),
    )
    dctfilms: dict[int, list] = {}
    for lngmovie, lngrow, strcaption in cur.fetchall():
        if lngmovie in setmovies:
            dctfilms.setdefault(lngmovie, []).append((lngrow, strcaption))
    return dctfilms


def _build_params(strmodel: str, streffort: str, strcontext: str, arrcaps: list) -> dict:
    strlist = "\n".join(f"[{lngrow}] {strcaption}" for lngrow, strcaption in arrcaps)
    return {
        "model": strmodel,
        "max_tokens": 16000,
        "output_config": {
            "effort": streffort,
            "format": {"type": "json_schema", "schema": TIMELINE_SCHEMA},
        },
        "system": TIMELINE_PROMPT,
        "messages": [{
            "role": "user",
            "content": f"FILM CONTEXT:\n{strcontext}\n\nSTILLS (id in brackets):\n{strlist}",
        }],
    }


def _parse(message, setids: set[int], lngruntime: int) -> list[tuple[int, int | None, int | None]]:
    """(id_row, start, end) for each still; start/end None when not placed."""
    strtext = next((b.text for b in message.content if b.type == "text"), "")
    arrout = []
    for dct in json.loads(strtext)["stills"]:
        lngid = int(dct["id"])
        if lngid not in setids:
            continue
        if not dct["placed"]:
            arrout.append((lngid, None, None))
            continue
        lngstart, lngend = sorted((int(dct["start_min"]), int(dct["end_min"])))
        if lngruntime:
            lngstart = max(0, min(lngstart, lngruntime))
            lngend = max(lngstart, min(lngend, lngruntime))
        arrout.append((lngid, lngstart, lngend))
    return arrout


def _sql_updates(arrplaced: list, strcapmodel: str, strtlmodel: str) -> list[str]:
    """UPDATE statements; placed stills ranked by (start, end, id)."""
    arrranked = sorted((t for t in arrplaced if t[1] is not None), key=lambda t: (t[1], t[2], t[0]))
    dctrank = {t[0]: i + 1 for i, t in enumerate(arrranked)}
    arrsql = []
    for lngrow, lngstart, lngend in arrplaced:
        strvals = (
            f"TIMELINE_START_MIN={lngstart}, TIMELINE_END_MIN={lngend}, TIMELINE_ORDER={dctrank[lngrow]}"
            if lngstart is not None
            else "TIMELINE_START_MIN=NULL, TIMELINE_END_MIN=NULL, TIMELINE_ORDER=NULL"
        )
        arrsql.append(
            f"UPDATE T_WC_TMDB_IMAGE_CAPTION SET {strvals}, TIMELINE_MODEL='{_sql_escape(strtlmodel)}' "
            f"WHERE TABLE_SOURCE='movie' AND ID_ROW={lngrow} AND CAPTION_MODEL='{_sql_escape(strcapmodel)}';"
        )
    return arrsql


def _runtime(strcontext: str) -> int:
    for strline in strcontext.splitlines():
        if strline.startswith("Runtime: "):
            return int(strline.split()[1])
    return 0


def f_run(args) -> int:
    setmovies = {int(s) for s in args.movie_ids.split(",") if s.strip()}
    if not setmovies:
        sys.exit("no --movie-ids given")
    conn = _open_state(Path(args.state))
    dctfilms = _captions_by_film(conn, setmovies, args.caption_model)

    arrjobs = []  # (id_movie, captions, context)
    for lngmovie, arrcaps in sorted(dctfilms.items()):
        row = conn.execute("SELECT n_captions FROM timeline_state WHERE id_movie=? AND model=?",
                           (lngmovie, args.model)).fetchone()
        if row and row[0] == len(arrcaps) and not args.force:
            print(f"  movie {lngmovie}: {len(arrcaps)} captions, unchanged since last run, skipped")
            continue
        strcontext = f_fetch_movie_context(args.api_base, args.api_key_name, args.api_key,
                                           lngmovie, args.context_chars)
        if strcontext is None:
            continue
        arrjobs.append((lngmovie, arrcaps, strcontext))
    print(f"-> {len(arrjobs)} film(s) to place on their timeline, model {args.model}")
    if not arrjobs or args.dry_run:
        return 0

    client = anthropic.Anthropic()
    dctmessages = {}
    if args.sync:
        for lngmovie, arrcaps, strcontext in arrjobs:
            dctmessages[lngmovie] = client.messages.create(
                **_build_params(args.model, args.effort, strcontext, arrcaps))
    else:
        batch = client.messages.batches.create(requests=[
            {"custom_id": f"movie-{lngmovie}",
             "params": _build_params(args.model, args.effort, strcontext, arrcaps)}
            for lngmovie, arrcaps, strcontext in arrjobs
        ])
        print(f"  submitted batch {batch.id}, waiting for it to end")
        while client.messages.batches.retrieve(batch.id).processing_status != "ended":
            time.sleep(30)
        for result in client.messages.batches.results(batch.id):
            lngmovie = int(result.custom_id.split("-", 1)[1])
            if result.result.type == "succeeded":
                dctmessages[lngmovie] = result.result.message
            else:
                print(f"  ! movie {lngmovie}: {result.result.type}")

    lngin = lngout = 0
    lngfail = 0
    with Path(args.out_sql).open("a", encoding="utf-8") as fpsql:
        for lngmovie, arrcaps, strcontext in arrjobs:
            message = dctmessages.get(lngmovie)
            if message is None:
                lngfail += 1
                continue
            lngin += message.usage.input_tokens
            lngout += message.usage.output_tokens
            setids = {lngrow for lngrow, _ in arrcaps}
            try:
                arrplaced = _parse(message, setids, _runtime(strcontext))
            except (ValueError, KeyError) as exc:
                print(f"  ! movie {lngmovie}: unreadable answer ({exc})")
                lngfail += 1
                continue
            lngmissing = len(setids) - len(arrplaced)
            for strsql in _sql_updates(arrplaced, args.caption_model, args.model):
                fpsql.write(strsql + "\n")
            conn.execute(
                "INSERT INTO timeline_state (id_movie, model, n_captions) VALUES (?, ?, ?) "
                "ON CONFLICT(id_movie, model) DO UPDATE SET n_captions=excluded.n_captions, "
                "finished_at=CURRENT_TIMESTAMP",
                (lngmovie, args.model, len(arrcaps)),
            )
            conn.commit()
            lngplaced = sum(1 for t in arrplaced if t[1] is not None)
            print(f"  movie {lngmovie}: {lngplaced} placed, {len(arrplaced) - lngplaced} not placed"
                  + (f", {lngmissing} missing from the answer" if lngmissing else ""))
    conn.close()

    dblcost = _cost(args.model, lngin, 0, 0, lngout, 1.0, 1.0 if args.sync else 0.5)
    print(f"\ntokens: in={lngin:,} out={lngout:,}  estimated cost: ${dblcost:.4f}\n"
          f"SQL appended to {args.out_sql} -> load on the VPS (voie 2), the TIMELINE_* columns exist since doc/sql/archive/alter-001-timeline.sql")
    return 1 if lngfail else 0


def main() -> int:
    load_dotenv()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--movie-ids", default="", help="comma-separated TMDb movie ids")
    p.add_argument("--model", default=os.environ.get("CAPTION_MODEL", "claude-sonnet-5-5"),
                   help="model that estimates the timeline")
    p.add_argument("--caption-model", default=os.environ.get("CAPTION_MODEL", "claude-sonnet-5-5"),
                   help="model whose captions are placed (CAPTION_MODEL of the rows)")
    p.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--api-base", default=os.environ.get("TEXT2SQL_BASE_URL", ""))
    p.add_argument("--api-key-name", default=os.environ.get("TEXT2SQL_API_KEY_NAME", "X-API-Key"))
    p.add_argument("--api-key", default=os.environ.get("TEXT2SQL_API_KEY_VALUE", ""))
    p.add_argument("--context-chars", type=int, default=8000)
    p.add_argument("--state", default=os.environ.get("STATE_DB", "./state.sqlite"))
    p.add_argument("--out-sql", default="./timeline.sql")
    p.add_argument("--sync", action="store_true", help="immediate calls at full price (default: Batch API)")
    p.add_argument("--force", action="store_true", help="re-estimate films whose captions did not change")
    p.add_argument("--dry-run", action="store_true", help="list the films to place, no API call")
    args = p.parse_args()
    if args.model not in PRICES:
        print(f"note: unknown model {args.model}, cost estimate will be zero")
    return f_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
