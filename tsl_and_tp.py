#!/usr/bin/env python3
"""Cyberjunky's 3Commas bot helpers."""
import argparse
import configparser
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from helpers.logging import Logger, NotificationHandler
from helpers.misc import check_deal, wait_time_interval
from helpers.threecommas import init_threecommas_api


def load_config():
    """Create default or load existing config file."""

    cfg = configparser.ConfigParser()
    if cfg.read(f"{datadir}/{program}.ini"):
        return cfg

    cfg["settings"] = {
        "timezone": "Europe/Amsterdam",
        "check-interval": 120,
        "monitor-interval": 60,
        "debug": False,
        "logrotate": 7,
        "botids": [12345, 67890],
        "activation-percentage": 3.0,
        "initial-stoploss-percentage": 1.0,
        "tp-increment-factor": 0.5,
        "3c-apikey": "Your 3Commas API Key",
        "3c-apisecret": "Your 3Commas API Secret",
        "notifications": False,
        "notify-urls": ["notify-url1"],
    }

    with open(f"{datadir}/{program}.ini", "w") as cfgfile:
        cfg.write(cfgfile)

    return None


def update_deal(thebot, deal, new_stoploss, new_take_profit):
    """Update bot with new SL."""
    bot_name = thebot["name"]
    deal_id = deal["id"]

    error, data = api.request(
        entity="deals",
        action="update_deal",
        action_id=str(deal_id),
        payload={
            "deal_id": thebot["id"],
            "stop_loss_percentage": new_stoploss,
            "take_profit": new_take_profit,
        },
    )
    if data:
        logger.info(
            f"Changing SL for deal {deal_id}/{deal['pair']} on bot \"{bot_name}\"\n"
            f"Changed SL from {deal['stop_loss_percentage']}% to {new_stoploss}%. "
            f"Changed TP from {deal['take_profit']}% to {new_take_profit}",
            True,
        )
    else:
        if error and "msg" in error:
            logger.error(
                "Error occurred updating bot with new SL/TP values: %s" % error["msg"]
            )
        else:
            logger.error("Error occurred updating bot with new SL/TP valuess")


def process_deals(thebot):
    """Check deals from bot and compare StopLoss against the database."""

    deals = thebot["active_deals"]
    monitored_deals = 0

    if deals:
        botid = thebot["id"]
        current_deals = ""

        for deal in deals:
            deal_id = deal["id"]

            if current_deals:
                current_deals += ","
            current_deals += str(deal_id)

            actual_profit_percentage = float(deal["actual_profit_percentage"])
            existing_deal = check_deal(cursor, deal_id)

            if not existing_deal and actual_profit_percentage >= activation_percentage:
                monitored_deals = +1

                # New deal which requires TSL
                activation_diff = actual_profit_percentage - activation_percentage
                new_stoploss = 0.0 - round(
                    initial_stoploss_percentage + (activation_diff), 2
                )

                # Increase TP using diff multiplied with the configured factor
                new_take_profit = round(
                    float(deal["take_profit"])
                    + (activation_diff * tp_increment_factor),
                    2,
                )

                update_deal(thebot, deal, new_stoploss, new_take_profit)

                db.execute(
                    f"INSERT INTO deals (dealid, botid, last_profit_percentage, last_stop_loss_percentage) "
                    f"VALUES ({deal_id}, {botid}, {actual_profit_percentage}, {new_stoploss})"
                )
                logger.info(
                    f"New deal found {deal_id}/{deal['pair']} on bot \"{thebot['name']}\"; "
                    f"current profit {actual_profit_percentage}, stoploss set to {new_stoploss} "
                    f"and tp to {new_take_profit}"
                )
            elif existing_deal:
                monitored_deals = +1
                last_profit_percentage = float(existing_deal["last_profit_percentage"])

                if actual_profit_percentage > last_profit_percentage:
                    monitored_deals = +1

                    # Existing deal with TSL and profit increased, so move TSL
                    actual_stoploss = float(deal["stop_loss_percentage"])
                    actual_take_profit = float(deal["take_profit"])
                    profit_diff = actual_profit_percentage - last_profit_percentage

                    logger.info(
                        f"Deal {deal_id} profit change from {last_profit_percentage}% to "
                        f"{actual_profit_percentage}%. Keep on monitoring."
                    )

                    new_stoploss = round(actual_stoploss - profit_diff, 2)
                    new_take_profit = round(
                        actual_take_profit + (profit_diff * tp_increment_factor), 2
                    )

                    update_deal(thebot, deal, new_stoploss, new_take_profit)

                    db.execute(
                        f"UPDATE deals SET last_profit_percentage = {actual_profit_percentage}, "
                        f"last_stop_loss_percentage = {new_stoploss} "
                        f"WHERE dealid = {deal_id}"
                    )
                else:
                    logger.info(
                        f"Deal {deal_id} no profit increase (current: {actual_profit_percentage}%, "
                        f"last: {last_profit_percentage}%. Keep on monitoring."
                    )

        logger.info(
            f"Finished processing {len(deals)} deals for bot \"{thebot['name']}\""
        )

        # Housekeeping, clean things up and prevent endless growing database
        if current_deals:
            logger.info(f"Deleting old deals from bot {botid} except {current_deals}")
            db.execute(
                f"DELETE FROM deals WHERE botid = {botid} AND dealid NOT IN ({current_deals})"
            )

        db.commit()

        logger.info(
            f"Bot {botid} has {len(deals)} of which {monitored_deals} deal(s) require monitoring."
        )
    # No else, no deals for this bot

    return monitored_deals


def init_tsl_db():
    """Create or open database to store bot and deals data."""
    try:
        dbname = f"{program}.sqlite3"
        dbpath = f"file:{datadir}/{dbname}?mode=rw"
        dbconnection = sqlite3.connect(dbpath, uri=True)
        dbconnection.row_factory = sqlite3.Row

        logger.info(f"Database '{datadir}/{dbname}' opened successfully")

    except sqlite3.OperationalError:
        dbconnection = sqlite3.connect(f"{datadir}/{dbname}")
        dbconnection.row_factory = sqlite3.Row
        dbcursor = dbconnection.cursor()
        logger.info(f"Database '{datadir}/{dbname}' created successfully")

        dbcursor.execute(
            "CREATE TABLE deals (dealid INT Primary Key, botid INT, last_profit_percentage FLOAT, last_stop_loss_percentage FLOAT)"
        )
        logger.info("Database tables created successfully")

    return dbconnection


# Start application
program = Path(__file__).stem

# Parse and interpret options.
parser = argparse.ArgumentParser(description="Cyberjunky's 3Commas bot helper.")
parser.add_argument("-d", "--datadir", help="data directory to use", type=str)

args = parser.parse_args()
if args.datadir:
    datadir = args.datadir
else:
    datadir = os.getcwd()

# Create or load configuration file
config = load_config()
if not config:
    # Initialise temp logging
    logger = Logger(datadir, program, None, 7, False, False)
    logger.info(
        f"Created example config file '{datadir}/{program}.ini', edit it and restart the program"
    )
    sys.exit(0)
else:
    # Handle timezone
    if hasattr(time, "tzset"):
        os.environ["TZ"] = config.get(
            "settings", "timezone", fallback="Europe/Amsterdam"
        )
        time.tzset()

    # Init notification handler
    notification = NotificationHandler(
        program,
        config.getboolean("settings", "notifications"),
        config.get("settings", "notify-urls"),
    )

    # Initialise logging
    logger = Logger(
        datadir,
        program,
        notification,
        int(config.get("settings", "logrotate", fallback=7)),
        config.getboolean("settings", "debug"),
        config.getboolean("settings", "notifications"),
    )

    logger.info(f"Loaded configuration from '{datadir}/{program}.ini'")

# Initialize 3Commas API
api = init_threecommas_api(config)

# Initialize or open the database
db = init_tsl_db()
cursor = db.cursor()

# TrailingStopLoss and TakeProfit %
while True:

    config = load_config()
    logger.info(f"Reloaded configuration from '{datadir}/{program}.ini'")

    # Configuration settings
    botids = json.loads(config.get("settings", "botids"))
    check_interval = int(config.get("settings", "check-interval"))
    monitor_interval = int(config.get("settings", "monitor-interval"))
    activation_percentage = float(
        json.loads(config.get("settings", "activation-percentage"))
    )
    initial_stoploss_percentage = float(
        json.loads(config.get("settings", "initial-stoploss-percentage"))
    )
    tp_increment_factor = float(
        json.loads(config.get("settings", "tp-increment-factor"))
    )

    deals_to_monitor = 0

    # Walk through all bots configured
    for bot in botids:
        boterror, botdata = api.request(
            entity="bots",
            action="show",
            action_id=str(bot),
        )
        if botdata:
            deals_to_monitor += process_deals(botdata)
        else:
            if boterror and "msg" in boterror:
                logger.error("Error occurred updating bots: %s" % boterror["msg"])
            else:
                logger.error("Error occurred updating bots")

    timeint = check_interval if deals_to_monitor == 0 else monitor_interval
    if not wait_time_interval(logger, notification, timeint):
        break

