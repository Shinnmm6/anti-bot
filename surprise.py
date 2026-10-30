import random
import time
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

TIMEOUT_MINUTES = (1, 2, 3)
TIMEOUT_WEIGHTS = (50, 30, 20)  # 1 minute is the most common, 3 minutes the rarest
UNLUCKY = "Better luck next time, you got unlucky today."  # the same words for nothing AND for bad luck


# how each recorded outcome is shown to staff in /surprise logs
OUTCOME_LABELS = {
    "win": "got lucky: won the prize",
    "win_dup": "got lucky, but already had the prize role",
    "win_failed": "got lucky, but I couldn't give the role",
    "timeout": "unlucky: timed out for {m} min",
    "timeout_skipped": "unlucky: {m} min timeout skipped (staff or above me)",
    "nothing": "nothing happened",
}
LUCKY = ("win", "win_dup", "win_failed")
UNLUCKY_OUTCOMES = ("timeout", "timeout_skipped")


def roll(win_chance: float, timeout_chance: float, rng=random) -> tuple[str, int]:
    """One try. Returns ("win", 0), ("timeout", minutes) or ("nothing", 0). Chances are percentages."""
    r = rng.random() * 100
    if r < win_chance:
        return "win", 0
    if r < win_chance + timeout_chance:
        return "timeout", rng.choices(TIMEOUT_MINUTES, weights=TIMEOUT_WEIGHTS)[0]
    return "nothing", 0


class SurprisePanel(discord.ui.View):
    """The Open button under the panel. Persistent: it keeps working after the bot restarts."""

    def __init__(self, cog: "Surprise"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Open the box", style=discord.ButtonStyle.success, custom_id="surprise:open")
    async def open(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.open_box(interaction)


class Surprise(commands.GroupCog, group_name="surprise", group_description="A box with a rare prize and some bad luck"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config: dict[int, dict] = {}
        self.busy: set[tuple[int, int]] = set()  # people whose try is being processed right now

    async def cog_load(self):
        db = self.bot.db
        await db.execute(
            """CREATE TABLE IF NOT EXISTS surprise_config (
                guild_id INTEGER PRIMARY KEY, prize_role_id INTEGER, win_chance REAL, timeout_chance REAL,
                cooldown_seconds INTEGER, title TEXT)""")
        await db.execute(
            """CREATE TABLE IF NOT EXISTS surprise_tries (
                guild_id INTEGER, user_id INTEGER, last_try INTEGER, tries INTEGER DEFAULT 0,
                wins INTEGER DEFAULT 0, timeouts INTEGER DEFAULT 0, PRIMARY KEY (guild_id, user_id))""")
        await db.execute(
            """CREATE TABLE IF NOT EXISTS surprise_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, user_id INTEGER, outcome TEXT,
                minutes INTEGER DEFAULT 0, created_at INTEGER)""")
        try:
            await db.execute("ALTER TABLE surprise_config ADD COLUMN prize_text TEXT")
        except Exception:
            pass  # column already exists
        await db.commit()
        async with db.execute("SELECT guild_id, prize_role_id, win_chance, timeout_chance, cooldown_seconds, title, "
                              "prize_text FROM surprise_config") as cur:
            for gid, role_id, win, tmo, cooldown, title, text in await cur.fetchall():
                self.config[gid] = {"role_id": role_id, "win": win, "timeout": tmo, "cooldown": cooldown,
                                    "title": title, "prize": text or "a role"}
        self.bot.add_view(SurprisePanel(self))

    # ---------- setup ----------

    @app_commands.command(name="setup", description="Post a Surprise Box panel with a rare prize and some bad luck")
    @app_commands.describe(
        channel="Where to post the box",
        prize="What a lucky member wins, in your own words (for example Gift card)",
        role="Optional: also give the winner this role",
        win_chance="Chance to win the prize, in percent (default 5). Kept secret",
        timeout_chance="Chance of bad luck, a 1 to 3 minute timeout, in percent (default 25). Kept secret",
        cooldown_minutes="How long each member waits between tries (default 10)",
        title="Name of the box (default Surprise Box)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def setup_cmd(self, interaction: discord.Interaction, channel: discord.TextChannel,
                        prize: app_commands.Range[str, 1, 80], role: discord.Role | None = None,
                        win_chance: app_commands.Range[float, 0.1, 50.0] = 5.0,
                        timeout_chance: app_commands.Range[float, 0.0, 80.0] = 25.0,
                        cooldown_minutes: app_commands.Range[int, 1, 1440] = 10,
                        title: app_commands.Range[str, 1, 40] = "Surprise Box"):
        guild = interaction.guild
        if role is not None and (role.is_default() or role.managed or role >= guild.me.top_role):
            return await interaction.response.send_message(
                "I can't hand out that role. Pick a normal role that is below my highest role, or leave it empty.",
                ephemeral=True)
        if win_chance + timeout_chance > 100:
            return await interaction.response.send_message(
                "The win chance and the bad luck chance can't add up to more than 100%.", ephemeral=True)
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return await interaction.response.send_message(
                f"I need View Channel, Send Messages and Embed Links in {channel.mention}.", ephemeral=True)
        warning = ""
        if timeout_chance > 0 and not guild.me.guild_permissions.moderate_members:
            warning = ("\n**Warning:** I don't have the Moderate Members permission, so I can't give the timeouts. "
                       "Bad luck will just do nothing until you give it to me.")

        embed = discord.Embed(
            title=title,
            description=("Feeling lucky? Press the button and see what the box has for you. Anything can happen.\n\n"
                         f"You can try once every {cooldown_minutes} minute{'s' if cooldown_minutes != 1 else ''}."),
            color=discord.Color.gold())  # not the prize role's color, which would give it away
        try:
            await channel.send(embed=embed, view=SurprisePanel(self), allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            return await interaction.response.send_message("I couldn't post in that channel.", ephemeral=True)

        cfg = {"role_id": role.id if role else None, "prize": prize, "win": win_chance, "timeout": timeout_chance,
               "cooldown": cooldown_minutes * 60, "title": title}
        await self.bot.db.execute(
            "INSERT OR REPLACE INTO surprise_config (guild_id, prize_role_id, win_chance, timeout_chance, "
            "cooldown_seconds, title, prize_text) VALUES (?,?,?,?,?,?,?)",
            (guild.id, cfg["role_id"], cfg["win"], cfg["timeout"], cfg["cooldown"], cfg["title"], prize))
        await self.bot.db.commit()
        self.config[guild.id] = cfg
        await interaction.response.send_message(f"The {title} is posted in {channel.mention}.{warning}", ephemeral=True)
        await self.bot.log(guild, "Surprise box set up",
                           f"Prize {prize}{' + ' + role.mention if role else ''}, win {win_chance:g}%, bad luck {timeout_chance:g}%, "
                           f"cooldown {cooldown_minutes}m, by {interaction.user}", discord.Color.green())

    @app_commands.command(name="off", description="Turn the Surprise Box off (old panels stop working)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def off(self, interaction: discord.Interaction):
        await self.bot.db.execute("DELETE FROM surprise_config WHERE guild_id=?", (interaction.guild_id,))
        await self.bot.db.commit()
        self.config.pop(interaction.guild_id, None)
        await interaction.response.send_message(
            "The Surprise Box is off. The button on the old panel now says it's closed.", ephemeral=True)

    @app_commands.command(name="status", description="Show the Surprise Box settings and how it has been going")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def status(self, interaction: discord.Interaction):
        cfg = self.config.get(interaction.guild_id)
        if not cfg:
            return await interaction.response.send_message("The Surprise Box isn't set up. Use `/surprise setup`.",
                                                           ephemeral=True)
        async with self.bot.db.execute(
                "SELECT COALESCE(SUM(tries),0), COALESCE(SUM(wins),0), COALESCE(SUM(timeouts),0) "
                "FROM surprise_tries WHERE guild_id=?", (interaction.guild_id,)) as cur:
            tries, wins, timeouts = await cur.fetchone()
        await interaction.response.send_message(
            f"**{cfg['title']}**\nPrize: {cfg['prize']}{' + <@&%d>' % cfg['role_id'] if cfg['role_id'] else ''} ({cfg['win']:g}%)\nBad luck: {cfg['timeout']:g}%\n"
            f"Cooldown: {cfg['cooldown'] // 60} min\nTries so far: **{tries}**, prizes won: **{wins}**, "
            f"timeouts given: **{timeouts}**", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    # ---------- who got lucky or unlucky ----------

    async def record(self, guild_id: int, user_id: int, outcome: str, minutes: int = 0):
        await self.bot.db.execute(
            "INSERT INTO surprise_log (guild_id, user_id, outcome, minutes, created_at) VALUES (?,?,?,?,?)",
            (guild_id, user_id, outcome, minutes, int(time.time())))
        # keep only the newest 5000 entries per server so the table can't grow forever
        await self.bot.db.execute(
            "DELETE FROM surprise_log WHERE guild_id=? AND id <= "
            "(SELECT id FROM surprise_log WHERE guild_id=? ORDER BY id DESC LIMIT 1 OFFSET 5000)", (guild_id, guild_id))
        await self.bot.db.commit()

    @app_commands.command(name="logs", description="See who got lucky or unlucky in the box (only you see it)")
    @app_commands.describe(member="Only show this member", show="What to show (default: everything)")
    @app_commands.choices(show=[app_commands.Choice(name="Everything", value="all"),
                                app_commands.Choice(name="Lucky (prizes)", value="lucky"),
                                app_commands.Choice(name="Unlucky (timeouts)", value="unlucky")])
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def logs(self, interaction: discord.Interaction, member: discord.Member | None = None,
                   show: app_commands.Choice[str] | None = None):
        gid, kind = interaction.guild_id, (show.value if show else "all")
        where, args = "guild_id=?", [gid]
        if member is not None:
            where += " AND user_id=?"
            args.append(member.id)
        wanted = {"lucky": LUCKY, "unlucky": UNLUCKY_OUTCOMES}.get(kind)
        if wanted:
            where += f" AND outcome IN ({','.join('?' * len(wanted))})"
            args += wanted
        async with self.bot.db.execute(
                f"SELECT user_id, outcome, minutes, created_at FROM surprise_log WHERE {where} "
                "ORDER BY id DESC LIMIT 20", args) as cur:
            rows = await cur.fetchall()
        async with self.bot.db.execute(f"SELECT COUNT(*) FROM surprise_log WHERE {where}", args) as cur:
            total = (await cur.fetchone())[0]
        if not rows:
            return await interaction.response.send_message("Nothing to show yet.", ephemeral=True)

        lines = [f"<t:{at}:R> <@{uid}> {OUTCOME_LABELS.get(outcome, outcome).format(m=minutes)}"
                 for uid, outcome, minutes, at in rows]
        embed = discord.Embed(title="Surprise box logs", description="\n".join(lines), color=discord.Color.gold())
        # totals for the same filter: how many times lucky, unlucky and nothing
        async with self.bot.db.execute(
                f"SELECT outcome, COUNT(*) FROM surprise_log WHERE guild_id=?{' AND user_id=?' if member else ''} "
                "GROUP BY outcome", [gid] + ([member.id] if member else [])) as cur:
            counts = dict(await cur.fetchall())
        lucky = sum(counts.get(o, 0) for o in LUCKY)
        unlucky = sum(counts.get(o, 0) for o in UNLUCKY_OUTCOMES)
        who = member.display_name if member else "Everyone"
        embed.set_footer(text=f"{who}: {lucky} lucky, {unlucky} unlucky, {counts.get('nothing', 0)} nothing. "
                              f"Showing the latest {len(rows)} of {total}.")
        await interaction.response.send_message(embed=embed, ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    # ---------- opening the box ----------

    async def open_box(self, interaction: discord.Interaction):
        guild, member = interaction.guild, interaction.user
        cfg = self.config.get(guild.id)
        if not cfg:
            return await interaction.response.send_message("This box is closed.", ephemeral=True)
        key = (guild.id, member.id)
        if key in self.busy:
            return await interaction.response.send_message("Easy there, your last try is still being opened.",
                                                           ephemeral=True)
        self.busy.add(key)
        try:
            await self._open(interaction, cfg)
        finally:
            self.busy.discard(key)

    async def _open(self, interaction: discord.Interaction, cfg: dict):
        await interaction.response.defer(ephemeral=True)  # acknowledge fast, the work below can take a moment
        guild, member = interaction.guild, interaction.user
        now = int(time.time())
        async with self.bot.db.execute("SELECT last_try FROM surprise_tries WHERE guild_id=? AND user_id=?",
                                       (guild.id, member.id)) as cur:
            row = await cur.fetchone()
        if row and now - row[0] < cfg["cooldown"]:
            return await interaction.followup.send(
                f"The box is still recharging for you. Try again <t:{row[0] + cfg['cooldown']}:R>.", ephemeral=True)

        outcome, minutes = roll(cfg["win"], cfg["timeout"])
        role = guild.get_role(cfg["role_id"]) if cfg["role_id"] else None  # the role is optional
        await self.bot.db.execute(
            "INSERT INTO surprise_tries (guild_id, user_id, last_try, tries) VALUES (?,?,?,1) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET last_try=excluded.last_try, tries=tries+1",
            (guild.id, member.id, now))
        await self.bot.db.commit()

        if outcome == "win":
            role_note, given = "", None
            if role is not None and role not in member.roles:
                try:
                    await member.add_roles(role, reason="Won the surprise box")
                    given = role
                except discord.HTTPException:
                    role_note = " I couldn't give you the role, so tell a staff member."
            elif role is not None:
                given = role
            await self.bot.db.execute("UPDATE surprise_tries SET wins=wins+1 WHERE guild_id=? AND user_id=?",
                                      (guild.id, member.id))
            await self.bot.db.commit()
            await self.record(guild.id, member.id, "win_failed" if role_note else "win")
            text = f"JACKPOT! You won **{cfg['prize']}**!"
            if given:
                text += f" You also got {given.mention}."
            text += " A staff member will get your prize to you."
            text += role_note
            await interaction.followup.send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            try:  # everyone gets to see a win, without the prize name
                await interaction.channel.send(
                    f"{member.mention} opened the {cfg['title']} and got lucky!",
                    allowed_mentions=discord.AllowedMentions(users=[member]))
            except discord.HTTPException:
                pass
            return await self.bot.log(guild, "Surprise box prize",
                                      f"{member} ({member.id}) won **{cfg['prize']}**. Give it to them if it isn't a role.",
                                      discord.Color.gold())

        if outcome == "timeout":
            if member.guild_permissions.administrator or member.id == guild.owner_id:
                await self.record(guild.id, member.id, "timeout_skipped", minutes)
                return await interaction.followup.send(UNLUCKY, ephemeral=True)  # staff are immune, say nothing more
            try:
                await member.timeout(timedelta(minutes=minutes), reason="Bad luck in the surprise box")
            except discord.HTTPException:
                await self.record(guild.id, member.id, "timeout_skipped", minutes)
                return await interaction.followup.send(UNLUCKY, ephemeral=True)  # I can't time them out
            await self.bot.db.execute("UPDATE surprise_tries SET timeouts=timeouts+1 WHERE guild_id=? AND user_id=?",
                                      (guild.id, member.id))
            await self.bot.db.commit()
            await self.record(guild.id, member.id, "timeout", minutes)
            await interaction.followup.send(UNLUCKY, ephemeral=True)
            return await self.bot.log(guild, "Surprise box bad luck", f"{member} timed out for {minutes}m")

        await self.record(guild.id, member.id, "nothing")
        await interaction.followup.send(UNLUCKY, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Surprise(bot))
