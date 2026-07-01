import discord
from discord.ext import commands
import json
import os
import logging
import pathlib
import asyncio
from commands import setup as setup_commands
from schedule_commands import setup_schedule
from scheduler import SchedulerCog
from channel_context import build_channel_index

# Logging setup
logging.basicConfig(
    level=logging.DEBUG,
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
CHANNEL_INDEX = build_channel_index(config.get('guild_channels', []))

# Token from environment variable (never hardcode or use token.txt in production)
def _load_token() -> str:
    """Return Discord bot token from env var or fallback file."""
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if token:
        return token

    # Fallback: read from var/env.txt (relative to the bot's directory)
    env_file = pathlib.Path(__file__).resolve().parent / "var" / "env.txt"
    if env_file.exists():
        token = env_file.read_text(encoding="utf-8").strip()
        if token:
            logging.getLogger(__name__).info("Using token from var/env.txt")
            return token

    raise RuntimeError(
        "Missing DISCORD_BOT_TOKEN environment variable.\n"
        "You can also place the token in 'var/env.txt'."
    )

BOT_TOKEN = _load_token()

# Setup bot
intents = discord.Intents.default()
intents.message_content = True

def get_prefix(bot, message):
    # 1. Determinar el prefijo base (personalizado por guild o global)
    base_prefix = COMMAND_PREFIX
    if message.guild is not None:
        guild_index = CHANNEL_INDEX.get(message.guild.id, {})
        base_prefix = guild_index.get("_prefix", COMMAND_PREFIX)

    # 2. Construir los posibles formatos de mención
    mention_prefixes = [f"<@{bot.user.id}> ", f"<@!{bot.user.id}> "]

    # 3. Si el mensaje empieza con una mención, extraer el texto restante
    for mention in mention_prefixes:
        if message.content.startswith(mention):
            remainder = message.content[len(mention):]
            # Si después de la mención viene el prefijo base, lo incluimos
            if remainder.startswith(base_prefix):
                return [mention + base_prefix]
            # Si solo hay mención, la devolvemos tal cual
            return [mention]

    # 4. Sin mención, devolvemos solo el prefijo base
    return [base_prefix]

bot = commands.Bot(command_prefix=get_prefix, intents=intents)
bot.remove_command('help')

bot.admin_user_ids = ADMIN_USER_IDS
bot.channel_index  = CHANNEL_INDEX


def is_bot_admin():
    async def predicate(ctx: commands.Context) -> bool:
        if ctx.author.id in ctx.bot.admin_user_ids:
            return True
        raise commands.CheckFailure(
            "❌ You don't have permission to use this command. "
            "Only authorised bot admins can do that."
        )
    return commands.check(predicate)

bot.is_bot_admin = is_bot_admin


@bot.event
async def on_ready():
    logger.info(f"Bot online as {bot.user} (ID: {bot.user.id})")
    logger.info(f"Registered commands: {[c.name for c in bot.commands]}")



@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    print(f"Content: '{message.content}' | author: {message.author} | channel: {message.channel}")
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
        await ctx.send(f"❌Command unknown. Use `{ctx.prefix}help` to see available commands.")
    else:
        logger.error(f"Unhandled error in command '{ctx.command}': {error}", exc_info=error)


async def main():
    async with bot:
        tournament_cog = await setup_commands(
            bot, TOURNAMENTS, MAPPING_SHEET_URL, DEFAULT_WEEK
        )
        await setup_schedule(bot)
        await bot.add_cog(SchedulerCog(bot, tournament_cog))
        await bot.start(BOT_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
