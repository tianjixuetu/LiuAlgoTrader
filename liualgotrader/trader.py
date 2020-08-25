"""
Trading strategy runner
"""
import importlib.util
import multiprocessing as mp
import os
import sys
import time
import uuid
from datetime import datetime
from math import ceil
from typing import List

import alpaca_trade_api as tradeapi
import pygit2
import toml
from pytz import timezone

from liualgotrader.common import config
from liualgotrader.common.market_data import get_historical_data_from_polygon
from liualgotrader.common.tlog import tlog
from liualgotrader.consumer import consumer_main
from liualgotrader.polygon_producer import polygon_producer_main
from liualgotrader.scanners.base import Scanner
from liualgotrader.scanners.momentum import Momentum


def motd(filename: str, version: str, unique_id: str) -> None:
    """Display welcome message"""

    print("+=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=+")
    tlog(f"{filename} {version} starting")
    tlog(f"unique id: {unique_id}")
    print("+=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=+")
    tlog(f"DSN: {config.dsn}")
    tlog(f"MAX SYMBOLS: {config.total_tickers}")
    print("+=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=+")


def get_trading_windows(tz, api):
    """Get start and end time for trading"""
    tlog("checking market schedule")
    today = datetime.today().astimezone(tz)
    today_str = datetime.today().astimezone(tz).strftime("%Y-%m-%d")

    calendar = api.get_calendar(start=today_str, end=today_str)[0]

    tlog(f"next open date {calendar.date.date()}")

    if today.date() < calendar.date.date():
        tlog(f"which is not today {today}")
        return None, None
    market_open = today.replace(
        hour=calendar.open.hour,
        minute=calendar.open.minute,
        second=0,
        microsecond=0,
    )
    market_close = today.replace(
        hour=calendar.close.hour,
        minute=calendar.close.minute,
        second=0,
        microsecond=0,
    )
    return market_open, market_close


"""
process main
"""


def ready_to_start(trading_api: tradeapi) -> bool:
    nyc = timezone("America/New_York")
    config.market_open, config.market_close = get_trading_windows(
        nyc, trading_api
    )

    if config.market_open or config.bypass_market_schedule:

        if not config.bypass_market_schedule:
            tlog(
                f"markets open {config.market_open} market close {config.market_close}"
            )

        # Wait until just before we might want to trade
        current_dt = datetime.today().astimezone(nyc)
        tlog(f"current time {current_dt}")

        if config.bypass_market_schedule:
            tlog("bypassing market schedule, are we debugging something?")
            return True
        elif current_dt < config.market_close:
            to_market_open = config.market_open - current_dt
            if to_market_open.total_seconds() > 0:
                try:
                    tlog(
                        f"waiting for market open: {to_market_open} ({to_market_open.total_seconds()} seconds)"
                    )
                    time.sleep(to_market_open.total_seconds() + 1)
                except KeyboardInterrupt:
                    return False

            return True

    return False


"""
starting
"""


if __name__ == "__main__":
    config.filename = os.path.basename(__file__)
    config.build_label = pygit2.Repository("./").describe(
        describe_strategy=pygit2.GIT_DESCRIBE_TAGS
    )
    uid = str(uuid.uuid4())
    motd(filename=config.filename, version=config.build_label, unique_id=uid)

    # load configuration
    tlog(
        f"loading configuration file from {os.getcwd()}/{config.configuration_filename}"
    )
    conf_dict = toml.load(config.configuration_filename)
    print("")

    # parse configuration
    config.bypass_market_schedule = conf_dict.get(
        "bypass_market_schedule", False
    )

    # basic validation for scanners and strategies
    tlog(f"bypass_market_schedule = {config.bypass_market_schedule}")
    if "scanners" not in conf_dict or len(conf_dict["scanners"]) == 0:
        tlog("must have at least one scanner configured")
        exit(0)
    elif "strategies" not in conf_dict or len(conf_dict["strategies"]) == 0:
        tlog("must have at least one strategy configured")
        exit(0)

    scanners = conf_dict["scanners"]
    for scanner in scanners:
        if len(list(scanner.keys())) != 1:
            tlog(f"invalid scanner configuration {scanner}")
            exit(0)
        else:
            tlog(f"- {list(scanner.keys())[0]} scanner detected")

    print("+=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=+")
    data_api = tradeapi.REST(
        base_url=config.prod_base_url,
        key_id=config.prod_api_key_id,
        secret_key=config.prod_api_secret,
    )

    if ready_to_start(data_api):
        symbols: List = []
        for scanner in scanners:
            scanner_name = list(scanner.keys())[0]
            if scanner_name == "momentum":
                scanner_details = scanner[scanner_name]
                try:
                    print(
                        "+=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=+"
                    )
                    scanner_object = Momentum(
                        provider=scanner_details["provider"],
                        data_api=data_api,
                        min_last_dv=scanner_details["min_last_dv"],
                        min_share_price=scanner_details["min_share_price"],
                        max_share_price=scanner_details["max_share_price"],
                        min_volume=scanner_details["min_volume"],
                        from_market_open=scanner_details["from_market_open"],
                        today_change_percent=scanner_details["min_gap"],
                        recurrence=scanner_details.get("recurrence", False),
                        max_symbols=scanner_details.get(
                            "max_symbols", config.total_tickers
                        ),
                    )
                    tlog(f"instantiated momentum scanner")
                except KeyError as e:
                    tlog(
                        f"Error {e} in processing of scanner configuration {scanner_details}"
                    )
                    exit(0)
            else:
                print("+=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=+")
                tlog(f"custom scanner {scanner_name} selected")
                scanner_details = scanner[scanner_name]
                try:
                    spec = importlib.util.spec_from_file_location(
                        "module.name", scanner_details["filename"]
                    )
                    custom_scanner_module = importlib.util.module_from_spec(
                        spec
                    )
                    spec.loader.exec_module(  # type: ignore
                        custom_scanner_module
                    )
                    class_name = list(scanner.keys())[0]
                    custom_scanner = getattr(custom_scanner_module, class_name)

                    if not issubclass(custom_scanner, Scanner):
                        tlog(
                            f"custom scanner must inherit from class {Scanner.__name__}"
                        )
                        exit(0)

                    if "recurrence" not in scanner_details:
                        scanner_object = custom_scanner(
                            recurrence=False,
                            data_api=data_api,
                            **scanner_details,
                        )
                    else:
                        scanner_object = custom_scanner(
                            data_api=data_api, **scanner_details
                        )

                except Exception as e:
                    tlog(f"Error {e}")
                    exit(0)

            symbols += scanner_object.run()

        print("+=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=+")
        # add open positions
        base_url = (
            config.prod_base_url
            if config.env == "PROD"
            else config.paper_base_url
        )
        api_key_id = (
            config.prod_api_key_id
            if config.env == "PROD"
            else config.paper_api_key_id
        )
        api_secret = (
            config.prod_api_secret
            if config.env == "PROD"
            else config.paper_api_secret
        )
        trading_api = tradeapi.REST(
            base_url=base_url, key_id=api_key_id, secret_key=api_secret
        )
        existing_positions = trading_api.list_positions()

        if len(existing_positions) == 0:
            tlog("no open positions")
        else:
            for position in existing_positions:
                if position.symbol not in symbols:
                    symbols.append(position.symbol)
                    tlog(f"added existing open position in {position.symbol}")
        tlog(f"Tracking {len(symbols)} symbols")

        # if use_finnhub or use_finnhub_history:
        #    minute_history = get_historical_data_from_finnhub(symbols=symbols)
        # elif use_polygon:
        minute_history = get_historical_data_from_polygon(
            api=data_api,
            symbols=symbols,
            max_tickers=min(config.total_tickers, len(symbols)),
        )

        symbols = list(minute_history.keys())

        if len(symbols) > 0:
            print("+=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=+")
            mp.set_start_method("spawn")

            # Consumers first
            _num_consumer_processes = ceil(
                1.0 * len(symbols) / config.num_consumer_processes_ratio
            )
            queues: List[mp.Queue] = [
                mp.Queue() for i in range(_num_consumer_processes)
            ]

            q_id_hash = {}
            symbol_by_queue = {}
            c = 0
            for symbol in symbols:
                _index = int(
                    list(minute_history.keys()).index(symbol)
                    / config.num_consumer_processes_ratio
                )

                q_id_hash[symbol] = _index

                # tlog(
                #    f"{symbol} consumer process index {c}/{q_id_hash[symbol]}"
                # )
                if _index not in symbol_by_queue:
                    symbol_by_queue[_index] = [symbol]
                else:
                    symbol_by_queue[_index].append(symbol)
                c += 1

            consumers = [
                mp.Process(
                    target=consumer_main,
                    args=(
                        queues[i],
                        symbol_by_queue[i],
                        minute_history,
                        uid,
                        conf_dict,
                    ),
                )
                for i in range(_num_consumer_processes)
            ]
            for p in consumers:
                # p.daemon = True
                p.start()

            # Producers second
            # if use_finnhub:
            #    producer = mp.Process(
            #        target=finnhub_producer_main,
            #        args=(queues, symbols, q_id_hash),
            #    )
            #    producer.start()
            # else:
            producer = mp.Process(
                target=polygon_producer_main,
                args=(queues, symbols, q_id_hash, config.market_close),
            )
            producer.start()

            # wait for completion and hope everyone plays nicely
            try:
                producer.join()
                for p in consumers:
                    p.join()
            except KeyboardInterrupt:
                producer.terminate()
                for p in consumers:
                    p.terminate()

    print("+=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=+")
    tlog(f"run {uid} completed")
    sys.exit(0)