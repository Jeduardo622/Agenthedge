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
    BacktestEngine,
    BacktestResult,
    BacktestRunConfig,
    InMemoryDataLoader,
    YFinanceDataLoader,
    build_backtest_engine_from_config,
)
from research_inputs.catalyst_calendar import CatalystCalendarPacket

app = typer.Typer(help="Backtesting utilities for the strategy council")

EXPECTED_RETURN_SIGNAL = "catalyst_expected_return"


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


def _build_promotion_report(
    *,
    result: BacktestResult,
    engine: BacktestEngine,
    runtime_config: AgentRuntimeConfig,
    run_config: BacktestRunConfig,
    price_fixture: str | None,
) -> dict[str, Any]:
    catalyst_packet = _find_catalyst_packet(engine)
    catalyst_trade_count = _count_catalyst_fills(result)
    fixture_backed = price_fixture is not None
    catalyst_opt_in = (
        runtime_config.experimental_strategies is not None
        and "catalyst" in runtime_config.experimental_strategies
    )

    return {
        "report_schema_version": 1,
        "run_id": result.run_id,
        "symbols": list(run_config.symbols),
        "start": run_config.start.isoformat(),
        "end": run_config.end.isoformat(),
        "initial_cash": float(run_config.initial_cash),
        "price_fixture": price_fixture,
        "fixture_backed": fixture_backed,
        "no_live_network": fixture_backed,
        "catalyst": _catalyst_report(catalyst_packet),
        "strategy_names": _strategy_names(engine),
        "trades": int(result.trades),
        "catalyst_trade_count": catalyst_trade_count,
        "final_nav": float(result.final_nav),
        "return_pct": float(result.return_pct),
        "validation": {
            "fixture_backed": fixture_backed,
            "no_live_network": fixture_backed,
            "catalyst_opt_in": catalyst_opt_in,
            "packet_loaded": catalyst_packet is not None,
            "no_stale_catalyst_trades": _no_stale_catalyst_trades(
                catalyst_packet,
                catalyst_trade_count=catalyst_trade_count,
                end=run_config.end,
            ),
        },
    }


def _write_promotion_report(
    path: Path,
    *,
    result: BacktestResult,
    engine: BacktestEngine,
    runtime_config: AgentRuntimeConfig,
    run_config: BacktestRunConfig,
    price_fixture: str | None,
) -> None:
    report = _build_promotion_report(
        result=result,
        engine=engine,
        runtime_config=runtime_config,
        run_config=run_config,
        price_fixture=price_fixture,
    )
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _find_catalyst_packet(engine: BacktestEngine) -> CatalystCalendarPacket | None:
    research_inputs = getattr(engine, "research_inputs", {})
    if not isinstance(research_inputs, Mapping):
        return None
    for value in research_inputs.values():
        if not isinstance(value, Mapping):
            continue
        packet = value.get("catalyst_calendar")
        if isinstance(packet, CatalystCalendarPacket):
            return packet
    return None


def _catalyst_report(packet: CatalystCalendarPacket | None) -> dict[str, Any] | None:
    if packet is None:
        return None
    return {
        "artifact_id": packet.artifact_id,
        "plugin": packet.plugin,
        "workflow": packet.workflow,
        "symbol": packet.symbol,
        "as_of": packet.as_of.isoformat(),
        "promotion_status": packet.promotion_status,
        "source_count": len(packet.source_labels),
        "catalyst_count": len(packet.catalysts),
        "signal_count": len(packet.signals),
    }


def _strategy_names(engine: BacktestEngine) -> list[str]:
    strategies = getattr(engine, "strategies", [])
    return [
        str(getattr(strategy, "name")) for strategy in strategies if getattr(strategy, "name", None)
    ]


def _count_catalyst_fills(result: BacktestResult) -> int:
    fills = getattr(result, "fills", [])
    if not isinstance(fills, list):
        return 0
    count = 0
    for fill in fills:
        if not isinstance(fill, Mapping):
            continue
        strategies = fill.get("strategies", [])
        if not isinstance(strategies, list):
            continue
        if any(
            isinstance(strategy, Mapping) and strategy.get("strategy") == "catalyst"
            for strategy in strategies
        ):
            count += 1
    return count


def _no_stale_catalyst_trades(
    packet: CatalystCalendarPacket | None,
    *,
    catalyst_trade_count: int,
    end: date,
) -> bool:
    if catalyst_trade_count == 0:
        return True
    if packet is None:
        return False
    active_catalyst = any(catalyst.expires_at >= end for catalyst in packet.catalysts)
    active_expected_return = any(
        signal.name == EXPECTED_RETURN_SIGNAL and signal.expires_at >= end
        for signal in packet.signals
    )
    return active_catalyst and active_expected_return


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
    promotion_report: bool = typer.Option(
        False,
        "--promotion-report",
        help="Write promotion_report.json alongside backtest artifacts for experiment review",
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
        if promotion_report:
            report_path = result_path.parent / "promotion_report.json"
            _write_promotion_report(
                report_path,
                result=result,
                engine=engine,
                runtime_config=runtime_config,
                run_config=config,
                price_fixture=price_fixture,
            )
            typer.echo(f"Promotion report saved under: {report_path}")


if __name__ == "__main__":
    app()
