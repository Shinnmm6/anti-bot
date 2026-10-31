import asyncio
import random
import string
import time

import discord
from discord import app_commands
from discord.ext import commands


# =====================================================================
# /telephone : a message gets garbled as it is passed along
# =====================================================================

def garble(text: str, rng=random, strength: float = 0.07) -> str:
    """Randomly drop, double or change letters, and sometimes swap two words."""
    out = []
    for ch in text:
        r = rng.random()
        if ch.isalpha() and r < strength:
            kind = rng.random()
            if kind < 0.34:
                continue                                      # drop the letter
            if kind < 0.67:
                out.append(ch + ch)                           # double it
            else:
                new = rng.choice(string.ascii_lowercase)      # change it
                out.append(new.upper() if ch.isupper() else new)
        else:
            out.append(ch)
    words = "".join(out).split(" ")
    if len(words) > 3 and rng.random() < 0.35:
        i = rng.randrange(len(words) - 1)
        words[i], words[i + 1] = words[i + 1], words[i]
    return " ".join(words)[:200]


class Telephone(commands.GroupCog, group_name="telephone", group_description="Pass a message along and watch it change"):
    MAX_HOPS = 8

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.db.execute(
            "CREATE TABLE IF NOT EXISTS telephone_games (guild_id INTEGER PRIMARY KEY, channel_id INTEGER, starter_id INTEGER, "
            "original TEXT, current TEXT, holder_id INTEGER, chain TEXT)")
        await self.bot.db.commit()

    async def game(self, guild_id: int):
        async with self.bot.db.execute(
                "SELECT channel_id, starter_id, original, current, holder_id, chain FROM telephone_games WHERE guild_id=?",
                (guild_id,)) as cur:
            return await cur.fetchone()

    @app_commands.command(name="start", description="Start a game and secretly send a message to the first player")
    @app_commands.describe(message="The message to pass along", first="Who gets it first")
    @app_commands.guild_only()
    async def start(self, interaction: discord.Interaction, message: app_commands.Range[str, 8, 200],
                    first: discord.Member):
        if first.bot or first.id == interaction.user.id:
            return await interaction.response.send_message("Pick another real member to start with.", ephemeral=True)
        if await self.game(interaction.guild_id):
            return await interaction.response.send_message("A game is already running. End it with `/telephone end`.",
                                                           ephemeral=True)
        await self.bot.db.execute(
            "INSERT INTO telephone_games (guild_id, channel_id, starter_id, original, current, holder_id, chain) "
            "VALUES (?,?,?,?,?,?,?)",
            (interaction.guild_id, interaction.channel_id, interaction.user.id, message, message, first.id,
             f"{interaction.user.id},{first.id}"))
        await self.bot.db.commit()
        await interaction.response.send_message(
            f"**Telephone!** {interaction.user.mention} whispered a secret message to {first.mention}. "
            f"{first.mention}: use `/telephone read` to see it, then `/telephone pass member message` to pass it on. "
            f"Every pass, the bot garbles it a little.", allowed_mentions=discord.AllowedMentions(users=[first]))

    @app_commands.command(name="read", description="Read the message you were given (only you see it)")
    @app_commands.guild_only()
    async def read(self, interaction: discord.Interaction):
        g = await self.game(interaction.guild_id)
        if not g:
            return await interaction.response.send_message("No telephone game is running.", ephemeral=True)
        if interaction.user.id != g[4]:
            return await interaction.response.send_message("The message isn't with you right now.", ephemeral=True)
        await interaction.response.send_message(f"The message you heard:\n> {g[3]}", ephemeral=True)

    @app_commands.command(name="pass", description="Pass the message on, in your own words")
    @app_commands.describe(member="Who hears it next", message="What you heard (type it as you remember it)")
    @app_commands.guild_only()
    async def pass_cmd(self, interaction: discord.Interaction, member: discord.Member,
                       message: app_commands.Range[str, 3, 200]):
        g = await self.game(interaction.guild_id)
        if not g:
            return await interaction.response.send_message("No telephone game is running.", ephemeral=True)
        channel_id, starter, original, current, holder, chain = g
        ids = [int(x) for x in chain.split(",")]
        if interaction.user.id != holder:
            return await interaction.response.send_message("You don't have the message.", ephemeral=True)
        if member.bot or member.id in ids:
            return await interaction.response.send_message("Pass it to someone who hasn't heard it yet.", ephemeral=True)
        new = garble(message)
        ids.append(member.id)
        hops = len(ids) - 1
        await self.bot.db.execute("UPDATE telephone_games SET current=?, holder_id=?, chain=? WHERE guild_id=?",
                                  (new, member.id, ",".join(map(str, ids)), interaction.guild_id))
        await self.bot.db.commit()
        await interaction.response.send_message(
            f"{interaction.user.mention} passed the message to {member.mention} (pass {hops}/{self.MAX_HOPS}). "
            f"{member.mention}: use `/telephone read`.", allowed_mentions=discord.AllowedMentions(users=[member]))
        if hops >= self.MAX_HOPS:
            await self.reveal(interaction.channel, interaction.guild_id, await self.game(interaction.guild_id))

    async def reveal(self, channel, guild_id: int, g):
        channel_id, starter, original, current, holder, chain = g
        await self.bot.db.execute("DELETE FROM telephone_games WHERE guild_id=?", (guild_id,))
        await self.bot.db.commit()
        embed = discord.Embed(title="Telephone results", color=discord.Color.orange())
        embed.add_field(name="What it started as", value=f"> {original}", inline=False)
        embed.add_field(name="What it became", value=f"> {current}", inline=False)
        embed.add_field(name="The chain", value=" > ".join(f"<@{u}>" for u in chain.split(","))[:1000], inline=False)
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

    @app_commands.command(name="end", description="End the game and reveal what the message became")
    @app_commands.guild_only()
    async def end(self, interaction: discord.Interaction):
        g = await self.game(interaction.guild_id)
        if not g:
            return await interaction.response.send_message("No telephone game is running.", ephemeral=True)
        if interaction.user.id != g[1] and not interaction.user.guild_permissions.manage_messages:
            return await interaction.response.send_message("Only the person who started it, or a moderator, can end it.",
                                                           ephemeral=True)
        await interaction.response.send_message("Ending the game...", ephemeral=True)
        await self.reveal(interaction.channel, interaction.guild_id, g)


# =====================================================================
# /minesweeper : a playable board made of spoilers
# =====================================================================

LEVELS = {"easy": (6, 5), "medium": (8, 10), "hard": (10, 18)}


def make_minefield(size: int, mines: int, rng=random) -> list[list[int]]:
    """-1 is a mine, other numbers count the mines next to the cell."""
    cells = [(r, c) for r in range(size) for c in range(size)]
    grid = [[0] * size for _ in range(size)]
    for r, c in rng.sample(cells, mines):
        grid[r][c] = -1
    for r in range(size):
        for c in range(size):
            if grid[r][c] == -1:
                continue
            grid[r][c] = sum(grid[rr][cc] == -1 for rr in range(max(0, r - 1), min(size, r + 2))
                             for cc in range(max(0, c - 1), min(size, c + 2)))
    return grid


def open_area(grid: list[list[int]], start: tuple[int, int]) -> set[tuple[int, int]]:
    """Cells revealed by clicking a zero: the zeros around it and the numbers that touch them."""
    size, seen, todo = len(grid), set(), [start]
    while todo:
        r, c = todo.pop()
        if (r, c) in seen:
            continue
        seen.add((r, c))
        if grid[r][c] == 0:
            todo += [(rr, cc) for rr in range(max(0, r - 1), min(size, r + 2))
                     for cc in range(max(0, c - 1), min(size, c + 2)) if (rr, cc) not in seen]
    return seen


def minesweeper_text(size: int, mines: int, rng=random) -> str:
    for _ in range(100):
        grid = make_minefield(size, mines, rng)
        zeros = [(r, c) for r in range(size) for c in range(size) if grid[r][c] == 0]
        if zeros:
            break
    shown = open_area(grid, rng.choice(zeros)) if zeros else set()
    lines = []
    for r in range(size):
        cells = []
        for c in range(size):
            v = "X" if grid[r][c] == -1 else str(grid[r][c])
            cells.append(v if (r, c) in shown else f"||{v}||")
        lines.append(" ".join(cells))
    return "\n".join(lines)


class Minesweeper(commands.GroupCog, group_name="minesweeper", group_description="Minesweeper made of spoilers"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="play", description="Get a minesweeper board to play right in chat")
    @app_commands.choices(level=[app_commands.Choice(name="Easy (6x6, 5 mines)", value="easy"),
                                 app_commands.Choice(name="Medium (8x8, 10 mines)", value="medium"),
                                 app_commands.Choice(name="Hard (10x10, 18 mines)", value="hard")])
    @app_commands.guild_only()
    async def play(self, interaction: discord.Interaction, level: app_commands.Choice[str] | None = None):
        key = level.value if level else "easy"
        size, mines = LEVELS[key]
        board = minesweeper_text(size, mines)
        await interaction.response.send_message(
            f"**Minesweeper ({key})**: {mines} mines hidden. Click a spoiler to reveal it. A number says how many "
            f"mines touch that square, `X` is a mine, and `0` means nothing nearby.\n\n{board}")


# =====================================================================
# /memory : match the pairs in as few moves as you can
# =====================================================================

SYMBOLS = list("ABCDEFGH")


class MemoryView(discord.ui.View):
    def __init__(self, cog, guild_id: int, player: discord.Member):
        super().__init__(timeout=300)
        self.cog, self.guild_id, self.player = cog, guild_id, player
        self.cards = SYMBOLS * 2
        random.shuffle(self.cards)
        self.matched: set[int] = set()
        self.first: int | None = None
        self.moves = 0
        self.busy = False
        self.done = False
        self.message: discord.Message | None = None
        for i in range(16):
            button = discord.ui.Button(label="?", style=discord.ButtonStyle.secondary, row=i // 4)
            button.callback = self.make_callback(i)
            self.add_item(button)

    def header(self) -> str:
        return f"**Memory** for {self.player.mention}. Moves: **{self.moves}**. Matched: {len(self.matched) // 2}/8"

    def make_callback(self, i: int):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.player.id:
                return await interaction.response.send_message("This is someone else's game. Start your own with `/memory play`.",
                                                               ephemeral=True)
            if self.busy or self.done or i in self.matched or i == self.first:
                return await interaction.response.defer()
            btn = self.children[i]
            btn.label, btn.style = self.cards[i], discord.ButtonStyle.primary
            if self.first is None:
                self.first = i
                return await interaction.response.edit_message(content=self.header(), view=self)
            a, b = self.first, i
            self.first = None
            self.moves += 1
            if self.cards[a] == self.cards[b]:
                self.matched |= {a, b}
                for k in (a, b):
                    self.children[k].style = discord.ButtonStyle.success
                    self.children[k].disabled = True
                if len(self.matched) == 16:
                    self.done = True
                    self.stop()
                    best = await self.cog.record(self.guild_id, self.player.id, self.moves)
                    return await interaction.response.edit_message(
                        content=f"**You did it in {self.moves} moves!** {best}", view=self)
                return await interaction.response.edit_message(content=self.header(), view=self)
            self.children[a].style = self.children[b].style = discord.ButtonStyle.danger
            self.busy = True
            await interaction.response.edit_message(content=self.header() + " (no match)", view=self)
            await asyncio.sleep(1.2)
            for k in (a, b):
                self.children[k].label, self.children[k].style = "?", discord.ButtonStyle.secondary
            self.busy = False
            try:
                await interaction.edit_original_response(content=self.header(), view=self)
            except discord.HTTPException:
                pass
        return callback

    async def on_timeout(self):
        if self.done or not self.message:
            return
        for child in self.children:
            child.disabled = True
        try:
            await self.message.edit(content="The memory game timed out.", view=self)
        except discord.HTTPException:
            pass


class Memory(commands.GroupCog, group_name="memory", group_description="A card matching game"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.db.execute("CREATE TABLE IF NOT EXISTS memory_best (guild_id INTEGER, user_id INTEGER, "
                                  "best INTEGER, plays INTEGER DEFAULT 0, PRIMARY KEY (guild_id, user_id))")
        await self.bot.db.commit()

    async def record(self, guild_id: int, user_id: int, moves: int) -> str:
        async with self.bot.db.execute("SELECT best FROM memory_best WHERE guild_id=? AND user_id=?",
                                       (guild_id, user_id)) as cur:
            row = await cur.fetchone()
        await self.bot.db.execute(
            "INSERT INTO memory_best (guild_id, user_id, best, plays) VALUES (?,?,?,1) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET best=MIN(best, excluded.best), plays=plays+1",
            (guild_id, user_id, moves))
        await self.bot.db.commit()
        if row is None or moves < row[0]:
            return "That's a new personal best!"
        return f"Your best is {row[0]} moves."

    @app_commands.command(name="play", description="Flip cards and find all 8 pairs")
    @app_commands.guild_only()
    async def play(self, interaction: discord.Interaction):
        view = MemoryView(self, interaction.guild_id, interaction.user)
        await interaction.response.send_message(view.header(), view=view,
                                                allowed_mentions=discord.AllowedMentions.none())
        view.message = await interaction.original_response()

    @app_commands.command(name="top", description="Fewest moves to clear the board")
    @app_commands.guild_only()
    async def top(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
                "SELECT user_id, best, plays FROM memory_best WHERE guild_id=? ORDER BY best ASC LIMIT 10",
                (interaction.guild_id,)) as cur:
            rows = await cur.fetchall()
        if not rows:
            return await interaction.response.send_message("Nobody has finished a game yet.", ephemeral=True)
        text = "\n".join(f"**{i}.** <@{u}> {b} moves ({p} game{'s' if p != 1 else ''})" for i, (u, b, p) in enumerate(rows, 1))
        await interaction.response.send_message(embed=discord.Embed(title="Memory masters", description=text,
                                                                    color=discord.Color.blurple()),
                                                allowed_mentions=discord.AllowedMentions.none())


# =====================================================================
# /quickdraw : wait for green, then be the first to press
# =====================================================================

class DrawView(discord.ui.View):
    def __init__(self, cog, guild_id: int, channel_id: int):
        super().__init__(timeout=60)
        self.cog, self.guild_id, self.channel_id = cog, guild_id, channel_id
        self.ready = False
        self.go_time = 0.0
        self.fouls: set[int] = set()
        self.winner: int | None = None
        self.message: discord.Message | None = None
        self.draw.style = discord.ButtonStyle.danger

    @discord.ui.button(label="Wait for it...", style=discord.ButtonStyle.danger)
    async def draw(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = interaction.user.id
        if self.winner is not None:
            return await interaction.response.send_message("Too slow, someone already drew.", ephemeral=True)
        if not self.ready:
            self.fouls.add(uid)
            return await interaction.response.send_message("False start! You're out this round.", ephemeral=True)
        if uid in self.fouls:
            return await interaction.response.send_message("You false started, you're out this round.", ephemeral=True)
        ms = int((time.monotonic() - self.go_time) * 1000)
        self.winner = uid
        self.cog.finish(self.channel_id)
        self.draw.disabled = True
        self.draw.label = f"Drawn in {ms} ms"
        self.stop()
        note = await self.cog.record(self.guild_id, uid, ms)
        await interaction.response.edit_message(content=f"{interaction.user.mention} drew first in **{ms} ms**! {note}",
                                                view=self)

    async def on_timeout(self):
        if self.winner is not None or not self.message:
            return
        self.cog.finish(self.channel_id)
        self.draw.disabled = True
        self.draw.label = "Nobody drew"
        try:
            await self.message.edit(content="Nobody drew in time.", view=self)
        except discord.HTTPException:
            pass


class QuickDraw(commands.GroupCog, group_name="quickdraw", group_description="A reaction speed duel"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.running: set[int] = set()

    async def cog_load(self):
        await self.bot.db.execute("CREATE TABLE IF NOT EXISTS quickdraw_best (guild_id INTEGER, user_id INTEGER, "
                                  "best_ms INTEGER, wins INTEGER DEFAULT 0, PRIMARY KEY (guild_id, user_id))")
        await self.bot.db.commit()

    def finish(self, channel_id: int):
        self.running.discard(channel_id)

    async def record(self, guild_id: int, user_id: int, ms: int) -> str:
        async with self.bot.db.execute("SELECT best_ms FROM quickdraw_best WHERE guild_id=? AND user_id=?",
                                       (guild_id, user_id)) as cur:
            row = await cur.fetchone()
        await self.bot.db.execute(
            "INSERT INTO quickdraw_best (guild_id, user_id, best_ms, wins) VALUES (?,?,?,1) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET best_ms=MIN(best_ms, excluded.best_ms), wins=wins+1",
            (guild_id, user_id, ms))
        await self.bot.db.commit()
        return "New personal best!" if row is None or ms < row[0] else f"Personal best: {row[0]} ms."

    @app_commands.command(name="start", description="Start a quick draw: first to press the green button wins")
    @app_commands.guild_only()
    async def start(self, interaction: discord.Interaction):
        if interaction.channel_id in self.running:
            return await interaction.response.send_message("A quick draw is already running here.", ephemeral=True)
        self.running.add(interaction.channel_id)
        view = DrawView(self, interaction.guild_id, interaction.channel_id)
        await interaction.response.send_message(
            "**Quick draw!** Get ready. When the button turns green, press it. Press too early and you're out.", view=view)
        view.message = await interaction.original_response()
        await asyncio.sleep(random.uniform(3, 9))
        if view.winner is not None or view.is_finished():
            return
        view.ready = True
        view.draw.style, view.draw.label = discord.ButtonStyle.success, "DRAW!"
        view.go_time = time.monotonic()
        try:
            await view.message.edit(content="**NOW!**", view=view)
        except discord.HTTPException:
            self.finish(interaction.channel_id)

    @app_commands.command(name="top", description="The fastest draws in the server")
    @app_commands.guild_only()
    async def top(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
                "SELECT user_id, best_ms, wins FROM quickdraw_best WHERE guild_id=? ORDER BY best_ms ASC LIMIT 10",
                (interaction.guild_id,)) as cur:
            rows = await cur.fetchall()
        if not rows:
            return await interaction.response.send_message("Nobody has won a draw yet.", ephemeral=True)
        text = "\n".join(f"**{i}.** <@{u}> {ms} ms ({w} win{'s' if w != 1 else ''})" for i, (u, ms, w) in enumerate(rows, 1))
        await interaction.response.send_message(
            embed=discord.Embed(title="Fastest draws", description=text, color=discord.Color.green()),
            allowed_mentions=discord.AllowedMentions.none())


# =====================================================================
# /ship : how well do two members match?
# =====================================================================

GREEN_FLAGS = ["Laughs at the same bad jokes", "Both reply to messages way too fast", "Same taste in snacks",
               "Would share the last slice of pizza", "Both stay up too late", "Great at hyping each other up",
               "Understand each other without talking", "Would carry each other in any game",
               "Never leave the other on read", "Both love a good plan, and ignore it"]
RED_FLAGS = ["Argue about where to eat for an hour", "Both too stubborn to say sorry first",
             "One of them always runs late", "Fight over the thermostat", "Both think they are the funny one",
             "Would spoil each other's favorite show", "Only one of them cleans up", "Both claim to be the better gamer",
             "Take forever to pick a movie", "Cannot agree on pineapple on pizza"]
VERDICTS = [(20, "Just friends, maybe not even that."), (40, "Some sparks, but they need work."),
            (60, "A solid match with real potential."), (80, "Very compatible! People notice."),
            (101, "A perfect duo. Made for each other.")]


def ship_name(a: str, b: str) -> str:
    a = "".join(ch for ch in a if ch.isalnum()) or "A"
    b = "".join(ch for ch in b if ch.isalnum()) or "B"
    return (a[:max(1, (len(a) + 1) // 2)] + b[len(b) // 2:]).capitalize()


def ship_result(id_a: int, id_b: int) -> dict:
    rng = random.Random(f"{min(id_a, id_b)}:{max(id_a, id_b)}")
    pct = rng.randint(0, 100)
    verdict = next(text for limit, text in VERDICTS if pct < limit)
    return {"pct": pct, "verdict": verdict, "green": rng.choice(GREEN_FLAGS), "red": rng.choice(RED_FLAGS)}


class Ship(commands.GroupCog, group_name="ship", group_description="Check how well two members match"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="match", description="See how compatible two members are (just for fun)")
    @app_commands.describe(first="The first member", second="The second member (default: you)")
    @app_commands.guild_only()
    async def match(self, interaction: discord.Interaction, first: discord.Member,
                    second: discord.Member | None = None):
        second = second or interaction.user
        if first.bot or second.bot:
            return await interaction.response.send_message("Bots don't have feelings. Pick two people.", ephemeral=True)
        if first.id == second.id:
            return await interaction.response.send_message("Self love! **100%** compatible. Treat yourself today.")
        r = ship_result(first.id, second.id)
        filled = r["pct"] // 10
        embed = discord.Embed(title=f"{first.display_name} + {second.display_name}",
                              description=f"Ship name: **{ship_name(first.display_name, second.display_name)}**\n\n"
                                          f"`{'#' * filled}{'-' * (10 - filled)}` **{r['pct']}%**\n{r['verdict']}",
                              color=discord.Color.from_rgb(255, 105, 180))
        embed.add_field(name="Green flag", value=r["green"], inline=False)
        embed.add_field(name="Red flag", value=r["red"], inline=False)
        embed.set_footer(text="Same result every time for the same pair. Just for fun!")
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot):
    for cog in (Telephone, Minesweeper, Memory, QuickDraw, Ship):
        await bot.add_cog(cog(bot))
