import random
import re
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

DURATION_RE = re.compile(r"(\d+)\s*([smhdw])", re.I)
UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
MIN_SECONDS, MAX_SECONDS = 10, 30 * 86400


def parse_duration(text: str) -> int | None:
    """'1h30m' -> 5400, '2d' -> 172800, '45 s' -> 45. None if it isn't a valid duration."""
    text = text.strip().lower()
    parts = DURATION_RE.findall(text)
    if not parts or DURATION_RE.sub("", text).strip(" ,"):
        return None
    return sum(int(n) * UNIT_SECONDS[u] for n, u in parts)


class EnterView(discord.ui.View):
    """The Enter button under a giveaway. Persistent: still works after the bot restarts."""

    def __init__(self, cog: "Giveaways", entries: int | None = None):
        super().__init__(timeout=None)
        self.cog = cog
        label = "Enter" if entries is None else f"Enter ({entries})"
        button = discord.ui.Button(label=label, emoji="🎉", style=discord.ButtonStyle.success,
                                   custom_id="giveaway:enter")
        button.callback = self.enter
        self.add_item(button)

    async def enter(self, interaction: discord.Interaction):
        await self.cog.toggle_entry(interaction)


class Giveaways(commands.GroupCog, group_name="giveaway", group_description="Run giveaways"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        db = self.bot.db
        await db.execute(
            """CREATE TABLE IF NOT EXISTS giveaways (
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, channel_id INTEGER, message_id INTEGER,
                prize TEXT, host_id INTEGER, end_at INTEGER, winners INTEGER, ended INTEGER DEFAULT 0,
                required_role_id INTEGER)""")
        await db.execute("CREATE TABLE IF NOT EXISTS giveaway_entries (giveaway_id INTEGER, user_id INTEGER, "
                         "PRIMARY KEY (giveaway_id, user_id))")
        await db.commit()
        self.bot.add_view(EnterView(self))
        self.check_loop.start()

    async def cog_unload(self):
        self.check_loop.cancel()

    # ---------- embeds ----------

    def running_embed(self, gid: int, prize: str, host_id: int, end_at: int, winners: int,
                      role_id: int | None) -> discord.Embed:
        text = (f"Press the button to enter!\n\n**Ends:** <t:{end_at}:R> (<t:{end_at}:f>)\n"
                f"**Winners:** {winners}\n**Hosted by:** <@{host_id}>")
        if role_id:
            text += f"\n**Requires:** <@&{role_id}>"
        embed = discord.Embed(title=f"🎉 {prize}", description=text, color=discord.Color.gold())
        embed.set_footer(text=f"Giveaway ID: {gid}")
        return embed

    def ended_embed(self, gid: int, prize: str, host_id: int, winner_ids: list[int], entries: int) -> discord.Embed:
        winners = ", ".join(f"<@{w}>" for w in winner_ids) if winner_ids else "No valid entries"
        embed = discord.Embed(title=f"🎉 {prize}", color=discord.Color.dark_grey(),
                              description=f"**Ended**\n**Winner{'s' if len(winner_ids) != 1 else ''}:** {winners}\n"
                                          f"**Entries:** {entries}\n**Hosted by:** <@{host_id}>")
        embed.set_footer(text=f"Giveaway ID: {gid}")
        return embed

    # ---------- entering ----------

    async def toggle_entry(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
                "SELECT id, ended, required_role_id FROM giveaways WHERE message_id=?",
                (interaction.message.id,)) as cur:
            row = await cur.fetchone()
        if not row or row[1]:
            return await interaction.response.send_message("This giveaway has ended.", ephemeral=True)
        gid, _, role_id = row
        if role_id and not any(r.id == role_id for r in interaction.user.roles):
            return await interaction.response.send_message(f"You need the <@&{role_id}> role to enter.",
                                                           ephemeral=True,
                                                           allowed_mentions=discord.AllowedMentions.none())
        cur = await self.bot.db.execute("DELETE FROM giveaway_entries WHERE giveaway_id=? AND user_id=?",
                                        (gid, interaction.user.id))
        if cur.rowcount:
            note = "You left the giveaway."
        else:
            await self.bot.db.execute("INSERT OR IGNORE INTO giveaway_entries (giveaway_id, user_id) VALUES (?,?)",
                                      (gid, interaction.user.id))
            note = "You're in! Good luck 🍀 (press the button again to leave)"
        await self.bot.db.commit()
        count = await self.entry_count(gid)
        await interaction.response.edit_message(view=EnterView(self, count))  # shows the live entry count
        await interaction.followup.send(note, ephemeral=True)

    async def entry_count(self, gid: int) -> int:
        async with self.bot.db.execute("SELECT COUNT(*) FROM giveaway_entries WHERE giveaway_id=?", (gid,)) as cur:
            return (await cur.fetchone())[0]

    # ---------- ending ----------

    async def pick_winners(self, guild: discord.Guild, gid: int, count: int, exclude: set[int] = frozenset()) -> list[int]:
        async with self.bot.db.execute("SELECT user_id FROM giveaway_entries WHERE giveaway_id=?", (gid,)) as cur:
            ids = [r[0] for r in await cur.fetchall()]
        valid = [uid for uid in ids if uid not in exclude and guild.get_member(uid) is not None]
        return random.sample(valid, min(count, len(valid)))

    async def end_giveaway(self, gid: int) -> bool:
        """Finish a giveaway. Returns False if it doesn't exist or was already ended."""
        cur = await self.bot.db.execute("UPDATE giveaways SET ended=1 WHERE id=? AND ended=0", (gid,))
        await self.bot.db.commit()
        if not cur.rowcount:
            return False  # claiming it with ended=1 first means two timers can't both announce it
        async with self.bot.db.execute(
                "SELECT guild_id, channel_id, message_id, prize, host_id, winners FROM giveaways WHERE id=?",
                (gid,)) as c:
            guild_id, channel_id, message_id, prize, host_id, winners = await c.fetchone()
        guild = self.bot.get_guild(guild_id)
        channel = guild.get_channel(channel_id) if guild else None
        if guild is None or channel is None:
            return True  # server or channel is gone, nothing to announce
        winner_ids = await self.pick_winners(guild, gid, winners)
        entries = await self.entry_count(gid)
        try:
            message = await channel.fetch_message(message_id)
            await message.edit(embed=self.ended_embed(gid, prize, host_id, winner_ids, entries), view=None)
        except discord.HTTPException:
            pass
        text = (f"🎉 Congratulations {', '.join(f'<@{w}>' for w in winner_ids)}! You won **{prize}**!"
                if winner_ids else f"Nobody entered the giveaway for **{prize}**, so there's no winner.")
        try:
            await channel.send(text, reference=discord.MessageReference(message_id=message_id, channel_id=channel_id,
                                                                        fail_if_not_exists=False),
                               allowed_mentions=discord.AllowedMentions(users=[discord.Object(w) for w in winner_ids]))
        except discord.HTTPException:
            pass
        return True

    @tasks.loop(seconds=15)
    async def check_loop(self):
        async with self.bot.db.execute("SELECT id FROM giveaways WHERE ended=0 AND end_at<=?",
                                       (int(time.time()),)) as cur:
            due = [r[0] for r in await cur.fetchall()]
        for gid in due:  # also catches giveaways that finished while the bot was offline
            try:
                await self.end_giveaway(gid)
            except Exception as e:  # one broken giveaway must not stop the loop
                print(f"giveaway {gid} failed to end: {e!r}")

    @check_loop.before_loop
    async def before_check_loop(self):
        await self.bot.wait_until_ready()

    # ---------- commands ----------

    @app_commands.command(name="start", description="Start a giveaway")
    @app_commands.describe(prize="What you're giving away", duration="How long it runs, e.g. 30m, 2h, 1d, 1d12h",
                           winners="How many winners (default 1)", channel="Where to post it (default: here)",
                           required_role="Only members with this role can enter (optional)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def start(self, interaction: discord.Interaction, prize: str, duration: str,
                    winners: app_commands.Range[int, 1, 20] = 1, channel: discord.TextChannel | None = None,
                    required_role: discord.Role | None = None):
        seconds = parse_duration(duration)
        if seconds is None or not MIN_SECONDS <= seconds <= MAX_SECONDS:
            return await interaction.response.send_message(
                "Use a duration like `30m`, `2h`, `1d` or `1d12h` (between 10 seconds and 30 days).", ephemeral=True)
        if len(prize) > 200:
            return await interaction.response.send_message("The prize can be at most 200 characters.", ephemeral=True)
        channel = channel or interaction.channel
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return await interaction.response.send_message(
                f"I need View Channel, Send Messages and Embed Links in {channel.mention}.", ephemeral=True)

        end_at = int(time.time()) + seconds
        cur = await self.bot.db.execute(
            "INSERT INTO giveaways (guild_id, channel_id, message_id, prize, host_id, end_at, winners, required_role_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (interaction.guild_id, channel.id, 0, prize, interaction.user.id, end_at, winners,
             required_role.id if required_role else None))
        gid = cur.lastrowid
        try:
            message = await channel.send(
                embed=self.running_embed(gid, prize, interaction.user.id, end_at, winners,
                                         required_role.id if required_role else None),
                view=EnterView(self), allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            await self.bot.db.execute("DELETE FROM giveaways WHERE id=?", (gid,))
            await self.bot.db.commit()
            return await interaction.response.send_message("I couldn't post in that channel.", ephemeral=True)
        await self.bot.db.execute("UPDATE giveaways SET message_id=? WHERE id=?", (message.id, gid))
        await self.bot.db.commit()
        await interaction.response.send_message(f"Giveaway started in {channel.mention} (ID {gid}).", ephemeral=True)
        await self.bot.log(interaction.guild, "Giveaway started", f"**{prize}** in {channel.mention} by {interaction.user}")

    @app_commands.command(name="end", description="End a giveaway now and pick the winners")
    @app_commands.describe(giveaway_id="The ID shown at the bottom of the giveaway (or in /giveaway list)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def end(self, interaction: discord.Interaction, giveaway_id: int):
        if not await self.owns(interaction, giveaway_id):
            return
        await interaction.response.defer(ephemeral=True)
        done = await self.end_giveaway(giveaway_id)
        await interaction.followup.send("Giveaway ended." if done else "That giveaway has already ended.",
                                        ephemeral=True)

    @app_commands.command(name="reroll", description="Pick new winners for a giveaway that has ended")
    @app_commands.describe(giveaway_id="The giveaway's ID", winners="How many new winners (default 1)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def reroll(self, interaction: discord.Interaction, giveaway_id: int,
                     winners: app_commands.Range[int, 1, 20] = 1):
        if not await self.owns(interaction, giveaway_id):
            return
        async with self.bot.db.execute("SELECT prize, ended, channel_id FROM giveaways WHERE id=?",
                                       (giveaway_id,)) as cur:
            prize, ended, channel_id = await cur.fetchone()
        if not ended:
            return await interaction.response.send_message("That giveaway is still running. Use `/giveaway end`.",
                                                           ephemeral=True)
        new = await self.pick_winners(interaction.guild, giveaway_id, winners)
        if not new:
            return await interaction.response.send_message("There are no other valid entries to pick from.",
                                                           ephemeral=True)
        channel = interaction.guild.get_channel(channel_id) or interaction.channel
        await channel.send(f"🎉 New winner{'s' if len(new) != 1 else ''} for **{prize}**: "
                           f"{', '.join(f'<@{w}>' for w in new)}! Congratulations!",
                           allowed_mentions=discord.AllowedMentions(users=[discord.Object(w) for w in new]))
        await interaction.response.send_message("Rerolled.", ephemeral=True)

    @app_commands.command(name="list", description="Show the giveaways that are running")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def list_cmd(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
                "SELECT id, prize, channel_id, end_at FROM giveaways WHERE guild_id=? AND ended=0 ORDER BY end_at",
                (interaction.guild_id,)) as cur:
            rows = await cur.fetchall()
        text = "\n".join(f"`{gid}` **{prize}** in <#{ch}> · ends <t:{end}:R>" for gid, prize, ch, end in rows)
        await interaction.response.send_message(text or "No giveaways are running.", ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    async def owns(self, interaction: discord.Interaction, gid: int) -> bool:
        """The ID must exist AND belong to this server (so nobody can touch another server's giveaway)."""
        async with self.bot.db.execute("SELECT guild_id FROM giveaways WHERE id=?", (gid,)) as cur:
            row = await cur.fetchone()
        if not row or row[0] != interaction.guild_id:
            await interaction.response.send_message("I can't find a giveaway with that ID here.", ephemeral=True)
            return False
        return True


async def setup(bot: commands.Bot):
    await bot.add_cog(Giveaways(bot))
