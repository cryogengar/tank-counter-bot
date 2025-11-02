"""
Discord "tank talk" counter bot with a live timer.

Features (per guild):
- /tank post → bot posts and binds a counter message
- /tank reset → resets timer to 0
- /tank template → set your custom message template
- /tank bind → rebind to an existing message

The timer updates automatically every 30 seconds and displays
{days}:{hours}:{minutes}:{seconds} with zero-padded formatting.

Example template:
🧯⚠️ {days}:{hours}:{minutes}:{seconds} WITHOUT TANK TALK ⚠️🧯
"""

import os
import json
import re
import asyncio
from pathlib import Path
from datetime import datetime, timedelta

import discord
from discord import app_commands

STATE_FILE = Path("state.json")

DEFAULT_TEMPLATE = "🧯⚠️ {days}:{hours}:{minutes}:{seconds} WITHOUT TANK TALK ⚠️🧯"


class GuildState:
    def __init__(self, guild_id: int, data: dict | None = None):
        self.guild_id = guild_id
        data = data or {}
        self.start_time: str | None = data.get("start_time")  # ISO string
        self.message_id: int | None = data.get("message_id")
        self.channel_id: int | None = data.get("channel_id")
        self.template: str = data.get("template", DEFAULT_TEMPLATE)

    def to_dict(self) -> dict:
        return {
            "start_time": self.start_time,
            "message_id": self.message_id,
            "channel_id": self.channel_id,
            "template": self.template,
        }


class State:
    def __init__(self):
        self._by_guild: dict[int, GuildState] = {}
        self.load()

    def load(self):
        if STATE_FILE.exists():
            raw = json.loads(STATE_FILE.read_text())
            for gid_str, data in raw.items():
                self._by_guild[int(gid_str)] = GuildState(int(gid_str), data)
        else:
            self._by_guild = {}

    def save(self):
        STATE_FILE.write_text(
            json.dumps({str(g): s.to_dict() for g, s in self._by_guild.items()}, indent=2)
        )

    def for_guild(self, gid: int) -> GuildState:
        if gid not in self._by_guild:
            self._by_guild[gid] = GuildState(gid)
        return self._by_guild[gid]


state = State()

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


def _elapsed(gs: GuildState):
    """Return days, hours, minutes, seconds since start_time."""
    if not gs.start_time:
        return 0, 0, 0, 0
    delta = datetime.utcnow() - datetime.fromisoformat(gs.start_time)
    days = delta.days
    hours, rem = divmod(delta.seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    return days, hours, minutes, seconds


async def _render_text(gs: GuildState) -> str:
    """Render the display text with padded time and case matching."""
    days, hours, minutes, seconds = _elapsed(gs)

    # zero-pad each unit for a clean timer aesthetic
    days_str = f"{days:02}"
    hours_str = f"{hours:02}"
    minutes_str = f"{minutes:02}"
    seconds_str = f"{seconds:02}"

    tpl = gs.template

    # choose singular/plural for 'day' if used
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
    """Update the bound message to show the latest timer text."""
    if not (gs.channel_id and gs.message_id):
        return
    channel = guild.get_channel(gs.channel_id) or await client.fetch_channel(gs.channel_id)
    try:
        msg = await channel.fetch_message(gs.message_id)
        await msg.edit(content=await _render_text(gs))
    except discord.NotFound:
        gs.message_id = None
        state.save()


async def _background_updater():
    """Periodically refresh all guilds every 30s."""
    await client.wait_until_ready()
    while not client.is_closed():
        for guild in client.guilds:
            gs = state.for_guild(guild.id)
            try:
                await _update_display(guild, gs)
            except Exception:
                pass
        await asyncio.sleep(30)


@tree.command(name="ping", description="Check if the bot is alive")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("pong", ephemeral=True)


class Tank(app_commands.Group):
    def __init__(self):
        super().__init__(name="tank", description="Tank talk counter controls")


tank = Tank()
tree.add_command(tank)


@tank.command(name="post", description="Post the live timer here")
async def post(interaction: discord.Interaction):
    assert interaction.guild
    gs = state.for_guild(interaction.guild.id)
    if not gs.start_time:
        gs.start_time = datetime.utcnow().isoformat()
    text = await _render_text(gs)
    await interaction.response.send_message(text)
    msg = await interaction.original_response()
    gs.channel_id = msg.channel.id
    gs.message_id = msg.id
    state.save()


@tank.command(name="reset", description="Reset the timer to 0")
async def reset(interaction: discord.Interaction):
    assert interaction.guild
    gs = state.for_guild(interaction.guild.id)
    gs.start_time = datetime.utcnow().isoformat()
    state.save()
    await _update_display(interaction.guild, gs)
    await interaction.response.send_message("Timer reset.", ephemeral=True)


@tank.command(name="template", description="Set the text template")
@app_commands.describe(text="Example: 🧯⚠️ {days}:{hours}:{minutes}:{seconds} WITHOUT TANK TALK ⚠️🧯")
async def template(interaction: discord.Interaction, text: str):
    assert interaction.guild
    gs = state.for_guild(interaction.guild.id)
    if "{days}" not in text:
        await interaction.response.send_message("Template must include `{days}`.", ephemeral=True)
        return
    gs.template = text
    state.save()
    await _update_display(interaction.guild, gs)
    await interaction.response.send_message("Template updated.", ephemeral=True)


@tank.command(name="bind", description="Bind to an existing message URL")
@app_commands.describe(message_url="Right-click → Copy Message Link")
async def bind(interaction: discord.Interaction, message_url: str):
    assert interaction.guild
    gs = state.for_guild(interaction.guild.id)
    m = re.match(r"https://(?:canary\.|ptb\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)", message_url)
    if not m:
        await interaction.response.send_message("Invalid message URL.", ephemeral=True)
        return
    guild_id, channel_id, message_id = map(int, m.groups())
    if guild_id != interaction.guild.id:
        await interaction.response.send_message("That message is not in this server.", ephemeral=True)
        return
    channel = interaction.guild.get_channel(channel_id) or await client.fetch_channel(channel_id)
    msg = await channel.fetch_message(message_id)
    if msg.author.id != client.user.id:
        await interaction.response.send_message("I can only bind to my own messages.", ephemeral=True)
        return
    gs.channel_id = channel_id
    gs.message_id = message_id
    state.save()
    await _update_display(interaction.guild, gs)
    await interaction.response.send_message("Bound to that message.", ephemeral=True)


@client.event
async def on_ready():
    print(f"Logged in as {client.user} (ID: {client.user.id})")
    for guild in client.guilds:
        await tree.sync(guild=guild)
        print(f"Synced commands to {guild.name}")
    client.loop.create_task(_background_updater())


if __name__ == "__main__":
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("Set DISCORD_BOT_TOKEN env var.")
    client.run(token)
