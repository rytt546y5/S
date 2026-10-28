from __future__ import annotations

import logging
import asyncio
import os

import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv

from services.sms_api_base import Config
from services.sms_provider import SMSProvider
from storage import initialize

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logging.getLogger('discord.http').setLevel(logging.WARNING)


class Application(commands.Bot):
    def __init__(self, config):
        intents = discord.Intents.default()
        super().__init__(command_prefix='!', intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.config = config
        self.http_session = None
        self.sms = None
        self.panel_refresh_task = None

    async def setup_hook(self):
        initialize()
        self.http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25))
        self.sms = SMSProvider(self.http_session, self.config)
        await self.add_cog(await __import__('cogs.paypay', fromlist=['PayPay']).make(self))
        await self.add_cog(await __import__('cogs.sales', fromlist=['Sales']).make(self))
        self.get_cog('Sales').restore_sales_panels()
        self.get_cog('Sales').restore_replacement_views()
        self.panel_refresh_task = asyncio.create_task(self.refresh_saved_panels())
        await self.tree.sync()
        logging.info('Commands synced')
        # The reference vending bot signs in when a receipt is about to be accepted.
        # Startup must not auto-refresh or change the login session.
        from storage import load_login
        if load_login(self.config.owner_id):
            logging.info('PayPay account credentials loaded (sign-in occurs at receipt time)')

    async def refresh_saved_panels(self):
        await self.wait_until_ready()
        try:
            from cogs.sales import current_price
            await self.get_cog('Sales').refresh_panels(current_price())
        except Exception:
            logging.exception('Saved panel refresh failed')

    async def close(self):
        try:
            if self.panel_refresh_task:
                self.panel_refresh_task.cancel()
            await super().close()
        finally:
            if self.http_session:
                await self.http_session.close()


if __name__ == '__main__':
    bot = Application(Config.from_env())
    bot.run(bot.config.bot_token, log_handler=None)
