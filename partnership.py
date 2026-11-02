from datetime import datetime, timezone
import io
import re

import discord
from discord import app_commands
from discord.ext import commands

from greetings import read_attachment, read_image_url  # same image rules as /welcome

MAX_MESSAGE = 4000
INVITE_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:discord\.gg|discord(?:app)?\.com/invite)/([A-Za-z0-9-]{2,32})", re.I)
CDN_HOSTS = ("cdn.discordapp.com", "media.discordapp.net", "images-ext-1.discordapp.net", "images-ext-2.discordapp.net")


def parse_invite(text: str) -> str | None:
    m = INVITE_RE.search(text or "")
    return m.group(1) if m else None


def is_cdn_link(url: str) -> bool:
    m = re.match(r"^https?://([^/]+)/", url.strip() + "/", re.I)
    return bool(m) and m.group(1).lower() in CDN_HOSTS


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
        title="Title (default: Partnership)",
        invite="A Discord invite link to show with a Join button (discord.gg/...)",
        invite2="A second invite link (optional)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def partnership(self, interaction: discord.Interaction, message: str,
                          channel: discord.TextChannel | None = None, ping: discord.Role | None = None,
                          image: discord.Attachment | None = None, image_url: str | None = None,
                          title: str | None = None, invite: str | None = None, invite2: str | None = None):
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

        codes = []
        for raw in (invite, invite2):
            if raw:
                code = parse_invite(raw)
                if not code:
                    return await interaction.response.send_message(
                        "That doesn't look like a Discord invite. Use a link like `discord.gg/abc123`.", ephemeral=True)
                codes.append(code)

        await interaction.response.defer(ephemeral=True)
        file = None
        embed = discord.Embed(title=title or "Partnership", description=text, color=discord.Color.gold(),
                              timestamp=datetime.now(timezone.utc))
        try:
            if image is not None:
                data, ext = await read_attachment(image)
            elif image_url and is_cdn_link(image_url):
                data = None
                embed.set_image(url=image_url.strip())  # Discord shows its own CDN links directly
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

        view = None
        for i, code in enumerate(codes):
            try:
                inv = await self.bot.fetch_invite(code, with_counts=True)
            except discord.NotFound:
                return await interaction.followup.send(f"The invite `{code}` is invalid or expired.", ephemeral=True)
            except discord.HTTPException:
                return await interaction.followup.send("I couldn't check that invite. Try again in a moment.", ephemeral=True)
            name = inv.guild.name if inv.guild else "Discord server"
            count = f" ({inv.approximate_member_count:,} members)" if inv.approximate_member_count else ""
            embed.add_field(name=name, value=f"https://discord.gg/{inv.code}{count}", inline=False)
            if i == 0 and inv.guild and inv.guild.icon and not (file or embed.image.url):
                embed.set_thumbnail(url=inv.guild.icon.url)
            view = view or discord.ui.View()
            view.add_item(discord.ui.Button(label=f"Join {name}"[:80], url=f"https://discord.gg/{inv.code}"))

        try:
            # Only the chosen role can be pinged. Mentions typed inside the text never ping anyone.
            await channel.send(content=ping.mention if ping else None, embed=embed,
                               **({"file": file} if file else {}), **({"view": view} if view else {}),
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
