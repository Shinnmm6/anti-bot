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


async def setup(bot: commands.Bot):
    await bot.add_cog(Premium(bot))
