import time
import sys

import mysql.connector
import requests
from klinklang.core.config import read_config

from klinklang import logger
from klinklang.core.db_return import db_return

config = read_config()
database_client = config.database.client()
account_table = database_client["accounts"]

# allows you to set whether you want accounts available for immediate dragonite
# set use_immediately: true/false in config.yml - default: false

# added two fields to the account generator: exported: bool and created: unix timestamp as int
# exported: lets us not have to fetch all klinklang accounts on every export
# created: eliminates the need for stats_collector and the generated_accounts table

# must update account_generator to use this exporter
# accounts in klinklang accounts table (mongodb) will be ignored if they do not have the exported field

# if you choose to use this i recommend this order of events:
# 1. stop account_generator
# 2. wait until your account_exporter runs one more time, or manually add the unexported accounts to dragonite
# 3. stop account_exporter
# 4. update and start account_generator
# 5. update and start account_exporter


def get_klinklang_accounts():
    # get only accounts that haven't been exported rather than getting all accounts every time
    # accounts that don't have an exported field are ignored
    return db_return(account_table.find({ "exported": False }))


def export_accounts():
    klinklang_accounts = get_klinklang_accounts()

    if not klinklang_accounts:
        logger.info("No accounts to export.")
        return

    # result stores the number of accounts exported and number of sql insert errors (ex: duplicate accounts)
    result = insert_to_db(klinklang_accounts)

    # sends a discord message with these stats since last export: accounts created, accounts exported, export errors
    if config.export.discord is True:
        send_discord_message(result["exported_count"], result["warning_count"])


def insert_to_db(klinklang_accounts):
    logger.info(f"Starting export of {len(klinklang_accounts)} accounts to {config.export.destination}.")
    
    # using insert ignore allows us to not be concerned with duplicate records
    # therefore we can skip pulling all account usernames from mariadb to compare with klinklang_accounts
    # if an attempt is made to insert an account that already exists, a warning is logged
    # example: [WARNING][2025-02-13T23:22:01.626765+00:00] [('Warning', 1062, "Duplicate entry 'x9ifisukufurabar' for key 'PRIMARY'")]
    # any other warnings will also be logged, but theoretically duplicate would be the only error we could encounter here.

    if config.export.use_immediately:
        insert_query = (f"INSERT IGNORE INTO {config.export.table_name} (username, password, last_released) VALUES (%s, %s, %s)")
    else:
        insert_query = (f"INSERT IGNORE INTO {config.export.table_name} (username, password) VALUES (%s, %s)")
    
    db_connection = mysql.connector.connect(
        host=config.export.host,
        user=config.export.username,
        password=config.export.password,
        database=config.export.db_name,
        port=config.export.port,
    )
    db_connection.get_warnings = True
    
    cursor = db_connection.cursor()
    exported_count = 0
    warning_count = 0
    for account in klinklang_accounts:
        if config.export.use_immediately:
            cursor.execute(insert_query, (account["username"], account["password"], 1))
        else:
            cursor.execute(insert_query, (account["username"], account["password"]))

        if cursor.fetchwarnings() is None:
            exported_count += 1
        else:
            warning_count += 1
            logger.warning(f"{cursor.fetchwarnings()}")
    
    db_connection.commit()
    cursor.close()
    db_connection.close()

    # sets ALL accounts in klinklang_accounts exported: True including any that have had an error
    # this is done after the db commit to ensure accounts aren't accidentally marked as exported
    # recommended to check exporter logs for warnings or enable the discord message so you're able to verify the duplicate accounts

    for account in klinklang_accounts:
        account_table.update_one({ "username": account["username"] }, { "$set": { "exported": True } })

    logger.info(f"Exported {exported_count} accounts to {config.export.destination}")
    return {"exported_count": exported_count, "warning_count": warning_count}


def get_accounts_generated_since(seconds_ago: int):
    seconds_ago_query = { "created": { "$gt": int(time.time()) - seconds_ago } }
    return len(db_return(account_table.find(seconds_ago_query)))


def send_discord_message(exported_count: int, warning_count: int):
    generated_count = get_accounts_generated_since(config.export.rate)

    if generated_count == 0:
        color = 0xFF0000
    elif generated_count >= 100:
        color = 0x00FF00
    else:
        color = 0xFFFF00

    if warning_count == 0:
        thumbnail = "success"
    else:
        thumbnail = "warning"
    
    data = {
        "content": "",
        "username": "Klinklang",
        "avatar_url": "https://raw.githubusercontent.com/dmallory89/PkmnShuffleMap/poracle/UICONS/pokemon/601.png",
        "embeds": [
            {
                "title": "Klinklang Status Report",
                "color": color,
                "thumbnail": {
                    "url": f"https://raw.githubusercontent.com/dmallory89/PkmnShuffleMap/poracle/UICONS/misc/status_{thumbnail}.png"
                },
                "description": f"📝 Created: {generated_count}\n📤 Exported: {exported_count}\n⚠️ Errors: {warning_count}\n\n⏰ <t:{int(time.time())}>"
            }
        ],
    }

    headers = {"Content-Type": "application/json"}
    response = requests.post(config.export.webhook, json=data, headers=headers)
    logger.info(f"Discord notification status: {response.status_code}")


if __name__ == "__main__":
    while config.export is None:
        logger.warning("No export config found.")
        logger.info("Ensure config.yml has an 'export:' section, not 'exporter:', then restart the container")
        time.sleep(60)
        
    logger.info(f"Loaded config: {config.export}")

    # instead of running every hour, on the hour, specify the rate (seconds) in config.yml
    # the default is one hour (3600s) and this value is used as the export rate AND discord message send rate (if enabled)
    while True:
        export_accounts()
        logger.info(f"Sleeping for {config.export.rate}s...")
        time.sleep(config.export.rate)
