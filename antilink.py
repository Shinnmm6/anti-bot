import re
import time
from collections import defaultdict, deque
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

DEFAULTS = {"enabled": 0, "invites": 1, "cdn": 1, "links": 1, "attachments": 0}
ESCALATE_HITS, ESCALATE_SECONDS, ESCALATE_TIMEOUT_MIN = 3, 300, 10  # 3 blocked msgs in 5 min = 10 min timeout

INVITE_RE = re.compile(
    r"(?:discord(?:app)?\.com/invite|discord\.gg|discord\.me|dsc\.gg|invite\.gg)/[\w-]+", re.I)
CDN_RE = re.compile(
    r"(?:cdn\.discordapp\.com|media\.discordapp\.net|images-ext-\d+\.discordapp\.net)", re.I)
URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>]+", re.I)
BARE_RE = re.compile(
    r"\b(?:[a-z0-9-]+\.)+(?:com|net|org|io|gg|me|co|xyz|ru|tk|ly|link|info|biz|us|uk|app|dev|site|"
    r"online|store|shop|top|club|live|tv|fm|to|cc|ws|ml|ga|cf|gq|click|cn|de|fr|br|in|ph)\b(?:/[^\s<>]*)?",
    re.I)

OPTIONS = {
    "invites": "Discord invites",
    "cdn": "Discord CDN links",
    "links": "All other links (https, www, bare domains)",
    "attachments": "File/image uploads",
}


def host_of(candidate: str) -> str:
    c = re.sub(r"^https?://", "", candidate, flags=re.I)
    c = re.sub(r"^www\.", "", c, flags=re.I)
    return re.split(r"[/?#:]", c, maxsplit=1)[0].lower().strip(".,)>")


def normalize_domain(text: str) -> str:
    return host_of(text.strip())


def safe_snippet(text: str) -> str:
    return text[:500].replace("```", "'''")


class AntiLink(commands.GroupCog, group_name="antilink",
               group_description="Block links from members who aren't trusted"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.cfg: dict[int, dict] = {}
        self.allow: dict[int, set[str]] = defaultdict(set)
        self.trusted: dict[int, set[int]] = defaultdict(set)
        self.hits: dict[tuple[int, int], deque] = defaultdict(deque)

    async def cog_load(self):
        db = self.bot.db
        await db.execute(
            """CREATE TABLE IF NOT EXISTS antilink_config (
                guild_id INTEGER PRIMARY KEY, enabled INTEGER, invites INTEGER,
                cdn INTEGER, links INTEGER, attachments INTEGER)""")
        await db.execute(
            "CREATE TABLE IF NOT EXISTS antilink_allow (guild_id INTEGER, domain TEXT, PRIMARY KEY (guild_id, domain))")
        await db.execute(
            "CREATE TABLE IF NOT EXISTS antilink_trusted (guild_id INTEGER, user_id INTEGER, PRIMARY KEY (guild_id, user_id))")
        await db.commit()
        async with db.execute("SELECT guild_id, enabled, invites, cdn, links, attachments FROM antilink_config") as cur:
            for g, en, inv, cdn, links, att in await cur.fetchall():
                self.cfg[g] = {"enabled": en, "invites": inv, "cdn": cdn, "links": links, "attachments": att}
        async with db.execute("SELECT guild_id, domain FROM antilink_allow") as cur:
            for g, d in await cur.fetchall():
                self.allow[g].add(d)
        async with db.execute("SELECT guild_id, user_id FROM antilink_trusted") as cur:
            for g, u in await cur.fetchall():
                self.trusted[g].add(u)

    async def save_cfg(self, guild_id: int):
        c = self.cfg[guild_id]
        await self.bot.db.execute(
            "INSERT OR REPLACE INTO antilink_config (guild_id, enabled, invites, cdn, links, attachments) "
            "VALUES (?,?,?,?,?,?)",
            (guild_id, c["enabled"], c["invites"], c["cdn"], c["links"], c["attachments"]))
        await self.bot.db.commit()

    # ---------- detection ----------

    def is_trusted(self, guild: discord.Guild, user_id: int) -> bool:
        if user_id == guild.owner_id or user_id in self.trusted[guild.id]:
            return True
        nuke = self.bot.get_cog("AntiNuke")  # anyone fully trusted by anti-nuke may post links too
        return bool(nuke and nuke.is_trusted(guild, user_id))

    @staticmethod
    def allowed_host(host: str, allow: set[str]) -> bool:
        return any(host == d or host.endswith("." + d) for d in allow)

    def find_violations(self, message: discord.Message, cfg: dict, allow: set[str]) -> list[str]:
        text = message.content or ""
        found = []
        if cfg["invites"] and INVITE_RE.search(text):
            found.append("Discord invites")
        if cfg["cdn"] and CDN_RE.search(text):
            found.append("Discord CDN links")
        if cfg["links"]:
            candidates = [m.group(0) for m in URL_RE.finditer(text)]
            candidates += [m.group(0) for m in BARE_RE.finditer(text)]
            if any(not self.allowed_host(host_of(c), allow) for c in candidates):
                found.append("links")
        if cfg["attachments"] and message.attachments:
            found.append("file uploads")
        return found

    async def check(self, message: discord.Message):
        if message.guild is None or message.author.bot or message.webhook_id:
            return
        cfg = self.cfg.get(message.guild.id)
        if not cfg or not cfg["enabled"]:
            return
        if self.is_trusted(message.guild, message.author.id):
            return
        reasons = self.find_violations(message, cfg, self.allow[message.guild.id])
        if not reasons:
            return

        try:
            await message.delete()
        except discord.HTTPException:
            return
        try:
            await message.channel.send(
                f"{message.author.mention} only trusted members can send {', '.join(reasons)} here.",
                delete_after=6)
        except discord.HTTPException:
            pass
        await self.bot.log(
            message.guild, "Link blocked",
            f"{message.author.mention} in {message.channel.mention}\n"
            f"**Type:** {', '.join(reasons)}\n```{safe_snippet(message.content or '(file upload)')}```",
            discord.Color.orange())

        # Repeat offenders get a short timeout
        key = (message.guild.id, message.author.id)
        now = time.monotonic()
        dq = self.hits[key]
        dq.append(now)
        while dq and now - dq[0] > ESCALATE_SECONDS:
            dq.popleft()
        if len(dq) >= ESCALATE_HITS and isinstance(message.author, discord.Member):
            dq.clear()
            try:
                await message.author.timeout(
                    timedelta(minutes=ESCALATE_TIMEOUT_MIN), reason="Anti-link: repeated blocked links")
                await self.bot.log(message.guild, "Anti-link timeout",
                                   f"{message.author.mention} timed out {ESCALATE_TIMEOUT_MIN} min for repeated links.",
                                   discord.Color.red())
            except discord.HTTPException:
                pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        await self.check(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.content != after.content:  # catches "post clean, edit in a link later"
            await self.check(after)

    # ---------- commands ----------

    async def owner_only(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or interaction.user.id != interaction.guild.owner_id:
            await interaction.response.send_message("Only the server owner can use this.", ephemeral=True)
            return False
        return True

    def get_cfg(self, guild_id: int) -> dict:
        return self.cfg.setdefault(guild_id, dict(DEFAULTS))

    @app_commands.command(name="on", description="Turn anti-link on")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def on(self, interaction: discord.Interaction):
        cfg = self.get_cfg(interaction.guild_id)
        cfg["enabled"] = 1
        await self.save_cfg(interaction.guild_id)
        await interaction.response.send_message(
            "Anti-link is **on**. Only the server owner and trusted members can send links now "
            "(admins and mods are NOT exempt unless trusted). Use `/antilink status` to review, "
            "`/antilink trust` to add people, and `/antilink allow` for safe domains like tenor.com.",
            ephemeral=True)

    @app_commands.command(name="off", description="Turn anti-link off")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def off(self, interaction: discord.Interaction):
        cfg = self.get_cfg(interaction.guild_id)
        cfg["enabled"] = 0
        await self.save_cfg(interaction.guild_id)
        await interaction.response.send_message("Anti-link is **off**.", ephemeral=True)

    @app_commands.command(name="toggle", description="Turn one blocked link type on or off")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.choices(option=[app_commands.Choice(name=v, value=k) for k, v in OPTIONS.items()])
    async def toggle(self, interaction: discord.Interaction, option: app_commands.Choice[str], enabled: bool):
        cfg = self.get_cfg(interaction.guild_id)
        cfg[option.value] = int(enabled)
        await self.save_cfg(interaction.guild_id)
        await interaction.response.send_message(
            f"**{option.name}** blocking is now {'on' if enabled else 'off'}."
            + ("" if cfg["enabled"] else " (Anti-link itself is still off. Use `/antilink on`.)"),
            ephemeral=True)

    @app_commands.command(name="allow", description="Allow a domain for everyone (e.g. tenor.com)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def allow_domain(self, interaction: discord.Interaction, domain: str):
        d = normalize_domain(domain)
        if "." not in d:
            return await interaction.response.send_message("That doesn't look like a domain.", ephemeral=True)
        await self.bot.db.execute(
            "INSERT OR IGNORE INTO antilink_allow (guild_id, domain) VALUES (?, ?)", (interaction.guild_id, d))
        await self.bot.db.commit()
        self.allow[interaction.guild_id].add(d)
        await interaction.response.send_message(f"`{d}` (and its subdomains) is now allowed.", ephemeral=True)

    @app_commands.command(name="unallow", description="Remove an allowed domain")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def unallow_domain(self, interaction: discord.Interaction, domain: str):
        d = normalize_domain(domain)
        await self.bot.db.execute(
            "DELETE FROM antilink_allow WHERE guild_id=? AND domain=?", (interaction.guild_id, d))
        await self.bot.db.commit()
        self.allow[interaction.guild_id].discard(d)
        await interaction.response.send_message(f"`{d}` is no longer allowed.", ephemeral=True)

    @app_commands.command(name="trust", description="Let a member send links (server owner only)")
    @app_commands.guild_only()
    async def trust(self, interaction: discord.Interaction, user: discord.Member):
        if not await self.owner_only(interaction):
            return
        await self.bot.db.execute(
            "INSERT OR IGNORE INTO antilink_trusted (guild_id, user_id) VALUES (?, ?)",
            (interaction.guild_id, user.id))
        await self.bot.db.commit()
        self.trusted[interaction.guild_id].add(user.id)
        await interaction.response.send_message(
            f"{user.mention} can now send links. (This does NOT exempt them from anti-nuke.)", ephemeral=True)

    @app_commands.command(name="untrust", description="Stop letting a member send links (server owner only)")
    @app_commands.guild_only()
    async def untrust(self, interaction: discord.Interaction, user: discord.Member):
        if not await self.owner_only(interaction):
            return
        await self.bot.db.execute(
            "DELETE FROM antilink_trusted WHERE guild_id=? AND user_id=?", (interaction.guild_id, user.id))
        await self.bot.db.commit()
        self.trusted[interaction.guild_id].discard(user.id)
        await interaction.response.send_message(f"{user.mention} can no longer send links.", ephemeral=True)

    @app_commands.command(name="status", description="Show anti-link settings")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def status(self, interaction: discord.Interaction):
        cfg = self.cfg.get(interaction.guild_id, DEFAULTS)
        lines = [f"**Anti-link:** {'on' if cfg['enabled'] else 'off'}"]
        lines += [f"- {label}: {'blocked' if cfg[k] else 'allowed'}" for k, label in OPTIONS.items()]
        allow = ", ".join(f"`{d}`" for d in sorted(self.allow[interaction.guild_id])) or "none"
        trusted = ", ".join(f"<@{u}>" for u in self.trusted[interaction.guild_id]) or "none"
        lines.append(f"**Allowed domains:** {allow}")
        lines.append(f"**Link-trusted members:** {trusted}")
        lines.append("Also allowed to post links: the server owner and everyone in `/antinuke trust`.")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AntiLink(bot))
