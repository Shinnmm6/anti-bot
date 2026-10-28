import time
from collections import defaultdict, deque

import discord
from discord import app_commands
from discord.ext import commands

A = discord.AuditLogAction

# ---- Tunables ----
# action -> (max allowed, within N seconds). Hitting the limit trips anti-nuke.
LIMITS = {
    A.channel_delete: (3, 10),
    A.channel_create: (5, 10),
    A.role_delete: (3, 10),
    A.role_create: (5, 10),
    A.ban: (4, 10),
    A.kick: (5, 10),
    A.webhook_create: (3, 10),
}
PUNISHMENT = "strip"  # "strip" (remove dangerous roles), "kick", or "ban"
DANGEROUS = (
    "administrator", "manage_guild", "manage_roles", "manage_channels",
    "ban_members", "kick_members", "manage_webhooks",
)
RESTORE_WINDOW = 60  # seconds: restore channels the offender deleted within this window
COOLDOWN = 30        # seconds: don't re-trigger on the same user


def snapshot(ch: discord.abc.GuildChannel) -> dict | None:
    if not isinstance(ch, (discord.TextChannel, discord.VoiceChannel, discord.CategoryChannel)):
        return None
    return {
        "name": ch.name,
        "type": ch.type,
        "category_id": ch.category_id,
        "overwrites": dict(ch.overwrites),
        "topic": getattr(ch, "topic", None),
        "nsfw": getattr(ch, "nsfw", False),
    }


class AntiNuke(commands.GroupCog, group_name="antinuke"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.trusted: dict[int, set[int]] = defaultdict(set)
        self.hits: dict[tuple, deque] = defaultdict(deque)
        self.cooldown: dict[tuple[int, int], float] = {}
        self.channel_cache: dict[int, dict] = {}
        self.deleted: dict[tuple[int, int], list] = defaultdict(list)

    async def cog_load(self):
        db = self.bot.db
        await db.execute(
            """CREATE TABLE IF NOT EXISTS antinuke_whitelist (
                guild_id INTEGER, user_id INTEGER, PRIMARY KEY (guild_id, user_id))"""
        )
        await db.commit()
        async with db.execute("SELECT guild_id, user_id FROM antinuke_whitelist") as cur:
            for guild_id, user_id in await cur.fetchall():
                self.trusted[guild_id].add(user_id)

    # ---------- helpers ----------

    def is_trusted(self, guild: discord.Guild, user_id: int) -> bool:
        return (
            user_id == guild.owner_id
            or user_id == self.bot.user.id
            or user_id in self.trusted[guild.id]
        )

    async def punish(self, guild: discord.Guild, user_id: int, reason: str) -> str:
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.HTTPException:
                return "user already gone"
        if member.id == guild.owner_id:
            return "is the server owner, can't act"
        if member.top_role >= guild.me.top_role:
            return "has a role at/above mine, can't act (move my role higher!)"
        try:
            if member.bot:
                await member.kick(reason=reason)
                return "bot kicked"
            if PUNISHMENT == "ban":
                await member.ban(reason=reason, delete_message_seconds=0)
                return "banned"
            if PUNISHMENT == "kick":
                await member.kick(reason=reason)
                return "kicked"
            risky = [
                r for r in member.roles
                if not r.is_default() and not r.managed and r < guild.me.top_role
                and any(getattr(r.permissions, p) for p in DANGEROUS)
            ]
            if not risky:
                return "no removable dangerous roles found"
            await member.remove_roles(*risky, reason=reason)
            return "stripped roles: " + ", ".join(r.name for r in risky)
        except discord.HTTPException as e:
            return f"failed ({e})"

    async def recreate(self, guild: discord.Guild, snap: dict):
        overwrites = {
            target: ow for target, ow in snap["overwrites"].items()
            if (guild.get_role(target.id) if isinstance(target, discord.Role) else guild.get_member(target.id))
        }
        category = guild.get_channel(snap["category_id"]) if snap["category_id"] else None
        common = {"name": snap["name"], "overwrites": overwrites, "reason": "Anti-nuke restore"}
        if snap["type"] == discord.ChannelType.text:
            await guild.create_text_channel(
                category=category, topic=snap["topic"], nsfw=snap["nsfw"], **common)
        elif snap["type"] == discord.ChannelType.voice:
            await guild.create_voice_channel(category=category, **common)
        elif snap["type"] == discord.ChannelType.category:
            await guild.create_category(**common)

    async def restore_channels(self, guild: discord.Guild, user_id: int) -> int:
        now = time.monotonic()
        snaps = [s for t, s in self.deleted.pop((guild.id, user_id), []) if now - t < RESTORE_WINDOW]
        restored = 0
        for snap in snaps:
            try:
                await self.recreate(guild, snap)
                restored += 1
            except discord.HTTPException:
                pass
        return restored

    async def trip(self, guild: discord.Guild, user_id: int, action: discord.AuditLogAction):
        now = time.monotonic()
        key = (guild.id, user_id)
        if self.cooldown.get(key, 0) > now:
            return
        self.cooldown[key] = now + COOLDOWN

        result = await self.punish(guild, user_id, f"Anti-nuke: too many {action.name}")
        restored = 0
        if action == A.channel_delete:
            restored = await self.restore_channels(guild, user_id)
        await self.bot.log(
            guild, "ANTI-NUKE TRIGGERED",
            f"<@{user_id}> hit the `{action.name}` limit.\n"
            f"**Action:** {result}\n**Channels restored:** {restored}",
            discord.Color.red(),
        )

    # ---------- channel snapshots (so deleted channels can be rebuilt) ----------

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            for ch in guild.channels:
                if snap := snapshot(ch):
                    self.channel_cache[ch.id] = snap

    @commands.Cog.listener()
    async def on_guild_channel_create(self, ch: discord.abc.GuildChannel):
        if snap := snapshot(ch):
            self.channel_cache[ch.id] = snap

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after: discord.abc.GuildChannel):
        if snap := snapshot(after):
            self.channel_cache[after.id] = snap

    # ---------- main detection ----------

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry):
        guild = entry.guild
        user_id = entry.user_id
        if user_id is None:
            return

        # Always remember deleted channels, even for trusted users (cheap and harmless)
        if entry.action == A.channel_delete and entry.target is not None:
            snap = self.channel_cache.pop(entry.target.id, None)
            if snap and not self.is_trusted(guild, user_id):
                bucket = self.deleted[(guild.id, user_id)]
                bucket.append((time.monotonic(), snap))
                del bucket[:-20]

        if self.is_trusted(guild, user_id):
            return

        if entry.action == A.bot_add:
            return await self.handle_bot_add(entry)
        if entry.action == A.role_update:
            return await self.handle_role_update(entry)

        limit = LIMITS.get(entry.action)
        if not limit:
            return
        max_hits, seconds = limit
        now = time.monotonic()
        dq = self.hits[(guild.id, user_id, entry.action)]
        dq.append(now)
        while dq and now - dq[0] > seconds:
            dq.popleft()
        if len(dq) >= max_hits:
            dq.clear()
            await self.trip(guild, user_id, entry.action)
        elif len(dq) >= 2:
            await self.bot.log(
                guild, "Suspicious activity",
                f"<@{user_id}> did `{entry.action.name}` {len(dq)}/{max_hits} times in {seconds}s.",
                discord.Color.orange())

    async def handle_bot_add(self, entry: discord.AuditLogEntry):
        guild = entry.guild
        bot_member = guild.get_member(entry.target.id) if entry.target else None
        result = "bot not found"
        if bot_member and bot_member.top_role < guild.me.top_role:
            try:
                await bot_member.kick(reason="Anti-nuke: bot added by untrusted user")
                result = "bot kicked"
            except discord.HTTPException as e:
                result = f"failed ({e})"
        await self.bot.log(
            guild, "Untrusted bot add blocked",
            f"<@{entry.user_id}> added <@{entry.target.id}>\n**Action:** {result}",
            discord.Color.red(),
        )

    async def handle_role_update(self, entry: discord.AuditLogEntry):
        before = getattr(entry.before, "permissions", None)
        after = getattr(entry.after, "permissions", None)
        if before is None or after is None:
            return
        gained = [p for p in DANGEROUS if getattr(after, p) and not getattr(before, p)]
        if not gained:
            return
        guild = entry.guild
        role = guild.get_role(entry.target.id)
        reverted = False
        if role and role < guild.me.top_role:
            try:
                await role.edit(permissions=before, reason="Anti-nuke: dangerous perms added by untrusted user")
                reverted = True
            except discord.HTTPException:
                pass
        result = await self.punish(guild, entry.user_id, "Anti-nuke: granted dangerous role permissions")
        await self.bot.log(
            guild, "Dangerous role edit blocked",
            f"<@{entry.user_id}> granted `{', '.join(gained)}` to {role.mention if role else entry.target.id}\n"
            f"**Reverted:** {reverted}\n**Action:** {result}",
            discord.Color.red(),
        )

    # ---------- commands (server owner only) ----------

    async def owner_only(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or interaction.user.id != interaction.guild.owner_id:
            await interaction.response.send_message("Only the server owner can use this.", ephemeral=True)
            return False
        return True

    @app_commands.command(description="Exempt a user from anti-nuke checks")
    async def trust(self, interaction: discord.Interaction, user: discord.Member):
        if not await self.owner_only(interaction):
            return
        await self.bot.db.execute(
            "INSERT OR IGNORE INTO antinuke_whitelist (guild_id, user_id) VALUES (?, ?)",
            (interaction.guild_id, user.id))
        await self.bot.db.commit()
        self.trusted[interaction.guild_id].add(user.id)
        await interaction.response.send_message(f"{user.mention} is now trusted.", ephemeral=True)

    @app_commands.command(description="Remove a user from the trusted list")
    async def untrust(self, interaction: discord.Interaction, user: discord.Member):
        if not await self.owner_only(interaction):
            return
        await self.bot.db.execute(
            "DELETE FROM antinuke_whitelist WHERE guild_id=? AND user_id=?",
            (interaction.guild_id, user.id))
        await self.bot.db.commit()
        self.trusted[interaction.guild_id].discard(user.id)
        await interaction.response.send_message(f"{user.mention} is no longer trusted.", ephemeral=True)

    @app_commands.command(description="Show anti-nuke settings and trusted users")
    async def status(self, interaction: discord.Interaction):
        if not await self.owner_only(interaction):
            return
        limits = "\n".join(f"`{a.name}`: {n} per {s}s" for a, (n, s) in LIMITS.items())
        trusted = ", ".join(f"<@{u}>" for u in self.trusted[interaction.guild_id]) or "none (owner is always trusted)"
        await interaction.response.send_message(
            f"**Punishment:** {PUNISHMENT}\n**Limits:**\n{limits}\n**Trusted:** {trusted}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AntiNuke(bot))
