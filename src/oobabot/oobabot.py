#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#

import asyncio
import signal
import sys

from oobabot.discord_bot import DiscordBot
from oobabot.fancy_logging import init_logging
from oobabot.ooba_client import OobaClient
from oobabot.settings import Settings


class LocalREPL:
    # local REPL for testing
    def __init__(self, ooba_client):
        self.ooba_client = ooba_client

    def show_prompt(self) -> None:
        print("\n>>> ", end='', flush=True)

    async def start(self) -> None:
        self.show_prompt()
        for user_prompt in sys.stdin:
            async for token in self.ooba_client.request_by_token(
                user_prompt
            ):
                if token:
                    print(token, end='', flush=True)
                else:
                    # end of response
                    print('')
            self.show_prompt()


def main():

    logger = init_logging()
    settings = Settings()

    bot = None

    def sigint_handler(_signum, _frame):
        logger.info('Received SIGINT, exiting...')
        if bot:
            bot.log_stats()
        exit(1)

    signal.signal(signal.SIGINT, sigint_handler)

    ooba_client = OobaClient(settings.base_url)

    logger.debug(f'Oobabooga base URL: {settings.base_url}')
    connect_error_msg = asyncio.run(ooba_client.try_connect())
    if connect_error_msg:
        logger.error(
            f'Could not connect to ooba server: [{ooba_client.api_url}]')
        logger.error('Please check the URL and try again.')
        logger.error(f'Reason: {connect_error_msg}')
        sys.exit(1)

    logger.info('Connected to Oobabooga!')

    if settings.local_repl:
        logger.debug('Using local REPL, not connecting to Discord')
        coroutine = LocalREPL(ooba_client).start()
    else:
        logger.debug('Connecting to Discord... ')
        bot = DiscordBot(
            ooba_client,
            ai_name=settings.ai_name,
            ai_persona=settings.persona,
            wakewords=settings.wakewords)
        coroutine = bot.start(settings.DISCORD_TOKEN)

    asyncio.run(coroutine)