import discord
from discord.ext import commands
import json
import os
import logging
import asyncio
from commands import setup as setup_commands
from schedule_commands import setup_schedule
from scheduler import SchedulerCog

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

# Load configuration
with open('config.json', 'r') as f:
    config = json.load(f)

TOURNAMENTS = config['tournaments']
MAPPING_SHEET_URL = config['mapping_sheet_url']
DEFAULT_WEEK = config.get('default_week', 4)
COMMAND_PREFIX = config.get('command_prefix', '!')

# Token from environment variable (never hardcode or use token.txt in production)
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Missing DISCORD_BOT_TOKEN environment variable.")

# Setup bot
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)
bot.remove_command('help')


@bot.event
async def on_ready():
    logger.info(f"Bot online as {bot.user} (ID: {bot.user.id})")


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    logger.debug(f"Message from {message.author}: {message.content}")
    await bot.process_commands(message)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You don't have permission to use this command.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument: `{error.param.name}`. Use `!help {ctx.command}` for usage.")
    elif isinstance(error, commands.CommandNotFound):
        pass  # Silently ignore unknown commands
    else:
        logger.error(f"Unhandled error in command '{ctx.command}': {error}", exc_info=error)


async def main():
    async with bot:
        # Load tournament commands cog first (scheduler needs a reference to it)
        tournament_cog = await setup_commands(bot, TOURNAMENTS, MAPPING_SHEET_URL, DEFAULT_WEEK)

        # Load schedule management commands
        await setup_schedule(bot)

        # Start the scheduler loop, passing a reference to the tournament cog
        await bot.add_cog(SchedulerCog(bot, tournament_cog))

        await bot.start(BOT_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
