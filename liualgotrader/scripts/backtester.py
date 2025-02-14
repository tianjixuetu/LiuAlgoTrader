import getopt
import os
import sys
from datetime import date, datetime
from typing import List, Optional

import pandas as pd
import parsedatetime.parsedatetime as pdt
import pygit2
import pytz
import toml
from requests.exceptions import HTTPError

from liualgotrader import enhanced_backtest
from liualgotrader.common import config, market_data, trading_data
from liualgotrader.common.database import create_db_connection
from liualgotrader.common.decorators import timeit
from liualgotrader.common.tlog import tlog
from liualgotrader.common.types import AssetType, TimeScale
from liualgotrader.fincalcs.vwap import add_daily_vwap
from liualgotrader.models.algo_run import AlgoRun
from liualgotrader.models.new_trades import NewTrade
from liualgotrader.models.trending_tickers import TrendingTickers
from liualgotrader.scanners.base import Scanner
from liualgotrader.scanners.momentum import Momentum
from liualgotrader.strategies.base import Strategy, StrategyType


def show_usage():
    print(
        f"\n{sys.argv[0]} from <start_date> [--asset=equity(DEFAULT)|crypto][--scanners=<scanner-name,>] [--strats=<strategy-name,>] [--to=<end_date> DEFAULT is today] [--scale=day(DEFAULT)|minute [--buy-fee-percentage=0.(DEFAULT)] [--sell-fee-percentage=0.(DEFAULT)]",
    )

    print("\n\noptions:")
    print(
        "asset\t\t\tAsset type being traded. equity = US Equities, crypt = Crypto-currency. Asset type affects the market schedule for backtesting."
    )
    print(
        "to\t\t\tdate string in the format YYYY-MM-DD, if not provided current day is selected"
    )
    print("scale\t\t\ttime-scale for loading past data for back-test-ing")
    print(
        "buy-fee-percentage\tBroker fees as percentage from transaction. Represented as 0-1."
    )
    print(
        "sell-fee-percentage\tBroker fees as percentage from transaction. Represented as 0-1."
    )


def show_version(filename: str, version: str) -> None:
    """Display welcome message"""
    print(f"filename:{filename}\ngit version:{version}\n")


def dateFromString(s: str) -> date:
    c = pdt.Calendar()
    result, what = c.parse(s)
    dt = None

    # what was returned (see http://code-bear.com/code/parsedatetime/docs/)
    # 0 = failed to parse
    # 1 = date (with current time, as a struct_time)
    # 2 = time (with current date, as a struct_time)
    # 3 = datetime
    if what in (1, 2):
        # result is struct_time
        dt = datetime(*result[:6]).date()
    elif what == 3:
        # result is a datetime
        dt = result.date()

    if dt is None:
        raise ValueError(f"Don't understand date '{s}'")

    return dt


def main_cli() -> None:
    try:
        config.build_label = pygit2.Repository("../").describe(
            describe_strategy=pygit2.GIT_DESCRIBE_TAGS
        )
    except pygit2.GitError:
        import liualgotrader

        config.build_label = liualgotrader.__version__ if hasattr(liualgotrader, "__version__") else ""  # type: ignore

    if len(sys.argv) == 1:
        show_usage()
        sys.exit(0)

    config.filename = os.path.basename(__file__)

    folder = (
        config.tradeplan_folder
        if config.tradeplan_folder[-1] == "/"
        else f"{config.tradeplan_folder}/"
    )
    fname = f"{folder}{config.configuration_filename}"
    try:
        conf_dict = toml.load(fname)
        tlog(f"loaded configuration file from {fname}")
    except FileNotFoundError:
        tlog(f"[ERROR] could not locate tradeplan file {fname}")
        sys.exit(0)
    conf_dict = toml.load(config.configuration_filename)
    config.portfolio_value = conf_dict.get("portfolio_value", None)
    if "risk" in conf_dict:
        config.risk = conf_dict["risk"]

    if sys.argv[1] == "from":
        from_date = dateFromString(sys.argv[2])
        tlog(f"selected {sys.argv[2]} applied {from_date}")
        try:
            scanners: Optional[List] = None
            strategies: Optional[List] = None
            to_date = datetime.now(tz=pytz.timezone("US/Eastern")).date()
            scale = TimeScale["day"]
            buy_fee_percentage = 0.0
            sell_fee_percentage = 0.0
            asset_type = AssetType.US_EQUITIES
            opts, args = getopt.getopt(
                sys.argv[3:],
                shortopts="",
                longopts=[
                    "to=",
                    "scale=",
                    "scanners=",
                    "strats=",
                    "buy-fee-percentage=",
                    "sell-fee-percentage=",
                    "asset=",
                ],
            )
            for opt, arg in opts:
                if opt in ("--scanners"):
                    scanners = arg.split(",")
                elif opt in ("--strats"):
                    strategies = arg.split(",")
                elif opt in ("--to"):
                    to_date = dateFromString(arg)
                elif opt in ("--buy-fee-percentage"):
                    buy_fee_percentage = float(arg)
                elif opt in ("--sell-fee-percentage"):
                    sell_fee_percentage = float(arg)
                elif opt in ("--asset"):
                    if arg.lower() == "crypto":
                        asset_type = AssetType.CRYPTO
                    elif arg.lower() == "equity":
                        asset_type = AssetType.US_EQUITIES
                    else:
                        print(
                            f"ERROR: value {arg} not supported for parameter 'asset'"
                        )
                        sys.exit(0)
                elif opt in ("--scale"):
                    if arg in ("day", "minute"):
                        scale = TimeScale[arg]
                    else:
                        print(
                            f"ERROR: wrong `scale` parameter {arg} passed: not implemented yet"
                        )
                        sys.exit(0)
        except getopt.GetoptError as e:
            print(f"Error parsing options:{e}\n")
            show_usage()
            sys.exit(0)

        enhanced_backtest.backtest(
            from_date=from_date,
            to_date=to_date,
            scale=scale,
            config=conf_dict,
            scanners=scanners,
            strategies=strategies,
            asset_type=asset_type,
            buy_fee_percentage=buy_fee_percentage,
            sell_fee_percentage=sell_fee_percentage,
        )
    else:
        print(f"Error parsing {sys.argv[1:]}\n")
        show_usage()
        sys.exit(0)

    sys.exit(0)
