"""Welcome message for a user's first DM to the bot.

The first time someone writes to the bot by DM (a command or just "hi"),
it greets them, says what it's for and points them to the main commands
and !help — then handles their message as usual.
"""
import logging

logger = logging.getLogger(__name__)


def build_welcome_message(user_name: str, tournament_names: list[str]) -> str:
    tournaments = " and ".join(f"**{name}**" for name in tournament_names) or "the tournaments"
    return (
        f"👋 Hi {user_name}, welcome! I'm the bot for {tournaments}.\n"
        "I can show you your matches, results and standings, right here by DM. The main commands:\n"
        "• `!m <player>` — your matches this week, with results\n"
        "• `!sum <player>` — your characters: division, position and win rate\n"
        "• `!sum_ext <player>` — every result of each character, week by week\n"
        "• `!sub <player>` — get your matches by DM every week (`!unsub` to stop)\n"
        "Use your player name as it appears in the tournament sheet, e.g. `!m Scorium`. "
        "Type `!help` to see everything I can do."
    )


async def welcome_if_first_dm(message, tournament_names: list[str]) -> bool:
    """Send the welcome if this DM is the user's first contact with the bot.
    Never lets a problem here stop the user's command. Returns True if sent."""
    from scheduler import claim_first_contact
    try:
        if not claim_first_contact(message.author.id):
            return False
        await message.channel.send(build_welcome_message(message.author.display_name, tournament_names))
        logger.info(f"Welcomed {message.author} ({message.author.id}) on their first DM")
        return True
    except Exception as e:
        logger.warning(f"could not send the first-DM welcome to {message.author.id}: {e}")
        return False
