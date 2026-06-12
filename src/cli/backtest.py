"""CLI for running historical backtests."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any, List

import typer
from dotenv import load_dotenv

from agents.config import AgentRuntimeConfig
from backtest import (
    BacktestBar,
    BacktestRunConfig,
    InMemoryDataLoader,
    YFinanceDataLoader,
    build_backtest_engine_from_config,
)

app = typer.Typer(help="Backtesting utilities for the strategy council")


def _parse_date(value: str) -> date:
    try:
        return datetime.fromisoformat(value).date()
    except ValueError as exc:  # pragma: no cover - user validation path
        raise typer.BadParameter(f"Invalid date: {value}") from exc


def _load_price_fixture(path: str) -> InMemoryDataLoader:
    target = Path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise typer.BadParameter(f"Unable to read price fixture: {target}") from exc
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"Invalid price fixture JSON: {target}") from exc
    if not isinstance(payload, Mapping):
        raise typer.BadParameter("price fixture must be a JSON object keyed by symbol")

    dataset: dict[str, list[BacktestBar]] = {}
    for raw_symbol, raw_rows in payload.items():
        if not isinstance(raw_symbol, str) or not isinstance(raw_rows, list):
            raise typer.BadParameter("price fixture entries must map symbols to row lists")
        rows: list[BacktestBar] = []
        for raw_row in raw_rows:
            if not isinstance(raw_row, Mapping):
                raise typer.BadParameter(f"price fixture rows for {raw_symbol} must be objects")
            rows.append(
                BacktestBar(
                    date=_parse_date(str(_required_fixture_field(raw_row, "date"))),
                    open=_float_fixture_field(raw_row, "open"),
                    high=_float_fixture_field(raw_row, "high"),
                    low=_float_fixture_field(raw_row, "low"),
                    close=_float_fixture_field(raw_row, "close"),
                    volume=_optional_float_fixture_field(raw_row, "volume"),
                )
            )
        dataset[raw_symbol.upper()] = rows
    return InMemoryDataLoader(dataset)


def _required_fixture_field(row: Mapping[str, Any], field: str) -> Any:
    value = row.get(field)
    if value is None:
        raise typer.BadParameter(f"price fixture row missing required field: {field}")
    return value


def _float_fixture_field(row: Mapping[str, Any], field: str) -> float:
    try:
        return float(_required_fixture_field(row, field))
    except (TypeError, ValueError) as exc:
        raise typer.BadParameter(f"price fixture field must be numeric: {field}") from exc


def _optional_float_fixture_field(row: Mapping[str, Any], field: str) -> float | None:
    value = row.get(field)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise typer.BadParameter(f"price fixture field must be numeric: {field}") from exc


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
    price_fixture: str | None = typer.Option(
        None,
        "--price-fixture",
        help="JSON OHLCV fixture for deterministic local runs instead of YFinance",
    ),
) -> None:
    """Run a backtest over the requested window using default strategies."""

    load_dotenv()
    if not symbol:
        raise typer.BadParameter("At least one --symbol must be provided")
    start_date = _parse_date(start)
    end_date = _parse_date(end)
    if start_date > end_date:
        raise typer.BadParameter("start date must be on/before end date")

    runtime_config = AgentRuntimeConfig.from_env()
    data_loader = _load_price_fixture(price_fixture) if price_fixture else YFinanceDataLoader()
    engine = build_backtest_engine_from_config(
        runtime_config,
        data_loader=data_loader,
        storage_dir=storage_dir,
    )
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
