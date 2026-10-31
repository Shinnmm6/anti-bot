import os
import random
import time
from collections import Counter
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands


def bar(pct: float, width: int = 10) -> str:
    n = max(0, min(width, round(pct / 100 * width)))
    return "#" * n + "-" * (width - n)


# =====================================================================
# /predict : call the future, get ranked on how often you're right
# =====================================================================

def predict_embed(question: str, yes: int, no: int, closes_at: int, result: str | None = None) -> discord.Embed:
    total = yes + no
    py = yes * 100 / total if total else 0
    embed = discord.Embed(title="Prediction", description=question,
                          color=discord.Color.dark_grey() if result else discord.Color.blurple())
    embed.add_field(name="Yes", value=f"`{bar(py)}` {yes}", inline=True)
    embed.add_field(name="No", value=f"`{bar(100 - py if total else 0)}` {no}", inline=True)
    if result:
        embed.add_field(name="Outcome", value=f"**{result.title()}**", inline=False)
    else:
        embed.set_footer(text="Pick Yes or No. You can change your pick until it closes.")
        embed.add_field(name="Closes", value=f"<t:{closes_at}:R>", inline=False)
    return embed


class PredictView(discord.ui.View):
    def __init__(self, cog, disabled: bool = False):
        super().__init__(timeout=None)
        self.cog = cog
        self.yes.disabled = self.no.disabled = disabled

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success, custom_id="predict:yes")
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.vote(interaction, "yes")

    @discord.ui.button(label="No", style=discord.ButtonStyle.danger, custom_id="predict:no")
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.vote(interaction, "no")


class Predict(commands.GroupCog, group_name="predict", group_description="Make predictions and build a record"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        db = self.bot.db
        await db.execute("CREATE TABLE IF NOT EXISTS predictions (message_id INTEGER PRIMARY KEY, guild_id INTEGER, "
                         "channel_id INTEGER, creator_id INTEGER, question TEXT, closes_at INTEGER, result TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS predict_votes (message_id INTEGER, user_id INTEGER, choice TEXT, "
                         "PRIMARY KEY (message_id, user_id))")
        await db.execute("CREATE TABLE IF NOT EXISTS predict_scores (guild_id INTEGER, user_id INTEGER, "
                         "right INTEGER DEFAULT 0, total INTEGER DEFAULT 0, PRIMARY KEY (guild_id, user_id))")
        await db.commit()
        self.bot.add_view(PredictView(self))

    async def counts(self, message_id: int) -> tuple[int, int]:
        async with self.bot.db.execute("SELECT choice, COUNT(*) FROM predict_votes WHERE message_id=? GROUP BY choice",
                                       (message_id,)) as cur:
            c = dict(await cur.fetchall())
        return c.get("yes", 0), c.get("no", 0)

    @app_commands.command(name="create", description="Ask the server to predict something (staff)")
    @app_commands.describe(question="For example: Will we reach 1000 members by December?",
                           hours="How long people can vote (default 24)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_messages=True)
    async def create(self, interaction: discord.Interaction, question: app_commands.Range[str, 5, 250],
                     hours: app_commands.Range[int, 1, 720] = 24):
        closes = int(time.time()) + hours * 3600
        await interaction.response.send_message(embed=predict_embed(question, 0, 0, closes), view=PredictView(self),
                                                allowed_mentions=discord.AllowedMentions.none())
        msg = await interaction.original_response()
        await self.bot.db.execute(
            "INSERT INTO predictions (message_id, guild_id, channel_id, creator_id, question, closes_at) VALUES (?,?,?,?,?,?)",
            (msg.id, interaction.guild_id, msg.channel.id, interaction.user.id, question, closes))
        await self.bot.db.commit()

    async def vote(self, interaction: discord.Interaction, choice: str):
        async with self.bot.db.execute("SELECT question, closes_at, result FROM predictions WHERE message_id=?",
                                       (interaction.message.id,)) as cur:
            row = await cur.fetchone()
        if not row:
            return await interaction.response.send_message("I can't find this prediction anymore.", ephemeral=True)
        question, closes, result = row
        if result or time.time() >= closes:
            return await interaction.response.send_message("Voting is closed.", ephemeral=True)
        await self.bot.db.execute(
            "INSERT INTO predict_votes (message_id, user_id, choice) VALUES (?,?,?) "
            "ON CONFLICT(message_id, user_id) DO UPDATE SET choice=excluded.choice",
            (interaction.message.id, interaction.user.id, choice))
        await self.bot.db.commit()
        yes, no = await self.counts(interaction.message.id)
        await interaction.response.edit_message(embed=predict_embed(question, yes, no, closes))
        await interaction.followup.send(f"Your prediction: **{choice.title()}**.", ephemeral=True)

    @app_commands.command(name="resolve", description="Say what really happened and score everyone")
    @app_commands.describe(message_id="The prediction message ID (right click, Copy Message ID)",
                           outcome="What actually happened")
    @app_commands.choices(outcome=[app_commands.Choice(name="Yes happened", value="yes"),
                                   app_commands.Choice(name="No, it didn't", value="no")])
    @app_commands.guild_only()
    async def resolve(self, interaction: discord.Interaction, message_id: str, outcome: app_commands.Choice[str]):
        if not message_id.isdigit():
            return await interaction.response.send_message("That isn't a message ID.", ephemeral=True)
        mid = int(message_id)
        async with self.bot.db.execute(
                "SELECT creator_id, channel_id, question, closes_at, result FROM predictions WHERE message_id=? AND guild_id=?",
                (mid, interaction.guild_id)) as cur:
            row = await cur.fetchone()
        if not row:
            return await interaction.response.send_message("I don't know a prediction with that ID.", ephemeral=True)
        creator, channel_id, question, closes, result = row
        if interaction.user.id != creator and not interaction.user.guild_permissions.manage_messages:
            return await interaction.response.send_message("Only the creator or a moderator can resolve it.",
                                                           ephemeral=True)
        if result:
            return await interaction.response.send_message("It's already resolved.", ephemeral=True)
        await self.bot.db.execute("UPDATE predictions SET result=? WHERE message_id=?", (outcome.value, mid))
        async with self.bot.db.execute("SELECT user_id, choice FROM predict_votes WHERE message_id=?", (mid,)) as cur:
            votes = await cur.fetchall()
        for uid, choice in votes:
            await self.bot.db.execute(
                "INSERT INTO predict_scores (guild_id, user_id, right, total) VALUES (?,?,?,1) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET right=right+?, total=total+1",
                (interaction.guild_id, uid, int(choice == outcome.value), int(choice == outcome.value)))
        await self.bot.db.commit()
        yes, no = await self.counts(mid)
        channel = interaction.guild.get_channel(channel_id)
        winners = [u for u, c in votes if c == outcome.value]
        try:
            await channel.get_partial_message(mid).edit(embed=predict_embed(question, yes, no, closes, outcome.value),
                                                        view=PredictView(self, disabled=True))
            text = f"**Prediction resolved: {outcome.value.title()}.** "
            text += ("Called it: " + " ".join(f"<@{u}>" for u in winners[:20]) + (" and more" if len(winners) > 20 else "")
                     if winners else "Nobody called it.")
            await channel.send(text, reference=discord.MessageReference(message_id=mid, channel_id=channel_id,
                                                                        fail_if_not_exists=False),
                               allowed_mentions=discord.AllowedMentions.none())
        except (discord.HTTPException, AttributeError):
            pass
        await interaction.response.send_message(f"Resolved. {len(winners)} of {len(votes)} were right.", ephemeral=True)

    @app_commands.command(name="top", description="The best predictors in the server")
    @app_commands.guild_only()
    async def top(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
                "SELECT user_id, right, total FROM predict_scores WHERE guild_id=? AND total>=3 "
                "ORDER BY CAST(right AS REAL)/total DESC, total DESC LIMIT 10", (interaction.guild_id,)) as cur:
            rows = await cur.fetchall()
        if not rows:
            return await interaction.response.send_message(
                "Nobody has 3 resolved predictions yet. Players need at least 3 to be ranked.", ephemeral=True)
        text = "\n".join(f"**{i}.** <@{u}> {r}/{t} right ({r * 100 // t}%)" for i, (u, r, t) in enumerate(rows, 1))
        await interaction.response.send_message(embed=discord.Embed(title="Best predictors", description=text,
                                                                    color=discord.Color.blurple()),
                                                allowed_mentions=discord.AllowedMentions.none())


# =====================================================================
# /pet : adopt a pet that needs you
# =====================================================================

PET_KINDS = {"cat": "meows", "dog": "wags its tail", "dragon": "puffs a little smoke", "slime": "wobbles happily",
             "fox": "yips", "owl": "hoots softly"}
FEED_COOLDOWN, PLAY_COOLDOWN = 2 * 3600, 3600
PET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "pets")


def pet_card(kind: str, title: str, text: str, color: discord.Color | None = None, big: bool = False) -> dict:
    """send_message arguments for an embed that shows the pet's picture."""
    embed = discord.Embed(title=title, description=text, color=color or discord.Color.pink())
    path = os.path.join(PET_DIR, f"{kind}.png")
    if not os.path.exists(path):
        return {"embed": embed}
    url = f"attachment://{kind}.png"
    embed.set_image(url=url) if big else embed.set_thumbnail(url=url)
    return {"embed": embed, "file": discord.File(path, filename=f"{kind}.png")}


def pet_decay(fullness: float, mood: float, seconds: float) -> tuple[float, float]:
    hours = max(0.0, seconds) / 3600
    fullness = max(0.0, fullness - 4 * hours)
    mood = max(0.0, mood - 3 * hours - (3 * hours if fullness <= 0 else 0))
    return fullness, mood


def pet_level(xp: int) -> int:
    return xp // 50 + 1


def pet_feeling(fullness: float, mood: float) -> str:
    low = min(fullness, mood)
    if low < 15:
        return "miserable. It misses you."
    if fullness < 30:
        return "very hungry."
    if mood < 30:
        return "bored and sad."
    if fullness > 70 and mood > 70:
        return "thriving!"
    return "doing okay."


class Pet(commands.GroupCog, group_name="pet", group_description="Adopt and care for a virtual pet"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.db.execute(
            """CREATE TABLE IF NOT EXISTS pets (
                guild_id INTEGER, user_id INTEGER, name TEXT, kind TEXT, fullness REAL, mood REAL, xp INTEGER DEFAULT 0,
                updated_at INTEGER, last_feed INTEGER DEFAULT 0, last_play INTEGER DEFAULT 0, created_at INTEGER,
                PRIMARY KEY (guild_id, user_id))""")
        await self.bot.db.commit()

    async def get(self, guild_id: int, user_id: int):
        """Return the pet with hunger and mood already updated for the time that passed."""
        async with self.bot.db.execute(
                "SELECT name, kind, fullness, mood, xp, updated_at, last_feed, last_play, created_at FROM pets "
                "WHERE guild_id=? AND user_id=?", (guild_id, user_id)) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        name, kind, full, mood, xp, upd, lf, lp, created = row
        now = int(time.time())
        full, mood = pet_decay(full, mood, now - upd)
        return {"name": name, "kind": kind, "fullness": full, "mood": mood, "xp": xp, "last_feed": lf,
                "last_play": lp, "created": created, "now": now}

    async def save(self, guild_id: int, user_id: int, p: dict):
        await self.bot.db.execute(
            "UPDATE pets SET fullness=?, mood=?, xp=?, updated_at=?, last_feed=?, last_play=? WHERE guild_id=? AND user_id=?",
            (p["fullness"], p["mood"], p["xp"], p["now"], p["last_feed"], p["last_play"], guild_id, user_id))
        await self.bot.db.commit()

    @app_commands.command(name="adopt", description="Adopt a pet (one per member)")
    @app_commands.describe(name="Name your pet", kind="What kind of pet")
    @app_commands.choices(kind=[app_commands.Choice(name=k.title(), value=k) for k in PET_KINDS])
    @app_commands.guild_only()
    async def adopt(self, interaction: discord.Interaction, name: app_commands.Range[str, 1, 24],
                    kind: app_commands.Choice[str]):
        if await self.get(interaction.guild_id, interaction.user.id):
            return await interaction.response.send_message("You already have a pet. Check on it with `/pet status`.",
                                                           ephemeral=True)
        now = int(time.time())
        await self.bot.db.execute(
            "INSERT INTO pets (guild_id, user_id, name, kind, fullness, mood, updated_at, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)", (interaction.guild_id, interaction.user.id, name, kind.value, 70, 70, now, now))
        await self.bot.db.commit()
        await interaction.response.send_message(**pet_card(
            kind.value, f"Meet {name}!",
            f"You adopted a {kind.value} named **{name}**! It {PET_KINDS[kind.value]}.\n"
            "Feed it with `/pet feed` and play with `/pet play`. It gets hungry and sad over time.", big=True))

    @app_commands.command(name="feed", description="Feed your pet")
    @app_commands.guild_only()
    async def feed(self, interaction: discord.Interaction):
        p = await self.get(interaction.guild_id, interaction.user.id)
        if not p:
            return await interaction.response.send_message("You don't have a pet. Use `/pet adopt`.", ephemeral=True)
        wait = p["last_feed"] + FEED_COOLDOWN
        if p["now"] < wait:
            return await interaction.response.send_message(f"{p['name']} isn't hungry yet. Try again <t:{wait}:R>.",
                                                           ephemeral=True)
        p["fullness"] = min(100, p["fullness"] + 35)
        p["xp"] += 5
        p["last_feed"] = p["now"]
        await self.save(interaction.guild_id, interaction.user.id, p)
        await interaction.response.send_message(**pet_card(
            p["kind"], f"{p['name']} is fed",
            f"**{p['name']}** munches happily and {PET_KINDS[p['kind']]}.\nFullness `{bar(p['fullness'])}` {p['fullness']:.0f}%"))

    @app_commands.command(name="play", description="Play with your pet")
    @app_commands.guild_only()
    async def play(self, interaction: discord.Interaction):
        p = await self.get(interaction.guild_id, interaction.user.id)
        if not p:
            return await interaction.response.send_message("You don't have a pet. Use `/pet adopt`.", ephemeral=True)
        wait = p["last_play"] + PLAY_COOLDOWN
        if p["now"] < wait:
            return await interaction.response.send_message(f"{p['name']} is tired. Try again <t:{wait}:R>.",
                                                           ephemeral=True)
        if p["fullness"] < 10:
            return await interaction.response.send_message(f"{p['name']} is too hungry to play. Feed it first!",
                                                           ephemeral=True)
        p["mood"] = min(100, p["mood"] + 30)
        p["fullness"] = max(0, p["fullness"] - 8)
        p["xp"] += 8
        p["last_play"] = p["now"]
        await self.save(interaction.guild_id, interaction.user.id, p)
        await interaction.response.send_message(**pet_card(
            p["kind"], f"Playtime with {p['name']}",
            f"You play with **{p['name']}** and it {PET_KINDS[p['kind']]}.\nMood `{bar(p['mood'])}` {p['mood']:.0f}%"))

    @app_commands.command(name="status", description="See how a pet is doing")
    @app_commands.guild_only()
    async def status(self, interaction: discord.Interaction, member: discord.Member | None = None):
        target = member or interaction.user
        p = await self.get(interaction.guild_id, target.id)
        if not p:
            return await interaction.response.send_message(
                "You don't have a pet. Use `/pet adopt`." if target.id == interaction.user.id
                else f"{target.display_name} doesn't have a pet.", ephemeral=True)
        await self.save(interaction.guild_id, target.id, p)  # store the time that passed
        card = pet_card(p["kind"], f"{p['name']} the {p['kind']}", f"{p['name']} is {pet_feeling(p['fullness'], p['mood'])}",
                        big=True)
        embed = card["embed"]
        embed.add_field(name="Fullness", value=f"`{bar(p['fullness'])}` {p['fullness']:.0f}%")
        embed.add_field(name="Mood", value=f"`{bar(p['mood'])}` {p['mood']:.0f}%")
        embed.add_field(name="Level", value=f"{pet_level(p['xp'])} ({p['xp'] % 50}/50 xp)")
        embed.set_footer(text=f"Owner: {target.display_name}. Adopted {(p['now'] - p['created']) // 86400} days ago.")
        await interaction.response.send_message(embed=embed, **({"file": card["file"]} if "file" in card else {}))

    @app_commands.command(name="top", description="The highest level pets")
    @app_commands.guild_only()
    async def top(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
                "SELECT user_id, name, kind, xp FROM pets WHERE guild_id=? ORDER BY xp DESC LIMIT 10",
                (interaction.guild_id,)) as cur:
            rows = await cur.fetchall()
        if not rows:
            return await interaction.response.send_message("Nobody has a pet yet. Use `/pet adopt`.", ephemeral=True)
        text = "\n".join(f"**{i}.** {n} the {k} (<@{u}>) level {pet_level(x)}" for i, (u, n, k, x) in enumerate(rows, 1))
        await interaction.response.send_message(embed=discord.Embed(title="Top pets", description=text,
                                                                    color=discord.Color.pink()),
                                                allowed_mentions=discord.AllowedMentions.none())


# =====================================================================
# /connect4 : two players, buttons for columns
# =====================================================================

ROWS, COLS = 6, 7


def new_board() -> list[list[str]]:
    return [["."] * COLS for _ in range(ROWS)]


def drop(board: list[list[str]], col: int, piece: str) -> int | None:
    for r in range(ROWS - 1, -1, -1):
        if board[r][col] == ".":
            board[r][col] = piece
            return r
    return None


def has_four(board: list[list[str]], piece: str) -> bool:
    for r in range(ROWS):
        for c in range(COLS):
            for dr, dc in ((0, 1), (1, 0), (1, 1), (1, -1)):
                er, ec = r + 3 * dr, c + 3 * dc
                if 0 <= er < ROWS and 0 <= ec < COLS and all(board[r + i * dr][c + i * dc] == piece for i in range(4)):
                    return True
    return False


def render_board(board: list[list[str]]) -> str:
    return "```\n1 2 3 4 5 6 7\n" + "\n".join(" ".join(row) for row in board) + "\n```"


class C4View(discord.ui.View):
    def __init__(self, cog, guild_id: int, p1: discord.Member, p2: discord.Member):
        super().__init__(timeout=300)
        self.cog, self.guild_id, self.p1, self.p2 = cog, guild_id, p1, p2
        self.board = new_board()
        self.turn = p1
        self.done = False
        self.message: discord.Message | None = None
        for c in range(COLS):
            button = discord.ui.Button(label=str(c + 1), style=discord.ButtonStyle.primary, row=0 if c < 5 else 1)
            button.callback = self.make_callback(c)
            self.add_item(button)

    def piece(self, p: discord.Member) -> str:
        return "O" if p.id == self.p1.id else "X"

    def embed(self, note: str) -> discord.Embed:
        return discord.Embed(title="Connect 4", color=discord.Color.blue(),
                             description=f"O = {self.p1.mention}   X = {self.p2.mention}\n{note}\n{render_board(self.board)}")

    def make_callback(self, col: int):
        async def callback(interaction: discord.Interaction):
            if self.done:
                return await interaction.response.send_message("The game is over.", ephemeral=True)
            if interaction.user.id not in (self.p1.id, self.p2.id):
                return await interaction.response.send_message("You're not playing this game.", ephemeral=True)
            if interaction.user.id != self.turn.id:
                return await interaction.response.send_message("Wait for your turn.", ephemeral=True)
            piece = self.piece(self.turn)
            row = drop(self.board, col, piece)
            if row is None:
                return await interaction.response.send_message("That column is full.", ephemeral=True)
            if self.board[0][col] != ".":
                self.children[col].disabled = True
            if has_four(self.board, piece):
                loser = self.p2 if self.turn is self.p1 else self.p1
                await self.finish(f"{self.turn.mention} wins with four in a row!", self.turn, loser)
                return await interaction.response.edit_message(embed=self.embed(self._note), view=self)
            if all(self.board[0][c] != "." for c in range(COLS)):
                await self.finish("It's a draw!", None, None)
                return await interaction.response.edit_message(embed=self.embed(self._note), view=self)
            self.turn = self.p2 if self.turn is self.p1 else self.p1
            await interaction.response.edit_message(embed=self.embed(f"{self.turn.mention}'s turn ({self.piece(self.turn)})"),
                                                    view=self)
        return callback

    async def finish(self, note: str, winner, loser):
        self._note = note
        self.done = True
        for child in self.children:
            child.disabled = True
        self.stop()
        if winner:
            await self.cog.add_result(self.guild_id, winner.id, loser.id)

    async def on_timeout(self):
        if self.done or not self.message:
            return
        for child in self.children:
            child.disabled = True
        try:
            await self.message.edit(embed=self.embed("The game expired."), view=self)
        except discord.HTTPException:
            pass


class C4Challenge(discord.ui.View):
    def __init__(self, cog, challenger: discord.Member, target: discord.Member):
        super().__init__(timeout=120)
        self.cog, self.challenger, self.target = cog, challenger, target
        self.message: discord.Message | None = None

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.target.id:
            return await interaction.response.send_message("Only the challenged member can accept.", ephemeral=True)
        self.stop()
        game = C4View(self.cog, interaction.guild_id, self.challenger, self.target)
        await interaction.response.edit_message(content=None, embed=game.embed(f"{game.turn.mention} goes first (O)"),
                                                view=game)
        game.message = await interaction.original_response()

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in (self.target.id, self.challenger.id):
            return await interaction.response.send_message("This isn't your game.", ephemeral=True)
        self.stop()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="The challenge was declined.", view=self)

    async def on_timeout(self):
        if self.message:
            for child in self.children:
                child.disabled = True
            try:
                await self.message.edit(content="Nobody answered, the challenge expired.", view=self)
            except discord.HTTPException:
                pass


class Connect4(commands.GroupCog, group_name="connect4", group_description="Connect four in a row"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.db.execute("CREATE TABLE IF NOT EXISTS c4_stats (guild_id INTEGER, user_id INTEGER, "
                                  "wins INTEGER DEFAULT 0, losses INTEGER DEFAULT 0, PRIMARY KEY (guild_id, user_id))")
        await self.bot.db.commit()

    async def add_result(self, guild_id: int, winner: int, loser: int):
        db = self.bot.db
        await db.execute("INSERT INTO c4_stats (guild_id, user_id, wins) VALUES (?,?,1) "
                         "ON CONFLICT(guild_id, user_id) DO UPDATE SET wins=wins+1", (guild_id, winner))
        await db.execute("INSERT INTO c4_stats (guild_id, user_id, losses) VALUES (?,?,1) "
                         "ON CONFLICT(guild_id, user_id) DO UPDATE SET losses=losses+1", (guild_id, loser))
        await db.commit()

    @app_commands.command(name="challenge", description="Challenge a member to Connect 4")
    @app_commands.guild_only()
    async def challenge(self, interaction: discord.Interaction, member: discord.Member):
        if member.bot or member.id == interaction.user.id:
            return await interaction.response.send_message("Pick another real member.", ephemeral=True)
        view = C4Challenge(self, interaction.user, member)
        await interaction.response.send_message(
            f"{member.mention}, {interaction.user.mention} challenges you to Connect 4!", view=view,
            allowed_mentions=discord.AllowedMentions(users=[member]))
        view.message = await interaction.original_response()

    @app_commands.command(name="top", description="Best Connect 4 players")
    @app_commands.guild_only()
    async def top(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
                "SELECT user_id, wins, losses FROM c4_stats WHERE guild_id=? ORDER BY wins DESC, losses ASC LIMIT 10",
                (interaction.guild_id,)) as cur:
            rows = await cur.fetchall()
        if not rows:
            return await interaction.response.send_message("No games finished yet.", ephemeral=True)
        text = "\n".join(f"**{i}.** <@{u}> {w}W {l}L" for i, (u, w, l) in enumerate(rows, 1))
        await interaction.response.send_message(embed=discord.Embed(title="Connect 4 leaders", description=text,
                                                                    color=discord.Color.blue()),
                                                allowed_mentions=discord.AllowedMentions.none())


# =====================================================================
# /wordle : your own 5 letter word puzzle
# =====================================================================

WORDLE_WORDS = ["apple", "brave", "chair", "dream", "eagle", "flame", "grape", "house", "ivory", "jolly", "knife", "lemon",
                "magic", "night", "ocean", "piano", "queen", "river", "stone", "tiger", "uncle", "voice", "water", "youth",
                "zebra", "beach", "cloud", "dance", "earth", "fresh", "ghost", "heart", "juice", "light", "money", "noise",
                "olive", "party", "quiet", "robot", "smile", "toast", "unity", "vivid", "whale", "pearl", "plant", "crane",
                "storm", "sugar", "bread", "candy", "dizzy", "fairy", "giant", "happy", "lucky", "movie", "nurse", "orbit",
                "pilot", "radio", "salad", "table", "urban", "video", "world", "angel", "bonus", "cabin", "daisy", "elbow",
                "flute", "globe", "honey", "input", "jelly", "koala", "lunar", "mango", "novel", "opera", "pizza", "quest"]
MARKS = {"g": "#", "y": "+", "x": "-"}
MAX_GUESSES = 6


def wordle_score(guess: str, answer: str) -> list[str]:
    result = ["x"] * len(answer)
    left = Counter(a for g, a in zip(guess, answer) if g != a)
    for i, (g, a) in enumerate(zip(guess, answer)):
        if g == a:
            result[i] = "g"
    for i, g in enumerate(guess):
        if result[i] != "g" and left[g] > 0:
            result[i] = "y"
            left[g] -= 1
    return result


def wordle_board(guesses: list[str], answer: str, hide_letters: bool = False) -> str:
    lines = []
    for g in guesses:
        marks = "".join(MARKS[m] for m in wordle_score(g, answer))
        lines.append(" ".join("*" for _ in g) + "\n" + " ".join(marks) if hide_letters
                     else " ".join(g.upper()) + "\n" + " ".join(marks))
    return "```\n" + "\n\n".join(lines) + "\n```" if lines else ""


class Wordle(commands.GroupCog, group_name="wordle", group_description="Guess the 5 letter word in 6 tries"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        db = self.bot.db
        await db.execute("CREATE TABLE IF NOT EXISTS wordle_games (guild_id INTEGER, user_id INTEGER, answer TEXT, "
                         "guesses TEXT DEFAULT '', PRIMARY KEY (guild_id, user_id))")
        await db.execute("CREATE TABLE IF NOT EXISTS wordle_stats (guild_id INTEGER, user_id INTEGER, played INTEGER DEFAULT 0, "
                         "wins INTEGER DEFAULT 0, streak INTEGER DEFAULT 0, best INTEGER DEFAULT 0, "
                         "PRIMARY KEY (guild_id, user_id))")
        await db.commit()

    async def game(self, guild_id: int, user_id: int):
        async with self.bot.db.execute("SELECT answer, guesses FROM wordle_games WHERE guild_id=? AND user_id=?",
                                       (guild_id, user_id)) as cur:
            row = await cur.fetchone()
        return (row[0], [g for g in row[1].split(",") if g]) if row else None

    @app_commands.command(name="start", description="Start a new word puzzle only you can see")
    @app_commands.guild_only()
    async def start(self, interaction: discord.Interaction):
        if await self.game(interaction.guild_id, interaction.user.id):
            return await interaction.response.send_message("You already have a puzzle. Use `/wordle guess`.",
                                                           ephemeral=True)
        await self.bot.db.execute("INSERT INTO wordle_games (guild_id, user_id, answer) VALUES (?,?,?)",
                                  (interaction.guild_id, interaction.user.id, random.choice(WORDLE_WORDS)))
        await self.bot.db.commit()
        await interaction.response.send_message(
            "I picked a 5 letter word. You have 6 tries. Use `/wordle guess`.\n"
            "`#` right letter in the right place, `+` right letter in the wrong place, `-` not in the word.",
            ephemeral=True)

    @app_commands.command(name="guess", description="Guess the 5 letter word")
    @app_commands.guild_only()
    async def guess(self, interaction: discord.Interaction, word: app_commands.Range[str, 5, 5]):
        word = word.lower()
        if not word.isalpha():
            return await interaction.response.send_message("Letters only.", ephemeral=True)
        game = await self.game(interaction.guild_id, interaction.user.id)
        if not game:
            return await interaction.response.send_message("You don't have a puzzle. Use `/wordle start`.", ephemeral=True)
        answer, guesses = game
        if word in guesses:
            return await interaction.response.send_message("You already tried that word.", ephemeral=True)
        guesses.append(word)
        won, lost = word == answer, len(guesses) >= MAX_GUESSES and word != answer
        if not (won or lost):
            await self.bot.db.execute("UPDATE wordle_games SET guesses=? WHERE guild_id=? AND user_id=?",
                                      (",".join(guesses), interaction.guild_id, interaction.user.id))
            await self.bot.db.commit()
            return await interaction.response.send_message(
                f"Guess {len(guesses)}/{MAX_GUESSES}\n{wordle_board(guesses, answer)}", ephemeral=True)
        await self.bot.db.execute("DELETE FROM wordle_games WHERE guild_id=? AND user_id=?",
                                  (interaction.guild_id, interaction.user.id))
        await self.bot.db.execute(
            "INSERT INTO wordle_stats (guild_id, user_id, played, wins, streak, best) VALUES (?,?,1,?,?,?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET played=played+1, wins=wins+?, "
            "streak=CASE WHEN ? THEN streak+1 ELSE 0 END, best=MAX(best, CASE WHEN ? THEN streak+1 ELSE 0 END)",
            (interaction.guild_id, interaction.user.id, int(won), int(won), int(won), int(won), int(won), int(won)))
        await self.bot.db.commit()
        if won:
            await interaction.response.send_message(f"You got it in {len(guesses)}/{MAX_GUESSES}!\n"
                                                    f"{wordle_board(guesses, answer)}", ephemeral=True)
            await interaction.followup.send(
                f"{interaction.user.mention} solved a Wordle in **{len(guesses)}/{MAX_GUESSES}**!\n"
                f"{wordle_board(guesses, answer, hide_letters=True)}",
                allowed_mentions=discord.AllowedMentions(users=[interaction.user]))
        else:
            await interaction.response.send_message(f"Out of tries. The word was **{answer.upper()}**.\n"
                                                    f"{wordle_board(guesses, answer)}", ephemeral=True)

    @app_commands.command(name="giveup", description="Give up on your current puzzle")
    @app_commands.guild_only()
    async def giveup(self, interaction: discord.Interaction):
        game = await self.game(interaction.guild_id, interaction.user.id)
        if not game:
            return await interaction.response.send_message("You don't have a puzzle.", ephemeral=True)
        await self.bot.db.execute("DELETE FROM wordle_games WHERE guild_id=? AND user_id=?",
                                  (interaction.guild_id, interaction.user.id))
        await self.bot.db.execute(
            "INSERT INTO wordle_stats (guild_id, user_id, played) VALUES (?,?,1) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET played=played+1, streak=0", (interaction.guild_id, interaction.user.id))
        await self.bot.db.commit()
        await interaction.response.send_message(f"The word was **{game[0].upper()}**.", ephemeral=True)

    @app_commands.command(name="stats", description="See Wordle stats")
    @app_commands.guild_only()
    async def stats(self, interaction: discord.Interaction, member: discord.Member | None = None):
        target = member or interaction.user
        async with self.bot.db.execute("SELECT played, wins, streak, best FROM wordle_stats WHERE guild_id=? AND user_id=?",
                                       (interaction.guild_id, target.id)) as cur:
            row = await cur.fetchone()
        if not row:
            return await interaction.response.send_message(f"{target.display_name} hasn't played yet.", ephemeral=True)
        played, wins, streak, best = row
        await interaction.response.send_message(
            f"**{target.display_name}**: played {played}, won {wins} ({wins * 100 // max(1, played)}%), "
            f"streak {streak}, best streak {best}.", ephemeral=True)

    @app_commands.command(name="top", description="Best Wordle players")
    @app_commands.guild_only()
    async def top(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
                "SELECT user_id, wins, played, best FROM wordle_stats WHERE guild_id=? ORDER BY wins DESC LIMIT 10",
                (interaction.guild_id,)) as cur:
            rows = await cur.fetchall()
        if not rows:
            return await interaction.response.send_message("No games yet.", ephemeral=True)
        text = "\n".join(f"**{i}.** <@{u}> {w} wins of {p} (best streak {b})" for i, (u, w, p, b) in enumerate(rows, 1))
        await interaction.response.send_message(embed=discord.Embed(title="Wordle champions", description=text,
                                                                    color=discord.Color.green()),
                                                allowed_mentions=discord.AllowedMentions.none())


# =====================================================================
# /fortune : your fortune for today
# =====================================================================

FORTUNES = [
    "A small risk you take today will pay off sooner than you expect.", "Someone is thinking about you right now.",
    "Your next idea will be the good one. Write it down.", "Patience is about to be rewarded.",
    "A message you have been waiting for is closer than you think.", "Today is a good day to start the thing you keep postponing.",
    "A friend will make you laugh when you need it most.", "Your hard work is being noticed by someone quiet.",
    "Say yes to the next small invitation.", "You will find something you thought was lost.",
    "A door closes today so a better one can open tomorrow.", "Kindness you gave away will come back to you.",
    "Do not rush. The best moment is still ahead.", "A surprise is waiting around the corner. Wear comfy shoes.",
    "You will win an argument today, but it is better to share a snack instead.", "Your luck is loading. Please wait.",
    "Trust the first instinct you have this afternoon.", "Someone will ask you for advice. You know more than you think.",
    "A new skill is easier than it looks. Try it for ten minutes.", "Take a break. Your best thought arrives when you stop looking for it.",
    "Today you are the main character. Act like it.", "The answer you need is in the thing you have been avoiding.",
    "A tiny win today leads to a big one next week.", "Drink some water. That is the real fortune.",
    "You are closer to your goal than the map says.", "A stranger will make your day better without knowing it.",
    "Your energy attracts the right people today.", "Stop checking. It is already working out.",
    "Something that annoys you today becomes a good story later.", "Make the call. Send the message. Press the button.",
]
COLORS = ["red", "orange", "yellow", "green", "teal", "blue", "indigo", "purple", "pink", "gold", "silver", "white"]
LUCK = {1: "rough day, be gentle", 2: "slow luck", 3: "balanced", 4: "good luck", 5: "extremely lucky"}


def todays_fortune(user_id: int, day: str) -> dict:
    rng = random.Random(f"{user_id}:{day}")
    return {"text": rng.choice(FORTUNES), "number": rng.randint(1, 99), "color": rng.choice(COLORS),
            "luck": rng.choices([1, 2, 3, 4, 5], weights=[8, 20, 36, 26, 10])[0]}


class Fortune(commands.GroupCog, group_name="fortune", group_description="Your fortune for today"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.db.execute("CREATE TABLE IF NOT EXISTS fortune_log (guild_id INTEGER, user_id INTEGER, day TEXT, "
                                  "luck INTEGER, PRIMARY KEY (guild_id, user_id, day))")
        await self.bot.db.commit()

    @app_commands.command(name="today", description="Open your fortune cookie (one fortune per day)")
    @app_commands.describe(public="Show it to everyone (default: only you)")
    @app_commands.guild_only()
    async def today(self, interaction: discord.Interaction, public: bool = False):
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        f = todays_fortune(interaction.user.id, day)
        await self.bot.db.execute("INSERT OR IGNORE INTO fortune_log (guild_id, user_id, day, luck) VALUES (?,?,?,?)",
                                  (interaction.guild_id, interaction.user.id, day, f["luck"]))
        await self.bot.db.commit()
        embed = discord.Embed(title="Your fortune", description=f"*{f['text']}*", color=discord.Color.gold())
        embed.add_field(name="Lucky number", value=str(f["number"]))
        embed.add_field(name="Lucky color", value=f["color"])
        embed.add_field(name="Luck today", value=f"{f['luck']}/5: {LUCK[f['luck']]}")
        embed.set_footer(text="Same fortune all day. A new one comes at midnight UTC.")
        await interaction.response.send_message(embed=embed, ephemeral=not public)

    @app_commands.command(name="luckiest", description="Who is the luckiest today?")
    @app_commands.guild_only()
    async def luckiest(self, interaction: discord.Interaction):
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        async with self.bot.db.execute(
                "SELECT user_id, luck FROM fortune_log WHERE guild_id=? AND day=? ORDER BY luck DESC, user_id LIMIT 10",
                (interaction.guild_id, day)) as cur:
            rows = await cur.fetchall()
        if not rows:
            return await interaction.response.send_message("Nobody opened a fortune yet today. Be the first!",
                                                           ephemeral=True)
        text = "\n".join(f"**{i}.** <@{u}> luck {l}/5 ({LUCK[l]})" for i, (u, l) in enumerate(rows, 1))
        await interaction.response.send_message(embed=discord.Embed(title="Luckiest today", description=text,
                                                                    color=discord.Color.gold()),
                                                allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot):
    for cog in (Predict, Pet, Connect4, Wordle, Fortune):
        await bot.add_cog(cog(bot))
