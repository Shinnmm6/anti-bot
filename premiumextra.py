import asyncio
import io
import re

import discord
from discord import app_commands
from discord.ext import commands

from premium import premium_only

MAX_FILE_CHARS = 7_000_000
NUMS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
MESSAGE_LINK_RE = re.compile(r"discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)")
HEX_RE = re.compile(r"^[0-9a-fA-F]{6}$")


def parse_color(text: str):
    text = text.strip().lstrip("#")
    if text.lower() == "random":
        return discord.Color.random()
    if HEX_RE.fullmatch(text):
        return discord.Color(int(text, 16))
    name = text.lower().replace(" ", "_")
    attr = getattr(discord.Color, name, None)
    if callable(attr):
        try:
            return attr()
        except TypeError:
            return None
    return None


def parse_options(text: str) -> list[str]:
    return [o.strip() for o in text.split(",") if o.strip()][:10]


def parse_message_link(text: str) -> tuple[int, int, int] | None:
    m = MESSAGE_LINK_RE.search(text.strip())
    if not m:
        return None
    gid, cid, mid = (int(g) for g in m.groups())
    return gid, cid, mid


class PremiumExtra(commands.Cog):
    """Five more premium commands: embed, archive, poll, bio and spotlight."""

    bio = app_commands.Group(name="bio", description="A short personal bio shown to others (premium)", guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.db.execute(
            "CREATE TABLE IF NOT EXISTS bios (guild_id INTEGER, user_id INTEGER, text TEXT, PRIMARY KEY (guild_id, user_id))")
        await self.bot.db.commit()

    # ------------------------------------------------------------------ embed

    @app_commands.command(name="embed", description="Post a custom embed as the bot (premium)")
    @app_commands.describe(description="The embed's text (type \\n for a new line)",
                           channel="Where to post it (default: here)", title="Optional title",
                           color="Hex like ffcc00, a color name like gold, or random",
                           image_url="A direct image link to show", footer="Optional small text at the bottom")
    @app_commands.guild_only()
    @premium_only()
    async def embed_cmd(self, interaction: discord.Interaction, description: app_commands.Range[str, 1, 4000],
                        channel: discord.TextChannel | None = None, title: app_commands.Range[str, 1, 256] | None = None,
                        color: str | None = None, image_url: str | None = None,
                        footer: app_commands.Range[str, 1, 2048] | None = None):
        channel = channel or interaction.channel
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return await interaction.response.send_message(
                f"I need View Channel, Send Messages and Embed Links in {channel.mention}.", ephemeral=True)
        col = discord.Color.blurple()
        if color:
            parsed = parse_color(color)
            if parsed is None:
                return await interaction.response.send_message(
                    "I couldn't understand that color. Try a hex code like `ffcc00`, a name like `gold`, or `random`.",
                    ephemeral=True)
            col = parsed
        embed = discord.Embed(title=title, description=description.replace("\\n", "\n"), color=col)
        if image_url:
            if not image_url.strip().lower().startswith(("http://", "https://")):
                return await interaction.response.send_message("The image link must start with http:// or https://",
                                                               ephemeral=True)
            embed.set_image(url=image_url.strip())
        if footer:
            embed.set_footer(text=footer)
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as e:
            return await interaction.response.send_message(f"I couldn't post that: {e}", ephemeral=True)
        await interaction.response.send_message(f"Posted in {channel.mention}.", ephemeral=True)

    # ------------------------------------------------------------------ archive

    @app_commands.command(name="archive", description="Export recent messages in this channel to a text file (premium)")
    @app_commands.describe(limit="How many recent messages to include (max 500)")
    @app_commands.guild_only()
    @premium_only()
    @app_commands.checks.bot_has_permissions(read_message_history=True)
    async def archive(self, interaction: discord.Interaction, limit: app_commands.Range[int, 1, 500] = 100):
        await interaction.response.defer(ephemeral=True)
        lines = []
        async for m in interaction.channel.history(limit=limit):
            ts = m.created_at.strftime("%Y-%m-%d %H:%M UTC")
            content = m.content or ("[attachment/embed, no text]" if (m.attachments or m.embeds) else "[no text]")
            lines.append(f"[{ts}] {m.author}: {content}")
        lines.reverse()
        text = "\n".join(lines) or "No messages found."
        data = text.encode("utf-8")[:MAX_FILE_CHARS]
        file = discord.File(io.BytesIO(data), filename=f"archive-{interaction.channel.name}.txt")
        await interaction.followup.send(f"{len(lines)} message(s) archived.", file=file, ephemeral=True)

    # ------------------------------------------------------------------ poll

    @app_commands.command(name="poll", description="Start a poll with up to 10 options (premium)")
    @app_commands.describe(question="The poll question", options="Separate options with commas (2 to 10)",
                           minutes="Auto-announce the winner after this many minutes (optional)")
    @app_commands.guild_only()
    @premium_only()
    @app_commands.checks.bot_has_permissions(add_reactions=True, embed_links=True)
    async def poll(self, interaction: discord.Interaction, question: app_commands.Range[str, 1, 256], options: str,
                   minutes: app_commands.Range[int, 1, 10080] | None = None):
        opts = parse_options(options)
        if len(opts) < 2:
            return await interaction.response.send_message(
                "Give at least two options, separated by commas.", ephemeral=True)
        desc = "\n".join(f"{NUMS[i]} {o}" for i, o in enumerate(opts))
        embed = discord.Embed(title=f"📊 {question}", description=desc, color=discord.Color.blurple())
        footer = f"Poll by {interaction.user.display_name}"
        if minutes:
            footer += f" • closes in {minutes} minute(s)"
        embed.set_footer(text=footer)
        await interaction.response.send_message(embed=embed)
        message = await interaction.original_response()
        for i in range(len(opts)):
            try:
                await message.add_reaction(NUMS[i])
            except discord.HTTPException:
                pass
        if minutes:
            asyncio.create_task(self.close_poll(message, opts, minutes * 60))

    async def close_poll(self, message: discord.Message, opts: list[str], delay: int):
        await asyncio.sleep(delay)
        try:
            message = await message.channel.fetch_message(message.id)
        except discord.HTTPException:
            return
        counts = []
        for i, o in enumerate(opts):
            reaction = discord.utils.get(message.reactions, emoji=NUMS[i])
            counts.append((o, max((reaction.count - 1), 0) if reaction else 0))
        counts.sort(key=lambda x: -x[1])
        lines = "\n".join(f"{o}: {c}" for o, c in counts)
        top = counts[0]
        embed = discord.Embed(title="📊 Poll closed",
                              description=f"Winner: **{top[0]}** ({top[1]} vote(s))\n\n{lines}",
                              color=discord.Color.green())
        try:
            await message.channel.send(embed=embed)
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------------ bio

    @bio.command(name="set", description="Set your bio (shown with /bio show)")
    @premium_only()
    async def bio_set(self, interaction: discord.Interaction, text: app_commands.Range[str, 0, 300] = ""):
        await self.bot.db.execute("INSERT OR REPLACE INTO bios (guild_id, user_id, text) VALUES (?,?,?)",
                                  (interaction.guild_id, interaction.user.id, text.replace("\\n", "\n")))
        await self.bot.db.commit()
        await interaction.response.send_message("Bio saved." if text else "Bio cleared.", ephemeral=True)

    @bio.command(name="show", description="Show a member's bio")
    async def bio_show(self, interaction: discord.Interaction, member: discord.Member | None = None):
        member = member or interaction.user
        async with self.bot.db.execute("SELECT text FROM bios WHERE guild_id=? AND user_id=?",
                                       (interaction.guild_id, member.id)) as cur:
            row = await cur.fetchone()
        if not row or not row[0]:
            return await interaction.response.send_message(f"{member.display_name} hasn't set a bio.", ephemeral=True,
                                                           allowed_mentions=discord.AllowedMentions.none())
        embed = discord.Embed(description=row[0], color=discord.Color.blurple())
        embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------ spotlight

    @app_commands.command(name="spotlight", description="Repost a great message into a highlights channel (premium)")
    @app_commands.describe(message_link="Right-click the message → Copy Message Link",
                           channel="Which channel to post the spotlight in")
    @app_commands.guild_only()
    @premium_only()
    async def spotlight(self, interaction: discord.Interaction, message_link: str, channel: discord.TextChannel):
        parsed = parse_message_link(message_link)
        if not parsed or parsed[0] != interaction.guild_id:
            return await interaction.response.send_message(
                "That doesn't look like a message link from this server. Right-click a message and choose "
                "Copy Message Link.", ephemeral=True)
        _, cid, mid = parsed
        src = interaction.guild.get_channel(cid)
        if src is None:
            return await interaction.response.send_message("I can't find that channel.", ephemeral=True)
        try:
            msg = await src.fetch_message(mid)
        except discord.HTTPException:
            return await interaction.response.send_message("I couldn't find that message.", ephemeral=True)
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return await interaction.response.send_message(
                f"I need View Channel, Send Messages and Embed Links in {channel.mention}.", ephemeral=True)
        embed = discord.Embed(description=(msg.content or "*(no text)*")[:4000], color=discord.Color.gold(),
                              timestamp=msg.created_at)
        embed.set_author(name=str(msg.author), icon_url=msg.author.display_avatar.url)
        embed.add_field(name="Jump to message", value=f"[Click here]({msg.jump_url})")
        image = next((a for a in msg.attachments if (a.content_type or "").startswith("image")), None)
        if image:
            embed.set_image(url=image.url)
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            return await interaction.response.send_message(f"I couldn't post in {channel.mention}.", ephemeral=True)
        await interaction.response.send_message(f"Spotlighted in {channel.mention}.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(PremiumExtra(bot))
