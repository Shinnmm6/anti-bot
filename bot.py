import os
import re
import time
from collections import defaultdict, deque
from datetime import timedelta, datetime, timezone

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.environ["DISCORD_TOKEN"]
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "mod.db")  # on Railway, point this at a mounted volume, e.g. /data/mod.db

# ---- Tunables ----
WARN_TIMEOUT_THRESHOLD = 3          # warnings before auto-timeout
WARN_TIMEOUT_MINUTES = 60
SPAM_MESSAGES, SPAM_SECONDS = 5, 5  # 5 msgs in 5s = spam
MAX_MENTIONS = 5
RAID_JOINS, RAID_SECONDS = 8, 10    # 8 joins in 10s = raid
RAID_MODE_SECONDS = 120
MIN_ACCOUNT_AGE_DAYS = 7            # during raid mode, kick younger accounts
INVITE_RE = re.compile(r"(discord\.gg|discord(?:app)?\.com/invite)/\w+", re.I)

intents = discord.Intents.default()
intents.message_content = True  # privileged: enable in dev portal
intents.members = True          # privileged: enable in dev portal


class ModBot(commands.Bot):
    db: aiosqlite.Connection

    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.msg_times: dict[int, deque] = defaultdict(deque)
        self.join_times: deque = deque()
        self.raid_until = 0.0
        self.log_channels: dict[int, int] = {}  # guild_id -> channel_id, set with /auditlog set

    async def setup_hook(self):
        os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
        self.db = await aiosqlite.connect(DB_PATH)
        await self.db.execute(
            """CREATE TABLE IF NOT EXISTS warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER, user_id INTEGER, mod_id INTEGER,
                reason TEXT, created_at TEXT)"""
        )
        await self.db.commit()
        for ext in ("antinuke", "verification", "audit_logs", "antilink", "help_cmd", "server_profile",
                    "changelog", "shop", "modtools", "greetings", "wordfilter"):
            await self.load_extension(ext)
        await self.tree.sync()

    async def log(self, guild: discord.Guild, title: str, description: str,
                  color: discord.Color = discord.Color.orange()):
        channel = guild.get_channel(self.log_channels.get(guild.id) or LOG_CHANNEL_ID)
        if channel:
            embed = discord.Embed(title=title, description=description, color=color,
                                  timestamp=datetime.now(timezone.utc))
            try:
                await channel.send(embed=embed)
            except discord.HTTPException:
                pass  # missing permissions or channel deleted: don't break whatever was being logged


bot = ModBot()


def hierarchy_error(interaction: discord.Interaction, target: discord.Member) -> str | None:
    """Return an error string if the action shouldn't be allowed, else None."""
    guild = interaction.guild
    if target.id == interaction.user.id:
        return "You can't do that to yourself."
    if target.id == guild.owner_id:
        return "You can't act on the server owner."
    if target.top_role >= guild.me.top_role:
        return "That member's top role is at or above mine, so I can't act on them."
    if interaction.user.id != guild.owner_id and target.top_role >= interaction.user.top_role:
        return "That member's top role is at or above yours."
    return None


# ---------------- Slash commands ----------------

@bot.tree.command(description="Kick a member")
@app_commands.guild_only()
@app_commands.checks.has_permissions(kick_members=True)
@app_commands.checks.bot_has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason given"):
    if err := hierarchy_error(interaction, member):
        return await interaction.response.send_message(err, ephemeral=True)
    await member.kick(reason=f"{interaction.user}: {reason}")
    await interaction.response.send_message(f"Kicked **{member}**.")
    await bot.log(interaction.guild, "Kick", f"{member} kicked by {interaction.user}\n{reason}")


@bot.tree.command(description="Ban a member")
@app_commands.guild_only()
@app_commands.checks.has_permissions(ban_members=True)
@app_commands.checks.bot_has_permissions(ban_members=True)
async def ban(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason given"):
    if err := hierarchy_error(interaction, member):
        return await interaction.response.send_message(err, ephemeral=True)
    await member.ban(reason=f"{interaction.user}: {reason}", delete_message_seconds=0)
    await interaction.response.send_message(f"Banned **{member}**.")
    await bot.log(interaction.guild, "Ban", f"{member} banned by {interaction.user}\n{reason}", discord.Color.red())


@bot.tree.command(description="Timeout a member (minutes)")
@app_commands.guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
@app_commands.checks.bot_has_permissions(moderate_members=True)
async def timeout(interaction: discord.Interaction, member: discord.Member,
                  minutes: app_commands.Range[int, 1, 40320], reason: str = "No reason given"):
    if err := hierarchy_error(interaction, member):
        return await interaction.response.send_message(err, ephemeral=True)
    await member.timeout(timedelta(minutes=minutes), reason=f"{interaction.user}: {reason}")
    await interaction.response.send_message(f"Timed out **{member}** for {minutes} min.")
    await bot.log(interaction.guild, "Timeout", f"{member} for {minutes}m by {interaction.user}\n{reason}")


@bot.tree.command(description="Warn a member")
@app_commands.guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str):
    if err := hierarchy_error(interaction, member):
        return await interaction.response.send_message(err, ephemeral=True)
    await bot.db.execute(
        "INSERT INTO warnings (guild_id, user_id, mod_id, reason, created_at) VALUES (?,?,?,?,?)",
        (interaction.guild_id, member.id, interaction.user.id, reason,
         datetime.now(timezone.utc).isoformat()),
    )
    await bot.db.commit()
    async with bot.db.execute(
        "SELECT COUNT(*) FROM warnings WHERE guild_id=? AND user_id=?",
        (interaction.guild_id, member.id),
    ) as cur:
        (count,) = await cur.fetchone()

    msg = f"Warned **{member}** ({count} total)."
    if count >= WARN_TIMEOUT_THRESHOLD and interaction.guild.me.guild_permissions.moderate_members:
        await member.timeout(timedelta(minutes=WARN_TIMEOUT_MINUTES), reason="Warning threshold reached")
        msg += f" Auto-timed out for {WARN_TIMEOUT_MINUTES} min."
    await interaction.response.send_message(msg)
    await bot.log(interaction.guild, "Warn", f"{member} warned by {interaction.user}\n{reason}")


@bot.tree.command(description="List a member's warnings")
@app_commands.guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
async def warnings(interaction: discord.Interaction, member: discord.Member):
    async with bot.db.execute(
        "SELECT id, reason, created_at FROM warnings WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 10",
        (interaction.guild_id, member.id),
    ) as cur:
        rows = await cur.fetchall()
    if not rows:
        return await interaction.response.send_message(f"{member} has no warnings.", ephemeral=True)
    text = "\n".join(f"`#{r[0]}` {r[2][:10]}: {r[1]}" for r in rows)
    await interaction.response.send_message(f"**Warnings for {member}**\n{text}", ephemeral=True)


@bot.tree.command(description="Delete recent messages")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.checks.bot_has_permissions(manage_messages=True)
async def purge(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f"Deleted {len(deleted)} messages.", ephemeral=True)
    await bot.log(interaction.guild, "Purge",
                  f"{len(deleted)} messages in {interaction.channel.mention} by {interaction.user}")


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, (app_commands.MissingPermissions, app_commands.BotMissingPermissions)):
        msg = str(error)
    else:
        msg = "Something went wrong."
        print(f"Command error: {error!r}")
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


# ---------------- Auto-moderation ----------------

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild or not isinstance(message.author, discord.Member):
        return
    # Don't automod staff
    if message.author.guild_permissions.manage_messages:
        return

    reason = None
    if len(message.mentions) >= MAX_MENTIONS:  # invites/links are handled by antilink.py
        reason = "Mass mentions"
    else:
        now = time.monotonic()
        times = bot.msg_times[message.author.id]
        times.append(now)
        while times and now - times[0] > SPAM_SECONDS:
            times.popleft()
        if len(times) >= SPAM_MESSAGES:
            reason = "Message spam"
            times.clear()

    if reason:
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        if reason in ("Message spam", "Mass mentions"):
            try:
                await message.author.timeout(timedelta(minutes=10), reason=f"Automod: {reason}")
            except discord.HTTPException:
                pass
        await bot.log(message.guild, "Automod", f"{message.author} in {message.channel.mention}\n{reason}")


# ---------------- Anti-raid ----------------

@bot.event
async def on_member_join(member: discord.Member):
    now = time.monotonic()
    bot.join_times.append(now)
    while bot.join_times and now - bot.join_times[0] > RAID_SECONDS:
        bot.join_times.popleft()

    if len(bot.join_times) >= RAID_JOINS and now > bot.raid_until:
        bot.raid_until = now + RAID_MODE_SECONDS
        await bot.log(member.guild, "Raid mode ON",
                      f"{len(bot.join_times)} joins in {RAID_SECONDS}s. "
                      f"Kicking accounts under {MIN_ACCOUNT_AGE_DAYS} days old for {RAID_MODE_SECONDS}s.",
                      discord.Color.red())

    if now < bot.raid_until:
        age = datetime.now(timezone.utc) - member.created_at
        if age < timedelta(days=MIN_ACCOUNT_AGE_DAYS):
            try:
                await member.kick(reason="Anti-raid: new account during raid mode")
                await bot.log(member.guild, "Raid kick", f"{member} (account age {age.days}d)")
            except discord.HTTPException:
                pass


# ---------------- Logging ----------------

@bot.event
async def on_message_delete(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    await bot.log(message.guild, "Message deleted",
                  f"{message.author} in {message.channel.mention}\n{message.content[:1000]}",
                  discord.Color.light_grey())


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if before.author.bot or not before.guild or before.content == after.content:
        return
    await bot.log(before.guild, "Message edited",
                  f"{before.author} in {before.channel.mention}\n"
                  f"**Before:** {before.content[:500]}\n**After:** {after.content[:500]}",
                  discord.Color.light_grey())


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")


bot.run(TOKEN)
