import discord
from discord import app_commands
from discord.ext import commands

MAX_BYTES = 8 * 1024 * 1024  # 8 MB cap
ALLOWED_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


class ServerProfile(commands.GroupCog, group_name="server",
                    group_description="Change the server's icon and banner"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @staticmethod
    def validate(guild: discord.Guild, image: discord.Attachment, banner: bool) -> str | None:
        """Return an error message if this image can't be used, else None."""
        ctype = (image.content_type or "").split(";")[0].lower()
        if ctype not in ALLOWED_TYPES:
            return "Please attach a PNG, JPG, GIF or WebP image."
        if image.size > MAX_BYTES:
            return "That image is too big. Keep it under 8 MB."
        if banner and "BANNER" not in guild.features:
            return "This server can't have a banner yet. It needs Server Boost Level 2."
        if ctype == "image/gif":
            needed = "ANIMATED_BANNER" if banner else "ANIMATED_ICON"
            if needed not in guild.features:
                kind = "banners" if banner else "icons"
                return f"This server can't use animated {kind} yet (needs more Server Boosts). Use a PNG, JPG or WebP."
        return None

    async def apply(self, interaction: discord.Interaction, image: discord.Attachment, banner: bool):
        guild = interaction.guild
        if err := self.validate(guild, image, banner):
            return await interaction.response.send_message(err, ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        what = "banner" if banner else "icon"
        try:
            data = await image.read()
            if banner:
                await guild.edit(banner=data, reason=f"Banner changed by {interaction.user}")
            else:
                await guild.edit(icon=data, reason=f"Icon changed by {interaction.user}")
        except discord.HTTPException as e:
            return await interaction.followup.send(
                f"Discord rejected that image ({e.text or e}). Try a different one. "
                "Icons work best at 512x512 and banners at 960x540.", ephemeral=True)

        await interaction.followup.send(f"Server {what} updated.", ephemeral=True)
        await self.bot.log(guild, f"Server {what} changed",
                           f"{interaction.user.mention} changed the server {what}.", discord.Color.blue())

    @app_commands.command(name="profile", description="Change the server icon (attach an image)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.checks.bot_has_permissions(manage_guild=True)
    @app_commands.describe(image="PNG, JPG, GIF or WebP, up to 8 MB (512x512 works best)")
    async def profile(self, interaction: discord.Interaction, image: discord.Attachment):
        await self.apply(interaction, image, banner=False)

    @app_commands.command(name="banner", description="Change the server banner (attach an image)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.checks.bot_has_permissions(manage_guild=True)
    @app_commands.describe(image="PNG, JPG, GIF or WebP, up to 8 MB (960x540 works best)")
    async def banner(self, interaction: discord.Interaction, image: discord.Attachment):
        await self.apply(interaction, image, banner=True)

    @app_commands.command(name="clear", description="Remove the server icon or banner")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.checks.bot_has_permissions(manage_guild=True)
    @app_commands.choices(which=[
        app_commands.Choice(name="Icon (profile)", value="icon"),
        app_commands.Choice(name="Banner", value="banner"),
    ])
    async def clear(self, interaction: discord.Interaction, which: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)
        try:
            if which.value == "banner":
                await interaction.guild.edit(banner=None, reason=f"Banner removed by {interaction.user}")
            else:
                await interaction.guild.edit(icon=None, reason=f"Icon removed by {interaction.user}")
        except discord.HTTPException as e:
            return await interaction.followup.send(f"Couldn't remove it ({e.text or e}).", ephemeral=True)
        await interaction.followup.send(f"Server {which.value} removed.", ephemeral=True)
        await self.bot.log(interaction.guild, f"Server {which.value} removed",
                           f"{interaction.user.mention} removed the server {which.value}.", discord.Color.blue())


async def setup(bot: commands.Bot):
    await bot.add_cog(ServerProfile(bot))
