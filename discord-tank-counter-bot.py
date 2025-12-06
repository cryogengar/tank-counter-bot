"""
Discord "tank talk" counter bot with live timer + milestone messages.

- /tank post → create the live counter message
- /tank reset → reset timer to now
- /tank start <ISO time> → manually set start time (e.g., 2025-10-30T12:00:00)
- /tank template → set your custom text (must include {days})
- auto updates every 30s
- sends a milestone message every 24h (rotating mood)
"""

import os
import json
import re
import asyncio
import random
from datetime import datetime, UTC
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

STATE_FILE = Path(os.getenv("STATE_FILE_PATH", "state.json"))
LOCAL_TZ = ZoneInfo(os.getenv("TIMEZONE", "America/Vancouver"))

SCORES_FILE = Path(os.getenv("SCORES_FILE_PATH", "scores.json"))
BLAME_TRIGGER_TEXT = "Who talked about tanks and reset the timer today?"
BLAME_EMOJI = "➕"

DEFAULT_TEMPLATE = "🧯⚠️ {days}:{hours}:{minutes}:{seconds} WITHOUT TANK TALK ⚠️🧯"


class GuildState:
    def __init__(self, guild_id: int, data: dict | None = None):
        data = data or {}
        self.guild_id = guild_id
        self.start_time: str | None = data.get("start_time")  # ISO string
        self.message_id: int | None = data.get("message_id")
        self.channel_id: int | None = data.get("channel_id")
        self.template: str = data.get("template", DEFAULT_TEMPLATE)
        self.last_announced_day: int = data.get("last_announced_day", -1)

    def to_dict(self):
        return {
            "start_time": self.start_time,
            "message_id": self.message_id,
            "channel_id": self.channel_id,
            "template": self.template,
            "last_announced_day": self.last_announced_day,
        }


class State:
    def __init__(self):
        self.by_guild: dict[int, GuildState] = {}
        self.load()

    def load(self):
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text())
            for gid, val in data.items():
                self.by_guild[int(gid)] = GuildState(int(gid), val)

    def save(self):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps({str(k): v.to_dict() for k, v in self.by_guild.items()}, indent=2)
        )

    def for_guild(self, gid: int) -> GuildState:
        if gid not in self.by_guild:
            self.by_guild[gid] = GuildState(gid)
        return self.by_guild[gid]


state = State()


intents = discord.Intents.default()
intents.reactions = True
intents.members = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

def _parse_iso_smart(value: str) -> datetime:
    """Parse a forgiving ISO-like local time string and return UTC datetime."""
    # Log raw value once in case of future debugging
    print("parsing time string:", repr(value))

    # Trim spaces
    value = value.strip()

    # Strip surrounding quotes if the user included them
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        value = value[1:-1].strip()

    # Normalise unicode dashes to plain hyphens
    value = value.replace("–", "-").replace("—", "-")

    # Accept: YYYY-MM-DDTHH:MM or YYYY-MM-DDTHH:MM:SS
    # Also accept a space instead of "T"
    m = re.fullmatch(
        r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?",
        value,
    )
    if not m:
        raise ValueError(f"Bad time format: {value!r}")

    year, month, day, hour, minute, second = m.groups()
    year = int(year)
    month = int(month)
    day = int(day)
    hour = int(hour)
    minute = int(minute)
    second = int(second) if second is not None else 0

    # Treat as local time, then convert to UTC
    dt_local = datetime(year, month, day, hour, minute, second, tzinfo=LOCAL_TZ)
    return dt_local.astimezone(UTC)

def _elapsed(gs: GuildState):
    """Return days, hours, minutes, seconds since start_time."""
    if not gs.start_time:
        return 0, 0, 0, 0
    now = datetime.now(UTC)
    start = datetime.fromisoformat(gs.start_time)
    # Back-compat: if the saved time is naive, assume UTC
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    delta = now - start
    days = delta.days
    hours, rem = divmod(delta.seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    return days, hours, minutes, seconds

def load_scores() -> dict[str, int]:
    if SCORES_FILE.exists():
        try:
            return json.loads(SCORES_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_scores(scores: dict[str, int]) -> None:
    SCORES_FILE.write_text(json.dumps(scores, indent=2))

async def _render_text(gs: GuildState) -> str:
    """Render the main message with zero-padded timer aesthetic."""
    days, hours, minutes, seconds = _elapsed(gs)

    tpl = gs.template
    days_str = f"{days:02}"
    hours_str = f"{hours:02}"
    minutes_str = f"{minutes:02}"
    seconds_str = f"{seconds:02}"

    base = "day" if days == 1 else "days"
    if "DAYS" in tpl:
        base = base.upper()
    elif "Days" in tpl:
        base = base.capitalize()

    return tpl.format(
        days=days_str,
        hours=hours_str,
        minutes=minutes_str,
        seconds=seconds_str,
        day_word=base.lower(),
        DAY_WORD=base.upper(),
        Day_Word=base.capitalize(),
    )

async def _update_display(guild: discord.Guild, gs: GuildState):
    """Update the bound message."""
    if not (gs.channel_id and gs.message_id):
        return
    channel = guild.get_channel(gs.channel_id) or await client.fetch_channel(gs.channel_id)
    try:
        msg = await channel.fetch_message(gs.message_id)
        await msg.edit(content=await _render_text(gs))
    except discord.NotFound:
        gs.message_id = None
        state.save()

async def _send_milestone_message(guild: discord.Guild, gs: GuildState, days: int):
    """send a milestone message every 24h with your chaotic double-emoji moods (all lowercase)."""
    if not gs.channel_id:
        return
    channel = guild.get_channel(gs.channel_id) or await client.fetch_channel(gs.channel_id)

    # correct singular vs plural wording
    day_word = "day" if days == 1 else "days"

    moods = [
        f"💥💥 {days} days 💀💀 without tank talk 🤯🤯 unbelievable",
        f"still 🧘‍♀️🧘‍♀️ no tanks 💀💀 after {days} 😮‍💨😮‍💨 days 🫠🫠 peace is suspicious",
        f"🫡🫡 we’ve survived 🌫️🌫️ {days} days 💣💣 without chaos 🐍🐍 why though",
        f"🐢🐢 {days} days 🫡🫡 tank-free 😵‍💫😵‍💫 weirdly peaceful ☁️☁️ unsettling",
        f"🧘‍♀️🧘‍♀️ {days} days 💥💥 no tanks 💀💀 everyone too calm 😪😪",
        f"🍷🍷 {days} days 🫡🫡 pretending ☠️☠️ we’re normal 🐢🐢",
        f"💣💣 {days} days 💀💀 no tank sighting ⚡⚡ spirits high 🫠🫠",
    ]

    # Randomly select a mood
    idx = random.randint(0, (len(moods) - 1))
    await channel.send(moods[idx])

    gs.last_announced_day = days
    state.save()

async def _background_updater():
    """Refresh all timers and post daily milestones."""
    await client.wait_until_ready()
    while not client.is_closed():
        for guild in client.guilds:
            gs = state.for_guild(guild.id)
            try:
                await _update_display(guild, gs)
                days, _, _, _ = _elapsed(gs)
                if gs.start_time and days > gs.last_announced_day:
                    await _send_milestone_message(guild, gs, days)
            except Exception:
                pass
        await asyncio.sleep(30)

@tree.command(name="ping", description="Check if the bot is alive")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("pong 🕷️", ephemeral=True)

class Tank(app_commands.Group):
    def __init__(self):
        super().__init__(name="tank", description="Tank talk counter controls")

tank = Tank()
tree.add_command(tank)

@tank.command(name="post", description="Post the live counter here")
async def post(inter: discord.Interaction):
    assert inter.guild
    gs = state.for_guild(inter.guild.id)
    if not gs.start_time:
        gs.start_time = datetime.now(UTC).isoformat()
    text = await _render_text(gs)
    await inter.response.send_message(text)
    msg = await inter.original_response()
    gs.channel_id = msg.channel.id
    gs.message_id = msg.id
    state.save()

@tank.command(name="reset", description="Reset timer to now")
async def reset(inter: discord.Interaction):
    assert inter.guild
    gs = state.for_guild(inter.guild.id)
    gs.start_time = datetime.now(UTC).isoformat()
    gs.last_announced_day = -1
    state.save()
    await _update_display(inter.guild, gs)
    
    await inter.response.send_message("Tank timer reset!", ephemeral=False)

    await inter.followup.send(
        f"{BLAME_TRIGGER_TEXT}\n\nReact with {BLAME_EMOJI} to claim it and add 1 to your tank score."
    )

@tank.command(name="start", description="Set a custom start time (ISO 8601)")
@app_commands.describe(time="Format: YYYY-MM-DDTHH:MM:SS (local time assumed, e.g., 2025-11-03T05:37:00)")
async def start(inter: discord.Interaction, time: str):
    assert inter.guild
    gs = state.for_guild(inter.guild.id)
    try:
        print("raw time string:", repr(time))  # 👈 DEBUG — log exactly what Discord sent

        start_dt = _parse_iso_smart(time)

        print("parsed to UTC:", start_dt)  # 👈 DEBUG — confirm correct conversion

        gs.start_time = start_dt.isoformat()
        gs.last_announced_day = -1
        state.save()
        await _update_display(inter.guild, gs)
        await inter.response.send_message(
            f"Start time set to `{gs.start_time}` (UTC).", ephemeral=True
        )
    except Exception as e:
        print("start() parse error:", e)  # 👈 DEBUG — see what went wrong
        await inter.response.send_message(
            "Invalid time format. Use `YYYY-MM-DDTHH:MM:SS` (your local time).",
            ephemeral=True
        )

@tank.command(name="template", description="Set text template (must include {days})")
@app_commands.describe(text="Example: 🧯⚠️ {days}:{hours}:{minutes}:{seconds} WITHOUT TANK TALK ⚠️🧯")
async def template(inter: discord.Interaction, text: str):
    assert inter.guild
    gs = state.for_guild(inter.guild.id)
    if "{days}" not in text:
        await inter.response.send_message("Template must include `{days}`.", ephemeral=True)
        return
    gs.template = text
    state.save()
    await _update_display(inter.guild, gs)
    await inter.response.send_message("Template updated.", ephemeral=True)

@tank.command(name="status", description="show how long it’s been since last tank talk")
async def status(inter: discord.Interaction):
    assert inter.guild
    gs = state.for_guild(inter.guild.id)
    days, hours, minutes, seconds = _elapsed(gs)
    msg = (
        f"it's been {days} day{'s' if days != 1 else ''}, "
        f"{hours} hour{'s' if hours != 1 else ''}, "
        f"{minutes} minute{'s' if minutes != 1 else ''}, "
        f"and {seconds} second{'s' if seconds != 1 else ''} "
        "since the last tank lecture. phenomenal restraint, everyone 😀"
    )
    await inter.response.send_message(msg)

@tank.command(name="scores", description="show the tank talk leaderboard")
async def scores(inter: discord.Interaction):
    scores = load_scores()
    if not scores:
        await inter.response.send_message(
            "no recorded tank sins yet. a peaceful land… for now.",
            ephemeral=False,
        )
        return

    # sort by score (high → low) and keep only top 3
    sorted_scores = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    top_three = sorted_scores[:3]

    medals = ["🥇", "🥈", "🥉"]
    lines: list[str] = []

    assert inter.guild is not None

    for idx, (user_id_str, count) in enumerate(top_three):
        user_id = int(user_id_str)

        member = inter.guild.get_member(user_id)
        if member is None:
            try:
                member = await inter.guild.fetch_member(user_id)
            except Exception:
                member = None

        name = (member.display_name.lower() if member else f"<@{user_id}>")
        medal = medals[idx]
        sin_word = "sin" if count == 1 else "sins"

        lines.append(f"{medal} {name} // **{count}** {sin_word}")

    description = "\n".join(lines)

    embed = discord.Embed(
        title="tank leaderboard ⚔️",
        description=description,
        colour=discord.Colour.dark_gray(),
    )

    await inter.response.send_message(embed=embed)

@tank.command(name="adjust_score", description="adjust someone’s tank score")
@app_commands.describe(
    member="who is guilty?",
    amount="add or subtract from their score (can be negative)"
)
async def adjust_score(inter: discord.Interaction, member: discord.Member, amount: int):
    # owner-only guard
    app_info = await client.application_info()
    if inter.user.id != app_info.owner.id:
        await inter.response.send_message(
            "only the bot owner can use this command.",
            ephemeral=True,
        )
        return

    scores = load_scores()
    user_id_str = str(member.id)

    old_value = scores.get(user_id_str, 0)
    new_value = old_value + amount
    if new_value < 0:
        new_value = 0

    scores[user_id_str] = new_value
    save_scores(scores)

    sign = "+" if amount >= 0 else ""
    display_name = member.display_name.lower()

    await inter.response.send_message(
        f"✅ adjusted {display_name}'s tank score: **{old_value}** → **{new_value}** "
        f"(delta {sign}{amount}).",
        ephemeral=False,
    )

@client.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    # ignore bot's own reactions
    if payload.user_id == client.user.id:
        return

    # only care about the specific emoji
    if str(payload.emoji) != BLAME_EMOJI:
        return

    # fetch the message
    channel = client.get_channel(payload.channel_id)
    if channel is None:
        channel = await client.fetch_channel(payload.channel_id)

    try:
        message = await channel.fetch_message(payload.message_id)
    except discord.NotFound:
        return

    # ensure it is one of our "who sinned?" messages
    if message.author.id != client.user.id:
        return
    if BLAME_TRIGGER_TEXT not in message.content:
        return

    # update scores
    scores = load_scores()
    user_id_str = str(payload.user_id)
    scores[user_id_str] = scores.get(user_id_str, 0) + 1
    save_scores(scores)

    # optional: edit message to show this person's new total
    guild = message.guild
    display_name = f"<@{payload.user_id}>"
    if guild is not None:
        member = guild.get_member(payload.user_id) or await guild.fetch_member(payload.user_id)
        if member is not None:
            display_name = member.display_name

    new_total = scores[user_id_str]
    suffix = f"\n\n{display_name} has now reset the tank timer **{new_total}** time(s)."
    if suffix not in message.content:
        await message.edit(content=message.content + suffix)

@client.event
async def on_ready():
    print(f"Logged in as {client.user} (ID: {client.user.id})")

    # 1) wipe ALL GLOBAL commands on Discord so they don't duplicate
    try:
        tree.clear_commands(guild=None)   # clear local global set
        await tree.sync()                 # pushes empty -> deletes globals on API
        print("🧹 cleared global commands")
    except Exception as e:
        print("⚠️ global clear error:", e)

    # 2) register ONLY guild-scoped commands (one copy per server)
    for g in client.guilds:
        try:
            # ensure no old guild set lingers, then attach /tank for this guild
            tree.clear_commands(guild=g)
            tree.add_command(tank, guild=g)   # add group for this guild only
            synced = await tree.sync(guild=g)
            print(f"✅ synced {len(synced)} guild cmds to {g.name} ({g.id})")
        except Exception as e:
            print(f"⚠️ guild sync error for {g.name}:", e)

    # 3) start the live updater
    client.loop.create_task(_background_updater())

@tree.command(name="resync", description="Force refresh all commands (owner only)")
async def resync(inter: discord.Interaction):
    app_info = await client.application_info()
    if inter.user.id != app_info.owner.id:
        await inter.response.send_message("Only the bot owner can use this.", ephemeral=True)
        return

    try:
        # wipe globals (stay guild-only)
        tree.clear_commands(guild=None)
        await tree.sync()

        # re-sync for each guild
        for g in client.guilds:
            tree.clear_commands(guild=g)
            tree.add_command(tank, guild=g)
            await tree.sync(guild=g)

        await inter.response.send_message("✅ Commands fully re-synced!", ephemeral=True)
        print("🔁 manual resync (guild-only) triggered by owner")
    except Exception as e:
        await inter.response.send_message(f"⚠️ Resync failed: {e}", ephemeral=True)
        print("resync error:", e)

if __name__ == "__main__":
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("Set DISCORD_BOT_TOKEN env var.")
    client.run(token)
