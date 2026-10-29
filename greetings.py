import asyncio
import io
import time
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

MAX_IMAGE_BYTES = 8 * 1024 * 1024
FETCH_TIMEOUT = aiohttp.ClientTimeout(total=15)
IMAGE_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
RAID_ACCOUNT_AGE = timedelta(days=7)  # same rule as the anti-raid kick in bot.py

KINDS = {
    "welcome": {
        "title": "Welcome to {server}!",
        "message": "Hey {user}, welcome to **{server}**! You are member #{count}.",
        "color": discord.Color.green(),
    },
    "bye": {
        "title": "Goodbye!",
        "message": "**{name}** has left **{server}**. We're now {count} members.",
        "color": discord.Color.red(),
    },
    "booster": {
        "title": "New Server Booster!",
        "message": "Thank you {user} for boosting **{server}**! 💜",
        "color": discord.Color(0xF47FFF),
    },
}
PLACEHOLDERS = "`{user}` (mention), `{name}`, `{server}`, `{count}` (member count)"


def render(text: str, member: discord.abc.User, guild: discord.Guild) -> str:
    """Fill in the placeholders. Plain replace, so stray braces in the text can't crash it."""
    name = discord.utils.escape_markdown(discord.utils.escape_mentions(member.display_name))
    server = discord.utils.escape_markdown(discord.utils.escape_mentions(guild.name))
    return (text.replace("{user}", member.mention).replace("{name}", name)
            .replace("{server}", server).replace("{count}", str(guild.member_count or 0)))


async def read_image_url(url: str) -> tuple[bytes, str]:
    """Download an image from a link. Raises ValueError with a message that is safe to show the user."""
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("The image link must start with http:// or https://")
    try:
        async with aiohttp.ClientSession(timeout=FETCH_TIMEOUT) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise ValueError(f"I couldn't download that link (HTTP {resp.status}). "
                                     "Discord links expire, so try uploading the image instead.")
                ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
                if ctype not in IMAGE_TYPES:
                    raise ValueError("That link isn't a PNG, JPG, GIF or WebP image.")
                data = await resp.content.read(MAX_IMAGE_BYTES + 1)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        raise ValueError("I couldn't download that link. Check it and try again.")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("That image is bigger than 8 MB.")
    return data, IMAGE_TYPES[ctype]


async def read_attachment(attachment: discord.Attachment) -> tuple[bytes, str]:
    ctype = (attachment.content_type or "").split(";")[0].strip().lower()
    if ctype not in IMAGE_TYPES:
        raise ValueError("The attachment must be a PNG, JPG, GIF or WebP image.")
    if attachment.size > MAX_IMAGE_BYTES:
        raise ValueError("That image is bigger than 8 MB.")
    return await attachment.read(), IMAGE_TYPES[ctype]


class GreetingBase(commands.GroupCog):
    """Shared logic for /welcome, /bye and /booster. Each subclass only sets `kind` and its group name."""

    kind = ""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.db.execute(
            """CREATE TABLE IF NOT EXISTS greetings (
                guild_id INTEGER, kind TEXT, channel_id INTEGER, title TEXT, message TEXT,
                image BLOB, image_ext TEXT, PRIMARY KEY (guild_id, kind))""")
        await self.bot.db.commit()

    # ---------- storage ----------

    async def load(self, guild_id: int):
        async with self.bot.db.execute(
                "SELECT channel_id, title, message, image, image_ext FROM greetings WHERE guild_id=? AND kind=?",
                (guild_id, self.kind)) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return {"channel_id": row[0], "title": row[1], "message": row[2], "image": row[3], "image_ext": row[4]}

    # ---------- building and sending ----------

    def build(self, cfg: dict, member: discord.abc.User, guild: discord.Guild):
        defaults = KINDS[self.kind]
        embed = discord.Embed(title=render(cfg["title"] or defaults["title"], member, guild)[:256],
                              description=render(cfg["message"] or defaults["message"], member, guild)[:4000],
                              color=defaults["color"], timestamp=datetime.now(timezone.utc))
        embed.set_thumbnail(url=member.display_avatar.url)
        file = None
        if cfg["image"]:
            name = f"{self.kind}.{cfg['image_ext'] or 'png'}"
            file = discord.File(io.BytesIO(cfg["image"]), filename=name)
            embed.set_image(url=f"attachment://{name}")
        return embed, file

    async def post(self, member: discord.Member):
        cfg = await self.load(member.guild.id)
        if not cfg or not cfg["channel_id"]:
            return
        channel = member.guild.get_channel(cfg["channel_id"])
        if channel is None:
            return
        embed, file = self.build(cfg, member, member.guild)
        try:
            await channel.send(embed=embed, **({"file": file} if file else {}),
                               allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass  # missing permissions or channel deleted

    def in_raid(self, member: discord.Member) -> bool:
        """True for a brand-new account while raid mode is on (those get kicked, so don't greet them)."""
        if time.monotonic() >= getattr(self.bot, "raid_until", 0.0):
            return False
        return datetime.now(timezone.utc) - member.created_at < RAID_ACCOUNT_AGE

    # ---------- command bodies (the subclasses attach them to slash commands) ----------

    async def do_setup(self, interaction: discord.Interaction, channel: discord.TextChannel,
                       title: str | None, message: str | None, image: discord.Attachment | None,
                       image_url: str | None, clear_image: bool):
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links and perms.attach_files):
            return await interaction.response.send_message(
                f"I need View Channel, Send Messages, Embed Links and Attach Files in {channel.mention}.",
                ephemeral=True)
        if title and len(title) > 256:
            return await interaction.response.send_message("The title can be at most 256 characters.", ephemeral=True)
        if message and len(message) > 2000:
            return await interaction.response.send_message("The message can be at most 2000 characters.",
                                                           ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        old = await self.load(interaction.guild_id) or {}
        data, ext = old.get("image"), old.get("image_ext")
        try:
            if image is not None:
                data, ext = await read_attachment(image)
            elif image_url:
                data, ext = await read_image_url(image_url.strip())
            elif clear_image:
                data, ext = None, None
        except ValueError as e:
            return await interaction.followup.send(str(e), ephemeral=True)

        await self.bot.db.execute(
            "INSERT OR REPLACE INTO greetings (guild_id, kind, channel_id, title, message, image, image_ext) "
            "VALUES (?,?,?,?,?,?,?)",
            (interaction.guild_id, self.kind, channel.id, title or old.get("title"),
             message or old.get("message"), data, ext))
        await self.bot.db.commit()
        await interaction.followup.send(
            f"**{self.kind.title()}** messages will be posted in {channel.mention}. "
            f"Use `/{self.kind} preview` to see how it looks.\nPlaceholders: {PLACEHOLDERS}", ephemeral=True)
        await self.bot.log(interaction.guild, f"{self.kind.title()} set up",
                           f"Channel {channel.mention} by {interaction.user}", discord.Color.green())

    async def do_off(self, interaction: discord.Interaction):
        cur = await self.bot.db.execute("DELETE FROM greetings WHERE guild_id=? AND kind=?",
                                        (interaction.guild_id, self.kind))
        await self.bot.db.commit()
        text = (f"{self.kind.title()} messages turned off and the saved settings removed." if cur.rowcount
                else f"{self.kind.title()} messages weren't set up.")
        await interaction.response.send_message(text, ephemeral=True)

    async def do_preview(self, interaction: discord.Interaction):
        cfg = await self.load(interaction.guild_id)
        if not cfg:
            return await interaction.response.send_message(
                f"Not set up yet. Run `/{self.kind} setup` first.", ephemeral=True)
        embed, file = self.build(cfg, interaction.user, interaction.guild)
        await interaction.response.send_message(embed=embed, ephemeral=True,
                                                **({"file": file} if file else {}))

    async def do_status(self, interaction: discord.Interaction):
        cfg = await self.load(interaction.guild_id)
        if not cfg:
            return await interaction.response.send_message(
                f"{self.kind.title()} messages are **off**. Use `/{self.kind} setup`.", ephemeral=True)
        channel = interaction.guild.get_channel(cfg["channel_id"])
        defaults = KINDS[self.kind]
        await interaction.response.send_message(
            f"**{self.kind.title()}** is **on** in {channel.mention if channel else '(deleted channel)'}\n"
            f"Title: {cfg['title'] or defaults['title']}\n"
            f"Message: {cfg['message'] or defaults['message']}\n"
            f"Image: {'yes' if cfg['image'] else 'none'}", ephemeral=True)


SETUP_DESC = dict(
    channel="Where to post it",
    title="Embed title (placeholders work)",
    message="Embed text (placeholders: {user} {name} {server} {count})",
    image="Upload an image to show in the embed",
    image_url="Or paste a link to an image (a Discord CDN link works)",
    clear_image="Remove the saved image",
)


class Welcome(GreetingBase, group_name="welcome", group_description="Greet new members"):
    kind = "welcome"

    @app_commands.command(name="setup", description="Set up the welcome message for new members")
    @app_commands.describe(**SETUP_DESC)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup_cmd(self, interaction: discord.Interaction, channel: discord.TextChannel,
                        title: str | None = None, message: str | None = None,
                        image: discord.Attachment | None = None, image_url: str | None = None,
                        clear_image: bool = False):
        await self.do_setup(interaction, channel, title, message, image, image_url, clear_image)

    @app_commands.command(name="off", description="Turn the welcome message off")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def off_cmd(self, interaction: discord.Interaction):
        await self.do_off(interaction)

    @app_commands.command(name="preview", description="See the welcome message (only you see it)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def preview_cmd(self, interaction: discord.Interaction):
        await self.do_preview(interaction)

    @app_commands.command(name="status", description="Show the welcome settings")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def status_cmd(self, interaction: discord.Interaction):
        await self.do_status(interaction)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot or self.in_raid(member):
            return
        await self.post(member)


class Bye(GreetingBase, group_name="bye", group_description="Say goodbye when members leave"):
    kind = "bye"

    @app_commands.command(name="setup", description="Set up the goodbye message for members who leave")
    @app_commands.describe(**SETUP_DESC)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup_cmd(self, interaction: discord.Interaction, channel: discord.TextChannel,
                        title: str | None = None, message: str | None = None,
                        image: discord.Attachment | None = None, image_url: str | None = None,
                        clear_image: bool = False):
        await self.do_setup(interaction, channel, title, message, image, image_url, clear_image)

    @app_commands.command(name="off", description="Turn the goodbye message off")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def off_cmd(self, interaction: discord.Interaction):
        await self.do_off(interaction)

    @app_commands.command(name="preview", description="See the goodbye message (only you see it)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def preview_cmd(self, interaction: discord.Interaction):
        await self.do_preview(interaction)

    @app_commands.command(name="status", description="Show the goodbye settings")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def status_cmd(self, interaction: discord.Interaction):
        await self.do_status(interaction)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if member.bot or self.in_raid(member):
            return
        await self.post(member)


class Booster(GreetingBase, group_name="booster", group_description="Thank members who boost the server"):
    kind = "booster"

    @app_commands.command(name="setup", description="Set up the thank-you message for server boosters")
    @app_commands.describe(**SETUP_DESC)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup_cmd(self, interaction: discord.Interaction, channel: discord.TextChannel,
                        title: str | None = None, message: str | None = None,
                        image: discord.Attachment | None = None, image_url: str | None = None,
                        clear_image: bool = False):
        await self.do_setup(interaction, channel, title, message, image, image_url, clear_image)

    @app_commands.command(name="off", description="Turn the booster message off")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def off_cmd(self, interaction: discord.Interaction):
        await self.do_off(interaction)

    @app_commands.command(name="preview", description="See the booster message (only you see it)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def preview_cmd(self, interaction: discord.Interaction):
        await self.do_preview(interaction)

    @app_commands.command(name="status", description="Show the booster settings")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def status_cmd(self, interaction: discord.Interaction):
        await self.do_status(interaction)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        # premium_since goes from None to a date the moment someone starts boosting
        if before.premium_since is None and after.premium_since is not None:
            await self.post(after)


async def setup(bot: commands.Bot):
    await bot.add_cog(Welcome(bot))
    await bot.add_cog(Bye(bot))
    await bot.add_cog(Booster(bot))
