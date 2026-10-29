import io
import re
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

TICKET_TYPES = {
    "partnership": ("Partnership", "🤝", "Tell us about your server: the invite link, member count and what you'd like to partner on."),
    "rewards": ("Claim rewards", "🎁", "Tell us which reward you're claiming and include proof (screenshots, giveaway link, etc.)."),
    "question": ("Question", "❓", "Ask your question and a staff member will answer as soon as they can."),
    "other": ("Other", "📩", "Tell us what you need help with."),
}


def channel_name(kind: str, user: discord.abc.User) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", user.name.lower()).strip("-") or "user"
    return f"{kind}-{slug}"[:90]


class TicketPanel(discord.ui.View):
    """The buttons under the panel message. Persistent: works again after the bot restarts."""

    def __init__(self, cog: "Tickets"):
        super().__init__(timeout=None)
        self.cog = cog
        for kind, (label, emoji, _) in TICKET_TYPES.items():
            button = discord.ui.Button(label=label, emoji=emoji, style=discord.ButtonStyle.primary,
                                       custom_id=f"ticket:open:{kind}")
            button.callback = self.make_callback(kind)
            self.add_item(button)

    def make_callback(self, kind: str):
        async def callback(interaction: discord.Interaction):
            await self.cog.open_ticket(interaction, kind)
        return callback


class TicketControls(discord.ui.View):
    """The Close button inside each ticket channel. Persistent too."""

    def __init__(self, cog: "Tickets"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Close ticket", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="ticket:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.close_ticket(interaction)


class Tickets(commands.GroupCog, group_name="ticket", group_description="Support tickets"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        db = self.bot.db
        await db.execute(
            """CREATE TABLE IF NOT EXISTS ticket_config (
                guild_id INTEGER PRIMARY KEY, category_id INTEGER, staff_role_id INTEGER, log_channel_id INTEGER)""")
        await db.execute(
            """CREATE TABLE IF NOT EXISTS tickets (
                channel_id INTEGER PRIMARY KEY, guild_id INTEGER, user_id INTEGER, kind TEXT, created_at TEXT)""")
        await db.commit()
        self.bot.add_view(TicketPanel(self))
        self.bot.add_view(TicketControls(self))

    async def config(self, guild_id: int):
        async with self.bot.db.execute(
                "SELECT category_id, staff_role_id, log_channel_id FROM ticket_config WHERE guild_id=?",
                (guild_id,)) as cur:
            row = await cur.fetchone()
        return {"category_id": row[0], "staff_role_id": row[1], "log_channel_id": row[2]} if row else None

    async def ticket_row(self, channel_id: int):
        async with self.bot.db.execute("SELECT user_id, kind FROM tickets WHERE channel_id=?",
                                       (channel_id,)) as cur:
            return await cur.fetchone()

    def is_staff(self, member: discord.Member, cfg: dict) -> bool:
        return (member.guild_permissions.manage_channels
                or any(r.id == cfg["staff_role_id"] for r in member.roles))

    # ---------- /ticket setup ----------

    @app_commands.command(name="setup", description="Post the ticket panel (Partnership, Claim rewards, Question, Other)")
    @app_commands.describe(channel="Where to post the panel", category="Category that new tickets are created in",
                           staff_role="Role that can see and answer every ticket",
                           log_channel="Where transcripts are sent when a ticket closes (optional)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True, manage_roles=True)
    async def setup_cmd(self, interaction: discord.Interaction, channel: discord.TextChannel,
                        category: discord.CategoryChannel, staff_role: discord.Role,
                        log_channel: discord.TextChannel | None = None):
        me = interaction.guild.me
        if not channel.permissions_for(me).send_messages or not channel.permissions_for(me).embed_links:
            return await interaction.response.send_message(
                f"I need Send Messages and Embed Links in {channel.mention}.", ephemeral=True)
        if not category.permissions_for(me).manage_channels:
            return await interaction.response.send_message(
                f"I need Manage Channels in the **{category.name}** category.", ephemeral=True)
        embed = discord.Embed(
            title="Support tickets",
            description="Need help? Press a button below and a private channel will be created for you "
                        "and our staff.\n\n" + "\n".join(f"{e} **{label}**" for label, e, _ in TICKET_TYPES.values()),
            color=discord.Color.blurple())
        try:
            await channel.send(embed=embed, view=TicketPanel(self))
        except discord.HTTPException:
            return await interaction.response.send_message("I couldn't post in that channel.", ephemeral=True)
        await self.bot.db.execute(
            "INSERT OR REPLACE INTO ticket_config (guild_id, category_id, staff_role_id, log_channel_id) "
            "VALUES (?,?,?,?)",
            (interaction.guild_id, category.id, staff_role.id, log_channel.id if log_channel else None))
        await self.bot.db.commit()
        await interaction.response.send_message(f"Ticket panel posted in {channel.mention}.", ephemeral=True)
        await self.bot.log(interaction.guild, "Ticket panel set up",
                           f"Panel in {channel.mention} by {interaction.user}", discord.Color.green())

    # ---------- opening ----------

    async def open_ticket(self, interaction: discord.Interaction, kind: str):
        guild, user = interaction.guild, interaction.user
        cfg = await self.config(guild.id)
        category = guild.get_channel(cfg["category_id"]) if cfg else None
        staff_role = guild.get_role(cfg["staff_role_id"]) if cfg else None
        if not cfg or category is None or staff_role is None:
            return await interaction.response.send_message(
                "Tickets aren't set up properly. Ask an admin to run `/ticket setup` again.", ephemeral=True)

        async with self.bot.db.execute("SELECT channel_id FROM tickets WHERE guild_id=? AND user_id=?",
                                       (guild.id, user.id)) as cur:
            existing = [r[0] for r in await cur.fetchall()]
        for channel_id in existing:
            channel = guild.get_channel(channel_id)
            if channel is not None:
                return await interaction.response.send_message(
                    f"You already have an open ticket: {channel.mention}", ephemeral=True)
            await self.bot.db.execute("DELETE FROM tickets WHERE channel_id=?", (channel_id,))  # deleted by hand
        await self.bot.db.commit()

        label, emoji, hint = TICKET_TYPES[kind]
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                              attach_files=True, embed_links=True),
            staff_role: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                    read_message_history=True, attach_files=True,
                                                    manage_messages=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True,
                                                  read_message_history=True, manage_channels=True),
        }
        await interaction.response.defer(ephemeral=True)
        try:
            channel = await guild.create_text_channel(channel_name(kind, user), category=category,
                                                      overwrites=overwrites, topic=f"{label} ticket for {user} ({user.id})",
                                                      reason=f"Ticket opened by {user}")
        except discord.HTTPException:
            return await interaction.followup.send(
                "I couldn't create the ticket channel (the category may be full, or I'm missing permissions).",
                ephemeral=True)
        await self.bot.db.execute("INSERT INTO tickets (channel_id, guild_id, user_id, kind, created_at) "
                                  "VALUES (?,?,?,?,?)",
                                  (channel.id, guild.id, user.id, kind, datetime.now(timezone.utc).isoformat()))
        await self.bot.db.commit()

        embed = discord.Embed(title=f"{emoji} {label}", description=f"{user.mention}, {hint}\n\n"
                              f"A member of {staff_role.mention} will be with you shortly.",
                              color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
        await channel.send(content=f"{user.mention} {staff_role.mention}", embed=embed, view=TicketControls(self),
                           allowed_mentions=discord.AllowedMentions(users=[user], roles=[staff_role]))
        await interaction.followup.send(f"Your ticket is ready: {channel.mention}", ephemeral=True)
        await self.bot.log(guild, "Ticket opened", f"{label} ticket {channel.mention} by {user}")

    # ---------- closing ----------

    async def close_ticket(self, interaction: discord.Interaction):
        row = await self.ticket_row(interaction.channel_id)
        cfg = await self.config(interaction.guild_id)
        if not row or not cfg:
            return await interaction.response.send_message("This isn't an open ticket.", ephemeral=True)
        owner_id, kind = row
        if interaction.user.id != owner_id and not self.is_staff(interaction.user, cfg):
            return await interaction.response.send_message("Only the ticket owner or staff can close this.",
                                                           ephemeral=True)
        await interaction.response.send_message("Closing this ticket and saving a transcript…")
        channel = interaction.channel
        lines = []
        try:
            async for m in channel.history(limit=None, oldest_first=True):
                text = m.content or ""
                if m.embeds and not text:
                    text = "[embed] " + " ".join(filter(None, (m.embeds[0].title, m.embeds[0].description)))[:300]
                files = " ".join(a.url for a in m.attachments)
                lines.append(f"[{m.created_at:%Y-%m-%d %H:%M}] {m.author} : {text} {files}".strip())
        except discord.HTTPException:
            lines.append("(transcript incomplete: I couldn't read the full history)")
        transcript = discord.File(io.BytesIO("\n".join(lines).encode("utf-8")), filename=f"{channel.name}.txt")

        log_channel = interaction.guild.get_channel(cfg["log_channel_id"]) if cfg["log_channel_id"] else None
        if log_channel is not None:
            try:
                await log_channel.send(
                    f"Ticket **{channel.name}** ({TICKET_TYPES.get(kind, (kind,))[0]}) closed by {interaction.user}. "
                    f"Opened by <@{owner_id}>.", file=transcript, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass
        await self.bot.db.execute("DELETE FROM tickets WHERE channel_id=?", (channel.id,))
        await self.bot.db.commit()
        await self.bot.log(interaction.guild, "Ticket closed", f"{channel.name} closed by {interaction.user}")
        try:
            await channel.delete(reason=f"Ticket closed by {interaction.user}")
        except discord.HTTPException:
            pass

    # ---------- /ticket add and remove (inside a ticket channel) ----------

    async def _staff_in_ticket(self, interaction: discord.Interaction) -> bool:
        row = await self.ticket_row(interaction.channel_id)
        cfg = await self.config(interaction.guild_id)
        if not row or not cfg:
            await interaction.response.send_message("Use this inside a ticket channel.", ephemeral=True)
            return False
        if not self.is_staff(interaction.user, cfg):
            await interaction.response.send_message("Only staff can do that.", ephemeral=True)
            return False
        return True

    @app_commands.command(name="add", description="Add someone to this ticket")
    @app_commands.guild_only()
    async def add(self, interaction: discord.Interaction, member: discord.Member):
        if not await self._staff_in_ticket(interaction):
            return
        await interaction.channel.set_permissions(member, view_channel=True, send_messages=True,
                                                  read_message_history=True, attach_files=True)
        await interaction.response.send_message(f"Added {member.mention} to this ticket.",
                                                allowed_mentions=discord.AllowedMentions(users=[member]))

    @app_commands.command(name="remove", description="Remove someone from this ticket")
    @app_commands.guild_only()
    async def remove(self, interaction: discord.Interaction, member: discord.Member):
        if not await self._staff_in_ticket(interaction):
            return
        row = await self.ticket_row(interaction.channel_id)
        if member.id == row[0]:
            return await interaction.response.send_message("You can't remove the ticket owner. Close the ticket instead.",
                                                           ephemeral=True)
        await interaction.channel.set_permissions(member, overwrite=None)
        await interaction.response.send_message(f"Removed {member.mention} from this ticket.",
                                                allowed_mentions=discord.AllowedMentions.none())

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        # keep the table tidy when a ticket channel is deleted by hand
        await self.bot.db.execute("DELETE FROM tickets WHERE channel_id=?", (channel.id,))
        await self.bot.db.commit()


async def setup(bot: commands.Bot):
    await bot.add_cog(Tickets(bot))
