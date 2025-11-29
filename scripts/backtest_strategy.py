"""Helper entrypoint for running the backtest CLI via `python scripts/backtest_strategy.py`."""

from cli.backtest import app

if __name__ == "__main__":
    app(prog_name="backtest")
