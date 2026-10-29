from datetime import datetime, timezone
import io

import discord
from discord import app_commands
from discord.ext import commands

from greetings import read_attachment, read_image_url  # same image rules as /welcome

MAX_MESSAGE = 4000


class Partnership(commands.Cog):
    """/partnership: the bot posts a partnership announcement for you, so the post comes from the bot only."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.db.execute(
            "CREATE TABLE IF NOT EXISTS partnership_channel (guild_id INTEGER PRIMARY KEY, channel_id INTEGER)")
        await self.bot.db.commit()

    @app_commands.command(name="partnership", description="Post a partnership announcement as the bot")
    @app_commands.describe(
        message="The announcement text (type \\n for a new line)",
        channel="Where to post it. Saved as the default, so you can leave it out next time",
        ping="Role to ping above the announcement (optional)",
        image="Upload an image to show in the announcement",
        image_url="Or paste a link to an image (a Discord CDN link works)",
        title="Title (default: Partnership)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def partnership(self, interaction: discord.Interaction, message: str,
                          channel: discord.TextChannel | None = None, ping: discord.Role | None = None,
                          image: discord.Attachment | None = None, image_url: str | None = None,
                          title: str | None = None):
        guild = interaction.guild
        if channel is None:
            async with self.bot.db.execute("SELECT channel_id FROM partnership_channel WHERE guild_id=?",
                                           (guild.id,)) as cur:
                row = await cur.fetchone()
            channel = guild.get_channel(row[0]) if row else None
            if channel is None:
                return await interaction.response.send_message(
                    "Pick a channel the first time: `/partnership message:... channel:#partnerships`. "
                    "I'll remember it after that.", ephemeral=True)

        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links and perms.attach_files):
            return await interaction.response.send_message(
                f"I need View Channel, Send Messages, Embed Links and Attach Files in {channel.mention}.",
                ephemeral=True)
        text = message.replace("\\n", "\n").strip()
        if not text or len(text) > MAX_MESSAGE:
            return await interaction.response.send_message(
                f"The announcement must be between 1 and {MAX_MESSAGE} characters.", ephemeral=True)
        if title and len(title) > 256:
            return await interaction.response.send_message("The title can be at most 256 characters.",
                                                           ephemeral=True)
        if ping and not (ping.mentionable or perms.mention_everyone):  # what the BOT is allowed to ping
            return await interaction.response.send_message(
                "I can't ping that role. Make it mentionable or give me the Mention Everyone permission.",
                ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        file = None
        embed = discord.Embed(title=title or "Partnership", description=text, color=discord.Color.gold(),
                              timestamp=datetime.now(timezone.utc))
        try:
            if image is not None:
                data, ext = await read_attachment(image)
            elif image_url:
                data, ext = await read_image_url(image_url.strip())
            else:
                data = None
            if data:
                name = f"partnership.{ext}"
                file = discord.File(io.BytesIO(data), filename=name)
                embed.set_image(url=f"attachment://{name}")
        except ValueError as e:
            return await interaction.followup.send(str(e), ephemeral=True)

        try:
            # Only the chosen role can be pinged. Mentions typed inside the text never ping anyone.
            await channel.send(content=ping.mention if ping else None, embed=embed,
                               **({"file": file} if file else {}),
                               allowed_mentions=discord.AllowedMentions(roles=[ping] if ping else []))
        except discord.HTTPException:
            return await interaction.followup.send(
                f"I couldn't post in {channel.mention}. Check my permissions there.", ephemeral=True)

        await self.bot.db.execute("INSERT OR REPLACE INTO partnership_channel (guild_id, channel_id) VALUES (?, ?)",
                                  (guild.id, channel.id))
        await self.bot.db.commit()
        await interaction.followup.send(f"Announcement posted in {channel.mention}.", ephemeral=True)
        await self.bot.log(guild, "Partnership posted", f"In {channel.mention} by {interaction.user}",
                           discord.Color.gold())


async def setup(bot: commands.Bot):
    await bot.add_cog(Partnership(bot))
