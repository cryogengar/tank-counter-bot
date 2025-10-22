"""
Discord "tank talk" counter bot for a small server.

Features (per guild):
- /tank post → bot posts and binds a counter message
- /tank set <days> → set to an exact number
- /tank inc → increment by 1
- /tank reset → set to 0
- /tank show → show current value
- /tank bind <message_url> → bind the bot to edit an existing message
- Optional: channel-name mode to show the counter in a channel name

Storage: simple JSON file (per guild state)
Python: 3.10+
Library: discord.py 2.3+

ENV:
DISCORD_BOT_TOKEN=your token
"""
from __future__ import annotations
import os
import json
import re
from pathlib import Path
import asyncio

import discord
from discord import app_commands

STATE_FILE = Path("state.json")

DEFAULT_TEMPLATE = "{days} days since Sean and Patrick talked about tanks"

class GuildState(discord.utils.SequenceProxy):
    __slots__ = ("guild_id", "days", "message_id", "channel_id", "mode", "template", "channel_name_channel_id")
    def __init__(self, guild_id: int, data: dict | None = None):
        self.guild_id = guild_id
        data = data or {}
        self.days: int = int(data.get("days", 0))
        self.message_id: int | None = data.get("message_id")
        self.channel_id: int | None = data.get("channel_id")
        # mode: "message" (default) or "channel_name"
        self.mode: str = data.get("mode", "message")
        self.template: str = data.get("template", DEFAULT_TEMPLATE)
        # if mode==channel_name, which channel to rename
        self.channel_name_channel_id: int | None = data.get("channel_name_channel_id")

    def to_dict(self) -> dict:
        return {
            "days": self.days,
            "message_id": self.message_id,
            "channel_id": self.channel_id,
            "mode": self.mode,
            "template": self.template,
            "channel_name_channel_id": self.channel_name_channel_id,
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
        out = {str(gid): gs.to_dict() for gid, gs in self._by_guild.items()}
        STATE_FILE.write_text(json.dumps(out, indent=2))

    def for_guild(self, guild_id: int) -> GuildState:
        if guild_id not in self._by_guild:
            self._by_guild[guild_id] = GuildState(guild_id)
        return self._by_guild[guild_id]

state = State()

intents = discord.Intents.default()
# We are only using slash commands + editing our own messages; no privileged intents needed
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

async def _render_text(gs: GuildState) -> str:
    return gs.template.format(days=gs.days)

async def _update_display(guild: discord.Guild, gs: GuildState):
    if gs.mode == "message":
        if gs.channel_id and gs.message_id:
            channel = guild.get_channel(gs.channel_id) or await client.fetch_channel(gs.channel_id)
            try:
                msg = await channel.fetch_message(gs.message_id)
            except discord.NotFound:
                # message deleted; clear binding
                gs.message_id = None
                state.save()
                return
            await msg.edit(content=await _render_text(gs))
    elif gs.mode == "channel_name":
        if gs.channel_name_channel_id:
            channel = guild.get_channel(gs.channel_name_channel_id) or await client.fetch_channel(gs.channel_name_channel_id)
            # Discord channel name restrictions: lowercase, spaces become dashes, <= 100 chars.
            text = await _render_text(gs)
            safe = re.sub(r"[^a-z0-9-]", "-", text.lower())[:95]
            try:
                await channel.edit(name=safe)
            except discord.Forbidden:
                pass

@tree.command(name="ping", description="Check if the bot is alive")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("pong", ephemeral=True)

# Group commands under /tank
class Tank(app_commands.Group):
    def __init__(self):
        super().__init__(name="tank", description="Tank talk counter controls")

tank = Tank()

tree.add_command(tank)

@tank.command(name="post", description="Post a new counter message here and bind to it")
async def post(interaction: discord.Interaction):
    assert interaction.guild
    gs = state.for_guild(interaction.guild.id)
    text = await _render_text(gs)
    await interaction.response.send_message(text)
    msg = await interaction.original_response()
    gs.mode = "message"
    gs.channel_id = msg.channel.id
    gs.message_id = msg.id
    state.save()

@tank.command(name="set", description="Set the counter to an exact number of days")
@app_commands.describe(days="Number of days")
async def set_days(interaction: discord.Interaction, days: app_commands.Range[int, 0, 10000]):
    assert interaction.guild
    gs = state.for_guild(interaction.guild.id)
    gs.days = int(days)
    state.save()
    await _update_display(interaction.guild, gs)
    await interaction.response.send_message(f"Set to **{gs.days}**.", ephemeral=True)

@tank.command(name="inc", description="Increment by 1 day")
async def increment(interaction: discord.Interaction):
    assert interaction.guild
    gs = state.for_guild(interaction.guild.id)
    gs.days += 1
    state.save()
    await _update_display(interaction.guild, gs)
    await interaction.response.send_message(f"Now **{gs.days}**.", ephemeral=True)

@tank.command(name="reset", description="Reset to 0")
async def reset(interaction: discord.Interaction):
    assert interaction.guild
    gs = state.for_guild(interaction.guild.id)
    gs.days = 0
    state.save()
    await _update_display(interaction.guild, gs)
    await interaction.response.send_message("Reset to **0**.", ephemeral=True)

@tank.command(name="show", description="Show current counter")
async def show(interaction: discord.Interaction):
    assert interaction.guild
    gs = state.for_guild(interaction.guild.id)
    await interaction.response.send_message(await _render_text(gs))

@tank.command(name="bind", description="Bind the bot to edit an existing message URL")
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
    # Check we can edit (must be our own message)
    channel = interaction.guild.get_channel(channel_id) or await client.fetch_channel(channel_id)
    try:
        msg = await channel.fetch_message(message_id)
    except discord.NotFound:
        await interaction.response.send_message("Could not fetch that message.", ephemeral=True)
        return
    if msg.author.id != client.user.id:
        await interaction.response.send_message("I can only bind to **my own** messages. Use /tank post to create one.", ephemeral=True)
        return
    gs.mode = "message"
    gs.channel_id = channel_id
    gs.message_id = message_id
    state.save()
    await _update_display(interaction.guild, gs)
    await interaction.response.send_message("Bound to that message.", ephemeral=True)

@tank.command(name="template", description="Set the text template (use {days} placeholder)")
@app_commands.describe(text="Example: '{days} days since Sean and Patrick talked about tanks'")
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

@tank.command(name="mode", description="Switch display mode: message or channel_name")
@app_commands.choices(kind=[
    app_commands.Choice(name="message", value="message"),
    app_commands.Choice(name="channel_name", value="channel_name"),
])
@app_commands.describe(channel="If channel_name mode: which channel to rename")
async def mode(interaction: discord.Interaction, kind: app_commands.Choice[str], channel: discord.TextChannel | None = None):
    assert interaction.guild
    gs = state.for_guild(interaction.guild.id)
    gs.mode = kind.value
    if gs.mode == "channel_name":
        if channel is None:
            await interaction.response.send_message("Choose a channel to rename in channel_name mode.", ephemeral=True)
            return
        gs.channel_name_channel_id = channel.id
    state.save()
    await _update_display(interaction.guild, gs)
    await interaction.response.send_message(f"Mode set to **{gs.mode}**.", ephemeral=True)

@client.event
async def on_ready():
    print(f"Logged in as {client.user} (ID: {client.user.id})")
    # Fast-sync slash commands to every guild the bot is currently in so they show up immediately.
    try:
        # Global sync is sometimes slow; push per-guild for instant availability.
        for guild in client.guilds:
            await tree.sync(guild=guild)
            print(f"Synced commands to guild {guild.name} ({guild.id})")
    except Exception as e:
        print("Failed to sync commands:", e)

    # Attempt to refresh displays on startup
    for guild in client.guilds:
        gs = state.for_guild(guild.id)
        try:
            await _update_display(guild, gs)
        except Exception:
            pass

# Owner-only quick /sync command to force a resync if commands don't appear
@tree.command(name="sync", description="Force-sync slash commands in this server (owner-only)")
async def sync_here(interaction: discord.Interaction):
    app_owner = (await client.application_info()).owner
    if interaction.user.id != app_owner.id:
        await interaction.response.send_message("Only the bot owner can use this.", ephemeral=True)
        return
    await tree.sync(guild=interaction.guild)
    await interaction.response.send_message("Commands synced to this server.", ephemeral=True)

if __name__ == "__main__":
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("Set DISCORD_BOT_TOKEN env var.")
    client.run(token)

    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("Set DISCORD_BOT_TOKEN env var.")
    client.run(token)
