import asyncio
import time
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

DANGEROUS = {
    "administrator": "Administrator",
    "manage_guild": "Manage Server",
    "manage_roles": "Manage Roles",
    "manage_channels": "Manage Channels",
    "manage_webhooks": "Manage Webhooks",
    "ban_members": "Ban Members",
    "kick_members": "Kick Members",
    "manage_messages": "Manage Messages",
    "moderate_members": "Timeout Members",
    "mention_everyone": "Mention Everyone",
}


def encode_prev(value: bool | None) -> int:
    return -1 if value is None else int(value)


def decode_prev(code: int) -> bool | None:
    return None if code == -1 else bool(code)


def too_new(created_at: datetime, now: datetime, min_days: int) -> bool:
    return (now - created_at) < timedelta(days=min_days)


def analyze(info: dict) -> tuple[list[str], list[str], list[str]]:
    """Turn plain facts about a server into (high, medium, good) findings."""
    high, medium, good = [], [], []
    if info["everyone_perms"]:
        high.append("**@everyone** has dangerous permissions: " + ", ".join(info["everyone_perms"])
                    + ". Anyone who joins gets them.")
    else:
        good.append("@everyone has no dangerous permissions.")
    for bot in info["admin_bots"]:
        high.append(f"The bot **{bot}** has Administrator. If its token leaks, your server is gone. "
                    "Give it only what it needs.")
    if info["admin_humans"] > 3:
        medium.append(f"**{info['admin_humans']}** people have Administrator. Fewer is safer; use specific permissions.")
    elif info["admin_humans"]:
        good.append(f"Only {info['admin_humans']} people have Administrator.")
    for r in info["risky_roles"]:
        if r["members"] >= 50 and not r["managed"]:
            medium.append(f"Role **{r['name']}** gives {', '.join(r['perms'])} to **{r['members']}** members.")
    if info["verification_level"] == "none":
        high.append("Verification level is **None**. Set at least Low in Server Settings, Safety Setup.")
    elif info["verification_level"] == "low":
        medium.append("Verification level is **Low**. Medium (registered for 5 minutes) stops many raid bots.")
    else:
        good.append(f"Verification level is {info['verification_level']}.")
    if not info["mfa"] and info["risky_roles"]:
        medium.append("2FA is not required for moderators. Turn on Require 2FA for moderation in Server Settings.")
    elif info["mfa"]:
        good.append("2FA is required for moderators.")
    if info["content_filter"] == "disabled":
        medium.append("Explicit media content filter is off.")
    if info["bot_rank"] > 3:
        medium.append(f"My role is #{info['bot_rank']} from the top. Move it higher so I can moderate more roles.")
    else:
        good.append("My role is near the top of the role list.")
    return high, medium, good


class RaidCleanView(discord.ui.View):
    def __init__(self, author_id: int, targets: list[discord.Member], action: str):
        super().__init__(timeout=60)
        self.author_id, self.targets, self.action = author_id, targets, action
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the person who ran the command can confirm.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content=f"Working on {len(self.targets)} members...", embed=None,
                                                view=self)
        done = failed = 0
        for m in self.targets:
            try:
                if self.action == "ban":
                    await m.ban(reason=f"Raid cleanup by {interaction.user}", delete_message_seconds=86400)
                else:
                    await m.kick(reason=f"Raid cleanup by {interaction.user}")
                done += 1
            except discord.HTTPException:
                failed += 1
            await asyncio.sleep(0.4)  # stay under the rate limit
        past = "banned" if self.action == "ban" else "kicked"
        await interaction.edit_original_response(
            content=f"Done. {done} {past}" + (f", {failed} failed." if failed else "."))
        bot = interaction.client
        await bot.log(interaction.guild, "Raid cleanup", f"{interaction.user} {past} {done} recent joiners "
                                                         f"({failed} failed).", discord.Color.red())

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Cancelled. Nobody was touched.", embed=None, view=self)

    async def on_timeout(self):
        if self.message:
            for child in self.children:
                child.disabled = True
            try:
                await self.message.edit(content="Timed out. Nobody was touched.", embed=None, view=self)
            except discord.HTTPException:
                pass


class Security(commands.Cog):
    """Lockdown, account age gate, quarantine, security scan and raid cleanup."""

    lockdown = app_commands.Group(name="lockdown", description="Emergency lock of the whole server",
                                  guild_only=True)
    gate = app_commands.Group(name="gate", description="Block brand new accounts from joining", guild_only=True)
    quarantine = app_commands.Group(name="quarantine", description="Isolate a suspicious member", guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.gates: dict[int, dict] = {}

    async def cog_load(self):
        db = self.bot.db
        await db.execute("CREATE TABLE IF NOT EXISTS lockdown_state (guild_id INTEGER, channel_id INTEGER, prev INTEGER, "
                         "PRIMARY KEY (guild_id, channel_id))")
        await db.execute("CREATE TABLE IF NOT EXISTS lockdown_active (guild_id INTEGER PRIMARY KEY, started_by INTEGER, "
                         "started_at INTEGER, reason TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS gate_config (guild_id INTEGER PRIMARY KEY, min_days INTEGER, "
                         "action TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS gate_allow (guild_id INTEGER, user_id INTEGER, "
                         "PRIMARY KEY (guild_id, user_id))")
        await db.execute("CREATE TABLE IF NOT EXISTS quarantine_info (guild_id INTEGER, user_id INTEGER, by_id INTEGER, "
                         "reason TEXT, created_at INTEGER, PRIMARY KEY (guild_id, user_id))")
        await db.execute("CREATE TABLE IF NOT EXISTS quarantine_roles (guild_id INTEGER, user_id INTEGER, role_id INTEGER, "
                         "PRIMARY KEY (guild_id, user_id, role_id))")
        await db.commit()
        async with db.execute("SELECT guild_id, min_days, action FROM gate_config") as cur:
            for gid, days, action in await cur.fetchall():
                self.gates[gid] = {"days": days, "action": action}

    # ------------------------------------------------------------------ lockdown

    @lockdown.command(name="start", description="Stop @everyone from talking in every text channel")
    @app_commands.describe(reason="Why (shown in the log)")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True, manage_roles=True)
    async def lockdown_start(self, interaction: discord.Interaction, reason: str = "Emergency lockdown"):
        guild = interaction.guild
        async with self.bot.db.execute("SELECT 1 FROM lockdown_active WHERE guild_id=?", (guild.id,)) as cur:
            if await cur.fetchone():
                return await interaction.response.send_message("A lockdown is already active. Use `/lockdown end`.",
                                                               ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        everyone = guild.default_role
        locked = failed = 0
        for ch in guild.text_channels:
            ow = ch.overwrites_for(everyone)
            await self.bot.db.execute("INSERT OR REPLACE INTO lockdown_state (guild_id, channel_id, prev) VALUES (?,?,?)",
                                      (guild.id, ch.id, encode_prev(ow.send_messages)))
            if ow.send_messages is False:
                continue
            ow.send_messages = False
            try:
                await ch.set_permissions(everyone, overwrite=ow, reason=f"Lockdown by {interaction.user}: {reason}")
                locked += 1
            except discord.HTTPException:
                failed += 1
        await self.bot.db.execute("INSERT OR REPLACE INTO lockdown_active (guild_id, started_by, started_at, reason) "
                                  "VALUES (?,?,?,?)", (guild.id, interaction.user.id, int(time.time()), reason))
        await self.bot.db.commit()
        await interaction.followup.send(
            f"Lockdown on. Locked **{locked}** channels" + (f", {failed} failed (check my permissions there)" if failed else "")
            + ". Staff with their own channel permissions can still talk. End it with `/lockdown end`.", ephemeral=True)
        await self.bot.log(guild, "LOCKDOWN STARTED", f"By {interaction.user}\n{reason}\n{locked} channels locked.",
                           discord.Color.red())

    @lockdown.command(name="end", description="Restore every channel to how it was before the lockdown")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True, manage_roles=True)
    async def lockdown_end(self, interaction: discord.Interaction):
        guild = interaction.guild
        async with self.bot.db.execute("SELECT 1 FROM lockdown_active WHERE guild_id=?", (guild.id,)) as cur:
            if not await cur.fetchone():
                return await interaction.response.send_message("There is no active lockdown.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        async with self.bot.db.execute("SELECT channel_id, prev FROM lockdown_state WHERE guild_id=?",
                                       (guild.id,)) as cur:
            rows = await cur.fetchall()
        everyone = guild.default_role
        restored = failed = 0
        for channel_id, prev in rows:
            ch = guild.get_channel(channel_id)
            if ch is None:
                continue
            ow = ch.overwrites_for(everyone)
            ow.send_messages = decode_prev(prev)
            try:
                await ch.set_permissions(everyone, overwrite=None if ow.is_empty() else ow,
                                         reason=f"Lockdown ended by {interaction.user}")
                restored += 1
            except discord.HTTPException:
                failed += 1
        await self.bot.db.execute("DELETE FROM lockdown_state WHERE guild_id=?", (guild.id,))
        await self.bot.db.execute("DELETE FROM lockdown_active WHERE guild_id=?", (guild.id,))
        await self.bot.db.commit()
        await interaction.followup.send(f"Lockdown over. Restored **{restored}** channels"
                                        + (f", {failed} failed" if failed else "") + ".", ephemeral=True)
        await self.bot.log(guild, "Lockdown ended", f"By {interaction.user}. {restored} channels restored.",
                           discord.Color.green())

    @lockdown.command(name="status", description="Is the server locked down?")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def lockdown_status(self, interaction: discord.Interaction):
        async with self.bot.db.execute("SELECT started_by, started_at, reason FROM lockdown_active WHERE guild_id=?",
                                       (interaction.guild_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            return await interaction.response.send_message("No lockdown is active.", ephemeral=True)
        await interaction.response.send_message(f"Locked down by <@{row[0]}> <t:{row[1]}:R>. Reason: {row[2]}",
                                                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    # ------------------------------------------------------------------ account age gate

    @gate.command(name="set", description="Act on accounts younger than a number of days when they join")
    @app_commands.describe(min_days="Minimum account age in days", action="What to do with younger accounts")
    @app_commands.choices(action=[app_commands.Choice(name="Kick them (and tell them why)", value="kick"),
                                  app_commands.Choice(name="Only log it", value="log")])
    @app_commands.checks.has_permissions(manage_guild=True)
    async def gate_set(self, interaction: discord.Interaction, min_days: app_commands.Range[int, 1, 365],
                       action: app_commands.Choice[str]):
        await self.bot.db.execute("INSERT OR REPLACE INTO gate_config (guild_id, min_days, action) VALUES (?,?,?)",
                                  (interaction.guild_id, min_days, action.value))
        await self.bot.db.commit()
        self.gates[interaction.guild_id] = {"days": min_days, "action": action.value}
        warn = ""
        if action.value == "kick" and not interaction.guild.me.guild_permissions.kick_members:
            warn = " **I don't have Kick Members yet, so I can't kick.**"
        await interaction.response.send_message(
            f"Accounts younger than **{min_days} days** will be {'kicked' if action.value == 'kick' else 'logged'}. "
            f"Use `/gate allow` for someone you trust.{warn}", ephemeral=True)

    @gate.command(name="off", description="Turn the account age gate off")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def gate_off(self, interaction: discord.Interaction):
        await self.bot.db.execute("DELETE FROM gate_config WHERE guild_id=?", (interaction.guild_id,))
        await self.bot.db.commit()
        self.gates.pop(interaction.guild_id, None)
        await interaction.response.send_message("Account age gate is off.", ephemeral=True)

    @gate.command(name="allow", description="Let a specific new account in anyway")
    @app_commands.describe(user_id="The user's ID")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def gate_allow(self, interaction: discord.Interaction, user_id: str):
        if not user_id.isdigit():
            return await interaction.response.send_message("That isn't a user ID.", ephemeral=True)
        await self.bot.db.execute("INSERT OR IGNORE INTO gate_allow (guild_id, user_id) VALUES (?,?)",
                                  (interaction.guild_id, int(user_id)))
        await self.bot.db.commit()
        await interaction.response.send_message("Done. That account can join even if it's new.", ephemeral=True)

    @gate.command(name="status", description="Show the account age gate settings")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def gate_status(self, interaction: discord.Interaction):
        cfg = self.gates.get(interaction.guild_id)
        await interaction.response.send_message(
            f"Accounts under **{cfg['days']} days**: {cfg['action']}." if cfg else "The gate is off.", ephemeral=True)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        cfg = self.gates.get(member.guild.id)
        if not cfg or member.bot or not too_new(member.created_at, datetime.now(timezone.utc), cfg["days"]):
            return
        async with self.bot.db.execute("SELECT 1 FROM gate_allow WHERE guild_id=? AND user_id=?",
                                       (member.guild.id, member.id)) as cur:
            if await cur.fetchone():
                return
        age = (datetime.now(timezone.utc) - member.created_at).days
        if cfg["action"] == "kick":
            try:
                await member.send(f"Your Discord account is only {age} day(s) old, and **{member.guild.name}** requires "
                                  f"accounts to be at least {cfg['days']} days old. You can try joining again later.")
            except discord.HTTPException:
                pass
            try:
                await member.kick(reason=f"Account age gate: {age}d old, minimum {cfg['days']}d")
                return await self.bot.log(member.guild, "Account gate kick",
                                          f"{member} ({member.id}) account is {age} day(s) old.")
            except discord.HTTPException:
                return await self.bot.log(member.guild, "Account gate failed",
                                          f"I couldn't kick {member} ({age}d old). Check my Kick Members permission.",
                                          discord.Color.red())
        await self.bot.log(member.guild, "Young account joined", f"{member} ({member.id}) account is {age} day(s) old.")

    # ------------------------------------------------------------------ quarantine

    def _blocked(self, interaction: discord.Interaction, target: discord.Member) -> str | None:
        guild = interaction.guild
        if target.id == interaction.user.id:
            return "You can't quarantine yourself."
        if target.id == guild.owner_id or target.guild_permissions.administrator:
            return "I can't quarantine the owner or an administrator."
        if target.top_role >= guild.me.top_role:
            return "That member's top role is at or above mine."
        if interaction.user.id != guild.owner_id and target.top_role >= interaction.user.top_role:
            return "That member's top role is at or above yours."
        return None

    @quarantine.command(name="add", description="Remove a member's roles and time them out until you review them")
    @app_commands.describe(member="The suspicious member", reason="Why", hours="How long the timeout lasts (default 24)")
    @app_commands.checks.has_permissions(moderate_members=True, manage_roles=True)
    @app_commands.checks.bot_has_permissions(moderate_members=True, manage_roles=True)
    async def quarantine_add(self, interaction: discord.Interaction, member: discord.Member,
                             reason: str = "Suspicious activity", hours: app_commands.Range[int, 1, 672] = 24):
        if err := self._blocked(interaction, member):
            return await interaction.response.send_message(err, ephemeral=True)
        async with self.bot.db.execute("SELECT 1 FROM quarantine_info WHERE guild_id=? AND user_id=?",
                                       (interaction.guild_id, member.id)) as cur:
            if await cur.fetchone():
                return await interaction.response.send_message("They're already quarantined.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        me_top = interaction.guild.me.top_role
        roles = [r for r in member.roles if not r.is_default() and not r.managed and r < me_top]
        try:
            await member.timeout(timedelta(hours=hours), reason=f"Quarantine by {interaction.user}: {reason}")
            if roles:
                await member.remove_roles(*roles, reason=f"Quarantine by {interaction.user}: {reason}")
        except discord.HTTPException:
            return await interaction.followup.send("Discord refused. Check my permissions and role order.",
                                                   ephemeral=True)
        await self.bot.db.execute("INSERT OR REPLACE INTO quarantine_info VALUES (?,?,?,?,?)",
                                  (interaction.guild_id, member.id, interaction.user.id, reason, int(time.time())))
        for r in roles:
            await self.bot.db.execute("INSERT OR IGNORE INTO quarantine_roles VALUES (?,?,?)",
                                      (interaction.guild_id, member.id, r.id))
        await self.bot.db.commit()
        await interaction.followup.send(
            f"{member.mention} is quarantined: {len(roles)} role(s) removed and timed out for {hours}h. "
            "Their roles are saved. Use `/quarantine release` to give everything back.", ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none())
        await self.bot.log(interaction.guild, "Quarantine", f"{member} by {interaction.user}\n{reason}",
                           discord.Color.red())

    @quarantine.command(name="release", description="Give a quarantined member their roles back")
    @app_commands.checks.has_permissions(moderate_members=True, manage_roles=True)
    @app_commands.checks.bot_has_permissions(moderate_members=True, manage_roles=True)
    async def quarantine_release(self, interaction: discord.Interaction, member: discord.Member):
        async with self.bot.db.execute("SELECT 1 FROM quarantine_info WHERE guild_id=? AND user_id=?",
                                       (interaction.guild_id, member.id)) as cur:
            if not await cur.fetchone():
                return await interaction.response.send_message("They aren't quarantined.", ephemeral=True)
        async with self.bot.db.execute("SELECT role_id FROM quarantine_roles WHERE guild_id=? AND user_id=?",
                                       (interaction.guild_id, member.id)) as cur:
            ids = [r[0] for r in await cur.fetchall()]
        guild = interaction.guild
        roles = [r for r in (guild.get_role(i) for i in ids) if r and r < guild.me.top_role]
        try:
            await member.timeout(None, reason=f"Quarantine released by {interaction.user}")
            if roles:
                await member.add_roles(*roles, reason=f"Quarantine released by {interaction.user}")
        except discord.HTTPException:
            return await interaction.response.send_message("Discord refused. Check my permissions and role order.",
                                                           ephemeral=True)
        await self.bot.db.execute("DELETE FROM quarantine_info WHERE guild_id=? AND user_id=?", (guild.id, member.id))
        await self.bot.db.execute("DELETE FROM quarantine_roles WHERE guild_id=? AND user_id=?", (guild.id, member.id))
        await self.bot.db.commit()
        await interaction.response.send_message(f"Released {member.mention}. {len(roles)} role(s) restored.",
                                                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        await self.bot.log(guild, "Quarantine released", f"{member} by {interaction.user}", discord.Color.green())

    @quarantine.command(name="list", description="Who is quarantined right now")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def quarantine_list(self, interaction: discord.Interaction):
        async with self.bot.db.execute("SELECT user_id, by_id, reason, created_at FROM quarantine_info WHERE guild_id=?",
                                       (interaction.guild_id,)) as cur:
            rows = await cur.fetchall()
        if not rows:
            return await interaction.response.send_message("Nobody is quarantined.", ephemeral=True)
        await interaction.response.send_message(
            "\n".join(f"<@{u}> by <@{b}> <t:{t}:R>: {r[:80]}" for u, b, r, t in rows), ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none())

    # ------------------------------------------------------------------ security scan

    @app_commands.command(name="securityscan", description="Check your server for risky permissions and settings")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def securityscan(self, interaction: discord.Interaction):
        guild = interaction.guild
        await interaction.response.defer(ephemeral=True)
        risky, admin_bots, admin_humans = [], [], 0
        for role in guild.roles:
            if role.is_default():
                continue
            perms = [label for attr, label in DANGEROUS.items() if getattr(role.permissions, attr)]
            if perms:
                risky.append({"name": role.name, "perms": perms, "members": len(role.members), "managed": role.managed})
        for m in guild.members:
            if m.guild_permissions.administrator and m.id != guild.owner_id:
                if m.bot:
                    admin_bots.append(m.display_name)
                else:
                    admin_humans += 1
        ordered = sorted(guild.roles, key=lambda r: r.position, reverse=True)
        info = {
            "everyone_perms": [label for attr, label in DANGEROUS.items()
                               if getattr(guild.default_role.permissions, attr)],
            "admin_bots": [b for b in admin_bots if b != guild.me.display_name],
            "admin_humans": admin_humans,
            "risky_roles": risky,
            "verification_level": guild.verification_level.name,
            "mfa": guild.mfa_level == discord.MFALevel.require_2fa,
            "content_filter": guild.explicit_content_filter.name,
            "bot_rank": ordered.index(guild.me.top_role) + 1 if guild.me.top_role in ordered else 99,
        }
        high, medium, good = analyze(info)
        embed = discord.Embed(title=f"Security scan: {guild.name}",
                              color=discord.Color.red() if high else discord.Color.orange() if medium
                              else discord.Color.green())
        embed.add_field(name=f"Fix now ({len(high)})", value="\n".join(high)[:1000] or "Nothing urgent.", inline=False)
        embed.add_field(name=f"Should improve ({len(medium)})", value="\n".join(medium)[:1000] or "Nothing.",
                        inline=False)
        embed.add_field(name=f"Looks good ({len(good)})", value="\n".join(good)[:1000] or "-", inline=False)
        embed.set_footer(text="Only you can see this. It checks roles and server settings, not channel permissions.")
        await interaction.followup.send(embed=embed)

    # ------------------------------------------------------------------ raid cleanup

    @app_commands.command(name="raidclean", description="Preview, then remove everyone who joined in the last few minutes")
    @app_commands.describe(minutes="How far back to look", action="Ban (default) or kick",
                           only_no_roles="Only members with no roles, so verified members are safe (default yes)")
    @app_commands.choices(action=[app_commands.Choice(name="Ban", value="ban"), app_commands.Choice(name="Kick", value="kick")])
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(ban_members=True, kick_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True, kick_members=True)
    async def raidclean(self, interaction: discord.Interaction, minutes: app_commands.Range[int, 1, 180],
                        action: app_commands.Choice[str] | None = None, only_no_roles: bool = True):
        guild = interaction.guild
        act = action.value if action else "ban"
        since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        targets = [m for m in guild.members
                   if m.joined_at and m.joined_at >= since and not m.bot and m.id != interaction.user.id
                   and m.id != guild.owner_id and m.top_role < guild.me.top_role
                   and (not only_no_roles or len(m.roles) == 1)]
        if not targets:
            return await interaction.response.send_message("Nobody matches. Nobody was touched.", ephemeral=True)
        targets.sort(key=lambda m: m.joined_at)
        capped = len(targets) > 200
        targets = targets[:200]
        sample = "\n".join(f"{m} ({m.id})" for m in targets[:15])
        embed = discord.Embed(
            title=f"{act.title()} {len(targets)} members?", color=discord.Color.red(),
            description=f"Everyone who joined in the last **{minutes} min**"
                        + (" and has no roles" if only_no_roles else "") + f".\n```\n{sample}\n```"
                        + (f"\n...and {len(targets) - 15} more." if len(targets) > 15 else "")
                        + ("\nCapped at 200 per run. Run it again for the rest." if capped else ""))
        view = RaidCleanView(interaction.user.id, targets, act)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()


async def setup(bot: commands.Bot):
    await bot.add_cog(Security(bot))
