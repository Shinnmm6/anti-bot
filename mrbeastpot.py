import discord
from discord import app_commands
from discord.ext import commands

DESCRIPTION = ("# DO NOT SEND MESSAGES IN THIS CHANNEL\n"
               "This channel is used to catch spam bots. Any messages sent here will result in a {action}.")
ACTION_WORDS = {"softban": "softban", "ban": "ban", "kick": "kick"}


class MrBeastPot(commands.GroupCog, group_name="mrbeastpot", group_description="A trap channel that catches spam bots"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config: dict[int, dict] = {}  # guild_id -> {channel_id, message_id, action, bans}

    async def cog_load(self):
        db = self.bot.db
        await db.execute(
            """CREATE TABLE IF NOT EXISTS mrbeastpot_config (
                guild_id INTEGER PRIMARY KEY, channel_id INTEGER, message_id INTEGER,
                action TEXT, bans INTEGER DEFAULT 0)""")
        await db.commit()
        async with db.execute("SELECT guild_id, channel_id, message_id, action, bans FROM mrbeastpot_config") as cur:
            for gid, ch, msg, action, bans in await cur.fetchall():
                self.config[gid] = {"channel_id": ch, "message_id": msg, "action": action, "bans": bans}

    @staticmethod
    def make_embed(action: str, bans: int) -> discord.Embed:
        embed = discord.Embed(description=DESCRIPTION.format(action=ACTION_WORDS[action]),
                              color=discord.Color.from_rgb(37, 37, 43))
        embed.set_footer(text=f"Bans: {bans}")
        return embed

    # ---------- setup ----------

    @app_commands.command(name="setup", description="Post the trap message in a channel that catches spam bots")
    @app_commands.describe(channel="The trap channel (spam bots post in every channel they can see)",
                           action="What happens to anyone who posts there (default: softban)")
    @app_commands.choices(action=[
        app_commands.Choice(name="Softban (ban, delete their messages, unban)", value="softban"),
        app_commands.Choice(name="Ban", value="ban"),
        app_commands.Choice(name="Kick", value="kick")])
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.checks.bot_has_permissions(ban_members=True, kick_members=True, manage_messages=True)
    async def setup_cmd(self, interaction: discord.Interaction, channel: discord.TextChannel,
                        action: app_commands.Choice[str] | None = None):
        guild = interaction.guild
        act = action.value if action else "softban"
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links and perms.manage_messages):
            return await interaction.response.send_message(
                f"I need View Channel, Send Messages, Embed Links and Manage Messages in {channel.mention}.",
                ephemeral=True)
        old = self.config.get(guild.id)
        bans = old["bans"] if old else 0  # keep the counter if the trap is moved
        try:
            msg = await channel.send(embed=self.make_embed(act, bans))
        except discord.HTTPException:
            return await interaction.response.send_message("I couldn't post in that channel.", ephemeral=True)
        cfg = {"channel_id": channel.id, "message_id": msg.id, "action": act, "bans": bans}
        await self.bot.db.execute(
            "INSERT OR REPLACE INTO mrbeastpot_config (guild_id, channel_id, message_id, action, bans) "
            "VALUES (?,?,?,?,?)", (guild.id, channel.id, msg.id, act, bans))
        await self.bot.db.commit()
        self.config[guild.id] = cfg
        await interaction.response.send_message(
            f"The trap is set in {channel.mention}. Anyone without staff permissions who posts there gets a {act}. "
            "Tip: let @everyone see the channel, and keep real members from needing it.", ephemeral=True)
        await self.bot.log(guild, "Mrbeastpot set up", f"{channel.mention}, action {act}, by {interaction.user}",
                           discord.Color.green())

    @app_commands.command(name="off", description="Remove the trap (the old message is left alone)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def off(self, interaction: discord.Interaction):
        await self.bot.db.execute("DELETE FROM mrbeastpot_config WHERE guild_id=?", (interaction.guild_id,))
        await self.bot.db.commit()
        self.config.pop(interaction.guild_id, None)
        await interaction.response.send_message("The trap is off. Posting there is safe again.", ephemeral=True)

    @app_commands.command(name="status", description="Show where the trap is and how many it caught")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def status(self, interaction: discord.Interaction):
        cfg = self.config.get(interaction.guild_id)
        if not cfg:
            return await interaction.response.send_message("No trap set. Use `/mrbeastpot setup`.", ephemeral=True)
        await interaction.response.send_message(
            f"Trap channel: <#{cfg['channel_id']}>\nAction: {cfg['action']}\nCaught so far: **{cfg['bans']}**",
            ephemeral=True)

    # ---------- the trap ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        guild = message.guild
        if guild is None or message.author.bot:
            return
        cfg = self.config.get(guild.id)
        if not cfg or message.channel.id != cfg["channel_id"]:
            return
        member = message.author
        if not isinstance(member, discord.Member):
            return
        # never touch staff, the owner, or anyone I can't act on
        if (member.id == guild.owner_id or member.guild_permissions.administrator
                or member.guild_permissions.manage_messages or member.guild_permissions.ban_members
                or member.top_role >= guild.me.top_role):
            return
        action = cfg["action"]
        try:
            if action == "kick":
                await message.delete()
                await member.kick(reason="Mrbeastpot: posted in the trap channel")
            else:
                # banning with message deletion wipes their recent messages everywhere (up to 7 days)
                await member.ban(reason="Mrbeastpot: posted in the trap channel", delete_message_seconds=604800)
                if action == "softban":
                    await guild.unban(member, reason="Mrbeastpot softban")
        except discord.HTTPException:
            return await self.bot.log(guild, "Mrbeastpot failed",
                                      f"I couldn't {action} {member} ({member.id}). Check my permissions and role order.",
                                      discord.Color.red())
        cfg["bans"] += 1
        await self.bot.db.execute("UPDATE mrbeastpot_config SET bans=? WHERE guild_id=?", (cfg["bans"], guild.id))
        await self.bot.db.commit()
        await self.bot.log(guild, "Mrbeastpot caught someone",
                           f"{member} ({member.id}) posted in {message.channel.mention}: {action}\n"
                           f"{message.content[:300] or '(no text)'}", discord.Color.red())
        try:  # keep the counter on the panel up to date
            panel = message.channel.get_partial_message(cfg["message_id"])
            await panel.edit(embed=self.make_embed(action, cfg["bans"]))
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(MrBeastPot(bot))
