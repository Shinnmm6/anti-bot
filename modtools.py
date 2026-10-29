from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands


class ModTools(commands.Cog):
    """Extra moderation tools: /unban, /clearwarnings, /slowmode, /lock, /unlock, /userinfo."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------- /unban ----------

    @app_commands.command(name="unban", description="Unban a user by their ID")
    @app_commands.describe(user_id="The user's ID (right-click them with Developer Mode on, or copy it from the ban list)",
                           reason="Why they are being unbanned")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: str = "No reason given"):
        try:
            uid = int(user_id.strip())
        except ValueError:
            return await interaction.response.send_message("That isn't a valid user ID (numbers only).",
                                                           ephemeral=True)
        try:
            await interaction.guild.unban(discord.Object(id=uid), reason=f"{interaction.user}: {reason}")
        except discord.NotFound:
            return await interaction.response.send_message("That user isn't banned here.", ephemeral=True)
        await interaction.response.send_message(f"Unbanned user `{uid}`.")
        await self.bot.log(interaction.guild, "Unban", f"`{uid}` unbanned by {interaction.user}\n{reason}",
                           discord.Color.green())

    # ---------- /clearwarnings ----------

    @app_commands.command(name="clearwarnings", description="Remove all warnings from a user")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(moderate_members=True)
    async def clearwarnings(self, interaction: discord.Interaction, user: discord.User):
        cur = await self.bot.db.execute("DELETE FROM warnings WHERE guild_id=? AND user_id=?",
                                        (interaction.guild_id, user.id))
        await self.bot.db.commit()
        removed = cur.rowcount
        if not removed:
            return await interaction.response.send_message(f"{user} has no warnings.", ephemeral=True)
        await interaction.response.send_message(
            f"Cleared {removed} warning{'s' if removed != 1 else ''} for **{user}**.")
        await self.bot.log(interaction.guild, "Warnings cleared",
                           f"{removed} warning(s) for {user} cleared by {interaction.user}")

    # ---------- /slowmode ----------

    @app_commands.command(name="slowmode", description="Set a channel's slowmode (0 turns it off)")
    @app_commands.describe(seconds="Seconds between messages per person, 0 to 21600",
                           channel="Channel to change (defaults to this one)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def slowmode(self, interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 21600],
                       channel: discord.TextChannel | None = None):
        channel = channel or interaction.channel
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message("Slowmode only works in text channels.", ephemeral=True)
        await channel.edit(slowmode_delay=seconds, reason=f"Slowmode by {interaction.user}")
        text = (f"Slowmode in {channel.mention} set to **{seconds}s**." if seconds
                else f"Slowmode in {channel.mention} turned **off**.")
        await interaction.response.send_message(text)
        await self.bot.log(interaction.guild, "Slowmode",
                           f"{channel.mention} set to {seconds}s by {interaction.user}")

    # ---------- /lock and /unlock ----------

    async def _set_locked(self, interaction: discord.Interaction, channel: discord.TextChannel | None,
                          locked: bool):
        channel = channel or interaction.channel
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message("Only text channels can be locked.", ephemeral=True)
        everyone = interaction.guild.default_role
        overwrite = channel.overwrites_for(everyone)
        overwrite.send_messages = False if locked else None  # None = back to the server default
        await channel.set_permissions(everyone, overwrite=overwrite,
                                      reason=f"{'Lock' if locked else 'Unlock'} by {interaction.user}")
        await interaction.response.send_message(
            f"{'Locked' if locked else 'Unlocked'} {channel.mention}. "
            + ("Members can no longer send messages." if locked else "Members can send messages again."))
        await self.bot.log(interaction.guild, "Channel locked" if locked else "Channel unlocked",
                           f"{channel.mention} by {interaction.user}",
                           discord.Color.red() if locked else discord.Color.green())

    @app_commands.command(name="lock", description="Stop @everyone from sending messages in a channel")
    @app_commands.describe(channel="Channel to lock (defaults to this one)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def lock(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        await self._set_locked(interaction, channel, True)

    @app_commands.command(name="unlock", description="Let @everyone send messages in a channel again")
    @app_commands.describe(channel="Channel to unlock (defaults to this one)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def unlock(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        await self._set_locked(interaction, channel, False)

    # ---------- /userinfo ----------

    @app_commands.command(name="userinfo", description="Show info about a member")
    @app_commands.describe(member="Who to look up (defaults to you)")
    @app_commands.guild_only()
    async def userinfo(self, interaction: discord.Interaction, member: discord.Member | None = None):
        member = member or interaction.user
        roles = [r.mention for r in reversed(member.roles) if not r.is_default()]
        roles_text = " ".join(roles[:15]) + (f" …and {len(roles) - 15} more" if len(roles) > 15 else "")
        embed = discord.Embed(title=str(member), color=member.color if member.color.value else discord.Color.blurple(),
                              timestamp=datetime.now(timezone.utc))
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="ID", value=f"`{member.id}`")
        embed.add_field(name="Bot", value="Yes" if member.bot else "No")
        embed.add_field(name="Top role", value=member.top_role.mention if not member.top_role.is_default()
                        else "None")
        embed.add_field(name="Account created", value=discord.utils.format_dt(member.created_at, "R"))
        if member.joined_at:
            embed.add_field(name="Joined server", value=discord.utils.format_dt(member.joined_at, "R"))
        if member.timed_out_until and member.timed_out_until > datetime.now(timezone.utc):
            embed.add_field(name="Timed out until", value=discord.utils.format_dt(member.timed_out_until, "R"))
        embed.add_field(name=f"Roles ({len(roles)})", value=roles_text or "None", inline=False)
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot):
    await bot.add_cog(ModTools(bot))
