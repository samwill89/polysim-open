"""PolySim CLI — typer entry point.

Phase 0 implements: init, status, version.
All other commands print a phase-gated notice and exit 2.
Build plan §0, demo panel references throughout.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from polysim import __version__
from polysim.config import load_config

# Windows-console default is cp1252, which can't encode rich's box-drawing or
# our "->"-style arrows if they ever pick up a Unicode variant. Force UTF-8.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

app = typer.Typer(
    name="polysim",
    help="Prediction-market insider detection & paper copy-trading simulator.",
    no_args_is_help=True,
    add_completion=False,
)
ingest_app = typer.Typer(help="Ingest Polymarket trade + market data.", no_args_is_help=True)
flags_app = typer.Typer(help="View and inspect flags.", no_args_is_help=True)
run_app = typer.Typer(help="Manage paper-trading runs.", no_args_is_help=True)
profile_app = typer.Typer(
    help="Wallet profiles (`rebuild`) + risk profiles (`list/show/create/edit`).",
    no_args_is_help=True,
)
intel_app = typer.Typer(
    help="Tier-3 intel channels — scrape public Telegram channels for insider wallets.",
    no_args_is_help=True,
)
discovery_app = typer.Typer(
    help="Wallet discovery pipeline — empirical-priors addendum §3.",
    no_args_is_help=True,
)
experiment_app = typer.Typer(
    help="Pre-registered experiments — start, monitor, end-of-run report (§8).",
    no_args_is_help=True,
)
tournament_app = typer.Typer(
    help="Strategy tournament — 10-variant pool with periodic allocator.",
    no_args_is_help=True,
)
equity_app = typer.Typer(
    help="Equity sentiment track — AI/semis/robotics paper variants.",
    no_args_is_help=True,
)
signals_app = typer.Typer(
    help="External conversation signals — Reddit attention → market conviction.",
    no_args_is_help=True,
)

evidence_app = typer.Typer(
    help="Evidence provenance, analyst probabilities, and pre-trade risk gates.",
    no_args_is_help=True,
)

app.add_typer(ingest_app, name="ingest")
app.add_typer(flags_app, name="flags")
app.add_typer(run_app, name="run")
app.add_typer(profile_app, name="profile")
app.add_typer(intel_app, name="intel")
app.add_typer(discovery_app, name="discovery")
app.add_typer(experiment_app, name="experiment")
app.add_typer(tournament_app, name="tournament")
app.add_typer(equity_app, name="equity")
app.add_typer(signals_app, name="signals")
app.add_typer(evidence_app, name="evidence")

console = Console()


# ── Phase 0 commands ───────────────────────────────────────────


@app.command()
def version() -> None:
    """Print PolySim version."""
    console.print(f"polysim {__version__}")


@app.command()
def init(
    config_path: Path = typer.Option(
        Path("config.yml"), "--config", help="Config file to create/update."
    ),
    db_path: Path = typer.Option(Path("polysim.db"), "--db", help="SQLite database path."),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files."),
) -> None:
    """Initialize config + database and run migrations."""
    from polysim.db.migrations.runner import apply_migrations

    console.print("[bold]Initializing PolySim...[/bold]")

    # Config template
    if not config_path.exists() or force:
        template = _locate_template("config.example.yml")
        if template is None:
            console.print("[yellow]! config.example.yml not found — skipping config copy.[/yellow]")
        else:
            config_path.write_bytes(template.read_bytes())
            console.print(f"-> wrote config template to [cyan]{config_path}[/cyan]")
    else:
        console.print(
            f"-> config already exists at [cyan]{config_path}[/cyan] (use --force to overwrite)"
        )

    # .env template
    env_path = Path(".env")
    env_template = _locate_template(".env.example")
    if not env_path.exists() and env_template is not None:
        env_path.write_bytes(env_template.read_bytes())
        console.print("-> wrote .env from .env.example")

    # Migrations
    console.print(f"-> initializing database at [cyan]{db_path}[/cyan]")
    applied = asyncio.run(apply_migrations(db_path))
    if applied:
        for m in applied:
            console.print(f"   applied migration [green]{m}[/green]")
    else:
        console.print("   (schema already at latest version)")

    # Config validation — non-fatal since init also writes the default.
    if config_path.exists():
        try:
            load_config(config_path)
            console.print("-> config validated [green]ok[/green]")
        except (FileNotFoundError, ValueError) as exc:
            console.print(f"[yellow]! config validation failed: {exc}[/yellow]")

    console.print("\n[green]OK[/green] polysim initialized.")
    console.print("Next: edit config.yml if needed, then run [cyan]polysim status[/cyan].")


@app.command()
def status(
    config_path: Path = typer.Option(Path("config.yml"), "--config"),
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
) -> None:
    """Show system status — DB, config, and phase-gated feature availability."""
    from polysim.db.dao import db_stats, list_active_runs
    from polysim.db.migrations.runner import current_version

    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    console.print(f"[bold]PolySim status[/bold] - {now}")
    console.print("[dim]" + "-" * 60 + "[/dim]")

    # Database
    if db_path.exists():
        size_mb = db_path.stat().st_size / (1024 * 1024)
        version_num = asyncio.run(current_version(db_path))
        stats = asyncio.run(db_stats(db_path))
        console.print(
            f"Database:         [cyan]{db_path}[/cyan] ({size_mb:.1f} MB, schema v{version_num})"
        )
        console.print(
            f"                  trades={stats['trades']:,} "
            f"wallets={stats['wallets']:,} "
            f"markets={stats['markets']:,} "
            f"flags={stats['flags']:,}"
        )
    else:
        console.print(
            f"Database:         [yellow]{db_path}[/yellow] - not initialized. Run `polysim init`."
        )

    # Config
    if config_path.exists():
        try:
            cfg = load_config(config_path)
            console.print(
                f"Config:           [cyan]{config_path}[/cyan] "
                f"- run=[cyan]{cfg.run.name}[/cyan] mode=[cyan]{cfg.run.mode}[/cyan]"
            )
            primary = [k for k, v in cfg.categories.items() if v.enabled and v.tier == "primary"]
            secondary = [
                k for k, v in cfg.categories.items() if v.enabled and v.tier == "secondary"
            ]
            console.print(
                f"Categories:       primary={len(primary)} secondary={len(secondary)} "
                f"(primary: {', '.join(primary)})"
            )
        except (FileNotFoundError, ValueError) as exc:
            console.print(f"Config:           [red]invalid[/red] - {exc}")
    else:
        console.print(
            f"Config:           [yellow]{config_path}[/yellow] - missing. Run `polysim init`."
        )

    # Phase-gated features
    console.print("Ingestor:         [dim]not running (Phase 1)[/dim]")
    console.print("Wallet profiler:  [dim]not implemented (Phase 2)[/dim]")
    console.print("Scorer:           [dim]not implemented (Phase 2)[/dim]")
    console.print("Investigator:     [dim]not implemented (Phase 2, measured Phase 3)[/dim]")
    console.print("Paper executor:   [dim]not implemented (Phase 4)[/dim]")
    console.print("Reporter:         [dim]not implemented (Phase 5+)[/dim]")

    # Active paper runs
    if db_path.exists():
        try:
            runs = asyncio.run(list_active_runs(db_path))
        except Exception:
            runs = []
        if runs:
            console.print("\n[bold]Active runs:[/bold]")
            for r in runs:
                bal = int(r["current_balance_cents"]) / 100
                console.print(
                    f"  [{r['id']}] {r['name']}  started {r['started_at']}  balance ${bal:,.2f}"
                )
        else:
            console.print("\nActive runs:      [dim](none)[/dim]")


# ── Phase-gated stubs ──────────────────────────────────────────


def _not_yet(phase: str) -> None:
    console.print(f"[yellow]Not yet implemented. See polysim-buildplan.md {phase}.[/yellow]")
    raise typer.Exit(code=2)


@ingest_app.command("start")
def ingest_start(
    config_path: Path = typer.Option(Path("config.yml"), "--config"),
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
) -> None:
    """Start live Polymarket ingestion.

    Runs until Ctrl+C. WS reconnects automatically on network errors.
    """
    from polysim.config import load_config, load_secrets
    from polysim.ingest.pipeline import IngestPipeline

    cfg = load_config(config_path)
    secrets = load_secrets()

    console.print(f"[bold]polysim ingest start[/bold]  db=[cyan]{db_path}[/cyan]")
    console.print(
        f"-> WS   [cyan]{cfg.ingest.polymarket_ws_url}[/cyan]\n"
        f"-> REST [cyan]{cfg.ingest.polymarket_gamma_url}[/cyan]\n"
        f"-> RPC  [cyan]{cfg.ingest.polygon_rpc_url.replace('{api_key}', '***')}[/cyan]"
    )

    async def _main() -> None:
        pipeline = IngestPipeline(db_path=db_path, config=cfg, secrets=secrets)
        await pipeline.start()
        console.print("[green]OK[/green] ingest started. Press Ctrl+C to stop.")
        try:
            while True:
                await asyncio.sleep(30)
                console.print(
                    f"[dim]heartbeat  trades={pipeline.writer.inserted_count} "
                    f"enriched={pipeline.enricher_worker.enriched_count} "
                    f"markets_indexed={pipeline.indexer.indexed_count} "
                    f"ws_frames={pipeline.ws.frame_count}[/dim]"
                )
        except (KeyboardInterrupt, asyncio.CancelledError):
            console.print("\n[yellow]shutdown requested...[/yellow]")
        finally:
            await pipeline.stop()
            console.print("[green]OK[/green] ingest stopped.")

    import contextlib as _ctx

    with _ctx.suppress(KeyboardInterrupt):
        asyncio.run(_main())


@ingest_app.command("backfill-active")
def ingest_backfill_active(
    max_markets: int = typer.Option(10_000, "--max-markets"),
    page_size: int = typer.Option(500, "--page-size"),
    config_path: Path = typer.Option(Path("config.yml"), "--config"),
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
) -> None:
    """Backfill ALL currently-active markets from Gamma (paging until empty).

    The live market indexer only pulls the top-by-volume batch on each tick
    so long-tail markets (e.g. niche box-office, specific NFL games) never
    land in the DB. Run this once to hydrate the full active set.
    """
    from polysim.config import load_config
    from polysim.db import dao as _dao
    from polysim.ingest.category import Classifier
    from polysim.ingest.polymarket_rest import PolymarketREST

    cfg = load_config(config_path)
    console.print(f"[bold]polysim ingest backfill-active[/bold]  cap={max_markets}")

    async def _main() -> None:
        classifier = Classifier(cfg.categories)
        upserted = 0
        offset = 0
        async with PolymarketREST(
            gamma_base_url=cfg.ingest.polymarket_gamma_url,
            data_base_url=cfg.ingest.polymarket_data_url,
            clob_base_url=cfg.ingest.polymarket_clob_url,
        ) as rest:
            while upserted < max_markets:
                batch = await rest.list_markets(
                    active=True,
                    closed=False,
                    limit=page_size,
                    offset=offset,
                )
                if not batch:
                    break
                for m in batch:
                    category = await classifier.classify(m.question)
                    m2 = m.model_copy(update={"category": category})
                    await _dao.upsert_market(db_path, m2)
                    upserted += 1
                    if upserted >= max_markets:
                        break
                offset += page_size
                if upserted and upserted % 1000 == 0:
                    console.print(f"  ... [dim]upserted {upserted}[/dim]")
        console.print(f"[green]OK[/green] active markets upserted=[cyan]{upserted}[/cyan]")

    asyncio.run(_main())


@ingest_app.command("backfill-closed")
def ingest_backfill_closed(
    days: int = typer.Option(90, "--days"),
    max_markets: int = typer.Option(3_000, "--max-markets"),
    page_size: int = typer.Option(200, "--page-size"),
    config_path: Path = typer.Option(Path("config.yml"), "--config"),
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
) -> None:
    """Backfill *closed* markets from Gamma REST so sentiment hints can
    match historic events (e.g. elections, ceasefires, past launches)."""
    from datetime import UTC, datetime, timedelta

    from polysim.config import load_config
    from polysim.db import dao as _dao
    from polysim.ingest.category import Classifier
    from polysim.ingest.polymarket_rest import PolymarketREST

    cfg = load_config(config_path)
    since_dt = datetime.now(UTC) - timedelta(days=days)

    console.print(
        f"[bold]polysim ingest backfill-closed[/bold]  "
        f"days=[cyan]{days}[/cyan]  cap=[cyan]{max_markets}[/cyan]"
    )

    async def _main() -> None:
        classifier = Classifier(cfg.categories)
        upserted = 0
        kept = 0
        skipped_old = 0
        async with PolymarketREST(
            gamma_base_url=cfg.ingest.polymarket_gamma_url,
            data_base_url=cfg.ingest.polymarket_data_url,
            clob_base_url=cfg.ingest.polymarket_clob_url,
        ) as rest:
            offset = 0
            while upserted < max_markets:
                batch = await rest.list_markets(
                    active=False,
                    closed=True,
                    limit=page_size,
                    offset=offset,
                )
                if not batch:
                    break
                for m in batch:
                    if m.resolved_at is not None and m.resolved_at < since_dt:
                        skipped_old += 1
                        continue
                    category = await classifier.classify(m.question)
                    m2 = m.model_copy(update={"category": category})
                    await _dao.upsert_market(db_path, m2)
                    upserted += 1
                    kept += 1
                    if upserted >= max_markets:
                        break
                offset += page_size
                if upserted and upserted % 500 == 0:
                    console.print(
                        f"  ... [dim]upserted {upserted} so far  "
                        f"(skipped {skipped_old} older than {days}d)[/dim]"
                    )
        console.print(
            f"[green]OK[/green] closed markets upserted=[cyan]{upserted}[/cyan]  "
            f"skipped_too_old=[cyan]{skipped_old}[/cyan]"
        )

    asyncio.run(_main())


@ingest_app.command("backfill")
def ingest_backfill(
    days: int = typer.Option(30, "--days"),
    config_path: Path = typer.Option(Path("config.yml"), "--config"),
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
    max_markets: int = typer.Option(500, "--max-markets"),
    max_trades_per_market: int = typer.Option(5000, "--max-trades-per-market"),
) -> None:
    """Backfill historical markets + trades via REST."""
    from datetime import UTC, datetime, timedelta

    from polysim.config import load_config
    from polysim.db import dao as _dao
    from polysim.ingest.category import Classifier
    from polysim.ingest.polymarket_rest import PolymarketREST

    cfg = load_config(config_path)
    since_dt = datetime.now(UTC) - timedelta(days=days)

    console.print(f"[bold]polysim ingest backfill[/bold]  days=[cyan]{days}[/cyan]")
    console.print(f"-> since [cyan]{since_dt.isoformat()}[/cyan]  max_markets={max_markets}")

    async def _main() -> None:
        classifier = Classifier(cfg.categories)
        async with PolymarketREST(
            gamma_base_url=cfg.ingest.polymarket_gamma_url,
            data_base_url=cfg.ingest.polymarket_data_url,
            clob_base_url=cfg.ingest.polymarket_clob_url,
        ) as rest:
            # 1) Markets
            console.print("-> fetching markets...")
            market_count = 0
            classified: dict[str, int] = {}
            async for market in rest.iter_markets(
                active=None, page_size=100, max_pages=max(1, max_markets // 100 + 1)
            ):
                if market_count >= max_markets:
                    break
                category = await classifier.classify(market.question)
                classified[category] = classified.get(category, 0) + 1
                m = market.model_copy(update={"category": category})
                await _dao.upsert_market(db_path, m)
                market_count += 1
            console.print(f"   upserted {market_count} markets")
            for cat, n in sorted(classified.items(), key=lambda kv: -kv[1]):
                console.print(f"      {cat}: {n}")

            # 2) Trades for enabled-category markets.
            console.print("-> fetching trades per enabled market...")
            enabled = {k for k, v in cfg.categories.items() if v.enabled}
            enabled_markets = [
                m
                for m in await _dao.get_markets_missing_category(db_path, limit=10_000)
                # only those just-upserted that we care about
            ]
            # Reload the markets we just wrote and filter to enabled categories.
            trade_count = 0
            async for market in rest.iter_markets(
                active=None, page_size=100, max_pages=max(1, max_markets // 100 + 1)
            ):
                if market.category and market.category not in enabled:
                    continue
                trades = []
                async for t in rest.iter_trades(
                    market_id=market.id, page_size=500, max_pages=max_trades_per_market // 500
                ):
                    if t.timestamp < since_dt:
                        break
                    trades.append(t)
                if trades:
                    ins = await _dao.insert_trades_batch(db_path, trades)
                    trade_count += ins
                if trade_count >= max_markets * max_trades_per_market:
                    break
            console.print(f"   inserted {trade_count} trades")

        console.print("[green]OK[/green] backfill complete.")
        _ = enabled_markets  # referenced for side effects above

    asyncio.run(_main())


@flags_app.command("list")
def flags_list(
    since: str = typer.Option("24h", "--since"),
    category: str | None = typer.Option(None, "--category"),
    limit: int = typer.Option(50, "--limit"),
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
) -> None:
    """List recent flags (joined with market category)."""
    from rich.table import Table

    from polysim.db import dao as _dao
    from polysim.utils.time import iso, now_utc, parse_since

    try:
        delta = parse_since(since)
    except ValueError as exc:
        console.print(f"[red]Invalid --since value:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    since_dt = now_utc() - delta
    rows = asyncio.run(
        _dao.list_flags_since(db_path, since_iso=iso(since_dt), category=category, limit=limit)
    )

    if not rows:
        console.print(
            f"[dim](no flags since {iso(since_dt)}{' in ' + category if category else ''})[/dim]"
        )
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("ID", justify="right")
    table.add_column("CREATED")
    table.add_column("WALLET")
    table.add_column("MARKET")
    table.add_column("CAT")
    table.add_column("SCORE", justify="right")
    table.add_column("VERDICT")
    for r in rows:
        wallet_short = f"{r['wallet_address'][:6]}...{r['wallet_address'][-4:]}"
        market_q = r.get("question") or r["market_id"]
        if len(market_q) > 40:
            market_q = market_q[:37] + "..."
        score = r.get("composite_score") or r.get("raw_score") or 0.0
        verdict = r.get("investigator_verdict") or "-"
        acted = "+" if r.get("acted_on") else ""
        table.add_row(
            f"{int(r['id'])}",
            str(r["created_at"])[:19].replace("T", " "),
            wallet_short,
            market_q,
            (r.get("category") or "-")[:12],
            f"{float(score):.2f}",
            f"{verdict}{acted}",
        )
    console.print(table)
    console.print(f"[dim]({len(rows)} flags) — `polysim flags show <id>` for detail[/dim]")


@flags_app.command("show")
def flags_show(
    flag_id: int,
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
) -> None:
    """Show full flag detail — matches demo panel #3."""
    import json as _json

    from polysim.db import dao as _dao

    row = asyncio.run(_dao.get_flag(db_path, flag_id))
    if row is None:
        console.print(f"[red]flag #{flag_id} not found[/red]")
        raise typer.Exit(code=1)

    console.print(f"[bold]Flag #{flag_id}[/bold] - {row['created_at']}")
    console.print("[dim]" + "=" * 60 + "[/dim]\n")

    console.print("[bold]Wallet:[/bold]")
    console.print(f"  Address:       [cyan]{row['wallet_address']}[/cyan]")
    if row.get("first_seen_at"):
        console.print(f"  First seen:    {row['first_seen_at']}")
    if row.get("funding_source"):
        console.print(f"  Funding:       {row['funding_source']}")
    if row.get("nonce") is not None:
        console.print(f"  Nonce:         {row['nonce']}")
    if row.get("lifetime_trades"):
        console.print(
            f"  Lifetime:      {row['lifetime_trades']} trades, "
            f"${int(row.get('lifetime_volume_cents') or 0) / 100:,.2f}"
        )

    console.print("\n[bold]Market:[/bold]")
    console.print(f"  Question:      {row.get('question') or '(unknown)'}")
    console.print(f"  Category:      {row.get('category') or '-'}")
    vol_cents = row.get("daily_volume_usd_cents")
    if vol_cents:
        console.print(f"  24h volume:    ${int(vol_cents) / 100:,.0f}")
    if row.get("resolves_at"):
        console.print(f"  Resolves:      {row['resolves_at']}")

    console.print("\n[bold]Composite score:[/bold]")
    score = row.get("composite_score") or row.get("raw_score") or 0.0
    console.print(
        f"  [yellow]{float(score):.2f} / 10[/yellow]   detector=[cyan]{row['detector_name']}[/cyan]"
    )

    components_raw = row.get("components_json")
    if components_raw:
        try:
            comps = _json.loads(components_raw)
        except _json.JSONDecodeError:
            comps = None
        if isinstance(comps, dict):
            per_detector = comps.get("per_detector") or {}
            if isinstance(per_detector, dict) and per_detector:
                console.print("\n[bold]Per-detector breakdown:[/bold]")
                for det_name, det in per_detector.items():
                    if not isinstance(det, dict):
                        continue
                    raw = det.get("raw_score")
                    conf = det.get("confidence")
                    console.print(
                        f"  {det_name:<28}  raw={float(raw or 0):.3f}  conf={float(conf or 0):.2f}"
                    )

    verdict = row.get("investigator_verdict")
    if verdict:
        console.print(f"\n[bold]Investigator verdict:[/bold] [cyan]{verdict}[/cyan]")
        if row.get("investigator_reasoning"):
            console.print("\n" + str(row["investigator_reasoning"]))
    else:
        console.print("\n[dim]Investigator: not run[/dim]")

    console.print(f"\n[bold]Acted on:[/bold] {'YES' if row.get('acted_on') else 'no'}")


@profile_app.command("list")
def profile_list_cmd() -> None:
    """List all discoverable risk profiles (built-in + user). Addendum §4.3."""
    from rich.table import Table

    from polysim.profiles import builtin_dir, list_profiles, load_profile, user_dir

    table = Table(show_header=True, header_style="bold")
    table.add_column("NAME")
    table.add_column("MODE")
    table.add_column("MIN SCORE", justify="right")
    table.add_column("MAX OPEN", justify="right")
    table.add_column("SOURCE")
    built = {p.name for p in builtin_dir().glob("*.yml")}
    user = {p.name for p in user_dir().glob("*.yml")} if user_dir().exists() else set()
    for name in list_profiles():
        try:
            p = load_profile(name)
        except (ValueError, FileNotFoundError) as exc:
            table.add_row(name, "[red]invalid[/red]", "-", "-", str(exc))
            continue
        src = "user" if f"{name}.yml" in user else ("built-in" if f"{name}.yml" in built else "?")
        table.add_row(
            name,
            p.position_sizing_mode,
            f"{p.min_composite_score:.1f}",
            str(p.max_open_positions),
            src,
        )
    console.print(table)


@profile_app.command("show")
def profile_show_cmd(name: str) -> None:
    """Print a risk-profile YAML."""
    from polysim.profiles import profile_path

    try:
        path = profile_path(name)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(f"[dim]# {path}[/dim]")
    console.print(path.read_text(encoding="utf-8"))


@profile_app.command("create")
def profile_create_cmd(
    name: str,
    from_profile: str = typer.Option("systematic", "--from", help="Existing profile to clone."),
) -> None:
    """Scaffold a new user profile by cloning an existing one."""
    from polysim.profiles import profile_path, user_dir

    try:
        src = profile_path(from_profile)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    target_dir = user_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{name}.yml"
    if target.exists():
        console.print(f"[red]{target} already exists[/red]")
        raise typer.Exit(code=1)
    text = src.read_text(encoding="utf-8").replace(f"name: {from_profile}", f"name: {name}", 1)
    target.write_text(text, encoding="utf-8")
    console.print(f"[green]OK[/green] wrote {target}")
    console.print(f"[dim]edit via: polysim profile edit {name}[/dim]")


@profile_app.command("edit")
def profile_edit_cmd(name: str) -> None:
    """Open the profile YAML in $EDITOR."""
    import os
    import shlex
    import subprocess

    from polysim.profiles import profile_path

    try:
        path = profile_path(name)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    editor = os.environ.get("EDITOR") or ("notepad" if os.name == "nt" else "vi")
    # shlex handles editors like "code --wait".
    cmd = [*shlex.split(editor), str(path)]
    subprocess.run(cmd, check=False)


@profile_app.command("rebuild")
def profile_rebuild_cmd(
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
    staleness: int = typer.Option(
        0,
        "--staleness-seconds",
        help="Only rebuild profiles older than this; 0 rebuilds everything.",
    ),
    max_wallets: int = typer.Option(1000, "--max"),
) -> None:
    """Recompute wallet profiles. No-op for wallets already fresh."""
    from polysim.profiler import wallet_profiler

    n = asyncio.run(
        wallet_profiler.refresh_stale_profiles(
            db_path, staleness_seconds=staleness, max_wallets=max_wallets
        )
    )
    console.print(f"[green]OK[/green] recomputed {n} profile(s).")


@run_app.command("start")
def run_start(
    config_path: Path = typer.Option(Path("config.yml"), "--config"),
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
    profile_name: str = typer.Option(
        "systematic", "--profile", help="Risk profile name (systematic/medium/degen/custom)."
    ),
    name: str | None = typer.Option(None, "--name", help="Run display name."),
    balance: int | None = typer.Option(
        None, "--balance", help="Starting balance in CENTS (overrides config)."
    ),
    tag: str | None = typer.Option(None, "--tag", help="Optional experiment tag."),
) -> None:
    """Start a paper run with a given risk profile."""
    from polysim.paper.run_manager import start_run
    from polysim.profiles import load_profile

    cfg = load_config(config_path)
    try:
        profile = load_profile(profile_name)
    except FileNotFoundError as exc:
        console.print(f"[red]profile not found: {exc}[/red]")
        raise typer.Exit(code=2) from exc

    run_id = asyncio.run(
        start_run(
            db_path,
            cfg,
            profile=profile,
            name_override=name,
            balance_override_cents=balance,
            tag=tag,
        )
    )
    console.print(
        f"[green]OK[/green] started run [cyan]#{run_id}[/cyan]  "
        f"profile=[cyan]{profile.name}[/cyan]  "
        f"balance=[cyan]${(balance or cfg.run.starting_balance_cents) / 100:,.2f}[/cyan]"
        + (f"  tag=[cyan]{tag}[/cyan]" if tag else "")
    )


@run_app.command("start-all")
def run_start_all(
    config_path: Path = typer.Option(Path("config.yml"), "--config"),
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
    balance: int = typer.Option(
        1_000_000, "--balance", help="Starting balance in CENTS, identical for all runs."
    ),
    tag: str = typer.Option(..., "--tag", help="Required experiment tag, shared across runs."),
    profiles: str = typer.Option(
        "systematic,medium,degen",
        "--profiles",
        help="Comma-separated profile names.",
    ),
) -> None:
    """Launch N concurrent runs (one per profile) against one flag stream.

    Addendum §4.3.
    """
    from polysim.paper.run_manager import start_run
    from polysim.profiles import load_profile

    cfg = load_config(config_path)
    names = [n.strip() for n in profiles.split(",") if n.strip()]
    if not names:
        console.print("[red]--profiles was empty[/red]")
        raise typer.Exit(code=2)

    async def _main() -> list[tuple[str, int]]:
        results: list[tuple[str, int]] = []
        for pname in names:
            try:
                profile = load_profile(pname)
            except FileNotFoundError as exc:
                console.print(f"[red]skip {pname}: {exc}[/red]")
                continue
            rid = await start_run(
                db_path,
                cfg,
                profile=profile,
                name_override=f"{tag}-{pname}",
                balance_override_cents=balance,
                tag=tag,
            )
            results.append((pname, rid))
        return results

    out = asyncio.run(_main())
    for pname, rid in out:
        console.print(
            f"[green]OK[/green] #{rid}  profile=[cyan]{pname}[/cyan]  balance=${balance / 100:,.2f}"
        )
    console.print(f"[dim]tag={tag}  —  `polysim run compare --tag {tag}` for side-by-side[/dim]")


@run_app.command("stop")
def run_stop(
    run_id: int,
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
    notes: str | None = typer.Option(None, "--notes"),
) -> None:
    """Mark a paper run as ended."""
    from polysim.paper.run_manager import stop_run

    asyncio.run(stop_run(db_path, run_id, notes=notes))
    console.print(f"[green]OK[/green] stopped run #{run_id}")


@run_app.command("status")
def run_status_cmd(
    run_id: int,
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
) -> None:
    """Show detailed run status."""
    from polysim.paper.run_manager import run_status

    s = asyncio.run(run_status(db_path, run_id))
    if "error" in s:
        console.print(f"[red]{s['error']}[/red]")
        raise typer.Exit(code=1)
    console.print(f"[bold]run #{s['id']}[/bold]  {s['name']}")
    console.print(f"  started:       {s['started_at']}")
    if s["ended_at"]:
        console.print(f"  ended:         {s['ended_at']}")
    if s["paused"]:
        console.print(f"  [yellow]PAUSED[/yellow] reason: {s.get('paused_reason') or '?'}")
    console.print(f"  starting bal:  ${s['starting_balance_cents'] / 100:,.2f}")
    console.print(f"  current bal:   ${s['current_balance_cents'] / 100:,.2f}")
    console.print(f"  open positions:{s['open_positions']}")
    console.print(f"  open notional: ${s['open_notional_cents'] / 100:,.2f}")
    console.print(f"  realized P&L:  ${s['realized_pnl_cents'] / 100:,.2f}")
    console.print(f"  unrealized:    ${s['unrealized_pnl_cents'] / 100:,.2f}")


@run_app.command("list")
def run_list(
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
    tag: str | None = typer.Option(None, "--tag", help="Filter by experiment tag."),
    limit: int = typer.Option(50, "--limit"),
) -> None:
    """List paper runs, optionally filtered by tag."""
    from rich.table import Table

    from polysim.db import dao as _dao

    if tag is not None:
        rows = asyncio.run(_dao.list_paper_runs_by_tag(db_path, tag=tag))
    else:
        rows = asyncio.run(_dao.list_paper_runs(db_path, limit=limit))

    if not rows:
        console.print("[dim](no runs)[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("ID", justify="right")
    table.add_column("NAME")
    table.add_column("PROFILE")
    table.add_column("TAG")
    table.add_column("STATUS")
    table.add_column("START $", justify="right")
    table.add_column("CUR $", justify="right")
    table.add_column("P&L %", justify="right")
    for r in rows:
        start = int(r.get("starting_balance_cents") or 0)
        cur = int(r.get("current_balance_cents") or 0)
        pnl_pct = 100.0 * (cur - start) / start if start else 0.0
        if r.get("ended_at"):
            status = "ended"
        elif r.get("paused_at"):
            status = "PAUSED"
        else:
            status = "active"
        table.add_row(
            f"{int(r['id'])}",
            str(r.get("name") or "-")[:28],
            str(r.get("profile_name") or "-"),
            str(r.get("tag") or "-"),
            status,
            f"{start / 100:,.0f}",
            f"{cur / 100:,.0f}",
            f"{pnl_pct:+.1f}",
        )
    console.print(table)


@run_app.command("resume")
def run_resume(
    run_id: int,
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
) -> None:
    """Resume a paused run (addendum §5.2 — explicit operator acknowledgment)."""
    from polysim.db import dao as _dao

    row = asyncio.run(_dao.get_paper_run(db_path, run_id))
    if row is None:
        console.print(f"[red]run #{run_id} not found[/red]")
        raise typer.Exit(code=1)
    if not row.get("paused_at"):
        console.print(f"[yellow]run #{run_id} is not paused[/yellow]")
        raise typer.Exit(code=2)
    reason = row.get("pause_reason") or "?"
    console.print(f"[bold]resuming run #{run_id}[/bold]  prior pause: [yellow]{reason}[/yellow]")
    asyncio.run(_dao.resume_paper_run(db_path, run_id))
    console.print("[green]OK[/green] resumed. Drawdown/daily-loss thresholds unchanged.")


@run_app.command("compare")
def run_compare(
    run_ids: str | None = typer.Option(None, "--runs", help="Comma-separated run ids, e.g. 1,2,3"),
    tag: str | None = typer.Option(None, "--tag", help="All runs sharing this tag."),
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
) -> None:
    """Side-by-side metrics comparison for dual-mode experiments (addendum §4.3)."""
    from rich.table import Table

    from polysim.db import dao as _dao
    from polysim.evaluator.metrics import compute_run_metrics

    if not run_ids and not tag:
        console.print("[red]pass --runs or --tag[/red]")
        raise typer.Exit(code=2)

    async def _load() -> list[dict[str, Any]]:
        if tag is not None:
            runs = await _dao.list_paper_runs_by_tag(db_path, tag=tag)
        else:
            ids = [int(x) for x in (run_ids or "").split(",") if x.strip()]
            runs = await _dao.list_paper_runs_by_ids(db_path, run_ids=ids)
        out = []
        for r in runs:
            metrics = await compute_run_metrics(db_path, int(r["id"]))
            out.append({"row": r, "metrics": metrics})
        return out

    results = asyncio.run(_load())
    if not results:
        console.print("[dim]no matching runs[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("ID", justify="right")
    table.add_column("PROFILE")
    table.add_column("NAME")
    table.add_column("START $", justify="right")
    table.add_column("CUR $", justify="right")
    table.add_column("RET %", justify="right")
    table.add_column("SHARPE", justify="right")
    table.add_column("MAX DD %", justify="right")
    table.add_column("TRADES", justify="right")
    table.add_column("WIN %", justify="right")
    for item in results:
        r = item["row"]
        m = item["metrics"]
        start = int(r.get("starting_balance_cents") or 0)
        cur = int(r.get("current_balance_cents") or 0)
        ret = 100.0 * (cur - start) / start if start else 0.0
        table.add_row(
            str(r["id"]),
            str(r.get("profile_name") or "-"),
            str(r.get("name") or "-")[:20],
            f"{start / 100:,.0f}",
            f"{cur / 100:,.0f}",
            f"{ret:+.1f}",
            f"{float(m.get('sharpe_annualized') or 0):.2f}",
            f"{100 * float(m.get('max_drawdown_pct') or 0):.1f}",
            f"{int(m.get('closed_positions') or 0)}",
            f"{100 * float(m.get('win_rate') or 0):.0f}",
        )
    console.print(table)


@app.command()
def investigate(
    flag_id: int,
    config_path: Path = typer.Option(Path("config.yml"), "--config"),
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
) -> None:
    """Manually invoke the Claude investigator on a flag."""
    from polysim.config import load_config, load_secrets
    from polysim.db import dao as _dao
    from polysim.investigator.agent import Investigator, investigate_flag

    cfg = load_config(config_path)
    secrets = load_secrets()
    if not secrets.ANTHROPIC_API_KEY:
        console.print("[red]ANTHROPIC_API_KEY not set in .env — cannot run investigator.[/red]")
        raise typer.Exit(code=2)

    inv = Investigator(
        api_key=secrets.ANTHROPIC_API_KEY,
        model=cfg.investigator.model,
        triage_model=cfg.investigator.triage_model,
        triage_threshold=cfg.investigator.min_composite_to_invoke + 1.0,
        max_calls_per_day=cfg.investigator.max_calls_per_day,
        min_verdict_confidence_to_act=cfg.investigator.min_verdict_confidence_to_act,
    )

    console.print(
        f"[bold]Investigating flag #{flag_id}[/bold]  "
        f"(remaining today: {inv.remaining_calls()}/{inv.max_calls_per_day})"
    )

    outcome = asyncio.run(investigate_flag(db_path, inv, flag_id))
    if outcome is None:
        console.print(
            "[yellow]No verdict returned — daily cap hit, flag not found, "
            "or missing profile/market.[/yellow]"
        )
        raise typer.Exit(code=2)

    colour = {
        "INFORMED": "green",
        "LUCKY": "yellow",
        "UNCLEAR": "white",
    }.get(outcome.verdict, "white")
    console.print(
        f"\n[bold]Verdict:[/bold] [{colour}]{outcome.verdict}[/{colour}]  "
        f"(confidence {outcome.confidence:.2f})"
    )
    if outcome.red_flags:
        console.print("\n[red]RED FLAGS[/red]")
        for item in outcome.red_flags:
            console.print(f"  * {item}")
    if outcome.green_flags:
        console.print("\n[green]GREEN FLAGS[/green]")
        for item in outcome.green_flags:
            console.print(f"  * {item}")
    if outcome.reasoning:
        console.print(f"\n[bold]REASONING[/bold]\n{outcome.reasoning}")

    # Cost summary
    cost_row = asyncio.run(_dao.get_flag_cost(db_path, flag_id))
    if cost_row is not None:
        cents = int(cost_row.get("cost_cents") or 0)
        model = cost_row.get("model") or "?"
        console.print(
            f"\n[dim]cost: ${cents / 100:.4f}  model: {model}  "
            f"tokens: in={cost_row.get('input_tokens')} "
            f"out={cost_row.get('output_tokens')} "
            f"cached={cost_row.get('cached_tokens')}[/dim]"
        )


@app.command()
def dashboard(
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
    run_id: int | None = typer.Option(
        None,
        "--run",
        help="Attach to a specific paper run's positions + P&L.",
    ),
    split: bool = typer.Option(
        False,
        "--split",
        help="Split-pane view of all active runs. Addendum §6.",
    ),
    compare_tag: str | None = typer.Option(
        None,
        "--compare",
        help="Split-pane view of every run sharing this tag.",
    ),
    fast_refresh_s: float = typer.Option(0.5, "--fast-refresh"),
    slow_refresh_s: float = typer.Option(2.0, "--slow-refresh"),
    pop_flag: int | None = typer.Option(
        None, "--pop-flag", help="One-shot: show flag detail and exit."
    ),
    pop_position: int | None = typer.Option(
        None, "--pop-position", help="One-shot: show position detail and exit."
    ),
    pop_wallet: str | None = typer.Option(
        None, "--pop-wallet", help="One-shot: show wallet detail and exit."
    ),
) -> None:
    """Rich TUI dashboard (Ctrl+C to exit).

    --split shows all active runs side-by-side (addendum §6); --compare <tag>
    does the same for runs sharing an experiment tag.
    """
    from polysim.db import dao as _dao
    from polysim.reporter.cli_dashboard import run_dashboard, run_split_dashboard

    if pop_flag is not None:
        flags_show(pop_flag, db_path=db_path)
        return
    if pop_position is not None:
        from polysim.db import dao as _dao

        pos = asyncio.run(_dao.get_position(db_path, pop_position))
        if pos is None:
            console.print(f"[red]position #{pop_position} not found[/red]")
            raise typer.Exit(code=1)
        console.print(f"[bold]Position #{pop_position}[/bold]")
        console.print(f"  market:        {pos.get('market_id')}")
        console.print(f"  outcome:       {pos.get('outcome')}")
        console.print(f"  size:          {pos.get('size_shares')}")
        console.print(f"  avg entry:     {pos.get('avg_entry_price_cents')}c")
        console.print(f"  status:        {pos.get('status')}")
        console.print(f"  source wallet: {pos.get('source_wallet')}")
        console.print(f"  realized P&L:  {pos.get('realized_pnl_cents')}")
        return
    if pop_wallet is not None:
        from polysim.db import dao as _dao

        w = asyncio.run(_dao.get_wallet(db_path, pop_wallet))
        if w is None:
            console.print(f"[red]wallet {pop_wallet} not found[/red]")
            raise typer.Exit(code=1)
        console.print(f"[bold]Wallet[/bold] {w.address}")
        console.print(f"  first seen: {w.first_seen_at}")
        console.print(f"  nonce:      {w.nonce}")
        console.print(f"  funding:    {w.funding_source}")
        console.print(
            f"  lifetime:   {w.lifetime_trades} trades, ${w.lifetime_volume_cents / 100:,.2f}"
        )
        return

    if split or compare_tag is not None:

        async def _ids() -> list[int]:
            if compare_tag is not None:
                rows = await _dao.list_paper_runs_by_tag(db_path, tag=compare_tag)
            else:
                rows = await _dao.list_active_runs(db_path)
            return [int(r["id"]) for r in rows]

        ids = asyncio.run(_ids())
        if not ids:
            console.print("[yellow]no runs to compare[/yellow]")
            raise typer.Exit(code=2)
        try:
            asyncio.run(
                run_split_dashboard(
                    db_path,
                    run_ids=ids,
                    fast_refresh_s=max(1.0, slow_refresh_s),
                )
            )
        except KeyboardInterrupt:
            console.print("\n[dim]split dashboard exited[/dim]")
        return

    try:
        asyncio.run(
            run_dashboard(
                db_path,
                run_id=run_id,
                fast_refresh_s=fast_refresh_s,
                slow_refresh_s=slow_refresh_s,
            )
        )
    except KeyboardInterrupt:
        console.print("\n[dim]dashboard exited[/dim]")


@app.command()
def report(
    run_id: int | None = typer.Option(None, "--run"),
    tag: str | None = typer.Option(
        None,
        "--tag",
        help="Comparative report for all runs sharing a tag (addendum §7).",
    ),
    fmt: str = typer.Option("md", "--format"),
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
    out: Path | None = typer.Option(None, "--out", help="Write to file instead of stdout."),
    include_baselines: bool = typer.Option(
        True,
        "--baselines/--no-baselines",
        help="Generate null + favorite-mid baselines and include p-values.",
    ),
    calibration_png: Path | None = typer.Option(
        None, "--calibration-png", help="Also write a calibration plot PNG here."
    ),
    bootstrap_iters: int = typer.Option(
        1000,
        "--bootstrap",
        help="Bootstrap resample count for --tag reports (addendum §11).",
    ),
) -> None:
    """Generate a Markdown run report — every §10 metric + baseline comparisons.

    --tag emits a comparative report across all runs sharing that tag (addendum §7).
    """
    if fmt != "md":
        console.print(f"[red]Unsupported --format {fmt!r}; only 'md' is implemented.[/red]")
        raise typer.Exit(code=2)

    if tag is not None:
        from polysim.reporter.comparative import render_comparative_report

        md_text = asyncio.run(
            render_comparative_report(db_path, tag=tag, bootstrap_iters=bootstrap_iters)
        )
        if out is not None:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(md_text, encoding="utf-8")
            console.print(f"[green]OK[/green] wrote comparative report to {out}")
        else:
            console.print(md_text)
        return

    if run_id is None:
        console.print("[red]pass --run <id> or --tag <tag>[/red]")
        raise typer.Exit(code=2)

    from polysim.db import dao as _dao
    from polysim.evaluator import baselines
    from polysim.evaluator.calibration import save_png
    from polysim.evaluator.metrics import (
        _build_balance_series,
        _load_run_state,
        compute_run_metrics,
        daily_returns_from_balance,
    )
    from polysim.evaluator.significance import paired_t_test
    from polysim.reporter.markdown import render

    async def _render() -> str:
        metrics = await compute_run_metrics(db_path, run_id)
        if not metrics.get("run_name"):
            raise RuntimeError(f"run #{run_id} not found in {db_path}")

        null_test = None
        favorite_test = None
        if include_baselines:
            run = await _dao.get_paper_run(db_path, run_id)
            if run is not None:
                start_bal = int(run.get("starting_balance_cents") or 0)
                try:
                    null_id = await baselines.run_null_baseline(
                        db_path,
                        primary_run_id=run_id,
                        starting_balance_cents=start_bal,
                        seed=0,
                    )
                    fav_id = await baselines.run_favorite_baseline(
                        db_path,
                        primary_run_id=run_id,
                        starting_balance_cents=start_bal,
                    )
                    primary_state = await _load_run_state(db_path, run_id)
                    null_state = await _load_run_state(db_path, null_id)
                    fav_state = await _load_run_state(db_path, fav_id)
                    if primary_state and null_state and fav_state:
                        p_returns = daily_returns_from_balance(_build_balance_series(primary_state))
                        n_returns = daily_returns_from_balance(_build_balance_series(null_state))
                        f_returns = daily_returns_from_balance(_build_balance_series(fav_state))
                        null_test = paired_t_test(p_returns, n_returns)
                        favorite_test = paired_t_test(p_returns, f_returns)
                except ValueError as exc:
                    console.print(f"[yellow]baseline skipped: {exc}[/yellow]")

        png_rel: str | None = None
        if calibration_png is not None:
            buckets = metrics.get("calibration_buckets") or []
            if buckets and save_png(buckets, calibration_png):
                png_rel = str(calibration_png)

        return render(
            metrics,
            null_test=null_test,
            favorite_test=favorite_test,
            calibration_png_path=png_rel,
        )

    md = asyncio.run(_render())

    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        console.print(f"[green]OK[/green] wrote report to {out}")
    else:
        console.print(md)


@app.command()
def calibrate(
    target: str,
    days: int = typer.Option(30, "--days"),
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
) -> None:
    """Calibration — currently supports `fill-model` only."""
    if target != "fill-model":
        console.print(f"[red]Unsupported calibration target: {target!r}[/red]")
        raise typer.Exit(code=2)

    import statistics as _stats
    from datetime import UTC, datetime, timedelta

    import aiosqlite

    async def _main() -> None:
        since = datetime.now(UTC) - timedelta(days=days)
        if not db_path.exists():
            console.print("[yellow]no DB yet — nothing to calibrate.[/yellow]")
            return
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            rows: list[aiosqlite.Row] = []
            try:
                async with db.execute(
                    "SELECT latency_ms, slippage_cents, size_shares, fill_price_cents "
                    "FROM paper_fills WHERE timestamp >= ?",
                    (since.isoformat(),),
                ) as cur:
                    rows = list(await cur.fetchall())
            except aiosqlite.OperationalError:
                rows = []
        if not rows:
            console.print("[yellow]No fills in window; nothing to calibrate.[/yellow]")
            return

        latencies = [int(r["latency_ms"]) for r in rows]
        slippages = [int(r["slippage_cents"]) for r in rows]

        def pct(xs: list[int], q: float) -> int:
            if not xs:
                return 0
            s = sorted(xs)
            k = max(0, min(len(s) - 1, round(q * (len(s) - 1))))
            return int(s[k])

        console.print(f"[bold]Fill-model calibration[/bold] — {len(rows)} fills in last {days}d")
        console.print()
        console.print(
            f"  Latency (ms):  p50 {pct(latencies, 0.5):>6}   p95 {pct(latencies, 0.95):>6}"
        )
        console.print(
            f"  Slippage (c):  mean {int(_stats.mean(slippages)):>5}   "
            f"p95 {pct(slippages, 0.95):>5}"
        )
        console.print()
        console.print("[dim]Suggested config under fill_model: in config.yml:[/dim]")
        console.print(f"  detection_latency_p50_ms: {pct(latencies, 0.5)}")
        console.print(f"  detection_latency_p95_ms: {pct(latencies, 0.95)}")

    asyncio.run(_main())


@app.command()
def doctor(
    config_path: Path = typer.Option(Path("config.yml"), "--config"),
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
) -> None:
    """Operational check — DB integrity, config validity, recent flow."""
    from datetime import UTC, datetime, timedelta

    import aiosqlite

    from polysim.config import load_config, load_secrets
    from polysim.db import dao as _dao
    from polysim.db.migrations.runner import current_version

    issues: list[str] = []
    ok: list[str] = []
    cfg = None

    console.print("[bold]polysim doctor[/bold]")
    console.print("[dim]" + "-" * 50 + "[/dim]")

    # 1. DB
    if not db_path.exists():
        issues.append(f"db missing: {db_path}  (run `polysim init`)")
    else:
        version = asyncio.run(current_version(db_path))
        ok.append(f"db schema at v{version}  ({db_path})")
        stats = asyncio.run(_dao.db_stats(db_path))
        ok.append(
            f"tables: trades={stats['trades']:,} wallets={stats['wallets']:,} "
            f"markets={stats['markets']:,} flags={stats['flags']:,}"
        )

    # 2. Config
    if not config_path.exists():
        issues.append(f"config missing: {config_path}")
    else:
        try:
            cfg = load_config(config_path)
            ok.append(f"config valid  run={cfg.run.name}  mode={cfg.run.mode}")
        except Exception as exc:
            issues.append(f"config invalid: {exc}")

    # 3. Secrets
    secrets = load_secrets()
    if cfg is not None and cfg.investigator.enabled and not secrets.ANTHROPIC_API_KEY:
        issues.append("investigator enabled but ANTHROPIC_API_KEY not set in .env")
    if not secrets.ALCHEMY_API_KEY:
        ok.append("[dim]ALCHEMY_API_KEY not set (Polygon enrichment will be skipped)[/dim]")
    else:
        ok.append("ALCHEMY_API_KEY present")

    # 4. Trade flow
    if db_path.exists():

        async def _check_flow() -> tuple[int, int]:
            async with aiosqlite.connect(str(db_path)) as db:
                db.row_factory = aiosqlite.Row
                one_hour_ago = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
                one_day_ago = (datetime.now(UTC) - timedelta(days=1)).isoformat()
                try:
                    async with db.execute(
                        "SELECT COUNT(*) FROM trades WHERE timestamp >= ?",
                        (one_hour_ago,),
                    ) as cur:
                        r = await cur.fetchone()
                        recent = int(r[0]) if r and r[0] is not None else 0
                    async with db.execute(
                        "SELECT COUNT(*) FROM trades WHERE timestamp >= ?",
                        (one_day_ago,),
                    ) as cur:
                        r = await cur.fetchone()
                        daily = int(r[0]) if r and r[0] is not None else 0
                except aiosqlite.OperationalError:
                    return 0, 0
            return recent, daily

        recent, daily = asyncio.run(_check_flow())
        if daily == 0:
            ok.append(
                "[dim]no trades in last 24h — expected pre-Phase-1 or when "
                "ingest hasn't run yet[/dim]"
            )
        else:
            ok.append(f"trade flow: {recent} last-hour  {daily:,} last-24h")
            if recent == 0 and daily > 0:
                issues.append(
                    "trades arrived in last 24h but none in the last hour — ingest may be stalled"
                )

    # 5. Paper runs
    if db_path.exists():
        runs = asyncio.run(_dao.list_active_runs(db_path))
        paused = [r for r in runs if r.get("paused_at")]
        ok.append(
            f"paper runs: {len(runs)} active" + (f" ({len(paused)} paused)" if paused else "")
        )

    # Summary
    console.print()
    for msg in ok:
        console.print(f"  [green]OK[/green]  {msg}")
    for msg in issues:
        console.print(f"  [red]!![/red]  {msg}")
    console.print()

    if issues:
        console.print(f"[red]{len(issues)} issue(s) found.[/red]")
        raise typer.Exit(code=1)
    console.print("[green]all checks passed[/green]")


@app.command()
def live(
    config_path: Path = typer.Option(Path("config.yml"), "--config"),
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
    profiles: str = typer.Option(
        "systematic",
        "--profiles",
        help="Comma-separated profile names to launch (addendum §4.3).",
    ),
    tag: str | None = typer.Option(None, "--tag", help="Shared experiment tag."),
    balance: int = typer.Option(
        1_000_000,
        "--balance",
        help="Starting balance per run, in CENTS.",
    ),
    ingest: bool = typer.Option(
        True,
        "--ingest/--no-ingest",
        help="Also run the WS/REST/enrichment pipeline.",
    ),
    inbound_bot: bool = typer.Option(
        True,
        "--bot/--no-bot",
        help="Run the inbound Telegram bot (requires TELEGRAM_* in .env).",
    ),
    daily_summary: bool = typer.Option(
        True,
        "--daily-summary/--no-daily-summary",
        help="Run the daily-summary scheduler.",
    ),
    equity_track: bool = typer.Option(
        True,
        "--equity-track/--no-equity-track",
        help="Run the parallel equity sentiment track.",
    ),
    signals: bool = typer.Option(
        False,
        "--signals/--no-signals",
        help=(
            "Run conversation-signal snapshots. Also requires signals.enabled: true in config.yml."
        ),
    ),
    evidence: bool = typer.Option(
        True,
        "--evidence/--no-evidence",
        help=(
            "Enable evidence, correlation, and verified-edge gates. Also "
            "requires evidence.enabled: true in config.yml."
        ),
    ),
    equity_variants: str = typer.Option(
        "smh_bh,momentum_slow",
        "--equity-variants",
        help=(
            "Comma-separated equity variants to run; use 'all' to include "
            "research-only sentiment variants."
        ),
    ),
    daily_hour: int = typer.Option(9, "--summary-hour"),
    tz: str = typer.Option("UTC", "--tz"),
) -> None:
    """Run the whole stack (ingest + scorer + paper runs + watchdog +
    Telegram) under one process until Ctrl-C.

    Spec §2 forbids a web UI and §16 forbids live trading. This command
    keeps everything paper-only + terminal-only + Telegram.
    """
    import contextlib as _ctx

    from polysim.config import load_config, load_secrets
    from polysim.live import LiveConfig, LiveOrchestrator

    cfg = load_config(config_path)
    secrets = load_secrets()

    profile_names = [p.strip() for p in profiles.split(",") if p.strip()]
    if not profile_names:
        console.print("[red]--profiles was empty[/red]")
        raise typer.Exit(code=2)
    if equity_variants.strip().lower() == "all":
        equity_variant_names = None
    else:
        equity_variant_names = [v.strip() for v in equity_variants.split(",") if v.strip()]
        if equity_track and not equity_variant_names:
            console.print("[red]--equity-variants was empty[/red]")
            raise typer.Exit(code=2)

    live_cfg = LiveConfig(
        profiles=profile_names,
        tag=tag,
        balance_cents=balance,
        enable_ingest=ingest,
        enable_inbound_bot=inbound_bot,
        enable_daily_summary=daily_summary,
        enable_equity_track=equity_track,
        enable_signals=signals,
        enable_evidence=evidence,
        equity_variants=equity_variant_names,
        daily_summary_hour=daily_hour,
        daily_summary_tz=tz,
    )
    equity_label = "all" if equity_variant_names is None else ",".join(equity_variant_names)
    console.print(
        f"[bold]polysim live[/bold]  db=[cyan]{db_path}[/cyan]  "
        f"profiles=[cyan]{','.join(profile_names)}[/cyan]  "
        f"ingest=[cyan]{ingest}[/cyan]  bot=[cyan]{inbound_bot}[/cyan]  "
        f"equity=[cyan]{equity_track}[/cyan] variants=[cyan]{equity_label}[/cyan]  "
        f"signals=[cyan]{signals}[/cyan]  evidence=[cyan]{evidence}[/cyan]"
    )

    async def _main() -> None:
        orch = LiveOrchestrator(
            db_path=db_path,
            cfg=cfg,
            secrets=secrets,
            live_cfg=live_cfg,
        )
        await orch.start()
        console.print(
            f"[green]OK[/green] live started. runs=[cyan]{orch.run_ids}[/cyan]  "
            "Press Ctrl+C to stop."
        )
        stop_event = asyncio.Event()
        try:
            await stop_event.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            console.print("\n[yellow]shutdown requested...[/yellow]")
        finally:
            await orch.stop()
            console.print("[green]OK[/green] live stopped.")

    with _ctx.suppress(KeyboardInterrupt):
        asyncio.run(_main())


@app.command()
def web(
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Bind address. Use 0.0.0.0 to expose to LAN (single-operator only).",
    ),
    port: int = typer.Option(8765, "--port"),
) -> None:
    """Launch the read-only web dashboard (companion to TUI + Telegram).

    Single-page HTML/JS, no auth, no mutations. Mirrors all 5 demo panels:
    ingest stream, TUI dashboard, flag detail, Telegram chat, runs list.
    Refreshes every 2s. Spec §2 deliberately overridden per operator
    request — paper-only (§16) invariant remains intact.
    """
    from polysim.web.server import run_server

    console.print(
        f"[bold]polysim web[/bold]  db=[cyan]{db_path}[/cyan]  "
        f"http://{host}:{port}/  [dim](Ctrl+C to stop)[/dim]"
    )
    try:
        run_server(db_path, host=host, port=port, serve_forever=True)
    except KeyboardInterrupt:
        console.print("\n[dim]web dashboard exited[/dim]")


@intel_app.command("sync")
def intel_sync(
    channel: str = typer.Argument(
        ..., help="Channel username, e.g. 'spaceinsights' or '@spaceinsights'"
    ),
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
    config_path: Path = typer.Option(Path("config.yml"), "--config"),
    limit: int = typer.Option(50, "--limit", help="Max messages to pull this sync."),
) -> None:
    """One-shot: pull the most recent messages from a public Telegram channel."""
    from polysim.config import load_secrets
    from polysim.ingest.intel_channels import SESSION_PATH, sync_channel_once

    cfg = load_config(config_path)
    secrets = load_secrets()
    if not secrets.TELEGRAM_API_ID or not secrets.TELEGRAM_API_HASH:
        console.print(
            "[red]TELEGRAM_API_ID / TELEGRAM_API_HASH missing in .env.[/red]  "
            "Get them from https://my.telegram.org/apps."
        )
        raise typer.Exit(code=2)
    if not SESSION_PATH.exists():
        console.print(
            f"[red]no telegram session at {SESSION_PATH}[/red]  "
            "run [cyan]python scripts/telegram_user_auth.py[/cyan] first"
        )
        raise typer.Exit(code=2)

    keywords = {cat: entry.keywords for cat, entry in cfg.categories.items() if entry.enabled}

    async def _main() -> dict[str, int]:
        from telethon import TelegramClient

        client = TelegramClient(
            str(SESSION_PATH), int(secrets.TELEGRAM_API_ID), secrets.TELEGRAM_API_HASH
        )
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise RuntimeError("telegram session not authorized")
        try:
            return await sync_channel_once(
                db_path,
                client,
                channel=channel,
                keywords_by_category=keywords,
                limit=limit,
            )
        finally:
            await client.disconnect()

    result = asyncio.run(_main())
    console.print(
        f"[green]OK[/green] {result.get('channel')}: "
        f"[cyan]+{result.get('new_messages', 0)}[/cyan] messages, "
        f"[cyan]+{result.get('new_wallets', 0)}[/cyan] wallets"
    )


@intel_app.command("rematch")
def intel_rematch(
    source: str | None = typer.Option(None, "--source"),
    limit: int = typer.Option(200, "--limit"),
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
    llm_fallback: bool = typer.Option(
        True,
        "--llm-fallback/--no-llm-fallback",
        help="When fuzzy-match misses, ask Claude Haiku to pick from top-20.",
    ),
    model: str = typer.Option("claude-haiku-4-5", "--model"),
) -> None:
    """Re-run market matching on existing interpretations, including the
    LLM-fallback step. Useful after backfilling historical markets."""
    import aiosqlite
    from anthropic import AsyncAnthropic

    from polysim.config import load_secrets
    from polysim.ingest import intel_llm

    secrets = load_secrets()

    async def _main() -> None:
        # Pull interpretations that either missed (matched_market_id IS NULL
        # AND is_market_relevant=1) or had a low score.
        query = (
            "SELECT i.id, i.market_hint, i.matched_market_id, i.entities_json "
            "FROM intel_interpretations i "
            "JOIN intel_messages m ON m.id = i.intel_message_id "
            "WHERE i.is_market_relevant = 1 AND i.market_hint IS NOT NULL "
        )
        args: list[Any] = []
        if source is not None:
            query += "AND m.source = ? "
            args.append(source)
        query += "ORDER BY i.id DESC LIMIT ?"
        args.append(limit)

        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, args) as cur:
                rows = [dict(r) for r in await cur.fetchall()]

        console.print(
            f"[bold]intel rematch[/bold]  candidates=[cyan]{len(rows)}[/cyan]  "
            f"llm_fallback=[cyan]{llm_fallback}[/cyan]"
        )
        fuzzy_found = 0
        llm_found = 0
        total_cost = 0
        client: AsyncAnthropic | None = None
        try:
            for r in rows:
                hint = str(r.get("market_hint") or "")
                if not hint:
                    continue
                mid, score = await intel_llm.match_market(
                    db_path,
                    market_hint=hint,
                )
                matched_via = "fuzzy"
                if not mid and llm_fallback:
                    if client is None:
                        if not secrets.ANTHROPIC_API_KEY:
                            console.print(
                                "[yellow]no ANTHROPIC_API_KEY — skipping LLM fallback[/yellow]"
                            )
                            break
                        client = AsyncAnthropic(api_key=secrets.ANTHROPIC_API_KEY)
                    import json as _json

                    try:
                        ents = _json.loads(r.get("entities_json") or "[]")
                    except (_json.JSONDecodeError, TypeError):
                        ents = []
                    mid2, conf, usage = await intel_llm.match_market_llm(
                        db_path,
                        market_hint=hint,
                        client=client,
                        model=model,
                        entities=[str(e) for e in ents if isinstance(e, str)],
                    )
                    total_cost += usage.cost_cents
                    if mid2:
                        mid = mid2
                        score = conf
                        matched_via = "llm"
                if not mid:
                    continue
                # Only update if changed.
                if str(r.get("matched_market_id") or "") == mid:
                    continue
                async with aiosqlite.connect(str(db_path)) as db2:
                    await db2.execute(
                        "UPDATE intel_interpretations "
                        "SET matched_market_id = ?, match_score = ? WHERE id = ?",
                        (mid, score, int(r["id"])),
                    )
                    await db2.commit()
                if matched_via == "fuzzy":
                    fuzzy_found += 1
                else:
                    llm_found += 1
        finally:
            if client is not None:
                await client.close()

        console.print(
            f"[green]OK[/green] updated: fuzzy=[cyan]{fuzzy_found}[/cyan]  "
            f"llm=[cyan]{llm_found}[/cyan]  cost=[cyan]${total_cost / 100:.4f}[/cyan]"
        )

    asyncio.run(_main())


@intel_app.command("interpret")
def intel_interpret(
    source: str | None = typer.Option(None, "--source"),
    limit: int = typer.Option(100, "--limit"),
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
    model: str = typer.Option("claude-haiku-4-5", "--model"),
    llm_match_fallback: bool = typer.Option(
        True,
        "--llm-match/--no-llm-match",
        help="When fuzzy-matching misses, ask Claude to pick from top-20 candidates.",
    ),
    redo: bool = typer.Option(
        False,
        "--redo",
        help="Re-interpret messages that already have a current-version interpretation.",
    ),
) -> None:
    """LLM-parse stored intel messages into sentiment + market direction.

    Uses Claude Haiku. Skips messages already interpreted at the current
    prompt version unless --redo. Matches each interpretation to a
    concrete market in our DB when possible.
    """
    from anthropic import AsyncAnthropic

    from polysim.config import load_secrets
    from polysim.db import dao as _dao
    from polysim.ingest import intel_llm

    secrets = load_secrets()
    if not secrets.ANTHROPIC_API_KEY:
        console.print("[red]ANTHROPIC_API_KEY not set[/red]")
        raise typer.Exit(code=2)

    async def _main() -> None:
        rows = await _dao.list_intel_messages(db_path, source=source, limit=limit)
        if not rows:
            console.print("[dim](no intel messages)[/dim]")
            return

        skip_ids = set() if redo else await intel_llm.already_interpreted_ids(db_path)
        queue = [r for r in rows if int(r["id"]) not in skip_ids]
        console.print(
            f"[bold]intel interpret[/bold]  rows={len(rows)}  "
            f"already_done={len(rows) - len(queue)}  "
            f"queued=[cyan]{len(queue)}[/cyan]  model=[cyan]{model}[/cyan]"
        )
        if not queue:
            console.print("[green]nothing to do[/green]  (use --redo to re-interpret)")
            return

        client = AsyncAnthropic(api_key=secrets.ANTHROPIC_API_KEY)
        total_cost = 0
        matched = 0
        relevant = 0
        try:
            for r in queue:
                text = str(r.get("text") or "")
                if not text.strip():
                    continue
                try:
                    interp, usage = await intel_llm.interpret(
                        text,
                        client=client,
                        model=model,
                    )
                except Exception as exc:
                    console.print(f"[yellow]skip msg {r['id']}: {exc}[/yellow]")
                    continue
                mid: str | None = None
                score: float = 0.0
                if interp.is_market_relevant:
                    relevant += 1
                    if interp.market_hint:
                        mid, score = await intel_llm.match_market(
                            db_path,
                            market_hint=interp.market_hint,
                        )
                        if not mid and llm_match_fallback:
                            mid2, conf, usage2 = await intel_llm.match_market_llm(
                                db_path,
                                market_hint=interp.market_hint,
                                client=client,
                                model=model,
                                entities=interp.entities,
                            )
                            total_cost += usage2.cost_cents
                            if mid2:
                                mid = mid2
                                score = conf
                        if mid:
                            matched += 1
                await intel_llm.save_interpretation(
                    db_path,
                    intel_message_id=int(r["id"]),
                    interp=interp,
                    matched_market_id=mid,
                    match_score=score,
                    usage=usage,
                )
                total_cost += usage.cost_cents
        finally:
            await client.close()

        console.print(
            f"[green]OK[/green] interpreted [cyan]{len(queue)}[/cyan] messages  "
            f"market-relevant: [cyan]{relevant}[/cyan]  "
            f"matched to a market: [cyan]{matched}[/cyan]  "
            f"cost: [cyan]${total_cost / 100:.4f}[/cyan]"
        )


@intel_app.command("match-wallets")
def intel_match_wallets(
    source: str | None = typer.Option(None, "--source"),
    min_conviction: float = typer.Option(0.55, "--min-conviction"),
    window_before_h: float = typer.Option(48.0, "--window-before-h"),
    window_after_h: float = typer.Option(24.0, "--window-after-h"),
    min_notional: int = typer.Option(10_000, "--min-notional", help="cents"),
    top_n: int = typer.Option(10, "--top-n"),
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
) -> None:
    """Close the loop: for each high-conviction interpretation with a
    matched market + direction, promote the wallets who traded in that
    direction inside the window into known_insiders.
    """
    from polysim.ingest.intel_wallet_matcher import match_wallets_for_source

    async def _main() -> None:
        result = await match_wallets_for_source(
            db_path,
            source=source,
            min_conviction=min_conviction,
            match_window_before_h=window_before_h,
            match_window_after_h=window_after_h,
            min_notional_cents=min_notional,
            top_n_per_post=top_n,
        )
        console.print(
            f"[bold]intel match-wallets[/bold]\n"
            f"  scanned interpretations:  [cyan]{result['interpretations_scanned']}[/cyan]\n"
            f"  matched (any wallets):    [cyan]{result['interpretations_matched']}[/cyan]\n"
            f"  new known_insiders rows:  [green]{result['wallets_promoted']}[/green]\n"
            f"  skipped (duplicate):      [dim]{result['wallets_skipped_dup']}[/dim]"
        )

    asyncio.run(_main())


@intel_app.command("review")
def intel_review(
    source: str | None = typer.Option(None, "--source"),
    limit: int = typer.Option(20, "--limit"),
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
    min_conviction: float = typer.Option(0.0, "--min-conviction"),
) -> None:
    """Show the LLM-parsed intel feed, newest first."""
    import json as _json

    from rich.table import Table

    from polysim.ingest import intel_llm

    rows = asyncio.run(intel_llm.list_interpretations(db_path, limit=limit, source=source))
    rows = [r for r in rows if float(r.get("conviction") or 0) >= min_conviction]
    if not rows:
        console.print("[dim](no interpretations — run `polysim intel interpret` first)[/dim]")
        return

    table = Table(show_header=True, header_style="bold", expand=True)
    table.add_column("MSG", justify="right", no_wrap=True)
    table.add_column("WHEN", no_wrap=True)
    table.add_column("SENT")
    table.add_column("DIR")
    table.add_column("CONV", justify="right")
    table.add_column("MKT?")
    table.add_column("SUMMARY")
    for r in rows:
        tags = _json.loads(r.get("tags_json") or "[]")
        sent = str(r.get("sentiment") or "-")
        colour = {
            "bullish": "green",
            "bearish": "red",
            "mixed": "yellow",
            "neutral": "dim",
        }.get(sent, "white")
        mkt = (
            "[green]✓[/green]"
            if r.get("matched_market_id")
            else ("[yellow]?[/yellow]" if r.get("is_market_relevant") else "[dim]-[/dim]")
        )
        conv = float(r.get("conviction") or 0)
        ts = str(r.get("posted_at") or "")[:16].replace("T", " ")
        summary = str(r.get("summary") or "")
        if tags:
            summary = f"[dim]{','.join(tags[:3])}[/dim]  {summary}"
        table.add_row(
            str(r.get("intel_message_id") or "-"),
            ts,
            f"[{colour}]{sent[:4]}[/{colour}]",
            str(r.get("direction") or "-"),
            f"{conv:.2f}",
            mkt,
            summary[:80],
        )
    console.print(table)


@intel_app.command("resolve-x")
def intel_resolve_x(
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
    source: str | None = typer.Option(
        None, "--source", help="Only process this intel source (e.g. spaceinsights)."
    ),
    limit: int = typer.Option(200, "--limit"),
) -> None:
    """Walk x.com/twitter.com links in stored intel messages (Tier 4) and
    extract wallets from each linked tweet. Re-processes older messages
    without re-hitting Telegram."""
    import json as _json

    from polysim.db import dao as _dao
    from polysim.ingest.intel_extract import extract_wallets
    from polysim.ingest.twitter_resolver import (
        TweetCache,
        extract_tweet_ids,
        resolve_many,
    )

    async def _main() -> None:
        rows = await _dao.list_intel_messages(db_path, source=source, limit=limit)
        if not rows:
            console.print("[dim](no intel messages)[/dim]")
            return
        cache = TweetCache()
        total_wallets = 0
        total_tweets_resolved = 0
        for r in rows:
            tweet_ids = extract_tweet_ids(str(r.get("text") or ""))
            if not tweet_ids:
                continue
            resolved = await resolve_many(tweet_ids, cache=cache)
            tweet_wallets: list[tuple[str, str, str]] = []
            for tid, tw in resolved.items():
                if tw is None:
                    continue
                total_tweets_resolved += 1
                for w in extract_wallets(tw.text):
                    tweet_wallets.append((w, tid, tw.author or "?"))
            if not tweet_wallets:
                continue
            # Merge into this message's wallets list (if not already there).
            existing = set(_json.loads(r.get("wallets_json") or "[]"))
            src_name = str(r.get("source") or "")
            for w, tid, auth in tweet_wallets:
                if w in existing:
                    continue
                label = f"{src_name}:x:{w[:10]}"
                await _dao.upsert_known_insider(
                    db_path,
                    label=label,
                    address=w,
                    source=f"intel:{src_name}->x:{auth}",
                    source_message_id=int(r["id"]),
                    notes=(
                        f"Resolved from @{src_name} msg {r.get('external_id')} → "
                        f"x.com/{auth}/status/{tid}"
                    ),
                )
                total_wallets += 1
        console.print(
            f"[green]OK[/green] resolved [cyan]{total_tweets_resolved}[/cyan] tweets, "
            f"extracted [cyan]{total_wallets}[/cyan] new wallets"
        )

    asyncio.run(_main())


@intel_app.command("sync-all")
def intel_sync_all(
    niches: str = typer.Option(
        "",
        "--niches",
        help="Comma-separated niche keys (empty = all). e.g. 'box_office,elections'",
    ),
    only: str = typer.Option(
        "",
        "--only",
        help="Filter source type: 'reddit', 'telegram', or blank for both.",
    ),
    limit: int = typer.Option(25, "--limit", help="Posts/messages per source."),
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
    config_path: Path = typer.Option(Path("config.yml"), "--config"),
) -> None:
    """Sync every source in `config.yml → intel_sources`.

    Reddit and Telegram by default. X entries are scaffolded in config
    but not yet active (no auth path).
    """
    from polysim.ingest.intel_channels import SESSION_PATH, sync_channel_once
    from polysim.ingest.reddit_channels import sync_subreddit_once

    cfg = load_config(config_path)
    keywords = {cat: entry.keywords for cat, entry in cfg.categories.items() if entry.enabled}
    selected = [n.strip() for n in niches.split(",") if n.strip()]
    if not selected:
        selected = list(cfg.intel_sources.keys())
    do_reddit = only in ("", "reddit")
    do_telegram = only in ("", "telegram")

    from rich.table import Table

    # We build the report table as we go.
    table = Table(show_header=True, header_style="bold", expand=True)
    table.add_column("NICHE", no_wrap=True)
    table.add_column("KIND", no_wrap=True)
    table.add_column("SOURCE")
    table.add_column("POSTS", justify="right")
    table.add_column("WALLETS", justify="right")
    table.add_column("STATUS")

    async def _main() -> None:
        tg_client = None
        if do_telegram:
            # Only bother opening the Telegram client if at least one
            # niche has telegram sources configured AND we're authed.
            tg_configured = any(
                cfg.intel_sources.get(n) and cfg.intel_sources[n].telegram for n in selected
            )
            secrets = None
            if tg_configured:
                from polysim.config import load_secrets as _ls

                secrets = _ls()
                if (
                    not secrets.TELEGRAM_API_ID
                    or not secrets.TELEGRAM_API_HASH
                    or not SESSION_PATH.exists()
                ):
                    console.print(
                        "[yellow]telegram not authed; run "
                        "`python scripts/telegram_user_auth.py` first. "
                        "Skipping telegram sources.[/yellow]"
                    )
                else:
                    from telethon import TelegramClient

                    tg_client = TelegramClient(
                        str(SESSION_PATH),
                        int(secrets.TELEGRAM_API_ID),
                        secrets.TELEGRAM_API_HASH,
                    )
                    await tg_client.connect()
                    if not await tg_client.is_user_authorized():
                        console.print("[yellow]telegram session stale; skipping[/yellow]")
                        await tg_client.disconnect()
                        tg_client = None

        totals = {"posts": 0, "wallets": 0, "failed": 0}
        try:
            for niche in selected:
                ss = cfg.intel_sources.get(niche)
                if ss is None:
                    continue
                if do_reddit:
                    for sub in ss.reddit:
                        try:
                            r = await sync_subreddit_once(
                                db_path,
                                subreddit=sub,
                                keywords_by_category=keywords,
                                limit=limit,
                            )
                            table.add_row(
                                niche,
                                "reddit",
                                f"r/{sub}",
                                str(r.get("new_messages") or 0),
                                str(r.get("new_wallets") or 0),
                                "[green]OK[/green]",
                            )
                            totals["posts"] += int(r.get("new_messages") or 0)
                            totals["wallets"] += int(r.get("new_wallets") or 0)
                        except Exception as exc:
                            table.add_row(
                                niche,
                                "reddit",
                                f"r/{sub}",
                                "-",
                                "-",
                                f"[red]FAIL {type(exc).__name__}[/red]",
                            )
                            totals["failed"] += 1
                if do_telegram and tg_client is not None:
                    for ch in ss.telegram:
                        try:
                            r = await sync_channel_once(
                                db_path,
                                tg_client,
                                channel=ch,
                                keywords_by_category=keywords,
                                limit=limit,
                            )
                            table.add_row(
                                niche,
                                "telegram",
                                f"@{ch}",
                                str(r.get("new_messages") or 0),
                                str(r.get("new_wallets") or 0),
                                "[green]OK[/green]",
                            )
                            totals["posts"] += int(r.get("new_messages") or 0)
                            totals["wallets"] += int(r.get("new_wallets") or 0)
                        except Exception as exc:
                            table.add_row(
                                niche,
                                "telegram",
                                f"@{ch}",
                                "-",
                                "-",
                                f"[red]FAIL {type(exc).__name__}[/red]",
                            )
                            totals["failed"] += 1
        finally:
            if tg_client is not None:
                await tg_client.disconnect()

        console.print(table)
        console.print(
            f"[dim]totals: posts={totals['posts']}  "
            f"wallets={totals['wallets']}  failed={totals['failed']}[/dim]"
        )

    asyncio.run(_main())


@intel_app.command("coverage")
def intel_coverage(
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
    min_messages: int = typer.Option(5, "--min-messages"),
) -> None:
    """Per-source match rate + wallet-promotion stats.

    The signal-density table. Use this to prune underperformers and keep
    the good ones in `polysim intel watch`.
    """
    import aiosqlite
    from rich.table import Table

    async def _main() -> None:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT m.source,
                       COUNT(DISTINCT m.id) AS messages,
                       COALESCE(SUM(i.is_market_relevant), 0) AS relevant,
                       COALESCE(SUM(
                           CASE WHEN i.matched_market_id IS NOT NULL THEN 1 ELSE 0 END
                       ), 0) AS matched,
                       (
                         SELECT COUNT(*) FROM known_insiders k
                         WHERE k.source = 'intel:' || m.source
                            OR k.source = 'sentiment-match:' || m.source
                       ) AS wallets
                FROM intel_messages m
                LEFT JOIN intel_interpretations i ON i.intel_message_id = m.id
                GROUP BY m.source
                HAVING messages >= ?
                ORDER BY wallets DESC, matched DESC, messages DESC
                """,
                (min_messages,),
            ) as cur:
                rows = list(await cur.fetchall())
        if not rows:
            console.print("[dim](no sources with enough messages yet)[/dim]")
            return
        table = Table(show_header=True, header_style="bold", expand=True)
        table.add_column("SOURCE", no_wrap=True)
        table.add_column("MSGS", justify="right")
        table.add_column("RELEVANT", justify="right")
        table.add_column("MATCHED", justify="right")
        table.add_column("MATCH %", justify="right")
        table.add_column("WALLETS", justify="right")
        table.add_column("GRADE")
        for r in rows:
            n = int(r["messages"] or 0)
            rel = int(r["relevant"] or 0)
            mat = int(r["matched"] or 0)
            wal = int(r["wallets"] or 0)
            pct = (mat / n * 100) if n else 0.0
            # Grade: A = ≥15% match or ≥10 wallets, B = ≥5%, C = ≥1%, D = else.
            if pct >= 15 or wal >= 10:
                grade = "[green]A[/green]"
            elif pct >= 5:
                grade = "[cyan]B[/cyan]"
            elif pct >= 1 or wal > 0:
                grade = "[yellow]C[/yellow]"
            else:
                grade = "[red]D[/red]"
            table.add_row(
                str(r["source"]),
                str(n),
                str(rel),
                str(mat),
                f"{pct:.1f}%",
                str(wal),
                grade,
            )
        console.print(table)
        console.print(
            "[dim]A: keep and lean in.  "
            "B: useful background.  "
            "C: watch.  "
            "D: consider dropping.[/dim]"
        )

    asyncio.run(_main())


@intel_app.command("sync-reddit")
def intel_sync_reddit(
    subreddits: str = typer.Argument(
        ..., help="Comma-separated subs, e.g. 'boxoffice,elections,sports'"
    ),
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
    config_path: Path = typer.Option(Path("config.yml"), "--config"),
    limit: int = typer.Option(50, "--limit"),
) -> None:
    """One-shot: pull the newest posts from each subreddit and ingest.

    Reuses the same intel_messages pipeline as Telegram (source =
    'reddit:<sub>'), so LLM-interpret / rematch / match-wallets all work
    against reddit-sourced messages out of the box.
    """
    from polysim.ingest.reddit_channels import sync_subreddit_once

    cfg = load_config(config_path)
    keywords = {cat: entry.keywords for cat, entry in cfg.categories.items() if entry.enabled}
    subs = [s.strip().lstrip("/").removeprefix("r/") for s in subreddits.split(",") if s.strip()]

    async def _main() -> None:
        total_msgs = 0
        total_wallets = 0
        for sub in subs:
            try:
                r = await sync_subreddit_once(
                    db_path,
                    subreddit=sub,
                    keywords_by_category=keywords,
                    limit=limit,
                )
                console.print(
                    f"[green]OK[/green] r/{sub}: "
                    f"[cyan]+{r['new_messages']}[/cyan] posts, "
                    f"[cyan]+{r['new_wallets']}[/cyan] wallets"
                )
                total_msgs += int(r.get("new_messages") or 0)
                total_wallets += int(r.get("new_wallets") or 0)
            except Exception as exc:
                console.print(f"[red]FAIL r/{sub}: {exc}[/red]")
        console.print(f"[dim]totals: {total_msgs} posts, {total_wallets} wallets[/dim]")

    asyncio.run(_main())


@intel_app.command("watch-reddit")
def intel_watch_reddit(
    subs_opt: str = typer.Option(
        "boxoffice,nfl,nba,elections,PoliticalForecast,geopolitics",
        "--subs",
        help="Comma-separated subreddit names.",
    ),
    interval: int = typer.Option(600, "--interval", help="Seconds between polls."),
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
    config_path: Path = typer.Option(Path("config.yml"), "--config"),
) -> None:
    """Long-running: poll N subreddits forever. Ctrl+C to stop."""
    from polysim.ingest.reddit_channels import RedditIntelPoller

    cfg = load_config(config_path)
    keywords = {cat: entry.keywords for cat, entry in cfg.categories.items() if entry.enabled}
    subs = [s.strip().lstrip("/").removeprefix("r/") for s in subs_opt.split(",") if s.strip()]
    console.print(f"[bold]intel watch-reddit[/bold]  subs={subs}  every {interval}s")

    async def _main() -> None:
        p = RedditIntelPoller(
            db_path,
            subreddits=subs,
            keywords_by_category=keywords,
            poll_interval_s=float(interval),
        )
        await p.start()
        try:
            stop = asyncio.Event()
            await stop.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await p.stop()
            console.print(
                f"[green]OK[/green] totals: {p.totals['messages']} posts, "
                f"{p.totals['wallets']} wallets"
            )

    import contextlib as _ctx

    with _ctx.suppress(KeyboardInterrupt):
        asyncio.run(_main())


@intel_app.command("list")
def intel_list(
    source: str | None = typer.Option(None, "--source"),
    limit: int = typer.Option(20, "--limit"),
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
) -> None:
    """List recently-scraped intel messages."""
    from rich.table import Table

    from polysim.db import dao as _dao

    rows = asyncio.run(_dao.list_intel_messages(db_path, source=source, limit=limit))
    if not rows:
        console.print("[dim](no intel messages)[/dim]")
        return
    import json as _json

    table = Table(show_header=True, header_style="bold")
    table.add_column("ID", justify="right")
    table.add_column("WHEN")
    table.add_column("SOURCE")
    table.add_column("WALLETS", justify="right")
    table.add_column("SLUGS", justify="right")
    table.add_column("SNIPPET")
    for r in rows:
        wallets = _json.loads(r.get("wallets_json") or "[]")
        slugs = _json.loads(r.get("market_slugs_json") or "[]")
        snippet = str(r.get("text") or "")[:80].replace("\n", " ")
        table.add_row(
            str(r.get("id")),
            str(r.get("posted_at") or "")[:19].replace("T", " "),
            str(r.get("source") or "-"),
            str(len(wallets)),
            str(len(slugs)),
            snippet,
        )
    console.print(table)


@intel_app.command("watch")
def intel_watch(
    channels: str = typer.Option(
        "spaceinsights", "--channels", help="Comma-separated channel names or @handles."
    ),
    interval: int = typer.Option(300, "--interval", help="Seconds between polls."),
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
    config_path: Path = typer.Option(Path("config.yml"), "--config"),
) -> None:
    """Long-running: poll N channels forever. Ctrl+C to stop."""
    from polysim.config import load_secrets
    from polysim.ingest.intel_channels import SESSION_PATH, IntelPoller

    cfg = load_config(config_path)
    secrets = load_secrets()
    if not secrets.TELEGRAM_API_ID or not secrets.TELEGRAM_API_HASH or not SESSION_PATH.exists():
        console.print("[red]intel not configured — run scripts/telegram_user_auth.py first[/red]")
        raise typer.Exit(code=2)

    ch_list = [c.strip() for c in channels.split(",") if c.strip()]
    keywords = {cat: entry.keywords for cat, entry in cfg.categories.items() if entry.enabled}
    console.print(f"[bold]intel watch[/bold]  channels={ch_list}  every {interval}s")

    async def _main() -> None:
        p = IntelPoller(
            db_path,
            channels=ch_list,
            api_id=int(secrets.TELEGRAM_API_ID),
            api_hash=secrets.TELEGRAM_API_HASH,
            keywords_by_category=keywords,
            poll_interval_s=float(interval),
        )
        await p.start()
        try:
            stop = asyncio.Event()
            await stop.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await p.stop()
            console.print(
                f"[green]OK[/green] totals: "
                f"{p.totals['messages']} msgs, {p.totals['wallets']} wallets"
            )

    import contextlib as _ctx

    with _ctx.suppress(KeyboardInterrupt):
        asyncio.run(_main())


@discovery_app.command("run")
def discovery_run(
    experiment_name: str = typer.Option(
        "experiment_001",
        "--experiment-id",
        help="Experiment to freeze the cohort under.",
    ),
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
    skip_sync: bool = typer.Option(
        False,
        "--skip-sync",
        help="Skip the poly_data git sync (use existing local clone).",
    ),
    freeze: bool = typer.Option(
        True,
        "--freeze/--no-freeze",
        help="Freeze the cohort to wallets_discovery + experiments. Off for dry runs.",
    ),
    per_niche: int = typer.Option(100, "--per-niche"),
    general: int = typer.Option(200, "--general"),
    min_niche_trades: int = typer.Option(50, "--min-niche-trades"),
) -> None:
    """One-shot: poly_data sync → features → classifier → cohort → freeze.

    Empirical-priors addendum §3.2.
    """
    from polysim.agents.belief_schema import SCHEMA_VERSION
    from polysim.discovery.classifier import (
        CLASSIFIER_VERSION,
        classify_population,
    )
    from polysim.discovery.cohort import (
        freeze_cohort,
        select_cohort,
        update_edge_likelihoods,
    )
    from polysim.discovery.features import extract_features_for_run, write_features
    from polysim.discovery.poly_data_sync import sync_poly_data

    async def _main() -> None:
        if not skip_sync:
            console.print("[bold]1/5[/bold] sync poly_data...")
            sync_result = await sync_poly_data()
            if sync_result.is_stale:
                console.print(
                    "[yellow]   poly_data is stale; using local DB only "
                    "(addendum §11 Q2 fallback path)[/yellow]"
                )
            else:
                console.print(
                    f"[dim]   fetched={sync_result.fetched}  "
                    f"latest_trade={sync_result.latest_trade_ts}[/dim]"
                )

        console.print("[bold]2/5[/bold] extract features (Polars)...")
        features = await extract_features_for_run(db_path)
        console.print(f"[dim]   {len(features)} (wallet, scope) feature rows[/dim]")
        await write_features(db_path, features)

        console.print("[bold]3/5[/bold] Tier-1 classify (no LLM)...")
        scores = classify_population(features)
        console.print(f"[dim]   classifier v{CLASSIFIER_VERSION}  {len(scores)} score rows[/dim]")
        await update_edge_likelihoods(db_path, scores)

        console.print("[bold]4/5[/bold] cohort selection...")
        features_by_key = {(f.wallet_address, f.scope): f for f in features}
        picks = select_cohort(
            scores,
            features_by_key,
            per_niche_target=per_niche,
            general_target=general,
            min_niche_trades=min_niche_trades,
        )
        # Bucket counts.
        from collections import Counter

        buckets = Counter(p.pool for p in picks)
        for pool, n in sorted(buckets.items()):
            console.print(f"   [cyan]{pool:<14}[/cyan] {n}")
        console.print(f"   [bold]total[/bold]          {len(picks)}")

        if not freeze:
            console.print("[yellow]5/5 freeze skipped (--no-freeze)[/yellow]")
            return
        console.print("[bold]5/5[/bold] freeze cohort...")
        experiment_id = await freeze_cohort(
            db_path,
            experiment_name=experiment_name,
            picks=picks,
            classifier_version=CLASSIFIER_VERSION,
            belief_schema_version=SCHEMA_VERSION,
        )
        console.print(
            f"[green]OK[/green] cohort frozen — experiment id=[cyan]{experiment_id}[/cyan]"
        )

    asyncio.run(_main())


@discovery_app.command("show-cohort")
def discovery_show_cohort(
    niche: str | None = typer.Option(None, "--niche"),
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
    limit: int = typer.Option(50, "--limit"),
) -> None:
    """Print the frozen cohort, optionally filtered to one pool."""
    import aiosqlite
    from rich.table import Table

    async def _main() -> None:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            q = (
                "SELECT address, cohort_niche, edge_likelihood_global, "
                "edge_likelihood_aec, edge_likelihood_ai_labs, "
                "edge_likelihood_creator_econ "
                "FROM wallets_discovery WHERE is_cohort = 1 "
            )
            args: list[Any] = []
            if niche is not None:
                q += "AND cohort_niche = ? "
                args.append(niche)
            q += "ORDER BY cohort_niche, address LIMIT ?"
            args.append(limit)
            async with db.execute(q, args) as cur:
                rows = list(await cur.fetchall())
        if not rows:
            console.print("[dim](no cohort frozen yet — run `polysim discovery run`)[/dim]")
            return
        table = Table(show_header=True, header_style="bold")
        table.add_column("ADDRESS", no_wrap=True)
        table.add_column("POOL")
        table.add_column("EL_GLOB", justify="right")
        table.add_column("EL_AEC", justify="right")
        table.add_column("EL_AI", justify="right")
        table.add_column("EL_CREATOR", justify="right")
        for r in rows:
            table.add_row(
                f"{str(r['address'])[:14]}...",
                str(r["cohort_niche"] or "-"),
                f"{(r['edge_likelihood_global'] or 0):.3f}",
                f"{(r['edge_likelihood_aec'] or 0):.3f}",
                f"{(r['edge_likelihood_ai_labs'] or 0):.3f}",
                f"{(r['edge_likelihood_creator_econ'] or 0):.3f}",
            )
        console.print(table)

    asyncio.run(_main())


@discovery_app.command("coverage")
def discovery_coverage(
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
) -> None:
    """Per-niche cohort population + recency stats."""
    import aiosqlite
    from rich.table import Table

    async def _main() -> None:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT cohort_niche, COUNT(*) AS n,
                       AVG(edge_likelihood_global) AS avg_global,
                       MAX(last_classified_at) AS last_classified
                FROM wallets_discovery
                WHERE is_cohort = 1
                GROUP BY cohort_niche
                ORDER BY cohort_niche
                """
            ) as cur:
                rows = list(await cur.fetchall())
            async with db.execute(
                "SELECT id, name, cohort_size, cohort_hash, cohort_frozen_at "
                "FROM experiments ORDER BY id DESC LIMIT 5"
            ) as cur:
                experiments = list(await cur.fetchall())
        if not rows:
            console.print("[dim](no cohort yet)[/dim]")
            return
        table = Table(show_header=True, header_style="bold")
        table.add_column("POOL")
        table.add_column("WALLETS", justify="right")
        table.add_column("AVG EL_GLOB", justify="right")
        table.add_column("LAST CLASSIFIED")
        for r in rows:
            table.add_row(
                str(r["cohort_niche"] or "-"),
                str(r["n"]),
                f"{(r['avg_global'] or 0):.3f}",
                str(r["last_classified"] or "-")[:19],
            )
        console.print(table)
        if experiments:
            console.print("\n[bold]Recent experiments[/bold]")
            for e in experiments:
                console.print(
                    f"  #{e['id']}  {e['name']:<20} "
                    f"size={e['cohort_size']:<4} "
                    f"hash={str(e['cohort_hash'] or '')[:12]}... "
                    f"frozen={str(e['cohort_frozen_at'] or '-')[:19]}"
                )

    asyncio.run(_main())


@app.command()
def replay(
    from_date: str = typer.Option(..., "--from"),
    to_date: str = typer.Option(..., "--to"),
    speed: str = typer.Option("100x", "--speed"),
    config_path: Path = typer.Option(Path("config.yml"), "--config"),
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
    open_run: bool = typer.Option(
        True,
        "--run/--no-run",
        help="Also open a paper run + sweep flags through it.",
    ),
    profile_name: str = typer.Option(
        "systematic",
        "--profile",
        help="Risk profile for the replay paper run (if --run).",
    ),
) -> None:
    """Replay historical trades through the scoring pipeline.

    Reads trades in [--from, --to] from the local DB, refreshes wallet
    profiles, runs every detector + composite scorer, and persists flags.
    With --run (default), also opens a paper run with the chosen profile
    and dispatches the resulting flags through it.

    The window is inclusive on both ends. --speed is accepted but unused —
    the replay is offline and runs as fast as IO permits.
    """
    from datetime import UTC
    from datetime import datetime as _dt

    from polysim.evaluator.backtest import parse_speed, run_backtest

    cfg = load_config(config_path)

    def _parse_date(s: str) -> _dt:
        # Accept "YYYY-MM-DD" or full ISO.
        try:
            if "T" in s:
                return _dt.fromisoformat(s).astimezone(UTC)
            return _dt.fromisoformat(s).replace(tzinfo=UTC)
        except ValueError as exc:
            console.print(f"[red]bad --from/--to value: {s!r}[/red]")
            raise typer.Exit(code=2) from exc

    start = _parse_date(from_date)
    end = _parse_date(to_date)
    if end < start:
        console.print("[red]--to must be on or after --from[/red]")
        raise typer.Exit(code=2)
    speed_mult = parse_speed(speed)

    console.print(
        f"[bold]polysim replay[/bold]  "
        f"from=[cyan]{start.isoformat()}[/cyan] to=[cyan]{end.isoformat()}[/cyan] "
        f"speed=[dim]{speed_mult}x (offline)[/dim]"
    )

    result = asyncio.run(
        run_backtest(
            start=start,
            end=end,
            db_path=db_path,
            cfg=cfg,
            open_paper_run=open_run,
            profile_name=profile_name,
            speed_multiplier=speed_mult,
        )
    )
    console.print(
        f"[green]OK[/green] trades=[cyan]{result['trades_seen']}[/cyan]  "
        f"profiles=[cyan]{result['profiles_refreshed']}[/cyan]  "
        f"flags=[cyan]{result['flags_created']}[/cyan]"
    )
    if result.get("run_id"):
        console.print(
            f"     run #[cyan]{result['run_id']}[/cyan]  "
            f"positions opened: [cyan]{result['positions_opened']}[/cyan]"
        )


# ── experiment subcommands (empirical-priors §8) ──────────────


@experiment_app.command("start")
def experiment_start(
    experiment_name: str = typer.Option(
        "experiment_001",
        "--experiment-id",
        help="Experiment to freeze the cohort under.",
    ),
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
    notes: str | None = typer.Option(
        None,
        "--notes",
        help="Free-text notes; written to experiments.notes for audit trail.",
    ),
    skip_sync: bool = typer.Option(False, "--skip-sync"),
) -> None:
    """Pre-register an experiment by freezing the discovery cohort.

    Equivalent to `polysim discovery run` but also stamps `--notes` and
    prints the cohort hash for the operator to commit alongside their
    pre-registration document.
    """
    import aiosqlite

    from polysim.agents.belief_schema import SCHEMA_VERSION
    from polysim.discovery.classifier import (
        CLASSIFIER_VERSION,
        classify_population,
    )
    from polysim.discovery.cohort import (
        freeze_cohort,
        select_cohort,
        update_edge_likelihoods,
    )
    from polysim.discovery.features import extract_features_for_run, write_features
    from polysim.discovery.poly_data_sync import sync_poly_data

    async def _main() -> None:
        if not skip_sync:
            sync = await sync_poly_data()
            console.print(f"[dim]poly_data: fetched={sync.fetched}  stale={sync.is_stale}[/dim]")
        features = await extract_features_for_run(db_path)
        await write_features(db_path, features)
        scores = classify_population(features)
        await update_edge_likelihoods(db_path, scores)
        features_by_key = {(f.wallet_address, f.scope): f for f in features}
        picks = select_cohort(scores, features_by_key)
        experiment_id = await freeze_cohort(
            db_path,
            experiment_name=experiment_name,
            picks=picks,
            classifier_version=CLASSIFIER_VERSION,
            belief_schema_version=SCHEMA_VERSION,
        )
        if notes:
            async with aiosqlite.connect(str(db_path)) as db:
                await db.execute(
                    "UPDATE experiments SET notes = ? WHERE id = ?",
                    (notes, experiment_id),
                )
                await db.commit()
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT cohort_hash, cohort_size FROM experiments WHERE id = ?",
                (experiment_id,),
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            console.print("[red]experiment row vanished after freeze[/red]")
            raise typer.Exit(code=1)
        console.print(
            f"[green]OK[/green] experiment id=[cyan]{experiment_id}[/cyan]  "
            f"cohort_size=[cyan]{row['cohort_size']}[/cyan]  "
            f"cohort_hash=[dim]{row['cohort_hash']}[/dim]"
        )
        console.print(
            "[yellow]→ Commit the cohort_hash to your pre-registration "
            "document before paper trading begins.[/yellow]"
        )

    asyncio.run(_main())


@experiment_app.command("monitor")
def experiment_monitor(
    experiment_id: int | None = typer.Option(
        None,
        "--experiment-id",
        help="Experiment id; omit to use the most recent.",
    ),
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
) -> None:
    """Live status: paper runs, positions, rejections, gate funnel."""
    from rich.table import Table

    from polysim.experiment.reporting import gather_experiment_data

    async def _main() -> None:
        try:
            data = await gather_experiment_data(db_path, experiment_id=experiment_id)
        except RuntimeError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        s = data.summary
        console.print(f"[bold]Experiment #{s.experiment_id}[/bold] [cyan]{s.name}[/cyan]")
        console.print(
            f"  cohort_size={s.cohort_size}  "
            f"hash={(s.cohort_hash or '-')[:16]}...  "
            f"started={s.started_at[:19]}"
        )
        console.print(
            f"  paper_runs={s.paper_runs}  "
            f"open/closed/resolved={s.paper_positions_open}/"
            f"{s.paper_positions_closed}/{s.paper_positions_resolved}  "
            f"realized=${s.realized_pnl_cents / 100:+,.2f}"
        )
        console.print(f"  rejections_total={s.rejections_total}")
        if s.rejections_by_gate:
            t = Table(show_header=True, header_style="bold")
            t.add_column("FIRST-FAILED GATE")
            t.add_column("COUNT", justify="right")
            for gate, n in sorted(s.rejections_by_gate.items(), key=lambda kv: -kv[1]):
                t.add_row(gate, str(n))
            console.print(t)

    asyncio.run(_main())


@experiment_app.command("report")
def experiment_report(
    experiment_id: int | None = typer.Option(
        None,
        "--experiment-id",
        help="Experiment id; omit to use the most recent.",
    ),
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
    out_path: Path | None = typer.Option(
        None,
        "--out",
        help="Where to write the Markdown report. Default: reports/experiment_<id>_<UTC-date>.md",
    ),
) -> None:
    """Render the §8.5 end-of-experiment Markdown report.

    Aggregates paper_runs / paper_positions / decision_rejections / cohort
    edge_likelihoods, runs H1..H6, and writes a verdict-table report.
    """
    from polysim.experiment.reporting import (
        gather_experiment_data,
        render_markdown,
        run_all_hypotheses,
    )

    async def _main() -> None:
        try:
            data = await gather_experiment_data(db_path, experiment_id=experiment_id)
        except RuntimeError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        results = run_all_hypotheses(data)
        text = render_markdown(data, results)

        target = out_path
        if target is None:
            stamp = datetime.now(UTC).strftime("%Y%m%d")
            reports_dir = Path("reports")
            reports_dir.mkdir(parents=True, exist_ok=True)
            target = reports_dir / (f"experiment_{data.summary.experiment_id}_{stamp}.md")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        console.print(f"[green]OK[/green] report → [cyan]{target}[/cyan]")
        # Summary one-liner per hypothesis.
        for r in results:
            console.print(
                f"  {r.hypothesis_id}: p={r.p_value:.3f}  n={r.n}  verdict=[bold]{r.verdict}[/bold]"
            )

    asyncio.run(_main())


@experiment_app.command("end")
def experiment_end(
    experiment_id: int = typer.Argument(..., help="Experiment id to mark ended."),
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
) -> None:
    """Stamp `ended_at` so subsequent reports treat the window as closed."""
    import aiosqlite

    async def _main() -> None:
        ended_at = datetime.now(UTC).isoformat()
        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute(
                "UPDATE experiments SET ended_at = ? WHERE id = ?",
                (ended_at, experiment_id),
            )
            await db.commit()
        console.print(f"[green]OK[/green] experiment #{experiment_id} ended_at={ended_at}")

    asyncio.run(_main())


# ── tournament subcommands ─────────────────────────────────────


@tournament_app.command("list")
def tournament_list() -> None:
    """Print the pre-registered variants."""
    from rich.table import Table

    from polysim.tournament import VARIANTS

    t = Table(show_header=True, header_style="bold")
    t.add_column("#", justify="right")
    t.add_column("NAME", no_wrap=True)
    t.add_column("BASE")
    t.add_column("DESCRIPTION", overflow="fold")
    for i, v in enumerate(VARIANTS, start=1):
        t.add_row(str(i), v.name, v.base_profile, v.description)
    console.print(t)


@tournament_app.command("seed")
def tournament_seed(
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
) -> None:
    """Open a paper run for every variant not already in the tournament."""
    from polysim.tournament import TournamentAllocatorLoop

    async def _main() -> None:
        loop = TournamentAllocatorLoop(db_path, auto_spawn=False)
        n = await loop.seed_pool()
        console.print(
            f"[green]OK[/green] seeded {n} new variant runs (existing runs left untouched)"
        )

    asyncio.run(_main())


@tournament_app.command("status")
def tournament_status(
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
) -> None:
    """Score the current tournament pool and show the allocator's verdict."""
    from rich.table import Table

    from polysim.tournament import TournamentAllocator, TournamentAllocatorLoop

    async def _main() -> None:
        loop = TournamentAllocatorLoop(db_path)
        scores = await loop._score_active_runs()
        if not scores:
            console.print("[dim](no tournament runs yet — try `polysim tournament seed`)[/dim]")
            return
        allocator = TournamentAllocator()
        decisions = {d.run_id: d for d in allocator.rebalance(scores)}
        t = Table(show_header=True, header_style="bold")
        t.add_column("RUN", justify="right")
        t.add_column("VARIANT")
        t.add_column("POSITIONS", justify="right")
        t.add_column("RETURN", justify="right")
        t.add_column("RANK", justify="right")
        t.add_column("VERDICT")
        t.add_column("REASON", overflow="fold")
        for s in sorted(scores, key=lambda x: -x.return_pct):
            d = decisions.get(s.run_id)
            verdict = d.verdict if d else "?"
            reason = d.reason if d else ""
            rank = str(d.rank) if d else "-"
            color = {
                "promote": "green",
                "retire": "red",
                "hold": "dim",
            }.get(verdict, "")
            t.add_row(
                f"#{s.run_id}",
                s.variant_name,
                str(s.n_positions),
                f"{s.return_pct * 100:+.2f}%",
                rank,
                f"[{color}]{verdict}[/{color}]" if color else verdict,
                reason,
            )
        console.print(t)

    asyncio.run(_main())


@tournament_app.command("rebalance")
def tournament_rebalance(
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
) -> None:
    """Run one allocator tick now (pause losers + resume winners)."""
    from polysim.tournament import TournamentAllocatorLoop

    async def _main() -> None:
        loop = TournamentAllocatorLoop(db_path, auto_spawn=False)
        await loop._tick_once()
        console.print(
            "[green]OK[/green] rebalance applied — see `polysim tournament status` for new state"
        )

    asyncio.run(_main())


@equity_app.command("status")
def equity_status(
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
) -> None:
    """Show the equity sentiment variant runs + holdings."""
    import sqlite3

    from rich.table import Table

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    runs = conn.execute(
        "SELECT id,name,starting_balance_cents,current_balance_cents "
        "FROM paper_runs WHERE tag='equity_v1' AND ended_at IS NULL "
        "ORDER BY current_balance_cents DESC"
    ).fetchall()
    if not runs:
        console.print("[dim](no equity runs yet — start `polysim live`)[/dim]")
        return
    t = Table(show_header=True, header_style="bold")
    for col in ("VARIANT", "EQUITY", "P&L", "OPEN", "CASH"):
        t.add_column(col, justify="right" if col != "VARIANT" else "left")
    for r in runs:
        start = int(r["starting_balance_cents"] or 0)
        bal = int(r["current_balance_cents"] or 0)
        pnl = (bal - start) / start if start else 0.0
        npos = conn.execute(
            "SELECT COUNT(*) FROM equity_positions WHERE run_id=? AND status='OPEN'", (r["id"],)
        ).fetchone()[0]
        cash = conn.execute(
            "SELECT cash_cents FROM equity_run_state WHERE run_id=?", (r["id"],)
        ).fetchone()
        color = "green" if pnl >= 0 else "red"
        t.add_row(
            str(r["name"]).replace("equity-", ""),
            f"${bal / 100:,.0f}",
            f"[{color}]{pnl * 100:+.2f}%[/{color}]",
            str(npos),
            f"${cash[0] / 100:,.0f}" if cash else "-",
        )
    console.print(t)


@equity_app.command("signals")
def equity_signals(
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
    limit: int = typer.Option(15, "--limit"),
) -> None:
    """Show the latest daily composite signals (attention-led)."""
    import sqlite3

    from rich.table import Table

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM equity_signals WHERE date=(SELECT MAX(date) FROM equity_signals) "
        "ORDER BY composite DESC LIMIT ?",
        (limit,),
    ).fetchall()
    if not rows:
        console.print("[dim](no signals yet — sentiment history is still building)[/dim]")
        return
    t = Table(show_header=True, header_style="bold")
    for col in ("TICKER", "COMPOSITE", "Z_ATTN", "STANCE", "MENTIONS"):
        t.add_column(col, justify="right" if col != "TICKER" else "left")
    for r in rows:
        t.add_row(
            r["ticker"],
            f"{r['composite']:+.2f}",
            f"{r['z_attn']:+.2f}",
            f"{r['stance']:+.2f}",
            str(r["mentions"]),
        )
    console.print(t)


# ── signals commands (external conversation-signal layer) ──────


@signals_app.command("snapshot")
def signals_snapshot(
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
    config_path: Path = typer.Option(Path("config.yml"), "--config"),
    provider_name: str = typer.Option(
        "",
        "--provider",
        help="Override config: reddit | fixture (needs --fixtures-dir).",
    ),
    fixtures_dir: Path | None = typer.Option(
        None,
        "--fixtures-dir",
        help="Fixture JSON dir when --provider fixture.",
    ),
) -> None:
    """One fetch+score pass over linkable open markets.

    Works even when config.signals.enabled is false — the flag only gates
    the *live loop*; this command is the manual/offline path.
    """
    from polysim.signals.providers import FixtureProvider, RedditPublicProvider
    from polysim.signals.service import run_snapshot_once

    cfg = load_config(config_path)
    if provider_name == "fixture" or (not provider_name and fixtures_dir):
        if fixtures_dir is None:
            console.print("[red]--provider fixture needs --fixtures-dir[/red]")
            raise typer.Exit(2)
        provider: Any = FixtureProvider(fixtures_dir)
    elif provider_name in ("", "reddit"):
        provider = RedditPublicProvider(timeout_s=cfg.signals.request_timeout_s)
    else:
        console.print(f"[red]unknown provider: {provider_name}[/red]")
        raise typer.Exit(2)

    async def _main() -> None:
        r = await run_snapshot_once(db_path, cfg, provider)
        console.print(
            f"[bold]signals snapshot[/bold]  "
            f"markets=[cyan]{r['markets_considered']}[/cyan]  "
            f"topics ok/failed=[cyan]{r['topics_fetched']}[/cyan]/"
            f"[yellow]{r['topics_failed']}[/yellow]  "
            f"signals written=[green]{r['signals_written']}[/green]"
        )

    asyncio.run(_main())


@signals_app.command("show")
def signals_show(
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
    limit: int = typer.Option(20, "--limit"),
) -> None:
    """Latest market signals (conviction composite, newest first)."""
    from rich.table import Table

    from polysim.signals.service import recent_market_signals

    async def _main() -> None:
        rows = await recent_market_signals(db_path, limit=limit)
        if not rows:
            console.print("[dim](no signals yet — run `polysim signals snapshot`)[/dim]")
            return
        t = Table(show_header=True, header_style="bold")
        for col in ("TS", "MARKET", "CAT", "CONV", "CONF", "Z", "VEL", "STANCE", "POSTS"):
            t.add_column(col, justify="left" if col in ("TS", "MARKET", "CAT") else "right")
        for r in rows:
            q = str(r.get("question") or r.get("market_id") or "?")
            t.add_row(
                str(r["ts"])[:16],
                q[:48],
                str(r.get("category") or "-"),
                f"{r['composite']:.2f}",
                f"{r['confidence']:.2f}",
                f"{r['attention_z']:+.1f}",
                f"{r['velocity']:.1f}x",
                f"{r['stance']:+.2f}",
                f"{r['n_matched_posts']}/{r['n_posts']}",
            )
        console.print(t)

    asyncio.run(_main())


@signals_app.command("eval")
def signals_eval(
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
) -> None:
    """Measured lift: composite buckets on resolved markets + adjusted-vs-
    untouched position P&L (realized only, MTM never enters)."""
    from rich.table import Table

    from polysim.signals.evaluation import (
        signal_bucket_outcomes,
        signal_coverage,
        signal_sizing_summary,
    )

    async def _main() -> None:
        cov = await signal_coverage(db_path)
        console.print(
            f"coverage: [cyan]{cov['with_any_signal']}[/cyan]/"
            f"[cyan]{cov['open_markets']}[/cyan] open markets have a signal"
        )

        buckets = await signal_bucket_outcomes(db_path)
        if buckets:
            t = Table(
                title="resolved markets by conviction bucket", show_header=True, header_style="bold"
            )
            for col in ("BUCKET", "MARKETS", "POSITIONS", "REALIZED $", "AVG $", "WIN%"):
                t.add_column(col, justify="right" if col != "BUCKET" else "left")
            for b in buckets:
                t.add_row(
                    b["bucket"],
                    str(b["n_markets"]),
                    str(b["n_positions"]),
                    f"{b['realized_pnl_cents'] / 100:,.2f}",
                    f"{b['avg_pnl_cents'] / 100:,.2f}",
                    f"{b['win_rate'] * 100:.0f}%",
                )
            console.print(t)
        else:
            console.print("[dim](no resolved markets with signals yet)[/dim]")

        s = await signal_sizing_summary(db_path)
        t2 = Table(
            title="signal-adjusted vs untouched positions (realized)",
            show_header=True,
            header_style="bold",
        )
        for col in ("GROUP", "N", "REALIZED $", "AVG $", "WIN%", "AVG MULT"):
            t2.add_column(col, justify="right" if col != "GROUP" else "left")
        adj, unt = s["adjusted"], s["untouched"]
        t2.add_row(
            "adjusted",
            str(adj["n"]),
            f"{adj['realized_pnl_cents'] / 100:,.2f}",
            f"{adj['avg_pnl_cents'] / 100:,.2f}",
            f"{adj['win_rate'] * 100:.0f}%",
            f"{adj['avg_multiplier']:.2f}" if adj["avg_multiplier"] else "-",
        )
        t2.add_row(
            "untouched",
            str(unt["n"]),
            f"{unt['realized_pnl_cents'] / 100:,.2f}",
            f"{unt['avg_pnl_cents'] / 100:,.2f}",
            f"{unt['win_rate'] * 100:.0f}%",
            "-",
        )
        console.print(t2)

    asyncio.run(_main())


# ── helpers ────────────────────────────────────────────────────


@evidence_app.command("scan")
def evidence_scan(
    market_id: str,
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
    config_path: Path = typer.Option(Path("config.yml"), "--config"),
) -> None:
    """Fetch and persist a fresh source-quality scan for one market."""
    from polysim.db import dao
    from polysim.risk_intelligence.scoring import (
        assess_market_evidence,
        catalyst_query_terms,
        extract_subject_key,
    )
    from polysim.risk_intelligence.service import (
        provider_from_config,
        write_assessment,
    )
    from polysim.utils.time import now_utc

    async def _main() -> None:
        cfg = load_config(config_path)
        market = await dao.get_market(db_path, market_id)
        if market is None:
            console.print(f"[red]market not found: {market_id}[/red]")
            raise typer.Exit(2)
        provider = provider_from_config(cfg.evidence)
        subject = extract_subject_key(market.question)
        articles = None
        if provider is not None and subject:
            articles = await provider.fetch_articles(
                subject,
                keywords=catalyst_query_terms(market.question),
                limit=cfg.evidence.max_articles,
                lookback_days=cfg.evidence.lookback_days,
            )
        assessment = assess_market_evidence(
            market,
            articles or [],
            provider=(provider.name if provider else "unavailable"),
            now=now_utc(),
        )
        if articles is None:
            assessment = assessment.model_copy(
                update={
                    "status": "insufficient",
                    "information_asymmetry_score": 1.0,
                    "summary": "insufficient: evidence provider unavailable or failed",
                }
            )
        assessment_id = await write_assessment(db_path, assessment)
        console.print(
            f"[bold]evidence #{assessment_id}[/bold] "
            f"status=[cyan]{assessment.status}[/cyan] "
            f"sources={assessment.relevant_source_count}/{assessment.source_count} relevant "
            f"quality={assessment.source_quality_score:.2f} "
            f"rumor={assessment.rumor_risk_score:.2f} "
            f"asymmetry={assessment.information_asymmetry_score:.2f}"
        )
        console.print(assessment.summary)

    asyncio.run(_main())


@evidence_app.command("show")
def evidence_show(
    market_id: str,
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
) -> None:
    """Show the freshest scan or analyst assessment for one market."""
    from polysim.risk_intelligence.service import latest_assessment

    async def _main() -> None:
        assessment = await latest_assessment(db_path, market_id, max_age_hours=24 * 365 * 10)
        if assessment is None:
            console.print("[dim](no evidence assessment)[/dim]")
            return
        console.print_json(data=assessment.model_dump(mode="json"))

    asyncio.run(_main())


@evidence_app.command("set-probability")
def evidence_set_probability(
    market_id: str,
    p_yes: float = typer.Option(..., "--p-yes", min=0.0, max=1.0),
    confidence: float = typer.Option(..., "--confidence", min=0.0, max=1.0),
    summary: str = typer.Option(..., "--summary"),
    db_path: Path = typer.Option(Path("polysim.db"), "--db"),
) -> None:
    """Attach an explicit analyst probability to a recent source scan."""
    from polysim.db import dao
    from polysim.risk_intelligence.service import (
        latest_assessment,
        write_analyst_assessment,
    )

    async def _main() -> None:
        market = await dao.get_market(db_path, market_id)
        if market is None:
            console.print(f"[red]market not found: {market_id}[/red]")
            raise typer.Exit(2)
        base = await latest_assessment(db_path, market_id, max_age_hours=24.0)
        if base is None:
            console.print(
                "[red]run `polysim evidence scan MARKET_ID` before setting a probability[/red]"
            )
            raise typer.Exit(2)
        assessment_id = await write_analyst_assessment(
            db_path,
            market,
            fair_probability_yes=p_yes,
            probability_confidence=confidence,
            summary=summary,
            base=base,
        )
        console.print(
            f"[green]OK[/green] analyst assessment #{assessment_id}: "
            f"P(YES)={p_yes:.1%}, confidence={confidence:.1%}"
        )

    asyncio.run(_main())


def _locate_template(name: str) -> Path | None:
    """Find a template file alongside the repo or next to the package."""
    here = Path.cwd() / name
    if here.exists():
        return here
    pkg = Path(__file__).parent.parent.parent / name
    if pkg.exists():
        return pkg
    return None


if __name__ == "__main__":
    app()
