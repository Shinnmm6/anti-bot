import time

import discord
from discord import app_commands
from discord.ext import commands

DEFAULT_PERKS = "- Custom role and color\n- Priority support\n- Access to premium-only channels"
DEFAULT_CONTACT = "Contact the server owner to purchase."
COOLDOWN_SECONDS = 10
_last_use: dict[int, float] = {}


class Premium(commands.Cog):
    """/premium: members look up what premium unlocks and how to pay for it. No DMs are ever sent."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.db.execute(
            "CREATE TABLE IF NOT EXISTS premium_config (guild_id INTEGER PRIMARY KEY, perks TEXT, price TEXT, "
            "contact TEXT)")
        await self.bot.db.execute(
            "CREATE TABLE IF NOT EXISTS premium_members (guild_id INTEGER, user_id INTEGER, granted_by INTEGER, "
            "granted_at INTEGER, PRIMARY KEY (guild_id, user_id))")
        await self.bot.db.commit()

    async def get_config(self, guild_id: int) -> tuple[str, str, str]:
        async with self.bot.db.execute("SELECT perks, price, contact FROM premium_config WHERE guild_id=?",
                                       (guild_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            return DEFAULT_PERKS, "Contact for pricing", DEFAULT_CONTACT
        return row[0] or DEFAULT_PERKS, row[1] or "Contact for pricing", row[2] or DEFAULT_CONTACT

    @app_commands.command(name="premium", description="See what premium unlocks and how to get it")
    @app_commands.guild_only()
    async def premium(self, interaction: discord.Interaction):
        now = time.monotonic()
        if now - _last_use.get(interaction.user.id, 0.0) < COOLDOWN_SECONDS:
            return await interaction.response.send_message("Slow down a little, then try again.", ephemeral=True)
        _last_use[interaction.user.id] = now

        perks, price, contact = await self.get_config(interaction.guild_id)
        embed = discord.Embed(title="✨ Premium", description=perks, color=discord.Color.gold())
        embed.add_field(name="Price", value=price, inline=False)
        embed.add_field(name="How to get it", value=contact, inline=False)
        embed.set_footer(text=f"{interaction.guild.name} premium")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="premiumgrant", description="Give a member premium (after they've paid you)")
    @app_commands.describe(member="The member to grant premium to")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def premiumgrant(self, interaction: discord.Interaction, member: discord.Member):
        await self.bot.db.execute(
            "INSERT OR REPLACE INTO premium_members (guild_id, user_id, granted_by, granted_at) VALUES (?,?,?,?)",
            (interaction.guild_id, member.id, interaction.user.id, int(time.time())))
        await self.bot.db.commit()
        await interaction.response.send_message(
            f"{member.mention} now has premium and can use the premium commands.", ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none())
        await self.bot.log(interaction.guild, "Premium granted",
                           f"{member.mention} granted by {interaction.user.mention}", discord.Color.gold())

    @app_commands.command(name="premiumrevoke", description="Take premium away from a member")
    @app_commands.describe(member="The member to remove premium from")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def premiumrevoke(self, interaction: discord.Interaction, member: discord.Member):
        await self.bot.db.execute("DELETE FROM premium_members WHERE guild_id=? AND user_id=?",
                                  (interaction.guild_id, member.id))
        await self.bot.db.commit()
        await interaction.response.send_message(f"Removed {member.mention}'s premium.", ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="premiumlist", description="See who has premium")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def premiumlist(self, interaction: discord.Interaction):
        async with self.bot.db.execute("SELECT user_id FROM premium_members WHERE guild_id=?",
                                       (interaction.guild_id,)) as cur:
            rows = await cur.fetchall()
        if not rows:
            return await interaction.response.send_message("Nobody has premium yet.", ephemeral=True)
        text = "\n".join(f"<@{r[0]}>" for r in rows[:50])
        await interaction.response.send_message(f"**Premium members ({len(rows)}):**\n{text}", ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="premiumsetup", description="Set what /premium shows (server owner/staff)")
    @app_commands.describe(
        perks="What premium unlocks, one line per perk (type \\n for a new line)",
        price="The price or price range to show",
        contact="How to pay or who to message (type \\n for a new line)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def premiumsetup(self, interaction: discord.Interaction, perks: str, price: str, contact: str):
        perks, contact = perks.replace("\\n", "\n").strip(), contact.replace("\\n", "\n").strip()
        if len(perks) > 1000 or len(price) > 200 or len(contact) > 500:
            return await interaction.response.send_message(
                "Keep perks under 1000 characters, price under 200, and contact under 500.", ephemeral=True)
        await self.bot.db.execute(
            "INSERT INTO premium_config (guild_id, perks, price, contact) VALUES (?,?,?,?) "
            "ON CONFLICT(guild_id) DO UPDATE SET perks=excluded.perks, price=excluded.price, contact=excluded.contact",
            (interaction.guild_id, perks, price, contact))
        await self.bot.db.commit()
        await interaction.response.send_message("Saved. Run `/premium` to see how it looks.", ephemeral=True)


async def has_premium(bot: commands.Bot, guild: discord.Guild, member: discord.Member) -> bool:
    if member.id == guild.owner_id or member.guild_permissions.manage_guild:
        return True
    async with bot.db.execute("SELECT 1 FROM premium_members WHERE guild_id=? AND user_id=?",
                              (guild.id, member.id)) as cur:
        return bool(await cur.fetchone())


def premium_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            return True
        if await has_premium(interaction.client, interaction.guild, interaction.user):
            return True
        raise app_commands.CheckFailure("This is a premium feature. Run `/premium` to see how to get it.")
    return app_commands.check(predicate)


async def setup(bot: commands.Bot):
    await bot.add_cog(Premium(bot))
