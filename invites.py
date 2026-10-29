import asyncio
from collections import defaultdict
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

FAKE_ACCOUNT_DAYS = 3  # joins from accounts younger than this are counted as "fake" (likely alts)


class InviteTracker(commands.GroupCog, group_name="invites", group_description="See who invited who"):
    """Works by remembering every invite's use count and comparing it each time someone joins.

    The bot needs the Manage Server permission to read the server's invites.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.cache: dict[int, dict[str, tuple[int, int, int | None]]] = {}  # guild -> code -> (uses, max_uses, inviter)
        self.locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.channels: dict[int, int] = {}  # guild -> channel for join announcements

    async def cog_load(self):
        db = self.bot.db
        await db.execute(
            """CREATE TABLE IF NOT EXISTS invite_joins (
                guild_id INTEGER, user_id INTEGER, inviter_id INTEGER, code TEXT, joined_at TEXT,
                fake INTEGER DEFAULT 0, left INTEGER DEFAULT 0, PRIMARY KEY (guild_id, user_id))""")
        await db.execute("CREATE TABLE IF NOT EXISTS invite_channels (guild_id INTEGER PRIMARY KEY, channel_id INTEGER)")
        await db.commit()
        async with db.execute("SELECT guild_id, channel_id FROM invite_channels") as cur:
            for guild_id, channel_id in await cur.fetchall():
                self.channels[guild_id] = channel_id

    # ---------- invite cache ----------

    async def snapshot(self, guild: discord.Guild) -> dict[str, tuple[int, int, int | None]] | None:
        if not guild.me.guild_permissions.manage_guild:
            return None
        try:
            invites = await guild.invites()
        except discord.HTTPException:
            return None
        return {i.code: (i.uses or 0, i.max_uses or 0, i.inviter.id if i.inviter else None) for i in invites}

    async def refresh(self, guild: discord.Guild):
        snap = await self.snapshot(guild)
        if snap is not None:
            self.cache[guild.id] = snap

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            await self.refresh(guild)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        await self.refresh(guild)

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        if invite.guild:
            self.cache.setdefault(invite.guild.id, {})[invite.code] = (
                invite.uses or 0, invite.max_uses or 0, invite.inviter.id if invite.inviter else None)

    # on_invite_delete is deliberately NOT handled: a single-use invite is deleted the moment it is used,
    # and on_member_join needs to still find it in the cache to credit the inviter.

    # ---------- joins and leaves ----------

    async def find_inviter(self, guild: discord.Guild) -> tuple[int | None, str | None]:
        """Compare the saved use counts with the current ones. Returns (inviter id, invite code)."""
        old = self.cache.get(guild.id)
        new = await self.snapshot(guild)
        if old is None or new is None:
            if new is not None:
                self.cache[guild.id] = new
            return None, None
        self.cache[guild.id] = new

        for code, (uses, _max, inviter) in new.items():
            if uses > old.get(code, (0, 0, None))[0]:
                return inviter, code
        for code, (uses, max_uses, inviter) in old.items():  # used up and deleted since the last check
            if code not in new and max_uses and uses + 1 >= max_uses:
                return inviter, code
        if guild.vanity_url_code:
            return None, "vanity"
        return None, None

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        guild = member.guild
        async with self.locks[guild.id]:  # two joins at once would otherwise confuse the comparison
            inviter_id, code = await self.find_inviter(guild)
        age_days = (datetime.now(timezone.utc) - member.created_at).days
        fake = 1 if age_days < FAKE_ACCOUNT_DAYS else 0
        await self.bot.db.execute(
            "INSERT OR REPLACE INTO invite_joins (guild_id, user_id, inviter_id, code, joined_at, fake, left) "
            "VALUES (?,?,?,?,?,?,0)",
            (guild.id, member.id, inviter_id, code, datetime.now(timezone.utc).isoformat(), fake))
        await self.bot.db.commit()

        channel = guild.get_channel(self.channels.get(guild.id, 0))
        if channel is None:
            return
        if inviter_id:
            stats = await self.stats_for(guild.id, inviter_id)
            text = (f"{member.mention} joined, invited by <@{inviter_id}> "
                    f"(**{stats['active']}** invite{'s' if stats['active'] != 1 else ''}).")
        elif code == "vanity":
            text = f"{member.mention} joined using the server's vanity link."
        else:
            text = f"{member.mention} joined, but I couldn't tell who invited them."
        try:
            await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        await self.bot.db.execute("UPDATE invite_joins SET left=1 WHERE guild_id=? AND user_id=?",
                                  (member.guild.id, member.id))
        await self.bot.db.commit()

    # ---------- stats ----------

    async def stats_for(self, guild_id: int, inviter_id: int) -> dict:
        async with self.bot.db.execute(
                "SELECT COUNT(*), COALESCE(SUM(left),0), COALESCE(SUM(fake),0), "
                "COALESCE(SUM(CASE WHEN left=0 AND fake=0 THEN 1 ELSE 0 END),0) "
                "FROM invite_joins WHERE guild_id=? AND inviter_id=?", (guild_id, inviter_id)) as cur:
            total, left, fake, active = await cur.fetchone()
        return {"total": total, "left": left, "fake": fake, "active": active}

    # ---------- commands ----------

    @app_commands.command(name="setup", description="Announce who invited each new member in a channel")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup_cmd(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not channel.permissions_for(interaction.guild.me).send_messages:
            return await interaction.response.send_message(f"I can't send messages in {channel.mention}.",
                                                           ephemeral=True)
        await self.bot.db.execute("INSERT OR REPLACE INTO invite_channels (guild_id, channel_id) VALUES (?,?)",
                                  (interaction.guild_id, channel.id))
        await self.bot.db.commit()
        self.channels[interaction.guild_id] = channel.id
        await self.refresh(interaction.guild)
        note = ("" if interaction.guild.me.guild_permissions.manage_guild else
                "\n**Warning:** I need the **Manage Server** permission to see invites, or I can't track anything.")
        await interaction.response.send_message(f"Join announcements will go to {channel.mention}.{note}",
                                                ephemeral=True)

    @app_commands.command(name="off", description="Stop the join announcements (tracking keeps running)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def off(self, interaction: discord.Interaction):
        await self.bot.db.execute("DELETE FROM invite_channels WHERE guild_id=?", (interaction.guild_id,))
        await self.bot.db.commit()
        self.channels.pop(interaction.guild_id, None)
        await interaction.response.send_message("Join announcements turned off.", ephemeral=True)

    @app_commands.command(name="stats", description="How many people someone has invited")
    @app_commands.guild_only()
    async def stats(self, interaction: discord.Interaction, member: discord.Member | None = None):
        member = member or interaction.user
        s = await self.stats_for(interaction.guild_id, member.id)
        embed = discord.Embed(title=f"Invites: {member.display_name}", color=discord.Color.blurple())
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Active", value=str(s["active"]))
        embed.add_field(name="Total joins", value=str(s["total"]))
        embed.add_field(name="Left", value=str(s["left"]))
        embed.add_field(name="Fake", value=f"{s['fake']} (account under {FAKE_ACCOUNT_DAYS} days old)")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="top", description="The members with the most invites")
    @app_commands.guild_only()
    async def top(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
                "SELECT inviter_id, SUM(CASE WHEN left=0 AND fake=0 THEN 1 ELSE 0 END) AS active, COUNT(*) "
                "FROM invite_joins WHERE guild_id=? AND inviter_id IS NOT NULL "
                "GROUP BY inviter_id ORDER BY active DESC, COUNT(*) DESC LIMIT 10", (interaction.guild_id,)) as cur:
            rows = await cur.fetchall()
        if not rows:
            return await interaction.response.send_message("No invites tracked yet.", ephemeral=True)
        lines = [f"`{i + 1}.` <@{uid}> · **{active}** active ({total} total)" for i, (uid, active, total) in enumerate(rows)]
        await interaction.response.send_message(
            embed=discord.Embed(title="Top inviters", description="\n".join(lines), color=discord.Color.gold()),
            allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="who", description="Find out who invited a member")
    @app_commands.guild_only()
    async def who(self, interaction: discord.Interaction, member: discord.Member):
        async with self.bot.db.execute("SELECT inviter_id, code FROM invite_joins WHERE guild_id=? AND user_id=?",
                                       (interaction.guild_id, member.id)) as cur:
            row = await cur.fetchone()
        if not row:
            text = f"I have no record of how {member.mention} joined (they joined before tracking started)."
        elif row[0]:
            text = f"{member.mention} was invited by <@{row[0]}> (invite `{row[1]}`)."
        elif row[1] == "vanity":
            text = f"{member.mention} joined with the server's vanity link."
        else:
            text = f"I couldn't tell who invited {member.mention}."
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot):
    await bot.add_cog(InviteTracker(bot))
