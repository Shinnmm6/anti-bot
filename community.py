import re
import time
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from giveaway import parse_duration
from leveling import level_from_xp
from wordfilter import normalize

LINK_RE = re.compile(r"(https?://|www\.|discord\.gg/|discord(?:app)?\.com/invite)", re.I)


def has_link(text: str) -> bool:
    return bool(LINK_RE.search(text))


def filtered(bot: commands.Bot, guild_id: int, text: str) -> bool:
    """True if the text contains a word from the server's /filter list."""
    cog = bot.get_cog("WordFilter")
    pattern = cog.regex.get(guild_id) if cog else None
    return bool(pattern and pattern.search(normalize(text)))


def today_utc():
    return datetime.now(timezone.utc).date()


# =====================================================================================================
# /profile: one card that pulls together levels, invites, kudos, streak and the custom role
# =====================================================================================================

class Profile(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def one(self, sql: str, args: tuple):
        async with self.bot.db.execute(sql, args) as cur:
            return await cur.fetchone()

    @app_commands.command(name="profile", description="Show a member's profile card: level, invites, kudos and more")
    @app_commands.guild_only()
    async def profile(self, interaction: discord.Interaction, member: discord.Member | None = None):
        member, gid = member or interaction.user, interaction.guild_id
        xp = (await self.one("SELECT xp FROM levels WHERE guild_id=? AND user_id=?", (gid, member.id)) or (0,))[0]
        level = level_from_xp(xp)[0]
        ahead = (await self.one("SELECT COUNT(*) FROM levels WHERE guild_id=? AND xp>?", (gid, xp)))[0]
        invited = (await self.one(
            "SELECT COALESCE(SUM(CASE WHEN left=0 AND fake=0 THEN 1 ELSE 0 END),0), COUNT(*) FROM invite_joins "
            "WHERE guild_id=? AND inviter_id=?", (gid, member.id)))
        kudos = (await self.one("SELECT COUNT(*) FROM kudos WHERE guild_id=? AND receiver_id=?", (gid, member.id)))[0]
        streak = (await self.one("SELECT streak, last_day FROM daily_streaks WHERE guild_id=? AND user_id=?",
                                 (gid, member.id)) or (0, ""))
        role_id = (await self.one("SELECT role_id FROM custom_roles WHERE guild_id=? AND user_id=?",
                                  (gid, member.id)) or (None,))[0]
        role = interaction.guild.get_role(role_id) if role_id else None

        embed = discord.Embed(title=member.display_name, color=member.color if member.color.value
                              else discord.Color.blurple())
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Level", value=f"{level}" + (f" (rank #{ahead + 1})" if xp else ""))
        embed.add_field(name="XP", value=f"{xp:,}")
        embed.add_field(name="Kudos", value=str(kudos))
        embed.add_field(name="Invites", value=f"{invited[0]} active of {invited[1]}")
        # a streak only counts while it is still alive (claimed today or yesterday)
        alive = streak[1] in (str(today_utc()), str(today_utc() - timedelta(days=1)))
        embed.add_field(name="Daily streak", value=f"{streak[0]} days" if alive and streak[0] else "none")
        embed.add_field(name="Custom role", value=role.mention if role else "none")
        embed.add_field(name="Joined", value=discord.utils.format_dt(member.joined_at, "R") if member.joined_at else "?")
        if member.premium_since:
            embed.add_field(name="Boosting since", value=discord.utils.format_dt(member.premium_since, "R"))
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())


# =====================================================================================================
# /confess: anonymous to members, but staff can trace a confession if a rule is broken
# =====================================================================================================

class Confess(commands.GroupCog, group_name="confess", group_description="Anonymous confessions"):
    COOLDOWN = 60
    MAX_LENGTH = 1500

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.channels: dict[int, int] = {}
        self.last: dict[tuple[int, int], float] = {}

    async def cog_load(self):
        db = self.bot.db
        await db.execute("CREATE TABLE IF NOT EXISTS confess_config (guild_id INTEGER PRIMARY KEY, channel_id INTEGER)")
        await db.execute(
            """CREATE TABLE IF NOT EXISTS confessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, number INTEGER, author_id INTEGER,
                channel_id INTEGER, message_id INTEGER, created_at INTEGER)""")
        await db.commit()
        async with db.execute("SELECT guild_id, channel_id FROM confess_config") as cur:
            for guild_id, channel_id in await cur.fetchall():
                self.channels[guild_id] = channel_id

    @app_commands.command(name="setup", description="Choose the channel where anonymous confessions are posted")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup_cmd(self, interaction: discord.Interaction, channel: discord.TextChannel):
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return await interaction.response.send_message(
                f"I need View Channel, Send Messages and Embed Links in {channel.mention}.", ephemeral=True)
        await self.bot.db.execute("INSERT OR REPLACE INTO confess_config (guild_id, channel_id) VALUES (?,?)",
                                  (interaction.guild_id, channel.id))
        await self.bot.db.commit()
        self.channels[interaction.guild_id] = channel.id
        await interaction.response.send_message(
            f"Confessions will be posted in {channel.mention}. Members stay anonymous to each other, but staff can "
            "look up the sender of a confession with `/confess reveal`. Members are told this when they confess.",
            ephemeral=True)

    @app_commands.command(name="off", description="Turn confessions off")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def off(self, interaction: discord.Interaction):
        await self.bot.db.execute("DELETE FROM confess_config WHERE guild_id=?", (interaction.guild_id,))
        await self.bot.db.commit()
        self.channels.pop(interaction.guild_id, None)
        await interaction.response.send_message("Confessions are turned off.", ephemeral=True)

    @app_commands.command(name="send", description="Post an anonymous confession")
    @app_commands.describe(message="What you want to say (no links, and no pinging people)")
    @app_commands.guild_only()
    async def send(self, interaction: discord.Interaction, message: str):
        guild = interaction.guild
        channel = guild.get_channel(self.channels.get(guild.id, 0))
        if channel is None:
            return await interaction.response.send_message("Confessions aren't set up on this server.", ephemeral=True)
        key, now = (guild.id, interaction.user.id), time.monotonic()
        wait = self.COOLDOWN - (now - self.last.get(key, -self.COOLDOWN))
        if wait > 0:
            return await interaction.response.send_message(f"Slow down. Try again in {int(wait) + 1} seconds.",
                                                           ephemeral=True)
        text = message.strip()
        if not text or len(text) > self.MAX_LENGTH:
            return await interaction.response.send_message(
                f"A confession must be between 1 and {self.MAX_LENGTH} characters.", ephemeral=True)
        if has_link(text):
            return await interaction.response.send_message("Links aren't allowed in confessions.", ephemeral=True)
        if filtered(self.bot, guild.id, text):
            return await interaction.response.send_message("That contains a word that isn't allowed here.",
                                                           ephemeral=True)
        self.last[key] = now

        async with self.bot.db.execute("SELECT COALESCE(MAX(number),0)+1 FROM confessions WHERE guild_id=?",
                                       (guild.id,)) as cur:
            number = (await cur.fetchone())[0]
        embed = discord.Embed(title=f"Anonymous confession #{number}", description=text, color=discord.Color.dark_purple(),
                              timestamp=datetime.now(timezone.utc))
        embed.set_footer(text="Anonymous to members. Staff can trace a confession if a rule is broken.")
        try:  # mentions inside the text never ping anyone
            posted = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            self.last.pop(key, None)
            return await interaction.response.send_message("I couldn't post in the confessions channel.", ephemeral=True)
        await self.bot.db.execute(
            "INSERT INTO confessions (guild_id, number, author_id, channel_id, message_id, created_at) VALUES (?,?,?,?,?,?)",
            (guild.id, number, interaction.user.id, channel.id, posted.id, int(time.time())))
        await self.bot.db.commit()
        await interaction.response.send_message(
            f"Posted as confession #{number}. Other members can't see who you are, but staff can look it up "
            "if the confession breaks a rule.", ephemeral=True)

    async def find(self, interaction: discord.Interaction, number: int):
        async with self.bot.db.execute(
                "SELECT author_id, channel_id, message_id FROM confessions WHERE guild_id=? AND number=?",
                (interaction.guild_id, number)) as cur:
            return await cur.fetchone()

    @app_commands.command(name="reveal", description="Staff: see who wrote a confession (this is logged)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def reveal(self, interaction: discord.Interaction, number: int):
        row = await self.find(interaction, number)
        if not row:
            return await interaction.response.send_message("There's no confession with that number.", ephemeral=True)
        await interaction.response.send_message(f"Confession #{number} was written by <@{row[0]}>.", ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())
        await self.bot.log(interaction.guild, "Confession revealed",
                           f"{interaction.user} looked up the author of confession #{number}", discord.Color.red())

    @app_commands.command(name="delete", description="Staff: delete a confession")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def delete(self, interaction: discord.Interaction, number: int):
        row = await self.find(interaction, number)
        if not row:
            return await interaction.response.send_message("There's no confession with that number.", ephemeral=True)
        channel = interaction.guild.get_channel(row[1])
        try:
            if channel is not None:
                await (await channel.fetch_message(row[2])).delete()
        except discord.HTTPException:
            pass  # already gone
        await interaction.response.send_message(f"Deleted confession #{number}.", ephemeral=True)
        await self.bot.log(interaction.guild, "Confession deleted", f"#{number} by {interaction.user}")


# =====================================================================================================
# /capsule: write a message that is delivered to the channel in the future
# =====================================================================================================

class Capsule(commands.GroupCog, group_name="capsule", group_description="Messages to the future"):
    MIN_SECONDS, MAX_SECONDS, MAX_PENDING, MAX_LENGTH = 60, 365 * 86400, 5, 1500

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.db.execute(
            """CREATE TABLE IF NOT EXISTS capsules (
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, channel_id INTEGER, author_id INTEGER,
                content TEXT, created_at INTEGER, deliver_at INTEGER, delivered INTEGER DEFAULT 0)""")
        await self.bot.db.commit()
        self.deliver_loop.start()

    async def cog_unload(self):
        self.deliver_loop.cancel()

    @tasks.loop(seconds=30)
    async def deliver_loop(self):
        async with self.bot.db.execute(
                "SELECT id, guild_id, channel_id, author_id, content, created_at FROM capsules "
                "WHERE delivered=0 AND deliver_at<=?", (int(time.time()),)) as cur:
            due = await cur.fetchall()
        for cid, guild_id, channel_id, author_id, content, created_at in due:
            # mark first, so a crash can never deliver the same capsule twice
            cur = await self.bot.db.execute("UPDATE capsules SET delivered=1 WHERE id=? AND delivered=0", (cid,))
            await self.bot.db.commit()
            if not cur.rowcount:
                continue
            guild = self.bot.get_guild(guild_id)
            channel = guild.get_channel(channel_id) if guild else None
            if channel is None:
                continue
            embed = discord.Embed(title="A message from the past", description=content, color=discord.Color.teal())
            embed.add_field(name="Written by", value=f"<@{author_id}>")
            embed.add_field(name="Written", value=f"<t:{created_at}:R>")
            try:
                await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass

    @deliver_loop.before_loop
    async def before_deliver_loop(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="write", description="Write a message that will be posted in this channel in the future")
    @app_commands.describe(message="What your future self (or the server) should read",
                           when="How long to wait, like 7d, 30d, 12w or 1d12h (1 minute to 1 year)")
    @app_commands.guild_only()
    async def write(self, interaction: discord.Interaction, message: str, when: str):
        seconds = parse_duration(when)
        if seconds is None or not self.MIN_SECONDS <= seconds <= self.MAX_SECONDS:
            return await interaction.response.send_message(
                "Use a time like `7d`, `30d`, `12w` or `1d12h`, between 1 minute and 1 year.", ephemeral=True)
        text = message.strip()
        if not text or len(text) > self.MAX_LENGTH:
            return await interaction.response.send_message(
                f"The message must be between 1 and {self.MAX_LENGTH} characters.", ephemeral=True)
        if has_link(text) or filtered(self.bot, interaction.guild_id, text):
            return await interaction.response.send_message(
                "No links, and no words from the server's filter list, please.", ephemeral=True)
        perms = interaction.channel.permissions_for(interaction.guild.me)
        if not (perms.send_messages and perms.embed_links):
            return await interaction.response.send_message("I can't post in this channel, so pick another one.",
                                                           ephemeral=True)
        async with self.bot.db.execute("SELECT COUNT(*) FROM capsules WHERE guild_id=? AND author_id=? AND delivered=0",
                                       (interaction.guild_id, interaction.user.id)) as cur:
            pending = (await cur.fetchone())[0]
        if pending >= self.MAX_PENDING:
            return await interaction.response.send_message(
                f"You already have {self.MAX_PENDING} capsules waiting. Cancel one with `/capsule cancel`.",
                ephemeral=True)
        now = int(time.time())
        cur = await self.bot.db.execute(
            "INSERT INTO capsules (guild_id, channel_id, author_id, content, created_at, deliver_at) VALUES (?,?,?,?,?,?)",
            (interaction.guild_id, interaction.channel_id, interaction.user.id, text, now, now + seconds))
        await self.bot.db.commit()
        await interaction.response.send_message(
            f"Capsule #{cur.lastrowid} sealed. It will be posted in {interaction.channel.mention} "
            f"<t:{now + seconds}:R> (<t:{now + seconds}:f>).", ephemeral=True)

    @app_commands.command(name="list", description="Show your capsules that are still waiting")
    @app_commands.guild_only()
    async def list_cmd(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
                "SELECT id, channel_id, deliver_at FROM capsules WHERE guild_id=? AND author_id=? AND delivered=0 "
                "ORDER BY deliver_at", (interaction.guild_id, interaction.user.id)) as cur:
            rows = await cur.fetchall()
        text = "\n".join(f"`#{i}` in <#{ch}> <t:{at}:R>" for i, ch, at in rows) or "You have no capsules waiting."
        await interaction.response.send_message(text, ephemeral=True)

    @app_commands.command(name="cancel", description="Cancel one of your waiting capsules")
    @app_commands.guild_only()
    async def cancel(self, interaction: discord.Interaction, capsule_id: int):
        cur = await self.bot.db.execute(
            "DELETE FROM capsules WHERE id=? AND guild_id=? AND author_id=? AND delivered=0",
            (capsule_id, interaction.guild_id, interaction.user.id))
        await self.bot.db.commit()
        await interaction.response.send_message("Capsule cancelled." if cur.rowcount else
                                                "I can't find a waiting capsule of yours with that number.",
                                                ephemeral=True)


# =====================================================================================================
# /kudos: thank people. 3 a day each, with a weekly and all-time top
# =====================================================================================================

class Kudos(commands.GroupCog, group_name="kudos", group_description="Thank someone who helped"):
    DAILY_LIMIT = 3

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.db.execute(
            """CREATE TABLE IF NOT EXISTS kudos (
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, giver_id INTEGER, receiver_id INTEGER,
                reason TEXT, created_at INTEGER)""")
        await self.bot.db.commit()

    @app_commands.command(name="give", description="Give someone kudos for helping (3 per day)")
    @app_commands.describe(member="Who you want to thank", reason="What they did (up to 140 characters)")
    @app_commands.guild_only()
    async def give(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        gid, giver = interaction.guild_id, interaction.user
        if member.bot or member.id == giver.id:
            return await interaction.response.send_message("Pick another member, not yourself or a bot.", ephemeral=True)
        reason = " ".join(reason.split())
        if not reason or len(reason) > 140:
            return await interaction.response.send_message("The reason must be between 1 and 140 characters.",
                                                           ephemeral=True)
        if has_link(reason) or filtered(self.bot, gid, reason):
            return await interaction.response.send_message("No links or filtered words in the reason, please.",
                                                           ephemeral=True)
        now = int(time.time())
        async with self.bot.db.execute("SELECT COUNT(*) FROM kudos WHERE guild_id=? AND giver_id=? AND created_at>?",
                                       (gid, giver.id, now - 86400)) as cur:
            used = (await cur.fetchone())[0]
        if used >= self.DAILY_LIMIT:
            return await interaction.response.send_message(
                f"You've used all {self.DAILY_LIMIT} of your kudos for the last 24 hours. Try again later.",
                ephemeral=True)
        await self.bot.db.execute(
            "INSERT INTO kudos (guild_id, giver_id, receiver_id, reason, created_at) VALUES (?,?,?,?,?)",
            (gid, giver.id, member.id, reason, now))
        await self.bot.db.commit()
        async with self.bot.db.execute("SELECT COUNT(*) FROM kudos WHERE guild_id=? AND receiver_id=?",
                                       (gid, member.id)) as cur:
            total = (await cur.fetchone())[0]
        embed = discord.Embed(description=f"{giver.mention} gave kudos to {member.mention}\n> {reason}",
                              color=discord.Color.gold())
        embed.set_footer(text=f"{member.display_name} now has {total} kudos")
        await interaction.response.send_message(
            embed=embed, allowed_mentions=discord.AllowedMentions(users=[member]))

    @app_commands.command(name="top", description="The members with the most kudos")
    @app_commands.choices(period=[app_commands.Choice(name="This week", value="week"),
                                  app_commands.Choice(name="All time", value="all")])
    @app_commands.guild_only()
    async def top(self, interaction: discord.Interaction, period: app_commands.Choice[str] | None = None):
        weekly = (period.value if period else "week") == "week"
        since = int(time.time()) - 7 * 86400 if weekly else 0
        async with self.bot.db.execute(
                "SELECT receiver_id, COUNT(*) AS n FROM kudos WHERE guild_id=? AND created_at>? "
                "GROUP BY receiver_id ORDER BY n DESC LIMIT 10", (interaction.guild_id, since)) as cur:
            rows = await cur.fetchall()
        if not rows:
            return await interaction.response.send_message("Nobody has any kudos yet.", ephemeral=True)
        lines = [f"`{i + 1}.` <@{uid}>: **{n}**" for i, (uid, n) in enumerate(rows)]
        await interaction.response.send_message(
            embed=discord.Embed(title="Kudos this week" if weekly else "Kudos of all time",
                                description="\n".join(lines), color=discord.Color.gold()),
            allowed_mentions=discord.AllowedMentions.none())


# =====================================================================================================
# /daily: a daily check-in that gives XP and builds a streak (needs /leveling on)
# =====================================================================================================

class Daily(commands.Cog):
    BASE_XP, STREAK_XP, STREAK_CAP = 50, 10, 7  # 50 XP, plus 10 for every streak day, up to 7 days (120 XP)

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.db.execute(
            "CREATE TABLE IF NOT EXISTS daily_streaks (guild_id INTEGER, user_id INTEGER, last_day TEXT, "
            "streak INTEGER, PRIMARY KEY (guild_id, user_id))")
        await self.bot.db.commit()

    @app_commands.command(name="daily", description="Claim your daily XP. Come back every day to build a streak")
    @app_commands.guild_only()
    async def daily(self, interaction: discord.Interaction):
        leveling = self.bot.get_cog("Leveling")
        if leveling is None or not leveling.config.get(interaction.guild_id, {}).get("enabled"):
            return await interaction.response.send_message(
                "Daily rewards give XP, and leveling isn't turned on here. Ask a staff member to run `/leveling on`.",
                ephemeral=True)
        gid, member, today = interaction.guild_id, interaction.user, today_utc()
        async with self.bot.db.execute("SELECT last_day, streak FROM daily_streaks WHERE guild_id=? AND user_id=?",
                                       (gid, member.id)) as cur:
            row = await cur.fetchone()
        last, streak = (row[0], row[1]) if row else ("", 0)
        next_claim = int(datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc).timestamp())
        if last == str(today):
            return await interaction.response.send_message(
                f"You already claimed today. Come back <t:{next_claim}:R> to keep your **{streak} day** streak.",
                ephemeral=True)
        streak = streak + 1 if last == str(today - timedelta(days=1)) else 1
        gained = self.BASE_XP + self.STREAK_XP * min(streak, self.STREAK_CAP)
        await self.bot.db.execute("INSERT OR REPLACE INTO daily_streaks (guild_id, user_id, last_day, streak) VALUES (?,?,?,?)",
                                  (gid, member.id, str(today), streak))
        old_xp = await leveling.get_xp(gid, member.id)
        await self.bot.db.execute("INSERT OR REPLACE INTO levels (guild_id, user_id, xp) VALUES (?,?,?)",
                                  (gid, member.id, old_xp + gained))
        await self.bot.db.commit()

        old_level, new_level = level_from_xp(old_xp)[0], level_from_xp(old_xp + gained)[0]
        text = f"You claimed **{gained} XP**. Streak: **{streak} day{'s' if streak != 1 else ''}**."
        if streak < self.STREAK_CAP:
            text += f" Come back <t:{next_claim}:R> for more."
        if new_level > old_level:
            text += f"\nYou reached **level {new_level}**!"
            for role in await leveling.sync_reward_roles(member, new_level):
                text += f" You earned {role.mention}."
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot):
    await bot.add_cog(Profile(bot))
    await bot.add_cog(Confess(bot))
    await bot.add_cog(Capsule(bot))
    await bot.add_cog(Kudos(bot))
    await bot.add_cog(Daily(bot))
