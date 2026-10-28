import random
import secrets
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

MAX_FAILS = 5               # wrong answers before the member is kicked
MAX_EXISTING_GRANT = 250    # max existing members the bot will auto-give the role to
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I to avoid confusion


def make_challenge() -> tuple[str, str]:
    """Pick a random challenge. Returns (label shown to the member, expected answer)."""
    kind = random.choice(["math", "code", "yes"])
    if kind == "math":
        op = random.choice(["+", "-", "x"])
        if op == "+":
            a, b = random.randint(10, 49), random.randint(10, 49)
            return f"What is {a} + {b}?", str(a + b)
        if op == "-":
            a, b = random.randint(30, 99), random.randint(10, 29)
            return f"What is {a} - {b}?", str(a - b)
        a, b = random.randint(3, 12), random.randint(3, 12)
        return f"What is {a} x {b}?", str(a * b)
    if kind == "code":
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        return f"Type this code: {code}", code
    return "Type yes to verify", "yes"


class ChallengeModal(discord.ui.Modal, title="Quick verification"):
    def __init__(self, cog: "Verification", answer: str, label: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.answer = answer
        self.box = discord.ui.TextInput(
            label=label, placeholder="Type your answer here", min_length=1, max_length=10)
        self.add_item(self.box)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.check_answer(interaction, self.answer, self.box.value)


class VerifyView(discord.ui.View):
    """Persistent view: survives restarts because of the fixed custom_id and timeout=None."""

    def __init__(self, cog: "Verification"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.success,
                       custom_id="verify:button", emoji="✅")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_click(interaction)


class Verification(commands.GroupCog, group_name="verify"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.fails: dict[tuple[int, int], int] = defaultdict(int)

    async def cog_load(self):
        db = self.bot.db
        await db.execute(
            """CREATE TABLE IF NOT EXISTS verify_config (
                guild_id INTEGER PRIMARY KEY,
                role_id INTEGER, channel_id INTEGER,
                min_age_days INTEGER, timeout_minutes INTEGER,
                enabled_at TEXT)"""
        )
        # Columns added later; ignore errors if they already exist
        for col in ("locked INTEGER DEFAULT 0", "prev_view INTEGER DEFAULT 0"):
            try:
                await db.execute(f"ALTER TABLE verify_config ADD COLUMN {col}")
            except Exception:
                pass
        await db.commit()
        self.bot.add_view(VerifyView(self))
        self.sweep.start()

    async def cog_unload(self):
        self.sweep.cancel()

    async def get_config(self, guild_id: int):
        async with self.bot.db.execute(
            "SELECT role_id, channel_id, min_age_days, timeout_minutes, enabled_at "
            "FROM verify_config WHERE guild_id=?", (guild_id,)
        ) as cur:
            return await cur.fetchone()

    # ---------- member-facing flow ----------

    async def handle_click(self, interaction: discord.Interaction):
        cfg = await self.get_config(interaction.guild_id)
        role = interaction.guild.get_role(cfg[0]) if cfg else None
        if not cfg or role is None:
            return await interaction.response.send_message(
                "Verification isn't set up properly. Please tell a moderator.", ephemeral=True)
        if role in interaction.user.roles:
            return await interaction.response.send_message("You're already verified.", ephemeral=True)

        label, answer = make_challenge()
        await interaction.response.send_modal(ChallengeModal(self, answer, label))

    async def grant(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role):
        self.fails.pop((interaction.guild_id, member.id), None)
        ok = True
        try:
            await member.add_roles(role, reason="Passed verification")
        except discord.HTTPException:
            ok = False
            msg = "I couldn't give you the role. Please tell a moderator."
        else:
            msg = "You're verified. Welcome!"
        await interaction.response.send_message(msg, ephemeral=True)
        if ok:
            await self.bot.log(interaction.guild, "Member verified",
                               f"{member.mention} ({member}) passed verification.", discord.Color.green())
        else:
            await self.bot.log(interaction.guild, "Verification role failed",
                               f"{member.mention} passed the challenge but I couldn't give the role. "
                               "Check that my role is above the member role.", discord.Color.red())

    async def check_answer(self, interaction: discord.Interaction, answer: str, given: str):
        cfg = await self.get_config(interaction.guild_id)
        role = interaction.guild.get_role(cfg[0]) if cfg else None
        if not cfg or role is None:
            return await interaction.response.send_message(
                "Verification isn't set up properly. Please tell a moderator.", ephemeral=True)

        member = interaction.user
        if given.strip().lower() == answer.lower():
            return await self.grant(interaction, member, role)

        key = (interaction.guild_id, member.id)
        self.fails[key] += 1
        if self.fails[key] >= MAX_FAILS:
            self.fails.pop(key, None)
            await interaction.response.send_message(
                "Too many wrong answers. You can rejoin and try again.", ephemeral=True)
            try:
                await member.kick(reason="Failed verification too many times")
                await self.bot.log(interaction.guild, "Verification kick",
                                   f"{member} failed the challenge {MAX_FAILS} times.")
            except discord.HTTPException:
                pass
        else:
            left = MAX_FAILS - self.fails[key]
            await interaction.response.send_message(
                f"Wrong answer. Click **Verify** again for a new challenge. ({left} tries left)",
                ephemeral=True)
            await self.bot.log(interaction.guild, "Failed verification attempt",
                               f"{member.mention} ({member}) answered wrong "
                               f"({self.fails[key]}/{MAX_FAILS}).", discord.Color.orange())

    # ---------- timed kick for members who never verify ----------

    @tasks.loop(minutes=1)
    async def sweep(self):
        async with self.bot.db.execute(
            "SELECT guild_id, role_id, timeout_minutes, enabled_at FROM verify_config"
        ) as cur:
            rows = await cur.fetchall()

        now = datetime.now(timezone.utc)
        for guild_id, role_id, timeout_min, enabled_at in rows:
            guild = self.bot.get_guild(guild_id)
            role = guild.get_role(role_id) if guild else None
            if not role:
                continue
            enabled = datetime.fromisoformat(enabled_at)
            cutoff = now - timedelta(minutes=timeout_min)

            for m in guild.members:
                if m.bot or m.pending or m.joined_at is None:
                    continue
                if len(m.roles) > 1:  # has any role at all (verified, staff, booster...): leave alone
                    continue
                # Only people who joined after verification was switched on, and have had enough time
                if m.joined_at <= enabled or m.joined_at > cutoff:
                    continue
                if m.top_role >= guild.me.top_role:
                    continue
                try:
                    try:
                        await m.send(
                            f"You were removed from **{guild.name}** for not verifying in time. "
                            "You're welcome to rejoin and verify.")
                    except discord.HTTPException:
                        pass
                    await m.kick(reason=f"Did not verify within {timeout_min} minutes")
                    await self.bot.log(guild, "Verification timeout", f"{m} was kicked for not verifying.")
                except discord.HTTPException:
                    pass

    @sweep.before_loop
    async def before_sweep(self):
        await self.bot.wait_until_ready()

    # ---------- admin commands ----------

    @app_commands.command(name="setup", description="Set up member verification")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True, manage_channels=True, kick_members=True)
    @app_commands.describe(
        member_role="Role given to verified members",
        verify_channel="Channel where the Verify button is posted",
        timeout_minutes="Kick unverified new joiners after this many minutes",
        lockdown="Hide every channel from unverified people except the verify channel",
        grant_existing="Give the member role to everyone already in the server (so they keep access)")
    async def setup_cmd(
        self, interaction: discord.Interaction,
        member_role: discord.Role, verify_channel: discord.TextChannel,
        timeout_minutes: app_commands.Range[int, 2, 1440] = 15,
        lockdown: bool = True,
        grant_existing: bool = True,
    ):
        guild = interaction.guild
        if member_role.is_default() or member_role.managed:
            return await interaction.response.send_message(
                "Pick a normal role (not @everyone or a bot/integration role).", ephemeral=True)
        if member_role >= guild.me.top_role:
            return await interaction.response.send_message(
                "That role is at or above my top role, so I can't hand it out. "
                "Move my role above it in Server Settings > Roles.", ephemeral=True)
        if lockdown and not any(
            r.permissions.view_channel or r.permissions.administrator
            for r in guild.me.roles if not r.is_default()
        ):
            return await interaction.response.send_message(
                "My own role needs the *View Channels* permission (not just through @everyone), "
                "or the lockdown would hide every channel from me too. Add it to my role and run setup again.",
                ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        notes: list[str] = []

        # 1) Verify channel: everyone can see it but not talk; verified members don't need it
        try:
            await verify_channel.set_permissions(
                guild.default_role, view_channel=True, send_messages=False,
                add_reactions=False, reason="Verification setup")
            await verify_channel.set_permissions(
                member_role, view_channel=False, reason="Verification setup")
            embed = discord.Embed(
                title="Verify to get access",
                description="Click the button below and complete the quick challenge "
                            "(it could be a small math question, a code to copy, or just typing *yes*).\n"
                            f"Members who don't verify within {timeout_minutes} minutes are removed.",
                color=discord.Color.green())
            await verify_channel.send(embed=embed, view=VerifyView(self))
        except discord.Forbidden:
            return await interaction.followup.send(
                f"I don't have permission to edit or post in {verify_channel.mention}.", ephemeral=True)

        # 2) Existing members (and other bots) get the role first, so nobody gets locked out
        if grant_existing:
            targets = [m for m in guild.members if member_role not in m.roles and m.id != guild.me.id]
            if len(targets) > MAX_EXISTING_GRANT:
                notes.append(
                    f"Skipped giving the role to existing members ({len(targets)} is over my limit of "
                    f"{MAX_EXISTING_GRANT}). Give it to them another way before they lose access.")
            else:
                granted = 0
                for m in targets:
                    try:
                        await m.add_roles(member_role, reason="Verification setup: existing member")
                        granted += 1
                    except discord.HTTPException:
                        pass
                notes.append(f"Gave {member_role.mention} to {granted} existing members/bots.")

        # 3) Lockdown: only verified members (and the verify channel) are visible
        locked, prev_view = 0, 0
        if lockdown:
            try:
                prev_view = int(guild.default_role.permissions.view_channel)
                role_perms = member_role.permissions
                role_perms.update(view_channel=True)
                await member_role.edit(permissions=role_perms, reason="Verification setup")

                everyone_perms = guild.default_role.permissions
                everyone_perms.update(view_channel=False)
                await guild.default_role.edit(permissions=everyone_perms, reason="Verification setup")

                # Channels that explicitly let @everyone view them would stay visible: reset those
                fixed = 0
                for ch in guild.channels:
                    if ch.id == verify_channel.id:
                        continue
                    ow = ch.overwrites_for(guild.default_role)
                    if ow.view_channel is True:
                        ow.view_channel = None
                        await ch.set_permissions(
                            guild.default_role, overwrite=None if ow.is_empty() else ow,
                            reason="Verification setup")
                        fixed += 1
                locked = 1
                notes.append(
                    "Lockdown on: @everyone can no longer view channels"
                    + (f" ({fixed} channels had an explicit @everyone view override, which I reset)." if fixed else "."))
            except discord.HTTPException as e:
                notes.append(f"Lockdown failed partway ({e}). Check Server Settings > Roles and channel permissions.")

        await self.bot.db.execute(
            "INSERT OR REPLACE INTO verify_config "
            "(guild_id, role_id, channel_id, min_age_days, timeout_minutes, enabled_at, locked, prev_view) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (guild.id, member_role.id, verify_channel.id, 0, timeout_minutes,
             datetime.now(timezone.utc).isoformat(), locked, prev_view))
        await self.bot.db.commit()

        await interaction.followup.send(
            f"Verification is on. Panel posted in {verify_channel.mention}.\n\n"
            + "\n".join(f"- {n}" for n in notes)
            + "\n\nNew joiners now only see the verify channel until they pass the challenge. "
              "Heads up: any staff/other role that relied on @everyone for *View Channels* "
              "may have lost access, so check them. Roles that have View Channels on their own are fine.",
            ephemeral=True)

    @app_commands.command(description="Turn verification off")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def disable(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
            "SELECT locked, prev_view FROM verify_config WHERE guild_id=?", (interaction.guild_id,)
        ) as cur:
            row = await cur.fetchone()

        restored = False
        if row and row[0] and row[1]:
            try:
                perms = interaction.guild.default_role.permissions
                perms.update(view_channel=True)
                await interaction.guild.default_role.edit(permissions=perms, reason="Verification disabled")
                restored = True
            except discord.HTTPException:
                pass

        await self.bot.db.execute("DELETE FROM verify_config WHERE guild_id=?", (interaction.guild_id,))
        await self.bot.db.commit()
        await interaction.response.send_message(
            "Verification disabled. The button no longer works and no one will be auto-kicked."
            + (" I also gave @everyone View Channels back." if restored else "")
            + " You can delete the panel message.", ephemeral=True)

    @app_commands.command(description="Show verification settings")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def status(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
            "SELECT role_id, channel_id, timeout_minutes, locked FROM verify_config WHERE guild_id=?",
            (interaction.guild_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return await interaction.response.send_message("Verification is not set up.", ephemeral=True)
        role_id, channel_id, timeout, locked = row
        await interaction.response.send_message(
            f"**Role:** <@&{role_id}>\n**Channel:** <#{channel_id}>\n"
            f"**Challenge:** random (math, code, or type yes)\n"
            f"**Kick if unverified after:** {timeout} minutes\n"
            f"**Lockdown:** {'on' if locked else 'off'}",
            ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Verification(bot))
