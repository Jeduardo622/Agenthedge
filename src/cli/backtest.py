"""CLI for running historical backtests."""

from __future__ import annotations

from datetime import date, datetime
from typing import List

import typer
from dotenv import load_dotenv

from backtest import BacktestEngine, BacktestRunConfig, YFinanceDataLoader

app = typer.Typer(help="Backtesting utilities for the strategy council")


def _parse_date(value: str) -> date:
    try:
        return datetime.fromisoformat(value).date()
    except ValueError as exc:  # pragma: no cover - user validation path
        raise typer.BadParameter(f"Invalid date: {value}") from exc


@app.command()
def run(
    symbol: List[str] = typer.Option(
        ...,
        "--symbol",
        "-s",
        help="Ticker symbol to include (may be specified multiple times)",
    ),
    start: str = typer.Option(..., help="Start date (YYYY-MM-DD)"),
    end: str = typer.Option(..., help="End date (YYYY-MM-DD)"),
    capital: float = typer.Option(1_000_000.0, "--capital", help="Initial cash balance"),
    storage_dir: str = typer.Option("storage/backtests", "--storage-dir", help="Output directory"),
) -> None:
    """Run a backtest over the requested window using default strategies."""

    load_dotenv()
    if not symbol:
        raise typer.BadParameter("At least one --symbol must be provided")
    start_date = _parse_date(start)
    end_date = _parse_date(end)
    if start_date > end_date:
        raise typer.BadParameter("start date must be on/before end date")

    engine = BacktestEngine(data_loader=YFinanceDataLoader(), storage_dir=storage_dir)
    config = BacktestRunConfig(symbols=symbol, start=start_date, end=end_date, initial_cash=capital)
    result = engine.run(config)
    result_path = result.save()
    typer.echo(
        f"[{result.run_id}] symbols={','.join(symbol)} final_nav=${result.final_nav:,.2f} "
        f"return={result.return_pct*100:.2f}% trades={result.trades}"
    )
    if result_path:
        typer.echo(f"Artifacts saved under: {result_path.parent}")


if __name__ == "__main__":
    app()
