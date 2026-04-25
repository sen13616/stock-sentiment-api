#!/usr/bin/env python3
"""
tools/db_viewer.py

Terminal UI database viewer for the SentimentAPI pipeline.

Run with:
    python3 tools/db_viewer.py

Navigation:
    1-6  switch screens
    A    toggle auto-refresh (30 s countdown in status bar)
    E    open export sub-menu (current screen / full DB / sentiment / signals / …)
    R    refresh current screen
    Q    quit
"""
from __future__ import annotations

import asyncio
import json
import os
import select
import sys
from datetime import datetime, timezone

# Allow running from the project root or from tools/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncpg
import redis.asyncio as aioredis
from dotenv import load_dotenv
from rich import box
from rich.columns import Columns
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

# db_exports loads dotenv itself; importing it here also initialises EXPORTS_DIR
from db_exports import EXPORTS_DIR, show_export_menu  # noqa: E402

_ENV_FILE = os.path.join(os.path.dirname(__file__), "..", ".env")
load_dotenv(_ENV_FILE, override=True)  # belt-and-suspenders for the viewer

console = Console()

# ---------------------------------------------------------------------------
# Auto-refresh + export state
# ---------------------------------------------------------------------------

_auto_refresh     = False
AUTO_REFRESH_SECS = 30

# Last fetched data per screen key ("1"–"6"), stored as plain dicts for CSV.
_last_data: dict[str, list[dict]] = {}
_last_ticker = ""   # most recent ticker entered by the user (screens 3 and 5)

# ---------------------------------------------------------------------------
# DB / Redis helpers
# ---------------------------------------------------------------------------

def _dsn() -> str:
    url = os.environ["DATABASE_URL"]
    return (
        url.replace("postgresql+asyncpg://", "postgresql://")
           .replace("postgres+asyncpg://", "postgres://")
    )


async def _get_conn() -> asyncpg.Connection:
    return await asyncpg.connect(dsn=_dsn())


def _redis_client() -> aioredis.Redis:
    url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    return aioredis.from_url(url, decode_responses=True)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _score_style(score: float | None) -> str:
    if score is None:
        return "white"
    if score > 60:
        return "bold green"
    if score < 40:
        return "bold red"
    return "white"


def _label(score: float | None) -> str:
    if score is None:
        return "—"
    if score > 60:
        return "Bullish"
    if score < 40:
        return "Bearish"
    return "Neutral"


def _fmt(v: float | None, decimals: int = 1) -> str:
    return f"{v:.{decimals}f}" if v is not None else "—"


def _ago(ts) -> str:
    if ts is None:
        return "—"
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    now = datetime.now(tz=timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = int((now - ts).total_seconds())
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


# ---------------------------------------------------------------------------
# Screen 1 — OVERVIEW
# ---------------------------------------------------------------------------

async def screen_overview() -> None:
    global _last_data
    conn  = await _get_conn()
    redis = _redis_client()
    try:
        tables = [
            "raw_signals", "raw_articles", "sentiment_history",
            "price_snapshots", "backtest_results",
        ]
        counts: dict[str, int] = {}
        for t in tables:
            counts[t] = await conn.fetchval(f"SELECT COUNT(*) FROM {t}")

        scored    = await conn.fetchval("SELECT COUNT(DISTINCT ticker) FROM sentiment_history")
        latest_ts = await conn.fetchval("SELECT MAX(timestamp) FROM sentiment_history")

        scheduler_keys = {
            "market":     "scheduler:last_run:market",
            "narrative":  "scheduler:last_run:narrative",
            "influencer": "scheduler:last_run:influencer",
            "macro":      "scheduler:last_run:macro",
        }
        scheduler_ts: dict[str, str | None] = {}
        for layer, key in scheduler_keys.items():
            scheduler_ts[layer] = await redis.get(key)
    finally:
        await conn.close()
        await redis.close()

    # Store for export
    export_rows: list[dict] = [{"metric": t, "value": counts[t]} for t in tables]
    export_rows.append({"metric": "tickers_scored",  "value": scored})
    export_rows.append({"metric": "latest_score_ts", "value": str(latest_ts)})
    for layer, raw in scheduler_ts.items():
        export_rows.append({"metric": f"scheduler_{layer}", "value": raw or "never"})
    _last_data["1"] = export_rows

    # Render
    console.clear()
    console.rule("[bold cyan]OVERVIEW[/bold cyan]")

    t = Table(title="Table Row Counts", box=box.SIMPLE_HEAD, show_edge=False)
    t.add_column("Table", style="cyan")
    t.add_column("Rows",  justify="right", style="yellow")
    for name in tables:
        t.add_row(name, f"{counts[name]:,}")
    console.print(t)

    console.print(
        f"  [bold]Tickers with scored data:[/bold] [yellow]{scored}[/yellow]   "
        f"[bold]Most recent score:[/bold] [yellow]{_ago(latest_ts)}[/yellow] "
        f"({latest_ts.strftime('%Y-%m-%d %H:%M UTC') if latest_ts else '—'})"
    )
    console.print()

    st = Table(title="Scheduler Last Run (Redis)", box=box.SIMPLE_HEAD, show_edge=False)
    st.add_column("Layer",    style="cyan", width=15)
    st.add_column("Last Run", style="white")
    st.add_column("Age",      style="yellow")
    for layer, raw in scheduler_ts.items():
        if raw:
            ts_obj  = datetime.fromisoformat(raw)
            display = ts_obj.strftime("%Y-%m-%d %H:%M UTC")
            age     = _ago(ts_obj)
        else:
            display = "never"
            age     = "—"
        st.add_row(layer, display, age)
    console.print(st)


# ---------------------------------------------------------------------------
# Screen 2 — SENTIMENT SCORES
# ---------------------------------------------------------------------------

async def screen_sentiment_scores() -> None:
    global _last_data
    conn = await _get_conn()
    try:
        rows = await conn.fetch(
            """
            SELECT DISTINCT ON (ticker)
                ticker, composite_score, market_index, narrative_index,
                influencer_index, macro_index, confidence_score, divergence, timestamp
            FROM sentiment_history
            ORDER BY ticker, timestamp DESC
            LIMIT 20
            """,
        )
        rows = sorted(rows, key=lambda r: r["timestamp"], reverse=True)[:20]
    finally:
        await conn.close()

    _last_data["2"] = [
        {
            "ticker":           r["ticker"],
            "composite_score":  r["composite_score"],
            "label":            _label(r["composite_score"]),
            "market_index":     r["market_index"],
            "narrative_index":  r["narrative_index"],
            "influencer_index": r["influencer_index"],
            "macro_index":      r["macro_index"],
            "confidence_score": r["confidence_score"],
            "divergence":       r["divergence"],
            "timestamp":        r["timestamp"].isoformat(),
        }
        for r in rows
    ]

    console.clear()
    console.rule("[bold cyan]SENTIMENT SCORES — 20 most recently scored tickers[/bold cyan]")

    t = Table(box=box.SIMPLE_HEAD, show_edge=False)
    t.add_column("Ticker",     style="bold white", width=7)
    t.add_column("Score",      justify="right",    width=7)
    t.add_column("Label",      width=9)
    t.add_column("Market",     justify="right",    width=8)
    t.add_column("Narrative",  justify="right",    width=10)
    t.add_column("Influencer", justify="right",    width=11)
    t.add_column("Macro",      justify="right",    width=7)
    t.add_column("Conf",       justify="right",    width=6)
    t.add_column("Divergence", width=18)
    t.add_column("Scored",     width=12)

    for r in rows:
        score = r["composite_score"]
        style = _score_style(score)
        t.add_row(
            r["ticker"],
            Text(f"{score:.1f}", style=style),
            Text(_label(score), style=style),
            _fmt(r["market_index"]),
            _fmt(r["narrative_index"]),
            _fmt(r["influencer_index"]),
            _fmt(r["macro_index"]),
            str(r["confidence_score"]),
            r["divergence"] or "—",
            _ago(r["timestamp"]),
        )

    console.print(t)


# ---------------------------------------------------------------------------
# Screen 3 — SIGNAL DATA
# ---------------------------------------------------------------------------

async def screen_signal_data() -> None:
    global _last_data, _last_ticker
    console.clear()
    console.rule("[bold cyan]SIGNAL DATA[/bold cyan]")
    ticker = Prompt.ask("  Ticker symbol").strip().upper()
    if not ticker:
        return
    _last_ticker = ticker

    conn = await _get_conn()
    try:
        rows = await conn.fetch(
            """
            SELECT signal_type, value, source, upload_type, timestamp
            FROM raw_signals
            WHERE ticker = $1
            ORDER BY timestamp DESC
            LIMIT 20
            """,
            ticker,
        )
    finally:
        await conn.close()

    _last_data["3"] = [
        {
            "ticker":      ticker,
            "signal_type": r["signal_type"],
            "value":       r["value"],
            "source":      r["source"],
            "upload_type": r["upload_type"],
            "timestamp":   r["timestamp"].isoformat(),
        }
        for r in rows
    ]

    console.rule(f"[bold cyan]Last 20 signals — {ticker}[/bold cyan]")

    if not rows:
        console.print(f"  [yellow]No signals found for {ticker}[/yellow]")
        return

    t = Table(box=box.SIMPLE_HEAD, show_edge=False)
    t.add_column("Signal Type", style="cyan",    width=30)
    t.add_column("Value",       justify="right", width=14)
    t.add_column("Source",      width=16)
    t.add_column("Upload Type", width=16)
    t.add_column("Timestamp",   width=22)

    for r in rows:
        t.add_row(
            r["signal_type"],
            _fmt(r["value"], 4),
            r["source"],
            r["upload_type"],
            r["timestamp"].strftime("%Y-%m-%d %H:%M UTC"),
        )
    console.print(t)


# ---------------------------------------------------------------------------
# Screen 4 — ARTICLES
# ---------------------------------------------------------------------------

async def screen_articles() -> None:
    global _last_data
    conn = await _get_conn()
    try:
        rows = await conn.fetch(
            """
            SELECT ticker, title, source, provider_sentiment, published_at
            FROM raw_articles
            ORDER BY published_at DESC
            LIMIT 20
            """
        )
    finally:
        await conn.close()

    _last_data["4"] = [
        {
            "ticker":             r["ticker"],
            "title":              r["title"],
            "source":             r["source"],
            "provider_sentiment": r["provider_sentiment"],
            "published_at":       r["published_at"].isoformat(),
        }
        for r in rows
    ]

    console.clear()
    console.rule("[bold cyan]ARTICLES — 20 most recent[/bold cyan]")

    t = Table(box=box.SIMPLE_HEAD, show_edge=False)
    t.add_column("Ticker",    style="bold white", width=7)
    t.add_column("Title",     width=62)
    t.add_column("Source",    width=16)
    t.add_column("Sentiment", justify="right",    width=10)
    t.add_column("Published", width=22)

    for r in rows:
        title = (r["title"] or "")[:60] + ("…" if len(r["title"] or "") > 60 else "")
        sent  = r["provider_sentiment"]
        if sent is not None:
            sent_style = "green" if sent > 0.1 else ("red" if sent < -0.1 else "white")
            sent_text  = Text(f"{sent:+.2f}", style=sent_style)
        else:
            sent_text = Text("—")
        t.add_row(
            r["ticker"],
            title,
            r["source"],
            sent_text,
            r["published_at"].strftime("%Y-%m-%d %H:%M UTC"),
        )
    console.print(t)


# ---------------------------------------------------------------------------
# Screen 5 — LIVE SCORE LOOKUP
# ---------------------------------------------------------------------------

async def screen_live_score() -> None:
    global _last_data, _last_ticker
    console.clear()
    console.rule("[bold cyan]LIVE SCORE LOOKUP (Redis)[/bold cyan]")
    ticker = Prompt.ask("  Ticker symbol").strip().upper()
    if not ticker:
        return
    _last_ticker = ticker

    redis = _redis_client()
    try:
        raw = await redis.get(f"sentiment:{ticker}")
    finally:
        await redis.close()

    console.rule(f"[bold cyan]Redis key: sentiment:{ticker}[/bold cyan]")

    if raw is None:
        console.print(f"  [yellow]No Redis key found for sentiment:{ticker}[/yellow]")
        _last_data["5"] = []
        return

    data = json.loads(raw)
    sub  = data.get("sub_indices") or {}

    _last_data["5"] = [{
        "ticker":           ticker,
        "composite_score":  data.get("composite_score"),
        "label":            _label(data.get("composite_score")),
        "confidence":       (data.get("confidence") or {}).get("score"),
        "divergence":       data.get("divergence"),
        "timestamp":        data.get("timestamp"),
        "market_index":     (sub.get("market")     or {}).get("value"),
        "narrative_index":  (sub.get("narrative")  or {}).get("value"),
        "influencer_index": (sub.get("influencer") or {}).get("value"),
        "macro_index":      (sub.get("macro")      or {}).get("value"),
        "explanation":      data.get("explanation"),
    }]

    score = data.get("composite_score")
    style = _score_style(score)
    conf  = (data.get("confidence") or {}).get("score", "—")
    ts    = data.get("timestamp", "—")

    console.print(Panel(
        f"[{style}]{_label(score)} — score {score:.1f}[/{style}]   "
        f"confidence: [yellow]{conf}[/yellow]   "
        f"scored: [dim]{_ago(ts)}[/dim] ({ts})",
        title=f"[bold]{ticker}[/bold]",
        expand=False,
    ))

    if sub:
        st = Table(title="Sub-indices", box=box.SIMPLE_HEAD, show_edge=False)
        st.add_column("Layer", style="cyan", width=14)
        st.add_column("Value", justify="right", width=8)
        for layer, info in sub.items():
            v = (info or {}).get("value")
            st.add_row(layer, _fmt(v))
        console.print(st)

    drivers = data.get("top_drivers") or []
    if drivers:
        dt = Table(title="Top Drivers", box=box.SIMPLE_HEAD, show_edge=False)
        dt.add_column("Signal",    style="cyan", width=30)
        dt.add_column("Direction", width=10)
        dt.add_column("Magnitude", justify="right", width=10)
        dt.add_column("Layer",     width=14)
        for d in drivers:
            direction = d.get("direction", "—")
            dir_style = "green" if direction == "bullish" else ("red" if direction == "bearish" else "white")
            dt.add_row(
                d.get("signal", "—"),
                Text(direction, style=dir_style),
                _fmt(d.get("magnitude"), 2),
                d.get("source_layer", "—"),
            )
        console.print(dt)

    explanation = data.get("explanation")
    if explanation:
        console.print(Panel(explanation, title="Explanation", expand=False))

    console.print()
    console.print("[dim]Full JSON:[/dim]")
    console.print_json(json.dumps(data))


# ---------------------------------------------------------------------------
# Screen 6 — TOP SCORES TODAY
# ---------------------------------------------------------------------------

async def screen_top_scores() -> None:
    global _last_data
    conn = await _get_conn()
    try:
        all_rows = await conn.fetch(
            """
            SELECT DISTINCT ON (ticker) ticker, composite_score, confidence_score, timestamp
            FROM sentiment_history
            WHERE timestamp > NOW() - INTERVAL '24 hours'
            ORDER BY ticker, timestamp DESC
            """,
        )
    finally:
        await conn.close()

    bullish = sorted(all_rows, key=lambda r: r["composite_score"], reverse=True)[:10]
    bearish = sorted(all_rows, key=lambda r: r["composite_score"])[:10]

    _last_data["6"] = (
        [{"category": "bullish", "ticker": r["ticker"],
          "composite_score": r["composite_score"], "confidence_score": r["confidence_score"],
          "timestamp": r["timestamp"].isoformat()} for r in bullish]
        +
        [{"category": "bearish", "ticker": r["ticker"],
          "composite_score": r["composite_score"], "confidence_score": r["confidence_score"],
          "timestamp": r["timestamp"].isoformat()} for r in bearish]
    )

    console.clear()
    console.rule("[bold cyan]TOP SCORES TODAY — last 24 hours[/bold cyan]")

    def _make_table(rows, title: str, score_style: str) -> Table:
        t = Table(title=title, box=box.SIMPLE_HEAD, show_edge=False, min_width=38)
        t.add_column("Ticker", style="bold white", width=7)
        t.add_column("Score",  justify="right",    width=7)
        t.add_column("Conf",   justify="right",    width=6)
        t.add_column("Scored", width=12)
        for r in rows:
            t.add_row(
                r["ticker"],
                Text(f"{r['composite_score']:.1f}", style=score_style),
                str(r["confidence_score"]),
                _ago(r["timestamp"]),
            )
        return t

    if not all_rows:
        console.print("  [yellow]No sentiment data in the last 24 hours.[/yellow]")
        return

    left  = _make_table(bullish, "Most Bullish", "bold green")
    right = _make_table(bearish, "Most Bearish", "bold red")
    console.print(Columns([left, right], equal=True, expand=False))


# ---------------------------------------------------------------------------
# Key input — blocking and timeout variants
# ---------------------------------------------------------------------------

def _read_key() -> str:
    """Blocking: read one character from stdin without requiring Enter."""
    import termios
    import tty
    fd  = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return ch


async def _wait_for_key(timeout: float) -> str | None:
    """
    Wait up to `timeout` seconds for a keypress.
    Returns the character, or None if the timeout elapsed.
    Runs a single blocking thread so there is no stdin-thread leak.
    """
    loop = asyncio.get_event_loop()

    def _blocking() -> str | None:
        import termios
        import tty
        fd  = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            readable, _, _ = select.select([sys.stdin], [], [], timeout)
            return sys.stdin.read(1) if readable else None
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    return await loop.run_in_executor(None, _blocking)


# ---------------------------------------------------------------------------
# Export — delegates to db_exports.show_export_menu
# ---------------------------------------------------------------------------

async def _handle_export(key: str) -> None:
    """Open the export sub-menu for the current screen key."""
    await show_export_menu(key, _last_data, SCREENS, console, _read_key)


# ---------------------------------------------------------------------------
# Navigation bar + screen runner
# ---------------------------------------------------------------------------

SCREENS: dict[str, tuple[str, ...]] = {
    "1": ("OVERVIEW",         screen_overview),         # type: ignore[dict-item]
    "2": ("SENTIMENT SCORES", screen_sentiment_scores), # type: ignore[dict-item]
    "3": ("SIGNAL DATA",      screen_signal_data),      # type: ignore[dict-item]
    "4": ("ARTICLES",         screen_articles),         # type: ignore[dict-item]
    "5": ("LIVE SCORE",       screen_live_score),       # type: ignore[dict-item]
    "6": ("TOP SCORES TODAY", screen_top_scores),       # type: ignore[dict-item]
}


def _print_nav(current: str) -> None:
    parts = []
    for key, (name, _) in SCREENS.items():
        if key == current:
            parts.append(f"[bold reverse cyan] {key}:{name} [/bold reverse cyan]")
        else:
            parts.append(f"[dim] {key}:{name} [/dim]")
    auto_badge = (
        "[bold yellow] A:auto ON [/bold yellow]" if _auto_refresh
        else "[dim] A:auto [/dim]"
    )
    parts.append(auto_badge)
    parts.append("[dim] E:export  R:refresh  Q:quit [/dim]")
    console.print("  " + "  ".join(parts))
    console.print()


async def _run_screen(key: str) -> None:
    _, fn = SCREENS[key]
    try:
        await fn()
    except Exception as exc:  # noqa: BLE001
        console.print(f"\n[bold red]Error:[/bold red] {exc}")
    _print_nav(key)


# ---------------------------------------------------------------------------
# Status-line helper (used by auto-refresh countdown)
# ---------------------------------------------------------------------------

def _write_status(text: str) -> None:
    """Overwrite the current terminal line in-place (no rich markup)."""
    sys.stdout.write(f"\r\033[K{text}")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def main() -> None:
    global _auto_refresh

    if "DATABASE_URL" not in os.environ:
        console.print("[bold red]DATABASE_URL not set — check your .env file.[/bold red]")
        sys.exit(1)

    current = "1"
    await _run_screen(current)

    while True:

        if _auto_refresh:
            # ----------------------------------------------------------------
            # Auto-refresh countdown — 1-second ticks, overwrites status line
            # ----------------------------------------------------------------
            countdown = AUTO_REFRESH_SECS
            while _auto_refresh:
                _write_status(
                    f"  ⏱  Auto-refresh in {countdown:2d}s  |  "
                    "A:stop  E:export  R:now  1-6:switch  Q:quit"
                )
                key = await _wait_for_key(1.0)

                if key is None:
                    # Tick
                    countdown -= 1
                    if countdown <= 0:
                        sys.stdout.write("\n")
                        await _run_screen(current)
                        countdown = AUTO_REFRESH_SECS
                    continue

                # A key was pressed inside the countdown
                sys.stdout.write("\n")
                key = key.lower()

                if key == "q":
                    console.print("[bold cyan]Bye.[/bold cyan]")
                    return
                elif key == "a":
                    _auto_refresh = False
                    console.print("  [yellow]Auto-refresh stopped.[/yellow]")
                elif key == "r":
                    await _run_screen(current)
                    countdown = AUTO_REFRESH_SECS
                elif key == "e":
                    await _handle_export(current)
                elif key in SCREENS:
                    current = key
                    await _run_screen(current)
                    countdown = AUTO_REFRESH_SECS
                # unknown keys: loop continues

        else:
            # ----------------------------------------------------------------
            # Normal mode — blocking single keypress
            # ----------------------------------------------------------------
            console.print("[dim]Press a key…[/dim]", end="")
            key = _read_key().lower()
            console.print()

            if key == "q":
                console.print("[bold cyan]Bye.[/bold cyan]")
                break
            elif key == "r":
                await _run_screen(current)
            elif key == "a":
                _auto_refresh = True
                console.print(
                    f"  [bold yellow]Auto-refresh ON[/bold yellow] — "
                    f"refreshing every {AUTO_REFRESH_SECS}s"
                )
            elif key == "e":
                await _handle_export(current)
            elif key in SCREENS:
                current = key
                await _run_screen(current)
            # unknown keys: ignored


if __name__ == "__main__":
    asyncio.run(main())
