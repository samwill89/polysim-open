"""Run the keyword classifier over every market and persist the result.

Markets ingested via Gamma backfill landed with category=NULL or 'other'
because category-classification only fires in the live WS pipeline.
This script runs the same classifier offline and updates rows in place.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import aiosqlite

from polysim.config import load_config
from polysim.ingest.category import OTHER_CATEGORY, Classifier


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("polysim.db"))
    parser.add_argument("--config", type=Path, default=Path("config.yml"))
    args = parser.parse_args()

    cfg = load_config(args.config)
    classifier = Classifier(cfg.categories)

    async with aiosqlite.connect(str(args.db)) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT id, question FROM markets "
            "WHERE category IS NULL OR category = ?",
            (OTHER_CATEGORY,),
        ) as cur:
            rows = list(await cur.fetchall())
    print(f"unclassified markets to process: {len(rows)}")

    counts: Counter[str] = Counter()
    updates: list[tuple[str, str]] = []
    for r in rows:
        cat = await classifier.classify(str(r["question"] or ""))
        counts[cat] += 1
        if cat != OTHER_CATEGORY:
            updates.append((cat, str(r["id"])))

    if updates:
        async with aiosqlite.connect(str(args.db)) as conn:
            await conn.executemany(
                "UPDATE markets SET category = ? WHERE id = ?", updates,
            )
            await conn.commit()

    print()
    print("category distribution after classify:")
    for cat, n in counts.most_common():
        print(f"  {cat:<20} {n:>6}")
    print()
    print(f"updated {len(updates)} rows out of {len(rows)} processed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
