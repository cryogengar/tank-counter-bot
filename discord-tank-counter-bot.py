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
from datetime import datetime, timedelta
from pathlib import Path

import discord
from discord import app_commands

STATE_FILE = Path("state.json")

DEFAULT_TEMPLATE = "🧯⚠️ {days}:{hours}:{minutes}:{seconds} WITHOUT TANK TALK ⚠️🧯"

# ──────────────────────────────────────────────────────────────
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

# ──────────────────────────────────────────────────────────────
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
        STATE_FILE.write_text(
            json.dumps({str(k): v.to_dict() for k, v in self.by_guild.items()}, indent=2)
        )

    def for_guild(self, gid: int) -> GuildState:
        if gid not in self.by_guild:
            self.by_guild[gid] = GuildState(gid)
        return self.by_guild[gid]


state = State()

# ──────────────────────────────────────────────────────────────
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ──────────────────────────────────────────────────────────────
def _elapsed(gs: GuildState):
    """Return days, hours, minutes, seconds since start_time."""
    if not gs.start_time:
        return 0, 0, 0, 0
    delta = datetime.utcnow() - datetime.fromisoformat(gs.start_time)
    days = delta.days
    hours, rem = divmod(delta.seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    return days, hours, minutes, seconds

# ──────────────────────────────────────────────────────────────
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

# ──────────────────────────────────────────────────────────────
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

# ──────────────────────────────────────────────────────────────
async def _send_milestone_message(guild: discord.Guild, gs: GuildState, days: int):
    """Send a milestone message every 24h with rotating moods."""
    if not gs.channel_id:
        return
    channel = guild.get_channel(gs.channel_id) or await client.fetch_channel(gs.channel_id)

    # cycle between 3 moods
    mood = days % 3
    if mood == 1:
        msg = f"🎉 *{days} day{'s' if days != 1 else ''} without tank talk!* Keep the peace, commanders. 🕊️"
    elif mood == 2:
        msg = f"🏆 *{days} day{'s' if days != 1 else ''} without tank talk.* Unbelievable. Truly a miracle. 🙄"
    else:
        msg = f"👁 *The silence stretches to {days} day{'s' if days != 1 else ''}.* The tanks wait. They always wait."

    await channel.send(msg)
    gs.last_announced_day = days
    state.save()

# ──────────────────────────────────────────────────────────────
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

# ──────────────────────────────────────────────────────────────
@tree.command(name="ping", description="Check if the bot is alive")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("pong 🕷️", ephemeral=True)

class Tank(app_commands.Group):
    def __init__(self):
        super().__init__(name="tank", description="Tank talk counter controls")

tank = Tank()
tree.add_command(tank)

# ──────────────────────────────────────────────────────────────
@tank.command(name="post", description="Post the live counter here")
async def post(inter: discord.Interaction):
    assert inter.guild
    gs = state.for_guild(inter.guild.id)
    if not gs.start_time:
        gs.start_time = datetime.utcnow().isoformat()
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
    gs.start_time = datetime.utcnow().isoformat()
    gs.last_announced_day = -1
    state.save()
    await _update_display(inter.guild, gs)
    await inter.response.send_message("Timer reset.", ephemeral=True)

@tank.command(name="start", description="Set a custom start time (ISO 8601)")
@app_commands.describe(time="Format: YYYY-MM-DDTHH:MM:SS (UTC)")
async def start(inter: discord.Interaction, time: str):
    assert inter.guild
    gs = state.for_guild(inter.guild.id)
    try:
        # Validate and set ISO format time
        datetime.fromisoformat(time)
        gs.start_time = time
        gs.last_announced_day = -1
        state.save()
        await _update_display(inter.guild, gs)
        await inter.response.send_message(f"Start time set to `{time}` UTC.", ephemeral=True)
    except Exception:
        await inter.response.send_message("Invalid time format. Use `YYYY-MM-DDTHH:MM:SS`.", ephemeral=True)

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

@client.event
async def on_ready():
    print(f"Logged in as {client.user} (ID: {client.user.id})")

    # 🧹 one-time cleanup: remove any old *global* slash commands
    try:
        tree.clear_commands(guild=None)   # clear globals in the local tree
        await tree.sync()                 # push an empty global set → deletes old global cmds
        print("🧹 cleared global commands")
    except Exception as e:
        print("⚠️ global clear/sync error:", e)

    # ✅ now register the current commands per-guild (fast availability)
    for g in client.guilds:
        try:
            synced = await tree.sync(guild=g)
            print(f"✅ synced {len(synced)} commands to {g.name} ({g.id})")
        except Exception as e:
            print(f"⚠️ guild sync error for {g.name}:", e)

    # ▶️ keep the live counter running
    client.loop.create_task(_background_updater())

if __name__ == "__main__":
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("Set DISCORD_BOT_TOKEN env var.")
    client.run(token)
