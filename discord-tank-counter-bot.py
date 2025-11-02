"""
Discord "tank talk" counter bot for a small server.

Now supports a live timer and daily snapshot posts.

Features (per guild):
- /tank post → bot posts and binds a counter message
- /tank bind <message_url> → bind the bot to edit an existing message
- /tank template → set the text template
- /tank mode → switch between editing a message or renaming a channel
- /tank reset → reset the timer to now (they talked about tanks)
- /tank set <days> → set the elapsed days (adjust last reset)
- /tank inc → legacy helper (+1 day)
- /tank snapshot on|off → enable/disable the daily snapshot

Timer: live days:hours:minutes:seconds since last reset.
Snapshot: once per day at 00:00 UTC, posts a summary line in the bound channel.

ENV: DISCORD_BOT_TOKEN=your token
"""
from __future__ import annotations

import os
import json
import re
import asyncio
from pathlib import Path
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands

STATE_FILE = Path("state.json")

# Default template – change anytime with /tank template
# Placeholders:
#   {days} {hours} {minutes} {seconds}
#   {day_word}  → "day"/"days"
#   {DAY_WORD}  → "DAY"/"DAYS"
#   {Day_Word}  → "Day"/"Days"
DEFAULT_TEMPLATE = "🧯⚠️ {days} {DAY_WORD} without tank talk ⚠️🧯"


# ---------------- State ---------------- #
class GuildState:
    __slots__ = (
        "guild_id", "days", "message_id", "channel_id", "mode", "template",
        "channel_name_channel_id", "last_reset", "daily_snapshot"
    )

    def __init__(self, guild_id: int, data: dict | None = None):
        self.guild_id = guild_id
        data = data or {}
        # Legacy field (kept for back-compat)
        self.days: int = int(data.get("days", 0))
        self.message_id: int | None = data.get("message_id")
        self.channel_id: int | None = data.get("channel_id")
        self.mode: str = data.get("mode", "message")  # "message" or "channel_name"
        self.template: str = data.get("template", DEFAULT_TEMPLATE)
        self.channel_name_channel_id: int | None = data.get("channel_name_channel_id")
        self.last_reset: float | None = data.get("last_reset")
        self.daily_snapshot: bool = bool(data.get("daily_snapshot", True))

        if self.last_reset is None:
            # Infer last_reset from legacy days so your count continues
            now_ts = datetime.now(timezone.utc).timestamp()
            self.last_reset = now_ts - (self.days * 86400)

    def to_dict(self) -> dict:
        return {
            "days": self.days,
            "message_id": self.message_id,
            "channel_id": self.channel_id,
            "mode": self.mode,
            "template": self.template,
            "channel_name_channel_id": self.channel_name_channel_id,
            "last_reset": self.last_reset,
            "daily_snapshot": self.daily_snapshot,
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


# ---------------- Discord client ---------------- #
intents = discord.Intents.default()  # no privileged intents needed
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


class Tank(app_commands.Group):
    def __init__(self):
        super().__init__(name="tank", description="Tank talk counter controls")


tank = Tank()
tree.add_command(tank)


# ---------------- Rendering / updates ---------------- #
async def _elapsed_parts(gs: GuildState) -> tuple[int, int, int, int]:
    now_ts = datetime.now(timezone.utc).timestamp()
    elapsed = max(0, int(now_ts - (gs.last_reset or now_ts)))
    d, rem = divmod(elapsed, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    return d, h, m, s


async def _render_text(gs: GuildState) -> str:
    d, h, m, s = await _elapsed_parts(gs)
    base = "day" if d == 1 else "days"
    tpl = gs.template

    if ("{day_word}" in tpl) or ("{DAY_WORD}" in tpl) or ("{Day_Word}" in tpl):
        return tpl.format(
            days=d, hours=h, minutes=m, seconds=s,
            day_word=base,
            DAY_WORD=base.upper(),
            Day_Word=base.capitalize(),
        )

    rendered = tpl.format(days=d, hours=h, minutes=m, seconds=s)
    if d == 1:
        rendered = rendered.replace("DAYS", "DAY").replace("days", "day")
    return rendered


async def _update_display(guild: discord.Guild, gs: GuildState):
    if gs.mode == "message":
        if gs.channel_id and gs.message_id:
            channel = guild.get_channel(gs.channel_id) or await client.fetch_channel(gs.channel_id)
            try:
                msg = await channel.fetch_message(gs.message_id)
            except discord.NotFound:
                gs.message_id = None
                state.save()
                return
            await msg.edit(content=await _render_text(gs))

    elif gs.mode == "channel_name":
        if gs.channel_name_channel_id:
            channel = guild.get_channel(gs.channel_name_channel_id) or await client.fetch_channel(gs.channel_name_channel_id)
            text = await _render_text(gs)
            safe = re.sub(r"[^a-z0-9-]", "-", text.lower())[:95]
            try:
                await channel.edit(name=safe)
            except discord.Forbidden:
                pass


async def background_updater():
    # updates the bound message/channel name every 30s
    while True:
        try:
            for guild in list(client.guilds):
                gs = state.for_guild(guild.id)
                await _update_display(guild, gs)
        except Exception:
            pass
        await asyncio.sleep(30)


async def daily_snapshot_loop():
    # posts once at 00:00 UTC in the bound channel
    while True:
        now = datetime.now(timezone.utc)
        next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        await asyncio.sleep((next_midnight - now).total_seconds())
        for guild in list(client.guilds):
            gs = state.for_guild(guild.id)
            if not gs.daily_snapshot:
                continue
            channel_id = gs.channel_id or gs.channel_name_channel_id
            if not channel_id:
                continue
            try:
                channel = guild.get_channel(channel_id) or await client.fetch_channel(channel_id)
                text = await _render_text(gs)
                await channel.send(f"🗓️ Daily snapshot: {text}")
            except Exception:
                pass


# ---------------- Commands ---------------- #
@tree.command(name="ping", description="Check if the bot is alive")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("pong", ephemeral=True)


@tank.command(name="post", description="Post a new counter message here and bind to it")
async def post(interaction: discord.Interaction):
    assert interaction.guild
    gs = state.for_guild(interaction.guild.id)
    txt = await _render_text(gs)
    await interaction.response.send_message(txt)
    msg = await interaction.original_response()
    gs.mode = "message"
    gs.channel_id = msg.channel.id
    gs.message_id = msg.id
    state.save()


@tank.command(name="bind", description="Bind the bot to edit an existing message URL")
@app_commands.describe(message_url="Right-click → Copy Message Link")
async def bind(interaction: discord.Interaction, message_url: str):
    assert interaction.guild
    gs = state.for_guild(interaction.guild.id)
    m = re.match(r"https://(?:canary\\.|ptb\\.)?discord(?:app)?\\.com/channels/(\\d+)/(\\d+)/(\\d+)", message_url)
    if not m:
        await interaction.response.send_message("Invalid message URL.", ephemeral=True)
        return
    guild_id, channel_id, message_id = map(int, m.groups())
    if guild_id != interaction.guild.id:
        await interaction.response.send_message("That message is not in this server.", ephemeral=True)
        return
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


@tank.command(name="template", description="Set the text template with placeholders")
@app_commands.describe(text="e.g., '{days}d {hours}h {minutes}m {seconds}s without tank talk' or include {day_word}/{DAY_WORD}/{Day_Word}")
async def template(interaction: discord.Interaction, text: str):
    assert interaction.guild
    gs = state.for_guild(interaction.guild.id)
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


@tank.command(name="reset", description="Reset the timer to now (they talked about tanks)")
async def reset(interaction: discord.Interaction):
    assert interaction.guild
    gs = state.for_guild(interaction.guild.id)
    gs.last_reset = datetime.now(timezone.utc).timestamp()
    state.save()
    await _update_display(interaction.guild, gs)
    await interaction.response.send_message("Timer reset to now.", ephemeral=True)


@tank.command(name="set", description="Set elapsed time by days (e.g., 3 → last reset was 3 days ago)")
@app_commands.describe(days="Number of days elapsed so far")
async def set_days(interaction: discord.Interaction, days: app_commands.Range[int, 0, 10000]):
    assert interaction.guild
    gs = state.for_guild(interaction.guild.id)
    gs.last_reset = datetime.now(timezone.utc).timestamp() - (int(days) * 86400)
    gs.days = int(days)  # keep legacy field roughly aligned
    state.save()
    await _update_display(interaction.guild, gs)
    await interaction.response.send_message(f"Set elapsed to **{days}** day(s).", ephemeral=True)


@tank.command(name="inc", description="Add 1 day to the elapsed timer (legacy helper)")
async def increment(interaction: discord.Interaction):
    assert interaction.guild
    gs = state.for_guild(interaction.guild.id)
    gs.last_reset = (gs.last_reset or datetime.now(timezone.utc).timestamp()) - 86400
    gs.days += 1
    state.save()
    await _update_display(interaction.guild, gs)
    await interaction.response.send_message("Added 1 day to the timer.", ephemeral=True)


@tank.command(name="snapshot", description="Turn the daily snapshot on or off")
@app_commands.describe(toggle="Choose on/off")
@app_commands.choices(toggle=[
    app_commands.Choice(name="on", value="on"),
    app_commands.Choice(name="off", value="off"),
])
async def snapshot(interaction: discord.Interaction, toggle: app_commands.Choice[str]):
    assert interaction.guild
    gs = state.for_guild(interaction.guild.id)
    gs.daily_snapshot = (toggle.value == "on")
    state.save()
    await interaction.response.send_message(f"Daily snapshot **{toggle.value}**.", ephemeral=True)


# ---------------- Lifecycle ---------------- #
@client.event
async def on_ready():
    print(f"Logged in as {client.user} (ID: {client.user.id})")
    try:
        for guild in client.guilds:
            await tree.sync(guild=guild)
            print(f"Synced commands to guild {guild.name} ({guild.id})")
    except Exception as e:
        print("Failed to sync commands:", e)

    # Initial refresh
    for guild in client.guilds:
        gs = state.for_guild(guild.id)
        try:
            await _update_display(guild, gs)
        except Exception:
            pass

    # Background loops
    client.loop.create_task(background_updater())
    client.loop.create_task(daily_snapshot_loop())


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
