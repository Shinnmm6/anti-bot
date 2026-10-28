import time

import discord
from discord import app_commands
from discord.ext import commands

# ---- Edit your products here ----
PRODUCTS: dict[str, dict] = {
    "self_bot": {
        "label": "Self bot",
        "emoji": "👍",
        "description": "Describe your Self bot here.",
        "price": "Contact v0zv2",
    },
    "tool_bot": {
        "label": "Nuke Bot",
        "emoji": "🥀",
        "description": "Describe your Tool Bot here.",
        "price": "Contact staff",
    },
}
PICK_COOLDOWN_SECONDS = 15  # stops one person spamming the staff log
_last_pick: dict[int, float] = {}


def store_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🛒 Store",
        description="Click **Buy** below, then choose the product you want.\n\n"
                    + "\n".join(f"{p['emoji']} **{p['label']}**" for p in PRODUCTS.values()),
        color=discord.Color.blurple())
    return embed


class ProductSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Choose a product",
            options=[discord.SelectOption(label=p["label"], value=key, emoji=p["emoji"])
                     for key, p in PRODUCTS.items()])

    async def callback(self, interaction: discord.Interaction):
        product = PRODUCTS[self.values[0]]
        now = time.monotonic()
        if now - _last_pick.get(interaction.user.id, 0.0) < PICK_COOLDOWN_SECONDS:
            return await interaction.response.send_message(
                "Slow down a little, then try again.", ephemeral=True)
        _last_pick[interaction.user.id] = now

        embed = discord.Embed(title=f"{product['emoji']} {product['label']}",
                              description=product["description"], color=discord.Color.green())
        embed.add_field(name="Price", value=product["price"])
        embed.set_footer(text="Staff have been notified and will get in touch.")
        await interaction.response.edit_message(content=None, embed=embed, view=None)

        await interaction.client.log(
            interaction.guild, "Purchase request",
            f"{interaction.user.mention} ({interaction.user}) chose **{product['label']}**",
            discord.Color.green())


class ProductPickView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(ProductSelect())


class BuyView(discord.ui.View):
    """Persistent view: survives restarts because of the fixed custom_id and timeout=None."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Buy", style=discord.ButtonStyle.success, emoji="🛒", custom_id="shop:buy")
    async def buy(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Choose a product:", view=ProductPickView(), ephemeral=True)


class Shop(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(BuyView())

    @app_commands.command(name="buy", description="Post the store panel with a Buy button")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def buy(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        channel = channel or interaction.channel
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return await interaction.response.send_message(
                f"I need View Channel, Send Messages and Embed Links in {channel.mention}.", ephemeral=True)
        await channel.send(embed=store_embed(), view=BuyView())
        await interaction.response.send_message(f"Store panel posted in {channel.mention}.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Shop(bot))
