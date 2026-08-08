"""Tradeable equity universe = curated seed + dynamic chatter promotion.

SEED is the hand-picked core that always trades. ALLOWLIST is a broader set of
AI / semis / memory / storage / datacenter / power / robotics / quantum /
AI-HPC-miner names; any allowlisted ticker that starts getting real chatter
gets promoted into the active universe for the day. The allowlist is the
junk filter — it keeps the dynamic part from trading random meme tickers.
"""

from __future__ import annotations

from pathlib import Path

# ── curated seed (always active) ─────────────────────────────
SEED: dict[str, str] = {
    # compute / logic
    "NVDA": "compute", "AMD": "compute", "AVGO": "compute", "TSM": "compute",
    "ARM": "compute", "MRVL": "compute", "QCOM": "compute", "INTC": "compute",
    "MPWR": "compute", "NXPI": "compute", "TXN": "compute", "ON": "compute",
    # memory / storage  (incl. SanDisk re-IPO)
    "MU": "memory", "SNDK": "memory", "WDC": "memory", "STX": "memory",
    "RMBS": "memory",
    # semicap
    "ASML": "semicap", "AMAT": "semicap", "LRCX": "semicap", "KLAC": "semicap",
    "TER": "semicap", "ENTG": "semicap",
    # AI infra / power / networking / software
    "ANET": "ai_infra", "VRT": "ai_infra", "SMCI": "ai_infra", "DELL": "ai_infra",
    "NOW": "ai_infra", "PLTR": "ai_infra", "CRWD": "ai_infra", "ORCL": "ai_infra",
    "CRDO": "ai_infra", "ALAB": "ai_infra", "COHR": "ai_infra",
    # AI-HPC datacenter miners (Cipher etc.)
    "CIFR": "ai_hpc", "CORZ": "ai_hpc", "WULF": "ai_hpc", "IREN": "ai_hpc",
    "APLD": "ai_hpc", "NBIS": "ai_hpc",
    # power for AI
    "GEV": "power", "VST": "power", "CEG": "power", "OKLO": "power", "TLN": "power",
    # robotics / automation / 3D printing
    "TSLA": "robotics", "ISRG": "robotics", "SYM": "robotics", "SERV": "robotics",
    "SSYS": "robotics", "DDD": "robotics",
    # quantum
    "IONQ": "quantum", "RGTI": "quantum", "QBTS": "quantum",
}

# ── broader allowlist for dynamic promotion (superset of SEED) ─
_EXTRA: dict[str, str] = {
    "MCHP": "compute", "SWKS": "compute", "QRVO": "compute", "LSCC": "compute",
    "ALGM": "compute", "AMBA": "compute", "SITM": "compute", "INDI": "compute",
    "POET": "compute", "AEVA": "compute", "ADI": "compute",
    "ACLS": "semicap", "ONTO": "semicap", "COHU": "semicap", "FORM": "semicap",
    "UCTT": "semicap", "AEIS": "semicap", "KLIC": "semicap", "NVMI": "semicap",
    "CAMT": "semicap",
    "HPE": "ai_infra", "CSCO": "ai_infra", "CIEN": "ai_infra", "LITE": "ai_infra",
    "NTNX": "ai_infra", "PSTG": "ai_infra", "NTAP": "ai_infra", "ANET": "ai_infra",
    "SNOW": "ai_infra", "MDB": "ai_infra", "DDOG": "ai_infra", "NET": "ai_infra",
    "AI": "ai_infra", "PANW": "ai_infra", "ZS": "ai_infra", "S": "ai_infra",
    "ESTC": "ai_infra", "GTLB": "ai_infra", "U": "ai_infra",
    "ORCL": "ai_infra", "GOOGL": "ai_infra", "META": "ai_infra", "MSFT": "ai_infra",
    "AMZN": "ai_infra",
    "HUT": "ai_hpc", "RIOT": "ai_hpc", "MARA": "ai_hpc", "BTDR": "ai_hpc",
    "GLXY": "ai_hpc", "CLSK": "ai_hpc",
    "ETN": "power", "PWR": "power", "SMR": "power", "NNE": "power", "MOD": "power",
    "ABB": "robotics", "ROK": "robotics", "ZBRA": "robotics", "PATH": "robotics",
    "RR": "robotics", "NNDM": "robotics", "MKFG": "robotics", "IRBT": "robotics",
    "QUBT": "quantum", "ARQQ": "quantum", "LAES": "quantum",
}

ALLOWLIST: dict[str, str] = {**_EXTRA, **SEED}

ETF_BENCHMARKS: list[str] = ["SMH", "SOXX", "BOTZ"]
PRIMARY_BENCHMARK = "SMH"

# Specific X accounts to mirror (needs paid X access to actually ingest).
TRACKED_X_ACCOUNTS: list[str] = ["_OutlierTrading", "Citrini7"]

# Dynamic-promotion thresholds.
PROMOTE_MENTION_THRESHOLD = 40       # ApeWisdom daily mentions to promote
PROMOTE_LOOKBACK_DAYS = 3
MAX_ACTIVE = 80                      # bound price/stocktwits fan-out


def seed_symbols() -> list[str]:
    return list(SEED.keys())


def allowlist_symbols() -> list[str]:
    return list(ALLOWLIST.keys())


def theme_of(ticker: str) -> str:
    return ALLOWLIST.get(ticker.upper(), "other")


async def resolve_active_universe(db_path: Path) -> list[str]:
    """SEED plus any allowlisted ticker getting real recent chatter."""
    active = set(SEED.keys())
    if not db_path.exists():
        return sorted(active)
    from datetime import timedelta

    import aiosqlite

    from polysim.utils.time import iso, now_utc
    since = iso(now_utc() - timedelta(days=PROMOTE_LOOKBACK_DAYS))
    try:
        async with aiosqlite.connect(str(db_path)) as db, db.execute(
            "SELECT ticker, MAX(mentions) m FROM equity_sentiment "
            "WHERE source='apewisdom' AND ts >= ? GROUP BY ticker",
            (since,),
        ) as cur:
            rows = await cur.fetchall()
    except aiosqlite.OperationalError:
        return sorted(active)
    promoted = sorted(
        ((int(m), t) for t, m in rows
         if t in ALLOWLIST and t not in SEED and int(m) >= PROMOTE_MENTION_THRESHOLD),
        reverse=True,
    )
    for _, t in promoted:
        if len(active) >= MAX_ACTIVE:
            break
        active.add(t)
    return sorted(active)


# Back-compat: some callers import EQUITY_UNIVERSE / all_symbols.
EQUITY_UNIVERSE = SEED


def all_symbols() -> list[str]:
    return list(SEED.keys()) + ETF_BENCHMARKS
