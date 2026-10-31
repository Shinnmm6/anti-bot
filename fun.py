import calendar
import random
import time
from datetime import date, datetime, timedelta, timezone
from itertools import combinations

import discord
from discord import app_commands
from discord.ext import commands, tasks

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]


def bar(pct: float, width: int = 12) -> str:
    n = round(pct / 100 * width)
    return "#" * n + "-" * (width - n)


# =====================================================================
# /wyr : would you rather, with live percentages
# =====================================================================

def wyr_embed(a: str, b: str, ca: int, cb: int, closed: bool = False, author: str | None = None) -> discord.Embed:
    total = ca + cb
    pa = ca * 100 / total if total else 0
    pb = cb * 100 / total if total else 0
    embed = discord.Embed(title="Would you rather...", color=discord.Color.dark_grey() if closed else discord.Color.blurple())
    embed.add_field(name=f"A. {a}"[:256], value=f"`{bar(pa)}` {pa:.0f}% ({ca})", inline=False)
    embed.add_field(name=f"B. {b}"[:256], value=f"`{bar(pb)}` {pb:.0f}% ({cb})", inline=False)
    foot = f"{total} vote{'s' if total != 1 else ''}. You can change your vote."
    if closed:
        foot = f"Closed. {total} vote{'s' if total != 1 else ''}."
    if author:
        foot += f" Asked by {author}."
    embed.set_footer(text=foot)
    return embed


class WyrView(discord.ui.View):
    def __init__(self, cog, a: str = "Option A", b: str = "Option B", disabled: bool = False):
        super().__init__(timeout=None)
        self.cog = cog
        self.vote_a.label, self.vote_b.label = a[:80], b[:80]
        self.vote_a.disabled = self.vote_b.disabled = disabled

    @discord.ui.button(label="Option A", style=discord.ButtonStyle.primary, custom_id="wyr:a")
    async def vote_a(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.vote(interaction, "a")

    @discord.ui.button(label="Option B", style=discord.ButtonStyle.danger, custom_id="wyr:b")
    async def vote_b(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.vote(interaction, "b")


class Wyr(commands.GroupCog, group_name="wyr", group_description="Would you rather polls with live results"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        db = self.bot.db
        await db.execute(
            """CREATE TABLE IF NOT EXISTS wyr_polls (
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, channel_id INTEGER, message_id INTEGER,
                creator_id INTEGER, a TEXT, b TEXT, closed INTEGER DEFAULT 0)""")
        await db.execute(
            """CREATE TABLE IF NOT EXISTS wyr_votes (
                poll_id INTEGER, user_id INTEGER, choice TEXT, PRIMARY KEY (poll_id, user_id))""")
        await db.commit()
        self.bot.add_view(WyrView(self))

    async def counts(self, poll_id: int) -> tuple[int, int]:
        async with self.bot.db.execute("SELECT choice, COUNT(*) FROM wyr_votes WHERE poll_id=? GROUP BY choice",
                                       (poll_id,)) as cur:
            c = dict(await cur.fetchall())
        return c.get("a", 0), c.get("b", 0)

    @app_commands.command(name="ask", description="Post a would you rather question")
    @app_commands.describe(option_a="The first choice", option_b="The second choice")
    @app_commands.guild_only()
    async def ask(self, interaction: discord.Interaction, option_a: app_commands.Range[str, 1, 200],
                  option_b: app_commands.Range[str, 1, 200]):
        await interaction.response.send_message(
            embed=wyr_embed(option_a, option_b, 0, 0, author=interaction.user.display_name),
            view=WyrView(self, option_a, option_b), allowed_mentions=discord.AllowedMentions.none())
        msg = await interaction.original_response()
        await self.bot.db.execute(
            "INSERT INTO wyr_polls (guild_id, channel_id, message_id, creator_id, a, b) VALUES (?,?,?,?,?,?)",
            (interaction.guild_id, msg.channel.id, msg.id, interaction.user.id, option_a, option_b))
        await self.bot.db.commit()

    async def vote(self, interaction: discord.Interaction, choice: str):
        async with self.bot.db.execute("SELECT id, a, b, closed, creator_id FROM wyr_polls WHERE message_id=?",
                                       (interaction.message.id,)) as cur:
            row = await cur.fetchone()
        if not row:
            return await interaction.response.send_message("I can't find that poll anymore.", ephemeral=True)
        pid, a, b, closed, _ = row
        if closed:
            return await interaction.response.send_message("This poll is closed.", ephemeral=True)
        await self.bot.db.execute(
            "INSERT INTO wyr_votes (poll_id, user_id, choice) VALUES (?,?,?) "
            "ON CONFLICT(poll_id, user_id) DO UPDATE SET choice=excluded.choice", (pid, interaction.user.id, choice))
        await self.bot.db.commit()
        ca, cb = await self.counts(pid)
        foot = interaction.message.embeds[0].footer.text if interaction.message.embeds else ""
        author = foot.rsplit("Asked by ", 1)[1].rstrip(".") if "Asked by " in (foot or "") else None
        await interaction.response.edit_message(embed=wyr_embed(a, b, ca, cb, author=author))
        await interaction.followup.send(f"You voted **{'A' if choice == 'a' else 'B'}**.", ephemeral=True)

    @app_commands.command(name="end", description="Close a would you rather poll")
    @app_commands.describe(message_id="The ID of the poll message (right click it, Copy Message ID)")
    @app_commands.guild_only()
    async def end(self, interaction: discord.Interaction, message_id: str):
        if not message_id.isdigit():
            return await interaction.response.send_message("That isn't a message ID.", ephemeral=True)
        async with self.bot.db.execute(
                "SELECT id, channel_id, a, b, creator_id, closed FROM wyr_polls WHERE message_id=? AND guild_id=?",
                (int(message_id), interaction.guild_id)) as cur:
            row = await cur.fetchone()
        if not row:
            return await interaction.response.send_message("I don't know a poll with that ID.", ephemeral=True)
        pid, channel_id, a, b, creator, closed = row
        if interaction.user.id != creator and not interaction.user.guild_permissions.manage_messages:
            return await interaction.response.send_message("Only the person who asked, or a moderator, can close it.",
                                                           ephemeral=True)
        if closed:
            return await interaction.response.send_message("It's already closed.", ephemeral=True)
        await self.bot.db.execute("UPDATE wyr_polls SET closed=1 WHERE id=?", (pid,))
        await self.bot.db.commit()
        ca, cb = await self.counts(pid)
        channel = interaction.guild.get_channel(channel_id)
        try:
            msg = await channel.fetch_message(int(message_id))
            await msg.edit(embed=wyr_embed(a, b, ca, cb, closed=True), view=WyrView(self, a, b, disabled=True))
        except (discord.HTTPException, AttributeError):
            pass
        await interaction.response.send_message("Poll closed.", ephemeral=True)


# =====================================================================
# /pairup : opt in, then staff match everyone into pairs
# =====================================================================

def make_pairs(ids: list[int], past: set, rng=random, tries: int = 60):
    """Pair people up, avoiding people who were matched recently. An odd one out joins the last pair."""
    if len(ids) < 2:
        return [], 0
    best, best_score = None, None
    for _ in range(tries):
        order = list(ids)
        rng.shuffle(order)
        groups = [tuple(order[i:i + 2]) for i in range(0, len(order) - 1, 2)]
        if len(order) % 2:
            groups[-1] = groups[-1] + (order[-1],)
        score = sum(1 for g in groups for x, y in combinations(g, 2) if frozenset((x, y)) in past)
        if best_score is None or score < best_score:
            best, best_score = groups, score
        if best_score == 0:
            break
    return best, best_score


class Pairup(commands.GroupCog, group_name="pairup", group_description="Meet someone new in the server"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        db = self.bot.db
        await db.execute("CREATE TABLE IF NOT EXISTS pairup_members (guild_id INTEGER, user_id INTEGER, "
                         "interest TEXT, PRIMARY KEY (guild_id, user_id))")
        await db.execute("CREATE TABLE IF NOT EXISTS pairup_history (guild_id INTEGER, a INTEGER, b INTEGER, "
                         "created_at INTEGER)")
        await db.commit()

    @app_commands.command(name="join", description="Join the pool to be matched with another member")
    @app_commands.describe(interest="What you like talking about, so your match has a way to start")
    @app_commands.guild_only()
    async def join(self, interaction: discord.Interaction, interest: app_commands.Range[str, 1, 100] = "anything"):
        await self.bot.db.execute(
            "INSERT INTO pairup_members (guild_id, user_id, interest) VALUES (?,?,?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET interest=excluded.interest",
            (interaction.guild_id, interaction.user.id, interest))
        await self.bot.db.commit()
        await interaction.response.send_message(
            f"You're in. Next time staff run a match you'll be paired with someone. Your topic: **{interest}**.",
            ephemeral=True)

    @app_commands.command(name="leave", description="Leave the pool")
    @app_commands.guild_only()
    async def leave(self, interaction: discord.Interaction):
        await self.bot.db.execute("DELETE FROM pairup_members WHERE guild_id=? AND user_id=?",
                                  (interaction.guild_id, interaction.user.id))
        await self.bot.db.commit()
        await interaction.response.send_message("You left the pool.", ephemeral=True)

    @app_commands.command(name="match", description="Match everyone in the pool into pairs (staff)")
    @app_commands.describe(channel="Where to announce the pairs (default: here)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def match(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        guild = interaction.guild
        channel = channel or interaction.channel
        async with self.bot.db.execute("SELECT user_id, interest FROM pairup_members WHERE guild_id=?",
                                       (guild.id,)) as cur:
            rows = await cur.fetchall()
        interests = {uid: i for uid, i in rows if (m := guild.get_member(uid)) and not m.bot}
        if len(interests) < 2:
            return await interaction.response.send_message(
                f"Only {len(interests)} member(s) in the pool. I need at least 2. Members join with `/pairup join`.",
                ephemeral=True)
        cutoff = int(time.time()) - 120 * 86400
        async with self.bot.db.execute("SELECT a, b FROM pairup_history WHERE guild_id=? AND created_at>?",
                                       (guild.id, cutoff)) as cur:
            past = {frozenset(r) for r in await cur.fetchall()}
        groups, repeats = make_pairs(list(interests), past)

        now = int(time.time())
        for g in groups:
            for x, y in combinations(g, 2):
                await self.bot.db.execute("INSERT INTO pairup_history (guild_id, a, b, created_at) VALUES (?,?,?,?)",
                                          (guild.id, x, y, now))
        await self.bot.db.commit()

        lines = [" and ".join(f"<@{u}>" for u in g) + " (" + ", ".join(sorted({interests[u] for u in g})) + ")"
                 for g in groups]
        await interaction.response.send_message(f"Matched {len(interests)} members into {len(groups)} groups.",
                                                ephemeral=True)
        chunk, first = "", True
        header = "**Pair up!** Say hi to your match and start a chat.\n"
        for line in lines + [None]:
            if line is None or len(chunk) + len(line) > 1700:
                if chunk:
                    try:
                        await channel.send((header if first else "") + chunk,
                                           allowed_mentions=discord.AllowedMentions(users=True))
                    except discord.HTTPException:
                        return await interaction.followup.send("I couldn't post in that channel.", ephemeral=True)
                    first, chunk = False, ""
            if line:
                chunk += line + "\n"

    @app_commands.command(name="pool", description="See how many members are in the pool")
    @app_commands.guild_only()
    async def pool(self, interaction: discord.Interaction):
        async with self.bot.db.execute("SELECT COUNT(*) FROM pairup_members WHERE guild_id=?",
                                       (interaction.guild_id,)) as cur:
            (n,) = await cur.fetchone()
        await interaction.response.send_message(f"**{n}** member{'s' if n != 1 else ''} in the pool. "
                                                "Join with `/pairup join`.", ephemeral=True)


# =====================================================================
# /vibe : set your mood, see the server's mood
# =====================================================================

MOODS = ["Happy", "Chill", "Hyped", "Focused", "Bored", "Tired", "Stressed", "Sad"]


class VibeView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.select(custom_id="vibe:pick", placeholder="How are you feeling right now?",
                       options=[discord.SelectOption(label=m, value=m) for m in MOODS])
    async def pick(self, interaction: discord.Interaction, select: discord.ui.Select):
        mood = select.values[0]
        await self.cog.bot.db.execute(
            "INSERT INTO vibe_moods (guild_id, user_id, mood, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET mood=excluded.mood, updated_at=excluded.updated_at",
            (interaction.guild_id, interaction.user.id, mood, int(time.time())))
        await self.cog.bot.db.commit()
        await interaction.response.send_message(f"Your vibe is set to **{mood}**.", ephemeral=True)


class Vibe(commands.GroupCog, group_name="vibe", group_description="Share your mood and see the server's mood"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.db.execute("CREATE TABLE IF NOT EXISTS vibe_moods (guild_id INTEGER, user_id INTEGER, "
                                  "mood TEXT, updated_at INTEGER, PRIMARY KEY (guild_id, user_id))")
        await self.bot.db.commit()
        self.bot.add_view(VibeView(self))

    @app_commands.command(name="set", description="Tell the server how you're feeling")
    @app_commands.guild_only()
    async def set_cmd(self, interaction: discord.Interaction):
        await interaction.response.send_message("Pick your mood:", view=VibeView(self), ephemeral=True)

    @app_commands.command(name="board", description="See the mood of the server over the last 24 hours")
    @app_commands.guild_only()
    async def board(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
                "SELECT mood, COUNT(*) FROM vibe_moods WHERE guild_id=? AND updated_at>? GROUP BY mood",
                (interaction.guild_id, int(time.time()) - 86400)) as cur:
            counts = dict(await cur.fetchall())
        total = sum(counts.values())
        if not total:
            return await interaction.response.send_message("Nobody has shared a vibe today. Use `/vibe set`.",
                                                           ephemeral=True)
        lines = [f"`{m:<8}` `{bar(counts.get(m, 0) * 100 / total, 14)}` {counts.get(m, 0)}"
                 for m in sorted(MOODS, key=lambda m: -counts.get(m, 0)) if counts.get(m, 0)]
        top = max(counts, key=counts.get)
        embed = discord.Embed(title="Server vibe", description="\n".join(lines), color=discord.Color.teal())
        embed.set_footer(text=f"{total} member{'s' if total != 1 else ''} shared. Most common: {top}. "
                              "Moods reset after 24 hours.")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="panel", description="Post a panel where members pick their mood (staff)")
    @app_commands.describe(channel="Where to post it (default: here)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def panel(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        channel = channel or interaction.channel
        embed = discord.Embed(title="Vibe check",
                              description="How are you feeling right now? Pick a mood. "
                                          "See the whole server's mood with `/vibe board`.",
                              color=discord.Color.teal())
        try:
            await channel.send(embed=embed, view=VibeView(self))
        except discord.HTTPException:
            return await interaction.response.send_message("I couldn't post in that channel.", ephemeral=True)
        await interaction.response.send_message(f"Posted in {channel.mention}.", ephemeral=True)


# =====================================================================
# /duel : rock paper scissors, first to 2 wins
# =====================================================================

BEATS = {"Rock": "Scissors", "Paper": "Rock", "Scissors": "Paper"}


def rps(a: str, b: str) -> int:
    """0 = tie, 1 = a wins, 2 = b wins"""
    if a == b:
        return 0
    return 1 if BEATS[a] == b else 2


class PlayView(discord.ui.View):
    def __init__(self, cog, p1: discord.Member, p2: discord.Member):
        super().__init__(timeout=180)
        self.cog, self.p1, self.p2 = cog, p1, p2
        self.picks: dict[int, str] = {}
        self.score = {p1.id: 0, p2.id: 0}
        self.round = 1
        self.done = False
        self.message: discord.Message | None = None

    def embed(self, note: str = "") -> discord.Embed:
        e = discord.Embed(title="Rock Paper Scissors", color=discord.Color.orange(),
                          description=f"{self.p1.mention} **{self.score[self.p1.id]}** : "
                                      f"**{self.score[self.p2.id]}** {self.p2.mention}\n"
                                      f"First to 2 wins.\n\n{note}")
        e.set_footer(text=f"Round {self.round}. Pick in secret, both picks are revealed together.")
        return e

    async def choose(self, interaction: discord.Interaction, pick: str):
        uid = interaction.user.id
        if self.done or uid not in self.score:
            return await interaction.response.send_message("This duel isn't yours.", ephemeral=True)
        if uid in self.picks:
            return await interaction.response.send_message("You already picked this round.", ephemeral=True)
        self.picks[uid] = pick
        if len(self.picks) < 2:
            return await interaction.response.send_message(f"You picked **{pick}**. Waiting for your opponent.",
                                                           ephemeral=True)
        pa, pb = self.picks[self.p1.id], self.picks[self.p2.id]
        result = rps(pa, pb)
        self.picks = {}
        note = f"{self.p1.display_name}: **{pa}**   {self.p2.display_name}: **{pb}**\n"
        if result == 0:
            note += "Tie. Go again."
        else:
            winner = self.p1 if result == 1 else self.p2
            self.score[winner.id] += 1
            note += f"{winner.mention} takes the round."
            self.round += 1
        winner = next((p for p in (self.p1, self.p2) if self.score[p.id] >= 2), None)
        if winner is None:
            return await interaction.response.edit_message(embed=self.embed(note), view=self)
        loser = self.p2 if winner is self.p1 else self.p1
        self.done = True
        for child in self.children:
            child.disabled = True
        self.stop()
        await self.cog.add_result(interaction.guild_id, winner.id, loser.id)
        await interaction.response.edit_message(embed=self.embed(note + f"\n**{winner.mention} wins the duel!**"),
                                                view=self)

    @discord.ui.button(label="Rock", style=discord.ButtonStyle.secondary)
    async def rock(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.choose(interaction, "Rock")

    @discord.ui.button(label="Paper", style=discord.ButtonStyle.secondary)
    async def paper(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.choose(interaction, "Paper")

    @discord.ui.button(label="Scissors", style=discord.ButtonStyle.secondary)
    async def scissors(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.choose(interaction, "Scissors")

    async def on_timeout(self):
        if self.done or not self.message:
            return
        for child in self.children:
            child.disabled = True
        try:
            await self.message.edit(embed=self.embed("The duel expired."), view=self)
        except discord.HTTPException:
            pass


class ChallengeView(discord.ui.View):
    def __init__(self, cog, challenger: discord.Member, target: discord.Member):
        super().__init__(timeout=120)
        self.cog, self.challenger, self.target = cog, challenger, target
        self.message: discord.Message | None = None

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.target.id:
            return await interaction.response.send_message("Only the challenged member can accept.", ephemeral=True)
        self.stop()
        play = PlayView(self.cog, self.challenger, self.target)
        await interaction.response.edit_message(embed=play.embed(), view=play)
        play.message = await interaction.original_response()

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in (self.target.id, self.challenger.id):
            return await interaction.response.send_message("This isn't your duel.", ephemeral=True)
        self.stop()
        for child in self.children:
            child.disabled = True
        who = "declined" if interaction.user.id == self.target.id else "cancelled"
        await interaction.response.edit_message(content=f"The duel was {who}.", embed=None, view=self)

    async def on_timeout(self):
        if not self.message:
            return
        for child in self.children:
            child.disabled = True
        try:
            await self.message.edit(content="Nobody answered, the duel expired.", embed=None, view=self)
        except discord.HTTPException:
            pass


class Duel(commands.GroupCog, group_name="duel", group_description="Challenge a member to rock paper scissors"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.db.execute("CREATE TABLE IF NOT EXISTS duel_stats (guild_id INTEGER, user_id INTEGER, "
                                  "wins INTEGER DEFAULT 0, losses INTEGER DEFAULT 0, PRIMARY KEY (guild_id, user_id))")
        await self.bot.db.commit()

    async def add_result(self, guild_id: int, winner: int, loser: int):
        db = self.bot.db
        await db.execute("INSERT INTO duel_stats (guild_id, user_id, wins) VALUES (?,?,1) "
                         "ON CONFLICT(guild_id, user_id) DO UPDATE SET wins=wins+1", (guild_id, winner))
        await db.execute("INSERT INTO duel_stats (guild_id, user_id, losses) VALUES (?,?,1) "
                         "ON CONFLICT(guild_id, user_id) DO UPDATE SET losses=losses+1", (guild_id, loser))
        await db.commit()

    @app_commands.command(name="challenge", description="Challenge a member to rock paper scissors (first to 2)")
    @app_commands.guild_only()
    async def challenge(self, interaction: discord.Interaction, member: discord.Member):
        if member.bot or member.id == interaction.user.id:
            return await interaction.response.send_message("Pick another real member to duel.", ephemeral=True)
        view = ChallengeView(self, interaction.user, member)
        await interaction.response.send_message(
            f"{member.mention}, {interaction.user.mention} challenges you to rock paper scissors. First to 2 wins!",
            view=view, allowed_mentions=discord.AllowedMentions(users=[member]))
        view.message = await interaction.original_response()

    @app_commands.command(name="top", description="The best duelists in the server")
    @app_commands.guild_only()
    async def top(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
                "SELECT user_id, wins, losses FROM duel_stats WHERE guild_id=? ORDER BY wins DESC, losses ASC LIMIT 10",
                (interaction.guild_id,)) as cur:
            rows = await cur.fetchall()
        if not rows:
            return await interaction.response.send_message("No duels played yet. Try `/duel challenge`.",
                                                           ephemeral=True)
        text = "\n".join(f"**{i}.** <@{u}> {w}W {l}L" for i, (u, w, l) in enumerate(rows, 1))
        await interaction.response.send_message(embed=discord.Embed(title="Duel leaderboard", description=text,
                                                                    color=discord.Color.orange()),
                                                allowed_mentions=discord.AllowedMentions.none())


# =====================================================================
# /birthday : set yours, the bot announces it
# =====================================================================

def is_birthday(month: int, day: int, local: date) -> bool:
    if month == 2 and day == 29 and not calendar.isleap(local.year):
        return (local.month, local.day) == (2, 28)  # leap day babies celebrate on the 28th in other years
    return (local.month, local.day) == (month, day)


def days_until(month: int, day: int, today: date) -> int:
    for year in (today.year, today.year + 1):
        d = day
        if month == 2 and day == 29 and not calendar.isleap(year):
            d = 28
        target = date(year, month, d)
        if target >= today:
            return (target - today).days
    return 365


class Birthday(commands.GroupCog, group_name="birthday", group_description="Birthdays announced in the server"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        db = self.bot.db
        await db.execute("CREATE TABLE IF NOT EXISTS birthdays (guild_id INTEGER, user_id INTEGER, month INTEGER, "
                         "day INTEGER, utc_offset INTEGER DEFAULT 0, last_year INTEGER DEFAULT 0, "
                         "PRIMARY KEY (guild_id, user_id))")
        await db.execute("CREATE TABLE IF NOT EXISTS birthday_config (guild_id INTEGER PRIMARY KEY, channel_id INTEGER)")
        await db.commit()
        self.check.start()

    async def cog_unload(self):
        self.check.cancel()

    @app_commands.command(name="set", description="Set your birthday (only the day and month are stored)")
    @app_commands.describe(month="Month number, 1 to 12", day="Day of the month",
                           utc_offset="Your time zone as hours from UTC, for example 8 for the Philippines")
    @app_commands.guild_only()
    async def set_cmd(self, interaction: discord.Interaction, month: app_commands.Range[int, 1, 12],
                      day: app_commands.Range[int, 1, 31], utc_offset: app_commands.Range[int, -12, 14] = 0):
        try:
            date(2000, month, day)  # 2000 was a leap year, so Feb 29 is allowed
        except ValueError:
            return await interaction.response.send_message(f"{MONTHS[month - 1]} doesn't have a day {day}.",
                                                           ephemeral=True)
        await self.bot.db.execute(
            "INSERT INTO birthdays (guild_id, user_id, month, day, utc_offset) VALUES (?,?,?,?,?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET month=excluded.month, day=excluded.day, "
            "utc_offset=excluded.utc_offset", (interaction.guild_id, interaction.user.id, month, day, utc_offset))
        await self.bot.db.commit()
        await interaction.response.send_message(
            f"Saved: **{MONTHS[month - 1]} {day}**. I'll wish you a happy birthday when it comes.", ephemeral=True)

    @app_commands.command(name="remove", description="Remove your birthday")
    @app_commands.guild_only()
    async def remove(self, interaction: discord.Interaction):
        await self.bot.db.execute("DELETE FROM birthdays WHERE guild_id=? AND user_id=?",
                                  (interaction.guild_id, interaction.user.id))
        await self.bot.db.commit()
        await interaction.response.send_message("Your birthday was removed.", ephemeral=True)

    @app_commands.command(name="channel", description="Choose where birthdays are announced (staff)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages):
            return await interaction.response.send_message(
                f"I need View Channel and Send Messages in {channel.mention}.", ephemeral=True)
        await self.bot.db.execute("INSERT OR REPLACE INTO birthday_config (guild_id, channel_id) VALUES (?,?)",
                                  (interaction.guild_id, channel.id))
        await self.bot.db.commit()
        await interaction.response.send_message(f"Birthdays will be announced in {channel.mention}.", ephemeral=True)

    @app_commands.command(name="next", description="See the next birthdays in the server")
    @app_commands.guild_only()
    async def next_cmd(self, interaction: discord.Interaction):
        today = datetime.now(timezone.utc).date()
        async with self.bot.db.execute("SELECT user_id, month, day FROM birthdays WHERE guild_id=?",
                                       (interaction.guild_id,)) as cur:
            rows = [r for r in await cur.fetchall() if interaction.guild.get_member(r[0])]
        if not rows:
            return await interaction.response.send_message("Nobody has set a birthday yet. Use `/birthday set`.",
                                                           ephemeral=True)
        rows.sort(key=lambda r: days_until(r[1], r[2], today))
        lines = []
        for uid, month, day in rows[:8]:
            n = days_until(month, day, today)
            lines.append(f"<@{uid}> {MONTHS[month - 1]} {day} ({'today' if n == 0 else f'in {n} day' + ('s' if n != 1 else '')})")
        await interaction.response.send_message(
            embed=discord.Embed(title="Upcoming birthdays", description="\n".join(lines), color=discord.Color.pink()),
            allowed_mentions=discord.AllowedMentions.none())

    @tasks.loop(minutes=5)
    async def check(self):
        now = datetime.now(timezone.utc)
        async with self.bot.db.execute(
                "SELECT b.guild_id, b.user_id, b.month, b.day, b.utc_offset, b.last_year, c.channel_id "
                "FROM birthdays b JOIN birthday_config c ON c.guild_id = b.guild_id") as cur:
            rows = await cur.fetchall()
        for gid, uid, month, day, offset, last_year, channel_id in rows:
            local = (now + timedelta(hours=offset)).date()
            if last_year == local.year or not is_birthday(month, day, local):
                continue
            guild = self.bot.get_guild(gid)
            member = guild.get_member(uid) if guild else None
            channel = guild.get_channel(channel_id) if guild else None
            if member is None or channel is None:
                continue
            await self.bot.db.execute("UPDATE birthdays SET last_year=? WHERE guild_id=? AND user_id=?",
                                      (local.year, gid, uid))
            await self.bot.db.commit()
            try:
                await channel.send(f"Happy birthday {member.mention}! Everyone say happy birthday!",
                                   allowed_mentions=discord.AllowedMentions(users=[member]))
            except discord.HTTPException:
                pass

    @check.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    for cog in (Wyr, Pairup, Vibe, Duel, Birthday):
        await bot.add_cog(cog(bot))
