import io
import re
import time
from collections import defaultdict, deque

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from premium import premium_only

CUSTOM_EMOJI_RE = re.compile(r"<(a?):([A-Za-z0-9_]{2,32}):(\d+)>")
URL_RE = re.compile(r"^https?://", re.I)
FETCH_TIMEOUT = aiohttp.ClientTimeout(total=15)
MAX_EMOJI_BYTES = 256 * 1024
SNIPE_MAX_AGE = 15 * 60       # a sniped message stops showing after 15 minutes
STICKY_COOLDOWN = 5           # seconds between reposts of a sticky message
AUTORESPONDER_LIMIT = 15      # per guild


def parse_emoji(text: str) -> tuple[bool, str, int] | None:
    """Return (animated, name, id) from a custom emoji like <a:name:123>, or None."""
    m = CUSTOM_EMOJI_RE.search(text.strip())
    if not m:
        return None
    return bool(m.group(1)), m.group(2), int(m.group(3))


class PremiumFeatures(commands.Cog):
    """Premium-only commands: AFK, snipe, steal-emoji, autoresponders and sticky messages."""

    autoresponder = app_commands.Group(name="autoresponder", description="Auto-reply to trigger words (premium)",
                                       guild_only=True)
    sticky = app_commands.Group(name="sticky", description="Keep a message pinned to the bottom of a channel (premium)",
                                guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.afk: dict[tuple[int, int], tuple[str, float]] = {}
        self.snipes: dict[int, dict] = {}                    # channel_id -> {author, content, at}
        self.responders: dict[int, list[tuple[str, str]]] = defaultdict(list)   # guild_id -> [(trigger, reply)]
        self.stickies: dict[int, dict] = {}                  # channel_id -> {text, message_id, last}

    async def cog_load(self):
        db = self.bot.db
        await db.execute("CREATE TABLE IF NOT EXISTS afk_status (guild_id INTEGER, user_id INTEGER, reason TEXT, "
                         "set_at INTEGER, PRIMARY KEY (guild_id, user_id))")
        await db.execute("CREATE TABLE IF NOT EXISTS autoresponders (guild_id INTEGER, trigger TEXT, reply TEXT, "
                         "PRIMARY KEY (guild_id, trigger))")
        await db.execute("CREATE TABLE IF NOT EXISTS sticky_messages (channel_id INTEGER PRIMARY KEY, guild_id INTEGER, "
                         "text TEXT, message_id INTEGER)")
        await db.commit()
        async with db.execute("SELECT guild_id, user_id, reason, set_at FROM afk_status") as cur:
            for gid, uid, reason, at in await cur.fetchall():
                self.afk[(gid, uid)] = (reason, at)
        async with db.execute("SELECT guild_id, trigger, reply FROM autoresponders") as cur:
            for gid, trig, reply in await cur.fetchall():
                self.responders[gid].append((trig, reply))
        async with db.execute("SELECT channel_id, guild_id, text, message_id FROM sticky_messages") as cur:
            for cid, gid, text, mid in await cur.fetchall():
                self.stickies[cid] = {"guild_id": gid, "text": text, "message_id": mid, "last": 0.0}

    # ------------------------------------------------------------------ AFK

    @app_commands.command(name="afk", description="Mark yourself AFK; I'll reply for you when someone mentions you (premium)")
    @app_commands.describe(reason="What to show people who mention you (optional)")
    @premium_only()
    async def afk_cmd(self, interaction: discord.Interaction, reason: app_commands.Range[str, 0, 200] = ""):
        self.afk[(interaction.guild_id, interaction.user.id)] = (reason or "AFK", time.time())
        await self.bot.db.execute(
            "INSERT OR REPLACE INTO afk_status (guild_id, user_id, reason, set_at) VALUES (?,?,?,?)",
            (interaction.guild_id, interaction.user.id, reason or "AFK", int(time.time())))
        await self.bot.db.commit()
        await interaction.response.send_message(f"You're now AFK: {reason or 'AFK'}", ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        key = (message.guild.id, message.author.id)
        if key in self.afk:
            del self.afk[key]
            await self.bot.db.execute("DELETE FROM afk_status WHERE guild_id=? AND user_id=?", key)
            await self.bot.db.commit()
            try:
                await message.reply("Welcome back, I've removed your AFK status.", mention_author=False)
            except discord.HTTPException:
                pass
        for member in message.mentions[:3]:
            info = self.afk.get((message.guild.id, member.id))
            if info and member.id != message.author.id:
                reason, at = info
                try:
                    await message.reply(f"{member.display_name} is AFK: {reason} (since <t:{int(at)}:R>)",
                                        mention_author=False, allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    pass

        await self.handle_sticky(message)
        await self.handle_autoresponder(message)

    # ------------------------------------------------------------------ snipe

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        self.snipes[message.channel.id] = {"author": message.author, "content": message.content or "*(no text — an embed, image or sticker)*",
                                           "at": time.time()}

    @app_commands.command(name="snipe", description="Show the last deleted message in this channel (premium)")
    @premium_only()
    async def snipe(self, interaction: discord.Interaction):
        info = self.snipes.get(interaction.channel_id)
        if not info or time.time() - info["at"] > SNIPE_MAX_AGE:
            return await interaction.response.send_message("Nothing recent to snipe here.", ephemeral=True)
        embed = discord.Embed(description=info["content"][:4000], color=discord.Color.greyple(),
                              timestamp=discord.utils.utcnow())
        embed.set_author(name=str(info["author"]), icon_url=info["author"].display_avatar.url)
        embed.set_footer(text="Deleted message")
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------ steal an emoji

    @app_commands.command(name="stealemoji", description="Add a custom emoji from another server to this one (premium)")
    @app_commands.describe(emoji="A custom emoji like <:name:123> (paste it, don't type its name)",
                           name="Name to give it here (only needed for a raw image link)")
    @app_commands.guild_only()
    @premium_only()
    @app_commands.checks.bot_has_permissions(manage_emojis=True)
    async def stealemoji(self, interaction: discord.Interaction, emoji: str, name: app_commands.Range[str, 2, 32] | None = None):
        parsed = parse_emoji(emoji)
        if parsed:
            animated, ename, eid = parsed
            url = f"https://cdn.discordapp.com/emojis/{eid}.{'gif' if animated else 'png'}"
            ename = name or ename
        elif URL_RE.match(emoji.strip()) and name:
            url, ename = emoji.strip(), name
        elif URL_RE.match(emoji.strip()):
            return await interaction.response.send_message("Give a `name` too when using a raw image link.", ephemeral=True)
        else:
            return await interaction.response.send_message(
                "Paste a custom emoji (like `<:name:123456789012345678>`) or an image link with a `name`.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        try:
            async with aiohttp.ClientSession(timeout=FETCH_TIMEOUT) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return await interaction.followup.send("I couldn't download that emoji.", ephemeral=True)
                    data = await resp.read()
        except (aiohttp.ClientError, TimeoutError):
            return await interaction.followup.send("I couldn't download that emoji.", ephemeral=True)
        if len(data) > MAX_EMOJI_BYTES:
            return await interaction.followup.send("That emoji's image is too large.", ephemeral=True)
        try:
            new_emoji = await interaction.guild.create_custom_emoji(
                name=re.sub(r"[^A-Za-z0-9_]", "_", ename)[:32] or "emoji", image=data,
                reason=f"Added by {interaction.user} with /stealemoji")
        except discord.HTTPException as e:
            return await interaction.followup.send(f"I couldn't add that emoji: {e.text if hasattr(e, 'text') else e}",
                                                   ephemeral=True)
        await interaction.followup.send(f"Added {new_emoji}.", ephemeral=True)

    # ------------------------------------------------------------------ autoresponders

    @autoresponder.command(name="add", description="Make me auto-reply when someone says a word or phrase")
    @premium_only()
    async def autoresponder_add(self, interaction: discord.Interaction, trigger: app_commands.Range[str, 1, 80],
                                reply: app_commands.Range[str, 1, 500]):
        gid = interaction.guild_id
        trig = trigger.lower().strip()
        existing = {t for t, _ in self.responders[gid]}
        if trig not in existing and len(existing) >= AUTORESPONDER_LIMIT:
            return await interaction.response.send_message(
                f"You can have at most {AUTORESPONDER_LIMIT} autoresponders. Remove one first.", ephemeral=True)
        await self.bot.db.execute("INSERT OR REPLACE INTO autoresponders (guild_id, trigger, reply) VALUES (?,?,?)",
                                  (gid, trig, reply.replace("\\n", "\n")))
        await self.bot.db.commit()
        self.responders[gid] = [(t, r) for t, r in self.responders[gid] if t != trig] + [(trig, reply.replace("\\n", "\n"))]
        await interaction.response.send_message(f"Added. I'll reply when someone says \"{trig}\".", ephemeral=True)

    @autoresponder.command(name="remove", description="Remove an autoresponder")
    @premium_only()
    async def autoresponder_remove(self, interaction: discord.Interaction, trigger: str):
        gid, trig = interaction.guild_id, trigger.lower().strip()
        await self.bot.db.execute("DELETE FROM autoresponders WHERE guild_id=? AND trigger=?", (gid, trig))
        await self.bot.db.commit()
        self.responders[gid] = [(t, r) for t, r in self.responders[gid] if t != trig]
        await interaction.response.send_message("Removed (if it existed).", ephemeral=True)

    @autoresponder.command(name="list", description="List this server's autoresponders")
    @premium_only()
    async def autoresponder_list(self, interaction: discord.Interaction):
        rows = self.responders.get(interaction.guild_id, [])
        if not rows:
            return await interaction.response.send_message("No autoresponders set up yet.", ephemeral=True)
        text = "\n".join(f"**{t}** → {r[:80]}" for t, r in rows)
        await interaction.response.send_message(text[:1900], ephemeral=True)

    async def handle_autoresponder(self, message: discord.Message):
        rows = self.responders.get(message.guild.id)
        if not rows:
            return
        content = message.content.lower()
        for trig, reply in rows:
            if trig in content:
                try:
                    await message.reply(reply[:2000], mention_author=False,
                                        allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    pass
                return

    # ------------------------------------------------------------------ sticky messages

    @sticky.command(name="set", description="Keep a message pinned to the bottom of a channel")
    @premium_only()
    @app_commands.checks.bot_has_permissions(send_messages=True, manage_messages=True)
    async def sticky_set(self, interaction: discord.Interaction, text: app_commands.Range[str, 1, 1500]):
        channel = interaction.channel
        text = text.replace("\\n", "\n")
        old = self.stickies.get(channel.id)
        if old and old.get("message_id"):
            try:
                msg = await channel.fetch_message(old["message_id"])
                await msg.delete()
            except discord.HTTPException:
                pass
        sent = await channel.send(f"📌 {text}")
        self.stickies[channel.id] = {"guild_id": interaction.guild_id, "text": text, "message_id": sent.id, "last": time.time()}
        await self.bot.db.execute(
            "INSERT OR REPLACE INTO sticky_messages (channel_id, guild_id, text, message_id) VALUES (?,?,?,?)",
            (channel.id, interaction.guild_id, text, sent.id))
        await self.bot.db.commit()
        await interaction.response.send_message("Sticky message set.", ephemeral=True)

    @sticky.command(name="off", description="Remove the sticky message from this channel")
    @premium_only()
    async def sticky_off(self, interaction: discord.Interaction):
        channel = interaction.channel
        old = self.stickies.pop(channel.id, None)
        await self.bot.db.execute("DELETE FROM sticky_messages WHERE channel_id=?", (channel.id,))
        await self.bot.db.commit()
        if old and old.get("message_id"):
            try:
                msg = await channel.fetch_message(old["message_id"])
                await msg.delete()
            except discord.HTTPException:
                pass
        await interaction.response.send_message("Sticky message removed.", ephemeral=True)

    async def handle_sticky(self, message: discord.Message):
        sticky = self.stickies.get(message.channel.id)
        if not sticky:
            return
        now = time.time()
        if now - sticky["last"] < STICKY_COOLDOWN:
            return
        sticky["last"] = now
        if sticky.get("message_id"):
            try:
                old = await message.channel.fetch_message(sticky["message_id"])
                await old.delete()
            except discord.HTTPException:
                pass
        try:
            sent = await message.channel.send(f"📌 {sticky['text']}")
        except discord.HTTPException:
            return
        sticky["message_id"] = sent.id
        await self.bot.db.execute("UPDATE sticky_messages SET message_id=? WHERE channel_id=?",
                                  (sent.id, message.channel.id))
        await self.bot.db.commit()


async def setup(bot: commands.Bot):
    await bot.add_cog(PremiumFeatures(bot))
