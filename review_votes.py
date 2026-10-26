"""Turn the votes exported from an A/B comparison page into reviews.sql.

The comparison page (built from two caption state files, one per variant) keeps
the reviewer's votes in the browser and exports them as JSON:
    {"votes": {"<id_row>": "A" | "=" | "B" | "0"}, "notes": {"<id_row>": "..."}}
This script joins that file with both state files (for the captions, image paths
and model) and writes one upsert per judged image into reviews.sql, for
T_WC_TMDB_IMAGE_CAPTION_REVIEW (doc/sql/T_WC_TMDB_IMAGE_CAPTION_REVIEW.sql).
Load it on the VPS by voie 2, then archive it (rule archive.md).

A comment without a vote is skipped and reported, since VERDICT is mandatory.

Usage:
    python review_votes.py --votes caption-ab-votes.json \\
        --state-a state-A.sqlite --label-a "full: 25 cast lines, whole plot" \\
        --state-b state-B.sqlite --label-b "light: 15 cast lines, plot <= 2,000 chars" \\
        --experiment ab-context-20261025
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from caption_images import _sql_escape

VERDICTS = {"A": "A", "B": "B", "=": "EQUAL", "0": "BOTH_WRONG"}


def _load_state(pathstate: Path) -> dict:
    conn = sqlite3.connect(pathstate)
    try:
        return {r[1]: r for r in conn.execute(
            "SELECT table_name, id_row, id_entity, image_path, model, caption "
            "FROM caption_state WHERE status='done'")}
    finally:
        conn.close()


def _sql_text(strvalue: str | None) -> str:
    return "NULL" if not strvalue else "'" + _sql_escape(strvalue) + "'"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--votes", required=True, type=Path, help="JSON exported by the comparison page")
    parser.add_argument("--state-a", required=True, type=Path)
    parser.add_argument("--state-b", required=True, type=Path)
    parser.add_argument("--label-a", required=True)
    parser.add_argument("--label-b", required=True)
    parser.add_argument("--experiment", required=True, help="ex. ab-context-20261025")
    parser.add_argument("--reviewer", default="vaugouin")
    parser.add_argument("--out", type=Path, default=Path("reviews.sql"), help="appended to")
    args = parser.parse_args()

    dctexport = json.loads(args.votes.read_text(encoding="utf-8"))
    # Page bug before 2026-10-26: the comment field also wrote its text under the key "".
    dctvotes = {k: v for k, v in dctexport.get("votes", {}).items() if k.isdigit()}
    dctnotes = {k: v.strip() for k, v in dctexport.get("notes", {}).items() if k.isdigit() and v.strip()}
    dcta, dctb = _load_state(args.state_a), _load_state(args.state_b)

    arrlines, dctcount = [], {}
    for strrow, strvote in sorted(dctvotes.items(), key=lambda kv: int(kv[0])):
        lngrow = int(strrow)
        if strvote not in VERDICTS or lngrow not in dcta or lngrow not in dctb:
            print(f"skipped {strrow}: vote {strvote!r} or caption missing", file=sys.stderr)
            continue
        strtable, _, lngentity, strpath, strmodel, strcapa = dcta[lngrow]
        strverdict = VERDICTS[strvote]
        dctcount[strverdict] = dctcount.get(strverdict, 0) + 1
        arrlines.append(
            "INSERT INTO T_WC_TMDB_IMAGE_CAPTION_REVIEW (EXPERIMENT, TABLE_SOURCE, ID_ROW, ID_ENTITY, "
            "IMAGE_PATH, CAPTION_MODEL, VARIANT_A, CAPTION_A, VARIANT_B, CAPTION_B, VERDICT, REVIEW_COMMENT, REVIEWER) "
            f"VALUES ({_sql_text(args.experiment)}, {_sql_text(strtable)}, {lngrow}, {lngentity}, "
            f"{_sql_text(strpath)}, {_sql_text(strmodel)}, {_sql_text(args.label_a)}, {_sql_text(strcapa)}, "
            f"{_sql_text(args.label_b)}, {_sql_text(dctb[lngrow][5])}, '{strverdict}', "
            f"{_sql_text(dctnotes.get(strrow))}, {_sql_text(args.reviewer)})\n"
            "ON DUPLICATE KEY UPDATE VERDICT=VALUES(VERDICT), REVIEW_COMMENT=VALUES(REVIEW_COMMENT), "
            "TIM_UPDATED=CURRENT_TIMESTAMP;\n")
    for strrow in sorted(set(dctnotes) - set(dctvotes)):
        print(f"skipped comment on {strrow}: no vote", file=sys.stderr)

    with args.out.open("a", encoding="utf-8") as fileout:
        fileout.writelines(arrlines)
    print(f"{len(arrlines)} reviews appended to {args.out}: "
          + ", ".join(f"{k} {v}" for k, v in sorted(dctcount.items()))
          + f"; {sum(1 for k in dctnotes if k in dctvotes)} with a comment")
    return 0


if __name__ == "__main__":
    sys.exit(main())
