import discord
from discord import app_commands
from discord.ext import commands

A = discord.AuditLogAction


class AuditLogs(commands.GroupCog, group_name="auditlog", group_description="Choose where bot logs are sent"):
    """Logs kicks, bans, unbans and timeouts done by humans or other bots.

    Actions done by THIS bot (slash commands, automod, verification, anti-nuke)
    already log themselves, so they are skipped here to avoid duplicates.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        db = self.bot.db
        await db.execute(
            "CREATE TABLE IF NOT EXISTS log_channels (guild_id INTEGER PRIMARY KEY, channel_id INTEGER)")
        await db.commit()
        async with db.execute("SELECT guild_id, channel_id FROM log_channels") as cur:
            for guild_id, channel_id in await cur.fetchall():
                self.bot.log_channels[guild_id] = channel_id

    # ---------- /auditlog commands ----------

    @app_commands.command(name="set", description="Choose the channel where all bot logs are sent")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return await interaction.response.send_message(
                f"I need View Channel, Send Messages and Embed Links in {channel.mention}. "
                "Fix that and run the command again.", ephemeral=True)
        await self.bot.db.execute(
            "INSERT OR REPLACE INTO log_channels (guild_id, channel_id) VALUES (?, ?)",
            (interaction.guild_id, channel.id))
        await self.bot.db.commit()
        self.bot.log_channels[interaction.guild_id] = channel.id
        await interaction.response.send_message(
            f"Logs will now go to {channel.mention}. Tip: make that channel staff-only.", ephemeral=True)
        await self.bot.log(interaction.guild, "Logging enabled",
                           f"Log channel set by {interaction.user.mention}.", discord.Color.green())

    @app_commands.command(name="off", description="Stop sending logs to the saved channel")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def off(self, interaction: discord.Interaction):
        await self.bot.db.execute("DELETE FROM log_channels WHERE guild_id=?", (interaction.guild_id,))
        await self.bot.db.commit()
        self.bot.log_channels.pop(interaction.guild_id, None)
        await interaction.response.send_message(
            "Saved log channel removed. If a LOG_CHANNEL_ID is set in the bot's variables, "
            "logs fall back to that one; otherwise logging is off.", ephemeral=True)

    @app_commands.command(name="status", description="Show where logs are being sent")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def status(self, interaction: discord.Interaction):
        channel_id = self.bot.log_channels.get(interaction.guild_id)
        if channel_id:
            msg = f"Logs go to <#{channel_id}> (set with /auditlog set)."
        else:
            msg = "No channel saved with /auditlog set. Use `/auditlog set` to pick one."
        await interaction.response.send_message(msg, ephemeral=True)

    # ---------- audit log listener ----------

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry):
        if entry.user_id is None or entry.user_id == self.bot.user.id:
            return

        guild = entry.guild
        who = f"<@{entry.user_id}>"
        whom = f"<@{entry.target.id}>" if entry.target is not None else "unknown"
        reason = entry.reason or "No reason given"

        if entry.action == A.kick:
            await self.bot.log(guild, "Kick", f"{whom} kicked by {who}\n**Reason:** {reason}",
                               discord.Color.orange())
        elif entry.action == A.ban:
            await self.bot.log(guild, "Ban", f"{whom} banned by {who}\n**Reason:** {reason}",
                               discord.Color.red())
        elif entry.action == A.unban:
            await self.bot.log(guild, "Unban", f"{whom} unbanned by {who}\n**Reason:** {reason}",
                               discord.Color.green())
        elif entry.action == A.member_update and hasattr(entry.after, "timed_out_until"):
            until = entry.after.timed_out_until
            if until:
                await self.bot.log(
                    guild, "Timeout",
                    f"{whom} timed out by {who} until {discord.utils.format_dt(until, 'R')}\n"
                    f"**Reason:** {reason}", discord.Color.orange())
            else:
                await self.bot.log(guild, "Timeout removed", f"{whom} timeout removed by {who}",
                                   discord.Color.green())


async def setup(bot: commands.Bot):
    await bot.add_cog(AuditLogs(bot))
