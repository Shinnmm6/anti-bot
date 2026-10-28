import discord
from discord.ext import commands

A = discord.AuditLogAction


class AuditLogs(commands.Cog):
    """Logs kicks, bans, unbans and timeouts done by humans or other bots.

    Actions done by THIS bot (slash commands, automod, verification, anti-nuke)
    already log themselves, so they are skipped here to avoid duplicates.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

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
