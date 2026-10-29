import random
import time

import discord
from discord import app_commands
from discord.ext import commands

XP_MIN, XP_MAX = 15, 25
XP_COOLDOWN_SECONDS = 60  # one message per minute counts, so spamming gives nothing
MIN_MESSAGE_LENGTH = 3


def xp_for_level(level: int) -> int:
    """XP needed to go from `level` to `level + 1`."""
    return 5 * level * level + 50 * level + 100


def level_from_xp(xp: int) -> tuple[int, int, int]:
    """(level, xp into this level, xp needed for the next level)."""
    level = 0
    while xp >= xp_for_level(level):
        xp -= xp_for_level(level)
        level += 1
    return level, xp, xp_for_level(level)


def progress_bar(done: int, total: int, width: int = 12) -> str:
    filled = min(width, int(width * done / total)) if total else 0
    return "▰" * filled + "▱" * (width - filled)


class Leveling(commands.GroupCog, group_name="leveling", group_description="XP, levels and rewards"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config: dict[int, dict] = {}            # guild_id -> {"enabled": bool, "channel_id": int | None}
        self.cooldown: dict[tuple[int, int], float] = {}

    async def cog_load(self):
        db = self.bot.db
        await db.execute("CREATE TABLE IF NOT EXISTS levels (guild_id INTEGER, user_id INTEGER, xp INTEGER, "
                         "PRIMARY KEY (guild_id, user_id))")
        await db.execute("CREATE TABLE IF NOT EXISTS leveling_config (guild_id INTEGER PRIMARY KEY, "
                         "enabled INTEGER, channel_id INTEGER)")
        await db.execute("CREATE TABLE IF NOT EXISTS level_rewards (guild_id INTEGER, level INTEGER, role_id INTEGER, "
                         "PRIMARY KEY (guild_id, level))")
        await db.commit()
        async with db.execute("SELECT guild_id, enabled, channel_id FROM leveling_config") as cur:
            for guild_id, enabled, channel_id in await cur.fetchall():
                self.config[guild_id] = {"enabled": bool(enabled), "channel_id": channel_id}

    # ---------- earning XP ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild or len(message.content) < MIN_MESSAGE_LENGTH:
            return
        cfg = self.config.get(message.guild.id)
        if not cfg or not cfg["enabled"]:
            return
        key = (message.guild.id, message.author.id)
        now = time.monotonic()
        if now - self.cooldown.get(key, -XP_COOLDOWN_SECONDS) < XP_COOLDOWN_SECONDS:
            return
        self.cooldown[key] = now

        gained = random.randint(XP_MIN, XP_MAX)
        old_xp = await self.get_xp(*key)
        new_xp = old_xp + gained
        await self.bot.db.execute("INSERT OR REPLACE INTO levels (guild_id, user_id, xp) VALUES (?,?,?)",
                                  (*key, new_xp))
        await self.bot.db.commit()

        old_level, new_level = level_from_xp(old_xp)[0], level_from_xp(new_xp)[0]
        if new_level > old_level and isinstance(message.author, discord.Member):
            await self.level_up(message, new_level)

    async def level_up(self, message: discord.Message, level: int):
        member = message.author
        gave = await self.sync_reward_roles(member, level)
        channel = (message.guild.get_channel(self.config[message.guild.id]["channel_id"] or 0)
                   or message.channel)
        text = f"🎉 {member.mention} reached **level {level}**!"
        if gave:
            text += f" You earned {', '.join(r.mention for r in gave)}."
        try:
            await channel.send(text, allowed_mentions=discord.AllowedMentions(users=[member]))
        except discord.HTTPException:
            pass

    async def sync_reward_roles(self, member: discord.Member, level: int) -> list[discord.Role]:
        """Give every reward role at or below `level` that the member doesn't have yet."""
        async with self.bot.db.execute("SELECT role_id FROM level_rewards WHERE guild_id=? AND level<=?",
                                       (member.guild.id, level)) as cur:
            role_ids = [r[0] for r in await cur.fetchall()]
        given = []
        for role_id in role_ids:
            role = member.guild.get_role(role_id)
            if role and role not in member.roles and role < member.guild.me.top_role and not role.managed:
                try:
                    await member.add_roles(role, reason=f"Reached level {level}")
                    given.append(role)
                except discord.HTTPException:
                    pass
        return given

    async def get_xp(self, guild_id: int, user_id: int) -> int:
        async with self.bot.db.execute("SELECT xp FROM levels WHERE guild_id=? AND user_id=?",
                                       (guild_id, user_id)) as cur:
            row = await cur.fetchone()
        return row[0] if row else 0

    # ---------- commands ----------

    @app_commands.command(name="on", description="Turn leveling on")
    @app_commands.describe(channel="Where level-up messages go (default: the channel the person was chatting in)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def on(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        if channel and not channel.permissions_for(interaction.guild.me).send_messages:
            return await interaction.response.send_message(f"I can't send messages in {channel.mention}.",
                                                           ephemeral=True)
        await self.save_config(interaction.guild_id, True, channel.id if channel else None)
        await interaction.response.send_message(
            "Leveling is **on**. Members earn 15-25 XP per message (once a minute)."
            + (f" Level-up messages go to {channel.mention}." if channel else ""), ephemeral=True)

    @app_commands.command(name="off", description="Turn leveling off (levels are kept)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def off(self, interaction: discord.Interaction):
        cfg = self.config.get(interaction.guild_id, {})
        await self.save_config(interaction.guild_id, False, cfg.get("channel_id"))
        await interaction.response.send_message("Leveling is **off**. Everyone's XP is kept.", ephemeral=True)

    async def save_config(self, guild_id: int, enabled: bool, channel_id: int | None):
        await self.bot.db.execute("INSERT OR REPLACE INTO leveling_config (guild_id, enabled, channel_id) VALUES (?,?,?)",
                                  (guild_id, int(enabled), channel_id))
        await self.bot.db.commit()
        self.config[guild_id] = {"enabled": enabled, "channel_id": channel_id}

    @app_commands.command(name="rank", description="Show your level (or someone else's)")
    @app_commands.guild_only()
    async def rank(self, interaction: discord.Interaction, member: discord.Member | None = None):
        member = member or interaction.user
        xp = await self.get_xp(interaction.guild_id, member.id)
        level, into, needed = level_from_xp(xp)
        async with self.bot.db.execute("SELECT COUNT(*) FROM levels WHERE guild_id=? AND xp>?",
                                       (interaction.guild_id, xp)) as cur:
            (ahead,) = await cur.fetchone()
        embed = discord.Embed(title=member.display_name, color=member.color if member.color.value
                              else discord.Color.blurple())
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Level", value=str(level))
        embed.add_field(name="Rank", value=f"#{ahead + 1}" if xp else "Unranked")
        embed.add_field(name="Total XP", value=f"{xp:,}")
        embed.add_field(name=f"Progress to level {level + 1}",
                        value=f"{progress_bar(into, needed)} {into:,} / {needed:,} XP", inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="top", description="Show the server's top 10")
    @app_commands.guild_only()
    async def top(self, interaction: discord.Interaction):
        async with self.bot.db.execute("SELECT user_id, xp FROM levels WHERE guild_id=? ORDER BY xp DESC LIMIT 10",
                                       (interaction.guild_id,)) as cur:
            rows = await cur.fetchall()
        if not rows:
            return await interaction.response.send_message("Nobody has any XP yet.", ephemeral=True)
        medals = ["🥇", "🥈", "🥉"]
        lines = [f"{medals[i] if i < 3 else f'`{i + 1}.`'} <@{uid}> · level **{level_from_xp(xp)[0]}** · {xp:,} XP"
                 for i, (uid, xp) in enumerate(rows)]
        await interaction.response.send_message(
            embed=discord.Embed(title=f"Top members in {interaction.guild.name}", description="\n".join(lines),
                                color=discord.Color.gold()),
            allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="reward", description="Give a role to members who reach a level")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def reward(self, interaction: discord.Interaction, level: app_commands.Range[int, 1, 500],
                     role: discord.Role):
        if role.managed or role.is_default() or role >= interaction.guild.me.top_role:
            return await interaction.response.send_message(
                "I can't hand out that role. It must be a normal role below my highest role.", ephemeral=True)
        await self.bot.db.execute("INSERT OR REPLACE INTO level_rewards (guild_id, level, role_id) VALUES (?,?,?)",
                                  (interaction.guild_id, level, role.id))
        await self.bot.db.commit()
        await interaction.response.send_message(f"Members reaching **level {level}** will get {role.mention}.",
                                                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="unreward", description="Remove the reward set for a level")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def unreward(self, interaction: discord.Interaction, level: app_commands.Range[int, 1, 500]):
        cur = await self.bot.db.execute("DELETE FROM level_rewards WHERE guild_id=? AND level=?",
                                        (interaction.guild_id, level))
        await self.bot.db.commit()
        await interaction.response.send_message(
            f"Removed the level {level} reward." if cur.rowcount else f"There's no reward at level {level}.",
            ephemeral=True)

    @app_commands.command(name="rewards", description="List the level rewards")
    @app_commands.guild_only()
    async def rewards(self, interaction: discord.Interaction):
        async with self.bot.db.execute("SELECT level, role_id FROM level_rewards WHERE guild_id=? ORDER BY level",
                                       (interaction.guild_id,)) as cur:
            rows = await cur.fetchall()
        text = "\n".join(f"Level **{lvl}**: <@&{rid}>" for lvl, rid in rows) or "No rewards yet. Add one with `/leveling reward`."
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="reset", description="Reset one member's XP to zero")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def reset(self, interaction: discord.Interaction, member: discord.Member):
        await self.bot.db.execute("DELETE FROM levels WHERE guild_id=? AND user_id=?",
                                  (interaction.guild_id, member.id))
        await self.bot.db.commit()
        await interaction.response.send_message(f"Reset {member.mention}'s XP.", ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())
        await self.bot.log(interaction.guild, "XP reset", f"{member} reset by {interaction.user}")


async def setup(bot: commands.Bot):
    await bot.add_cog(Leveling(bot))
