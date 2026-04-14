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
ADMIN_USER_IDS: list[int] = config.get('admin_user_ids', [])

# Token from environment variable (never hardcode or use token.txt in production)
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Missing DISCORD_BOT_TOKEN environment variable.")

# Setup bot
intents = discord.Intents.default()
intents.message_content = True
# Por esto:
async def get_prefix(bot, message):
    # Acepta "!comando" o "@bot !comando" o "@bot comando"
    mention_prefixes = [f"<@{bot.user.id}> ", f"<@!{bot.user.id}> "]
    for mention in mention_prefixes:
        if message.content.startswith(mention):
            remainder = message.content[len(mention):]
            # Si tras la mención viene "!comando", quitar el "!"
            if remainder.startswith(COMMAND_PREFIX):
                return mention + COMMAND_PREFIX
            # Si viene "comando" sin "!", añadir prefijo virtual
            return mention
    return COMMAND_PREFIX

bot = commands.Bot(command_prefix=get_prefix, intents=intents)
bot.remove_command('help')

# Expose admin list on the bot object so cogs can access it
bot.admin_user_ids = ADMIN_USER_IDS


# ------------------------------------------------------------------
# Custom admin check — used by all admin commands instead of
# has_permissions(administrator=True).
# Raises commands.CheckFailure so on_command_error handles it.
# ------------------------------------------------------------------
def is_bot_admin():
    async def predicate(ctx: commands.Context) -> bool:
        if ctx.author.id in ctx.bot.admin_user_ids:
            return True
        raise commands.CheckFailure(
            "❌ You don't have permission to use this command. "
            "Only authorised bot admins can do that."
        )
    return commands.check(predicate)


# Attach to bot so cogs can import it via bot.is_bot_admin
bot.is_bot_admin = is_bot_admin


@bot.event
async def on_ready():
    logger.info(f"Bot online as {bot.user} (ID: {bot.user.id})")


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    await bot.process_commands(message)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await ctx.send(str(error) or "❌ You don't have permission to use this command.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(
            f"❌ Missing argument: `{error.param.name}`. "
            f"Use `@{ctx.bot.user.name} !help {ctx.command}` for usage."
        )
    elif isinstance(error, commands.CommandNotFound):
        pass
    else:
        logger.error(f"Unhandled error in command '{ctx.command}': {error}", exc_info=error)


async def main():
    async with bot:
        tournament_cog = await setup_commands(bot, TOURNAMENTS, MAPPING_SHEET_URL, DEFAULT_WEEK)
        await setup_schedule(bot)
        await bot.add_cog(SchedulerCog(bot, tournament_cog))
        await bot.start(BOT_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
