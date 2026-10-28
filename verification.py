import random
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

MAX_FAILS = 5  # wrong challenge answers before the member is kicked


class ChallengeModal(discord.ui.Modal, title="Quick verification"):
    def __init__(self, cog: "Verification", answer: int, question: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.answer = answer
        self.box = discord.ui.TextInput(
            label=question, placeholder="Type the answer", min_length=1, max_length=4)
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
        await self.bot.db.execute(
            """CREATE TABLE IF NOT EXISTS verify_config (
                guild_id INTEGER PRIMARY KEY,
                role_id INTEGER, channel_id INTEGER,
                min_age_days INTEGER, timeout_minutes INTEGER,
                enabled_at TEXT)"""
        )
        await self.bot.db.commit()
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

        member = interaction.user
        if role in member.roles:
            return await interaction.response.send_message("You're already verified.", ephemeral=True)

        account_age = datetime.now(timezone.utc) - member.created_at
        if account_age >= timedelta(days=cfg[2]):
            return await self.grant(interaction, member, role)

        # Newer account: ask a quick question
        a, b = random.randint(10, 49), random.randint(10, 49)
        await interaction.response.send_modal(ChallengeModal(self, a + b, f"What is {a} + {b}?"))

    async def grant(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role):
        self.fails.pop((interaction.guild_id, member.id), None)
        try:
            await member.add_roles(role, reason="Passed verification")
        except discord.HTTPException:
            msg = "I couldn't give you the role. Please tell a moderator."
        else:
            msg = "You're verified. Welcome!"
        await interaction.response.send_message(msg, ephemeral=True)

    async def check_answer(self, interaction: discord.Interaction, answer: int, given: str):
        cfg = await self.get_config(interaction.guild_id)
        role = interaction.guild.get_role(cfg[0]) if cfg else None
        if not cfg or role is None:
            return await interaction.response.send_message(
                "Verification isn't set up properly. Please tell a moderator.", ephemeral=True)

        member = interaction.user
        if given.strip() == str(answer):
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
                f"Wrong answer. Click **Verify** again for a new question. ({left} tries left)",
                ephemeral=True)

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
        min_account_age_days="Accounts at least this old only need to click the button",
        timeout_minutes="Kick unverified new joiners after this many minutes")
    async def setup_cmd(
        self, interaction: discord.Interaction,
        member_role: discord.Role, verify_channel: discord.TextChannel,
        min_account_age_days: app_commands.Range[int, 0, 365] = 7,
        timeout_minutes: app_commands.Range[int, 2, 1440] = 15,
    ):
        guild = interaction.guild
        if member_role.is_default() or member_role.managed:
            return await interaction.response.send_message(
                "Pick a normal role (not @everyone or a bot/integration role).", ephemeral=True)
        if member_role >= guild.me.top_role:
            return await interaction.response.send_message(
                "That role is at or above my top role, so I can't hand it out. "
                "Move my role above it in Server Settings > Roles.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        try:
            await verify_channel.set_permissions(
                guild.default_role, view_channel=True, send_messages=False,
                add_reactions=False, reason="Verification setup")
            await verify_channel.set_permissions(
                member_role, view_channel=False, reason="Verification setup")
            embed = discord.Embed(
                title="Verify to get access",
                description="Click the button below to verify and unlock the server.\n"
                            f"Newer accounts will get a quick question first. "
                            f"Unverified members are removed after {timeout_minutes} minutes.",
                color=discord.Color.green())
            await verify_channel.send(embed=embed, view=VerifyView(self))
        except discord.Forbidden:
            return await interaction.followup.send(
                f"I don't have permission to edit or post in {verify_channel.mention}.", ephemeral=True)

        await self.bot.db.execute(
            "INSERT OR REPLACE INTO verify_config "
            "(guild_id, role_id, channel_id, min_age_days, timeout_minutes, enabled_at) "
            "VALUES (?,?,?,?,?,?)",
            (guild.id, member_role.id, verify_channel.id, min_account_age_days,
             timeout_minutes, datetime.now(timezone.utc).isoformat()))
        await self.bot.db.commit()

        await interaction.followup.send(
            f"Verification is on. Panel posted in {verify_channel.mention}.\n\n"
            f"**One manual step left:** in Server Settings > Roles, turn OFF *View Channels* for "
            f"@everyone and turn it ON for {member_role.mention}. Otherwise unverified people "
            f"can still see the whole server. Existing members won't be kicked, only people who "
            f"join from now on. Give the member role to anyone who should already have access.",
            ephemeral=True)

    @app_commands.command(description="Turn verification off")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def disable(self, interaction: discord.Interaction):
        await self.bot.db.execute("DELETE FROM verify_config WHERE guild_id=?", (interaction.guild_id,))
        await self.bot.db.commit()
        await interaction.response.send_message(
            "Verification disabled. The button no longer works and no one will be auto-kicked. "
            "You can delete the panel message.", ephemeral=True)

    @app_commands.command(description="Show verification settings")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def status(self, interaction: discord.Interaction):
        cfg = await self.get_config(interaction.guild_id)
        if not cfg:
            return await interaction.response.send_message("Verification is not set up.", ephemeral=True)
        role_id, channel_id, age, timeout, _ = cfg
        await interaction.response.send_message(
            f"**Role:** <@&{role_id}>\n**Channel:** <#{channel_id}>\n"
            f"**Button only for accounts older than:** {age} days (younger ones get a question)\n"
            f"**Kick if unverified after:** {timeout} minutes",
            ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Verification(bot))
