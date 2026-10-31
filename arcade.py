import asyncio
import random
import re
import time

import discord
from discord import app_commands
from discord.ext import commands

# =====================================================================
# /trivia : timed multiple choice, quick correct answers earn a bonus
# =====================================================================

QUESTIONS = [
    ("What is the capital of Australia?", ["Canberra", "Sydney", "Melbourne", "Perth"], 0),
    ("Which planet is the biggest in our solar system?", ["Jupiter", "Saturn", "Neptune", "Earth"], 0),
    ("What is the chemical symbol for gold?", ["Au", "Ag", "Gd", "Go"], 0),
    ("How many continents are there?", ["7", "5", "6", "8"], 0),
    ("Who painted the Mona Lisa?", ["Leonardo da Vinci", "Michelangelo", "Raphael", "Van Gogh"], 0),
    ("What is the hardest natural substance?", ["Diamond", "Quartz", "Steel", "Granite"], 0),
    ("Which is the largest ocean?", ["Pacific", "Atlantic", "Indian", "Arctic"], 0),
    ("In what year did the Titanic sink?", ["1912", "1905", "1920", "1898"], 0),
    ("What is the smallest prime number?", ["2", "1", "3", "0"], 0),
    ("Which planet is known as the Red Planet?", ["Mars", "Venus", "Mercury", "Jupiter"], 0),
    ("Which language has the most native speakers?", ["Mandarin Chinese", "English", "Spanish", "Hindi"], 0),
    ("What is the tallest mountain above sea level?", ["Mount Everest", "K2", "Kilimanjaro", "Mont Blanc"], 0),
    ("Which gas do plants absorb from the air?", ["Carbon dioxide", "Oxygen", "Nitrogen", "Helium"], 0),
    ("How many players per team are on the field in soccer?", ["11", "9", "10", "12"], 0),
    ("In which country is the city of Marrakech?", ["Morocco", "Egypt", "Turkey", "Tunisia"], 0),
    ("What is the currency of Japan?", ["Yen", "Won", "Yuan", "Baht"], 0),
    ("Who wrote Romeo and Juliet?", ["William Shakespeare", "Charles Dickens", "Mark Twain", "Jane Austen"], 0),
    ("How many bones does an adult human have?", ["206", "180", "250", "300"], 0),
    ("What is the fastest land animal?", ["Cheetah", "Lion", "Horse", "Greyhound"], 0),
    ("What is the capital of Canada?", ["Ottawa", "Toronto", "Vancouver", "Montreal"], 0),
    ("At what temperature in Celsius does water boil at sea level?", ["100", "90", "120", "80"], 0),
    ("Which element has atomic number 1?", ["Hydrogen", "Helium", "Oxygen", "Carbon"], 0),
    ("What is the largest hot desert in the world?", ["Sahara", "Gobi", "Kalahari", "Arabian"], 0),
    ("How many keys does a standard piano have?", ["88", "76", "92", "100"], 0),
    ("How many sides does a hexagon have?", ["6", "5", "7", "8"], 0),
    ("Who developed the theory of relativity?", ["Albert Einstein", "Isaac Newton", "Nikola Tesla", "Galileo"], 0),
    ("What is the capital of Brazil?", ["Brasilia", "Rio de Janeiro", "Sao Paulo", "Salvador"], 0),
    ("Which planet is famous for its rings?", ["Saturn", "Mars", "Mercury", "Venus"], 0),
    ("What is the main ingredient of guacamole?", ["Avocado", "Tomato", "Pea", "Cucumber"], 0),
    ("In Minecraft, which mob explodes when it gets close to you?", ["Creeper", "Zombie", "Skeleton", "Enderman"], 0),
]


def shuffled(question):
    """Shuffle the answer order and return (text, options, index of the correct one)."""
    text, options, correct = question
    right = options[correct]
    mixed = list(options)
    random.shuffle(mixed)
    return text, mixed, mixed.index(right)


def award(answers: dict[int, tuple[int, float]], correct: int) -> dict[int, int]:
    """answers: user -> (choice, time taken). Right answers get 1 point, the fastest right one gets 2."""
    right = sorted((t, u) for u, (c, t) in answers.items() if c == correct)
    return {u: (2 if i == 0 else 1) for i, (t, u) in enumerate(right)}


class TriviaView(discord.ui.View):
    def __init__(self, cog, guild_id: int, options: list[str], correct: int, seconds: int):
        super().__init__(timeout=seconds)
        self.cog, self.guild_id, self.options, self.correct = cog, guild_id, options, correct
        self.answers: dict[int, tuple[int, float]] = {}
        self.started = time.monotonic()
        self.message: discord.Message | None = None
        self.question = ""
        for i, opt in enumerate(options):
            button = discord.ui.Button(label=opt[:80], style=discord.ButtonStyle.secondary)
            button.callback = self.make_callback(i)
            self.add_item(button)

    def make_callback(self, index: int):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id in self.answers:
                return await interaction.response.send_message("You already locked in an answer.", ephemeral=True)
            self.answers[interaction.user.id] = (index, time.monotonic() - self.started)
            await interaction.response.send_message(f"Locked in: **{self.options[index]}**. Wait for the reveal.",
                                                    ephemeral=True)
        return callback

    async def on_timeout(self):
        self.cog.running.discard(self.guild_id)
        points = award(self.answers, self.correct)
        for uid, pts in points.items():
            await self.cog.bot.db.execute(
                "INSERT INTO trivia_scores (guild_id, user_id, points, correct) VALUES (?,?,?,1) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET points=points+?, correct=correct+1",
                (self.guild_id, uid, pts, pts))
        await self.cog.bot.db.commit()
        for i, child in enumerate(self.children):
            child.disabled = True
            child.style = discord.ButtonStyle.success if i == self.correct else discord.ButtonStyle.secondary
        embed = discord.Embed(title="Trivia", description=f"{self.question}\n\nAnswer: **{self.options[self.correct]}**",
                              color=discord.Color.green())
        if points:
            lines = [f"<@{u}> +{p}" + (" (fastest)" if p == 2 else "") for u, p in points.items()]
            embed.add_field(name=f"Got it ({len(points)} of {len(self.answers)})", value="\n".join(lines)[:1000])
        else:
            embed.add_field(name="Result", value="Nobody got it right." if self.answers else "Nobody answered.")
        if self.message:
            try:
                await self.message.edit(embed=embed, view=self)
            except discord.HTTPException:
                pass


class Trivia(commands.GroupCog, group_name="trivia", group_description="Quick multiple choice questions"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.running: set[int] = set()

    async def cog_load(self):
        await self.bot.db.execute("CREATE TABLE IF NOT EXISTS trivia_scores (guild_id INTEGER, user_id INTEGER, "
                                  "points INTEGER DEFAULT 0, correct INTEGER DEFAULT 0, PRIMARY KEY (guild_id, user_id))")
        await self.bot.db.commit()

    @app_commands.command(name="ask", description="Ask a trivia question everyone can answer")
    @app_commands.describe(seconds="Time to answer (default 20)")
    @app_commands.guild_only()
    async def ask(self, interaction: discord.Interaction, seconds: app_commands.Range[int, 10, 60] = 20):
        if interaction.guild_id in self.running:
            return await interaction.response.send_message("A question is already running. Wait for the reveal.",
                                                           ephemeral=True)
        text, options, correct = shuffled(random.choice(QUESTIONS))
        self.running.add(interaction.guild_id)
        view = TriviaView(self, interaction.guild_id, options, correct, seconds)
        view.question = text
        embed = discord.Embed(title="Trivia", description=text, color=discord.Color.blurple())
        embed.set_footer(text=f"{seconds} seconds. One answer each. Right answers get 1 point, the fastest gets 2.")
        await interaction.response.send_message(embed=embed, view=view)
        view.message = await interaction.original_response()

    @app_commands.command(name="top", description="Trivia leaderboard")
    @app_commands.guild_only()
    async def top(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
                "SELECT user_id, points, correct FROM trivia_scores WHERE guild_id=? ORDER BY points DESC LIMIT 10",
                (interaction.guild_id,)) as cur:
            rows = await cur.fetchall()
        if not rows:
            return await interaction.response.send_message("No scores yet. Try `/trivia ask`.", ephemeral=True)
        text = "\n".join(f"**{i}.** <@{u}> {p} pts ({c} right)" for i, (u, p, c) in enumerate(rows, 1))
        await interaction.response.send_message(embed=discord.Embed(title="Trivia leaders", description=text,
                                                                    color=discord.Color.blurple()),
                                                allowed_mentions=discord.AllowedMentions.none())


# =====================================================================
# /hangman : the whole server guesses letters together
# =====================================================================

HM_WORDS = ["rocket", "pirate", "guitar", "volcano", "diamond", "jungle", "castle", "dolphin", "bridge", "planet",
            "pizza", "wizard", "dragon", "island", "library", "monster", "penguin", "rainbow", "sandwich", "thunder",
            "umbrella", "village", "whistle", "zombie", "balloon", "camera", "desert", "engine", "forest", "garden",
            "hammer", "iceberg", "jacket", "kitchen", "lantern", "mirror", "napkin", "octopus", "pencil", "rabbit",
            "saddle", "tunnel", "violin", "window", "yogurt", "zipper", "anchor", "bicycle", "cactus", "dinosaur"]
HM_ART = [
    "  +---+\n  |   |\n      |\n      |\n      |\n      |\n=========",
    "  +---+\n  |   |\n  O   |\n      |\n      |\n      |\n=========",
    "  +---+\n  |   |\n  O   |\n  |   |\n      |\n      |\n=========",
    "  +---+\n  |   |\n  O   |\n /|   |\n      |\n      |\n=========",
    "  +---+\n  |   |\n  O   |\n /|\\  |\n      |\n      |\n=========",
    "  +---+\n  |   |\n  O   |\n /|\\  |\n /    |\n      |\n=========",
    "  +---+\n  |   |\n  O   |\n /|\\  |\n / \\  |\n      |\n=========",
]
MAX_WRONG = 6


def hm_mask(word: str, guessed: str) -> str:
    return " ".join(c if c in guessed else "_" for c in word)


def hm_apply(word: str, guessed: str, wrong: int, guess: str):
    """Return (guessed, wrong, outcome). Outcomes: invalid, repeat, hit, miss, word_win, word_miss."""
    guess = guess.strip().lower()
    if not guess.isalpha():
        return guessed, wrong, "invalid"
    if len(guess) == 1:
        if guess in guessed:
            return guessed, wrong, "repeat"
        guessed += guess
        if guess in word:
            return guessed, wrong, "hit"
        return guessed, wrong + 1, "miss"
    if guess == word:
        return guessed + word, wrong, "word_win"
    return guessed, wrong + 1, "word_miss"


class Hangman(commands.GroupCog, group_name="hangman", group_description="Guess the word before the man hangs"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        db = self.bot.db
        await db.execute("CREATE TABLE IF NOT EXISTS hangman_games (guild_id INTEGER PRIMARY KEY, channel_id INTEGER, "
                         "word TEXT, guessed TEXT DEFAULT '', wrong INTEGER DEFAULT 0)")
        await db.execute("CREATE TABLE IF NOT EXISTS hangman_scores (guild_id INTEGER, user_id INTEGER, "
                         "wins INTEGER DEFAULT 0, PRIMARY KEY (guild_id, user_id))")
        await db.commit()

    @staticmethod
    def board(word: str, guessed: str, wrong: int, note: str = "") -> discord.Embed:
        misses = " ".join(sorted(set(guessed) - set(word))) or "none"
        done = wrong >= MAX_WRONG or all(c in guessed for c in word)
        embed = discord.Embed(title="Hangman", color=discord.Color.red() if wrong >= MAX_WRONG else
                              discord.Color.green() if done else discord.Color.orange(),
                              description=f"```\n{HM_ART[min(wrong, MAX_WRONG)]}\n```\n`{hm_mask(word, guessed)}`")
        embed.add_field(name="Wrong letters", value=misses)
        embed.add_field(name="Lives left", value=str(max(0, MAX_WRONG - wrong)))
        if note:
            embed.set_footer(text=note)
        return embed

    @app_commands.command(name="start", description="Start a game of hangman")
    @app_commands.guild_only()
    async def start(self, interaction: discord.Interaction):
        async with self.bot.db.execute("SELECT 1 FROM hangman_games WHERE guild_id=?", (interaction.guild_id,)) as cur:
            if await cur.fetchone():
                return await interaction.response.send_message("A game is already running. Use `/hangman guess`.",
                                                               ephemeral=True)
        word = random.choice(HM_WORDS)
        await self.bot.db.execute("INSERT INTO hangman_games (guild_id, channel_id, word) VALUES (?,?,?)",
                                  (interaction.guild_id, interaction.channel_id, word))
        await self.bot.db.commit()
        await interaction.response.send_message(
            embed=self.board(word, "", 0, f"{len(word)} letters. Use /hangman guess with a letter or the whole word."))

    @app_commands.command(name="guess", description="Guess a letter, or the whole word")
    @app_commands.describe(guess="One letter, or the full word")
    @app_commands.guild_only()
    async def guess(self, interaction: discord.Interaction, guess: app_commands.Range[str, 1, 20]):
        async with self.bot.db.execute("SELECT word, guessed, wrong FROM hangman_games WHERE guild_id=?",
                                       (interaction.guild_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            return await interaction.response.send_message("No game is running. Start one with `/hangman start`.",
                                                           ephemeral=True)
        word, guessed, wrong = row
        guessed, wrong, outcome = hm_apply(word, guessed, wrong, guess)
        if outcome == "invalid":
            return await interaction.response.send_message("Letters only.", ephemeral=True)
        if outcome == "repeat":
            return await interaction.response.send_message("That letter was already guessed.", ephemeral=True)
        won = all(c in guessed for c in word)
        lost = wrong >= MAX_WRONG and not won
        who = interaction.user.display_name
        note = {"hit": f"{who} found a letter!", "miss": f"{who} missed.", "word_win": f"{who} got the word!",
                "word_miss": f"{who} guessed the wrong word."}[outcome]
        if won or lost:
            await self.bot.db.execute("DELETE FROM hangman_games WHERE guild_id=?", (interaction.guild_id,))
            if won:
                await self.bot.db.execute(
                    "INSERT INTO hangman_scores (guild_id, user_id, wins) VALUES (?,?,1) "
                    "ON CONFLICT(guild_id, user_id) DO UPDATE SET wins=wins+1", (interaction.guild_id, interaction.user.id))
                note += f" The word was {word}. {who} takes the win!"
            else:
                note += f" Game over. The word was {word}."
        else:
            await self.bot.db.execute("UPDATE hangman_games SET guessed=?, wrong=? WHERE guild_id=?",
                                      (guessed, wrong, interaction.guild_id))
        await self.bot.db.commit()
        shown = guessed + word if lost else guessed  # reveal the word when the game is lost
        await interaction.response.send_message(embed=self.board(word, shown, wrong, note))

    @app_commands.command(name="stop", description="End the current game (staff)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_messages=True)
    async def stop(self, interaction: discord.Interaction):
        cur = await self.bot.db.execute("DELETE FROM hangman_games WHERE guild_id=?", (interaction.guild_id,))
        await self.bot.db.commit()
        await interaction.response.send_message("Game stopped." if cur.rowcount else "No game is running.", ephemeral=True)

    @app_commands.command(name="top", description="Best hangman players")
    @app_commands.guild_only()
    async def top(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
                "SELECT user_id, wins FROM hangman_scores WHERE guild_id=? ORDER BY wins DESC LIMIT 10",
                (interaction.guild_id,)) as cur:
            rows = await cur.fetchall()
        if not rows:
            return await interaction.response.send_message("No wins yet.", ephemeral=True)
        text = "\n".join(f"**{i}.** <@{u}> {w} win{'s' if w != 1 else ''}" for i, (u, w) in enumerate(rows, 1))
        await interaction.response.send_message(embed=discord.Embed(title="Hangman winners", description=text,
                                                                    color=discord.Color.orange()),
                                                allowed_mentions=discord.AllowedMentions.none())


# =====================================================================
# /scramble : unscramble the word first, in the chat
# =====================================================================

SCRAMBLE_WORDS = ["python", "discord", "server", "channel", "message", "keyboard", "monitor", "gaming", "player",
                  "victory", "journey", "network", "science", "history", "puzzle", "garden", "orange", "banana",
                  "purple", "yellow", "circle", "square", "planet", "galaxy", "meteor", "silver", "golden", "forest",
                  "ocean", "island", "castle", "dragon", "knight", "wizard", "pirate", "treasure", "rocket", "engine",
                  "bottle", "pencil", "button", "window", "mirror", "ticket", "pocket", "summer", "winter", "spring",
                  "autumn", "coffee", "cookie", "butter", "chicken", "pepper", "breeze", "shadow", "thunder", "crystal"]


def scramble(word: str, rng=random) -> str:
    letters = list(word)
    for _ in range(20):
        rng.shuffle(letters)
        if "".join(letters) != word:
            break
    return "".join(letters)


class Scramble(commands.GroupCog, group_name="scramble", group_description="Race to unscramble words in chat"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.games: dict[int, dict] = {}  # channel_id -> state

    async def cog_load(self):
        await self.bot.db.execute("CREATE TABLE IF NOT EXISTS scramble_scores (guild_id INTEGER, user_id INTEGER, "
                                  "wins INTEGER DEFAULT 0, PRIMARY KEY (guild_id, user_id))")
        await self.bot.db.commit()

    async def cog_unload(self):
        for g in self.games.values():
            g["task"].cancel()

    @app_commands.command(name="start", description="Start a word scramble race in this channel")
    @app_commands.describe(rounds="How many words (default 5)", seconds="Time per word (default 25)")
    @app_commands.guild_only()
    async def start(self, interaction: discord.Interaction, rounds: app_commands.Range[int, 1, 15] = 5,
                    seconds: app_commands.Range[int, 10, 60] = 25):
        if interaction.channel_id in self.games:
            return await interaction.response.send_message("A race is already running here.", ephemeral=True)
        await interaction.response.send_message(f"**Word scramble!** {rounds} words, {seconds} seconds each. "
                                                "Just type the answer in this channel. First one wins the point.")
        state = {"word": None, "future": None, "scores": {}, "guild": interaction.guild_id}
        self.games[interaction.channel_id] = state
        state["task"] = asyncio.create_task(self.run(interaction.channel, state, rounds, seconds))

    async def run(self, channel: discord.abc.Messageable, state: dict, rounds: int, seconds: int):
        try:
            words = random.sample(SCRAMBLE_WORDS, min(rounds, len(SCRAMBLE_WORDS)))
            for n, word in enumerate(words, 1):
                await asyncio.sleep(2)
                state["word"] = word
                state["future"] = asyncio.get_running_loop().create_future()
                await channel.send(f"Round {n}/{len(words)}: unscramble **{scramble(word)}** "
                                   f"({len(word)} letters, starts with **{word[0]}**)")
                try:
                    winner = await asyncio.wait_for(state["future"], seconds)
                    state["scores"][winner] = state["scores"].get(winner, 0) + 1
                    await channel.send(f"<@{winner}> got it! The word was **{word}**.",
                                       allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=winner)]))
                except asyncio.TimeoutError:
                    await channel.send(f"Time's up! The word was **{word}**.")
                state["word"] = None
            await self.finish(channel, state)
        except asyncio.CancelledError:
            raise
        finally:
            self.games.pop(getattr(channel, "id", 0), None)

    async def finish(self, channel, state: dict):
        scores = sorted(state["scores"].items(), key=lambda kv: -kv[1])
        for uid, pts in scores:
            await self.bot.db.execute(
                "INSERT INTO scramble_scores (guild_id, user_id, wins) VALUES (?,?,?) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET wins=wins+?", (state["guild"], uid, pts, pts))
        await self.bot.db.commit()
        text = "\n".join(f"**{i}.** <@{u}> {p}" for i, (u, p) in enumerate(scores, 1)) or "Nobody scored."
        await channel.send(embed=discord.Embed(title="Race over", description=text, color=discord.Color.gold()),
                           allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="stop", description="Stop the race in this channel (staff)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_messages=True)
    async def stop(self, interaction: discord.Interaction):
        state = self.games.pop(interaction.channel_id, None)
        if not state:
            return await interaction.response.send_message("No race is running here.", ephemeral=True)
        state["task"].cancel()
        await interaction.response.send_message("Race stopped.")

    @app_commands.command(name="top", description="Best word unscramblers")
    @app_commands.guild_only()
    async def top(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
                "SELECT user_id, wins FROM scramble_scores WHERE guild_id=? ORDER BY wins DESC LIMIT 10",
                (interaction.guild_id,)) as cur:
            rows = await cur.fetchall()
        if not rows:
            return await interaction.response.send_message("No points yet.", ephemeral=True)
        text = "\n".join(f"**{i}.** <@{u}> {w} word{'s' if w != 1 else ''}" for i, (u, w) in enumerate(rows, 1))
        await interaction.response.send_message(embed=discord.Embed(title="Unscramble leaders", description=text,
                                                                    color=discord.Color.gold()),
                                                allowed_mentions=discord.AllowedMentions.none())

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        state = self.games.get(message.channel.id)
        if not state or not state["word"] or message.content.strip().lower() != state["word"]:
            return
        if state["future"] and not state["future"].done():
            state["future"].set_result(message.author.id)


# =====================================================================
# /tierlist : the server ranks things from S to D
# =====================================================================

TIERS = ["S", "A", "B", "C", "D"]


def tier_rows(items: list[str], votes: list[tuple[int, int]]) -> tuple[dict[str, list], list[str]]:
    """votes: (item index, tier index). Returns ({tier: [(item, average, count)]}, items with no votes)."""
    totals: dict[int, list[int]] = {}
    for idx, tier in votes:
        totals.setdefault(idx, []).append(tier)
    rows: dict[str, list] = {t: [] for t in TIERS}
    unvoted = []
    for i, item in enumerate(items):
        if i not in totals:
            unvoted.append(item)
            continue
        avg = sum(totals[i]) / len(totals[i])
        rows[TIERS[min(len(TIERS) - 1, int(avg + 0.5))]].append((item, avg, len(totals[i])))
    for t in TIERS:
        rows[t].sort(key=lambda r: r[1])
    return rows, unvoted


def tier_embed(topic: str, items: list[str], votes: list[tuple[int, int]], voters: int, final: bool = False):
    rows, unvoted = tier_rows(items, votes)
    embed = discord.Embed(title=f"Tier list: {topic}"[:256],
                          color=discord.Color.dark_grey() if final else discord.Color.blurple())
    for t in TIERS:
        names = ", ".join(name for name, avg, n in rows[t])
        embed.add_field(name=f"{t} tier", value=names or "-", inline=False)
    if unvoted:
        embed.add_field(name="No votes yet", value=", ".join(unvoted)[:1000], inline=False)
    embed.set_footer(text=f"{voters} voter{'s' if voters != 1 else ''}" + (". Final result." if final else ""))
    return embed


class TierVoteView(discord.ui.View):
    def __init__(self, cog, message_id: int, items: list[str], mine: dict[int, int]):
        super().__init__(timeout=300)
        self.cog, self.message_id, self.items, self.mine = cog, message_id, items, mine
        self.item: int | None = None
        self.pick_item.options = [discord.SelectOption(label=f"{i + 1}. {it}"[:100], value=str(i))
                                  for i, it in enumerate(items)]
        self.pick_tier.options = [discord.SelectOption(label=f"{t} tier", value=str(n)) for n, t in enumerate(TIERS)]

    def summary(self) -> str:
        lines = [f"{'> ' if i == self.item else ''}{it}: **{TIERS[self.mine[i]] if i in self.mine else '-'}**"
                 for i, it in enumerate(self.items)]
        head = "Pick an item, then a tier. Do it for as many items as you like." if self.item is None else \
            f"Now choose a tier for **{self.items[self.item]}**."
        return head + "\n" + "\n".join(lines)

    @discord.ui.select(placeholder="1. Choose an item", min_values=1, max_values=1)
    async def pick_item(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.item = int(select.values[0])
        await interaction.response.edit_message(content=self.summary(), view=self)

    @discord.ui.select(placeholder="2. Choose its tier", min_values=1, max_values=1)
    async def pick_tier(self, interaction: discord.Interaction, select: discord.ui.Select):
        if self.item is None:
            return await interaction.response.send_message("Choose an item first.", ephemeral=True)
        async with self.cog.bot.db.execute("SELECT closed FROM tier_lists WHERE message_id=?", (self.message_id,)) as cur:
            row = await cur.fetchone()
        if not row or row[0]:
            return await interaction.response.send_message("This tier list is closed.", ephemeral=True)
        tier = int(select.values[0])
        await self.cog.bot.db.execute(
            "INSERT INTO tier_votes (message_id, user_id, item_index, tier) VALUES (?,?,?,?) "
            "ON CONFLICT(message_id, user_id, item_index) DO UPDATE SET tier=excluded.tier",
            (self.message_id, interaction.user.id, self.item, tier))
        await self.cog.bot.db.commit()
        self.mine[self.item] = tier
        await interaction.response.edit_message(content=self.summary(), view=self)


class TierView(discord.ui.View):
    def __init__(self, cog, disabled: bool = False):
        super().__init__(timeout=None)
        self.cog = cog
        self.vote.disabled = self.results.disabled = disabled

    @discord.ui.button(label="Vote", style=discord.ButtonStyle.primary, custom_id="tier:vote")
    async def vote(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = await self.cog.load(interaction.message.id)
        if not data:
            return await interaction.response.send_message("I can't find this tier list anymore.", ephemeral=True)
        topic, items, closed = data
        async with self.cog.bot.db.execute("SELECT item_index, tier FROM tier_votes WHERE message_id=? AND user_id=?",
                                           (interaction.message.id, interaction.user.id)) as cur:
            mine = dict(await cur.fetchall())
        view = TierVoteView(self.cog, interaction.message.id, items, mine)
        await interaction.response.send_message(view.summary(), view=view, ephemeral=True)

    @discord.ui.button(label="Results", style=discord.ButtonStyle.secondary, custom_id="tier:results")
    async def results(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = await self.cog.load(interaction.message.id)
        if not data:
            return await interaction.response.send_message("I can't find this tier list anymore.", ephemeral=True)
        topic, items, closed = data
        votes, voters = await self.cog.votes(interaction.message.id)
        await interaction.response.send_message(embed=tier_embed(topic, items, votes, voters), ephemeral=True)


class TierList(commands.GroupCog, group_name="tierlist", group_description="Rank things from S to D together"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        db = self.bot.db
        await db.execute("CREATE TABLE IF NOT EXISTS tier_lists (message_id INTEGER PRIMARY KEY, guild_id INTEGER, "
                         "channel_id INTEGER, creator_id INTEGER, topic TEXT, items TEXT, closed INTEGER DEFAULT 0)")
        await db.execute("CREATE TABLE IF NOT EXISTS tier_votes (message_id INTEGER, user_id INTEGER, item_index INTEGER, "
                         "tier INTEGER, PRIMARY KEY (message_id, user_id, item_index))")
        await db.commit()
        self.bot.add_view(TierView(self))

    async def load(self, message_id: int):
        async with self.bot.db.execute("SELECT topic, items, closed FROM tier_lists WHERE message_id=?",
                                       (message_id,)) as cur:
            row = await cur.fetchone()
        return (row[0], row[1].split("\n"), row[2]) if row else None

    async def votes(self, message_id: int):
        async with self.bot.db.execute("SELECT item_index, tier FROM tier_votes WHERE message_id=?", (message_id,)) as cur:
            votes = await cur.fetchall()
        async with self.bot.db.execute("SELECT COUNT(DISTINCT user_id) FROM tier_votes WHERE message_id=?",
                                       (message_id,)) as cur:
            (voters,) = await cur.fetchone()
        return votes, voters

    @app_commands.command(name="create", description="Let the server rank a list of things")
    @app_commands.describe(topic="What you are ranking, for example Pizza toppings",
                           items="3 to 10 things, separated by commas")
    @app_commands.guild_only()
    async def create(self, interaction: discord.Interaction, topic: app_commands.Range[str, 1, 80], items: str):
        names = [i.strip()[:40] for i in items.split(",") if i.strip()]
        if not 3 <= len(names) <= 10 or len(set(n.lower() for n in names)) != len(names):
            return await interaction.response.send_message(
                "Give me 3 to 10 different items separated by commas.", ephemeral=True)
        embed = discord.Embed(title=f"Tier list: {topic}", color=discord.Color.blurple(),
                              description="Rank these from S (best) to D (worst):\n" +
                                          "\n".join(f"{i + 1}. {n}" for i, n in enumerate(names)))
        embed.set_footer(text="Press Vote to rank them. Results shows the current tier list (only you see it).")
        await interaction.response.send_message(embed=embed, view=TierView(self),
                                                allowed_mentions=discord.AllowedMentions.none())
        msg = await interaction.original_response()
        await self.bot.db.execute(
            "INSERT INTO tier_lists (message_id, guild_id, channel_id, creator_id, topic, items) VALUES (?,?,?,?,?,?)",
            (msg.id, interaction.guild_id, msg.channel.id, interaction.user.id, topic, "\n".join(names)))
        await self.bot.db.commit()

    @app_commands.command(name="end", description="Close a tier list and post the final result")
    @app_commands.describe(message_id="The tier list message ID (right click, Copy Message ID)")
    @app_commands.guild_only()
    async def end(self, interaction: discord.Interaction, message_id: str):
        if not message_id.isdigit():
            return await interaction.response.send_message("That isn't a message ID.", ephemeral=True)
        async with self.bot.db.execute(
                "SELECT creator_id, channel_id, closed FROM tier_lists WHERE message_id=? AND guild_id=?",
                (int(message_id), interaction.guild_id)) as cur:
            row = await cur.fetchone()
        if not row:
            return await interaction.response.send_message("I don't know a tier list with that ID.", ephemeral=True)
        creator, channel_id, closed = row
        if interaction.user.id != creator and not interaction.user.guild_permissions.manage_messages:
            return await interaction.response.send_message("Only the creator or a moderator can end it.", ephemeral=True)
        if closed:
            return await interaction.response.send_message("It's already closed.", ephemeral=True)
        await self.bot.db.execute("UPDATE tier_lists SET closed=1 WHERE message_id=?", (int(message_id),))
        await self.bot.db.commit()
        topic, items, _ = await self.load(int(message_id))
        votes, voters = await self.votes(int(message_id))
        embed = tier_embed(topic, items, votes, voters, final=True)
        channel = interaction.guild.get_channel(channel_id)
        try:
            await channel.get_partial_message(int(message_id)).edit(embed=embed, view=TierView(self, disabled=True))
        except (discord.HTTPException, AttributeError):
            pass
        await interaction.response.send_message("Closed. The final tier list is on the original message.", ephemeral=True)


# =====================================================================
# /whosaid : guess who wrote a random recent message
# =====================================================================

LINK_RE = re.compile(r"https?://|discord\.gg|<@|<#|@everyone|@here", re.I)


def pick_round(candidates: list[tuple[int, str, str]], rng=random):
    """candidates: (author_id, author_name, text). Returns (text, correct_author_id, [(id, name)] choices) or None."""
    authors = {}
    for aid, name, _ in candidates:
        authors.setdefault(aid, name)
    if len(authors) < 3:
        return None
    aid, name, text = rng.choice(candidates)
    others = [(a, n) for a, n in authors.items() if a != aid]
    rng.shuffle(others)
    choices = [(aid, name)] + others[:3]
    rng.shuffle(choices)
    return text, aid, choices


class WhoSaidView(discord.ui.View):
    def __init__(self, cog, channel_id: int, guild_id: int, text: str, correct: int, choices: list[tuple[int, str]]):
        super().__init__(timeout=30)
        self.cog, self.channel_id, self.guild_id = cog, channel_id, guild_id
        self.text, self.correct, self.choices = text, correct, choices
        self.tried: set[int] = set()
        self.done = False
        self.message: discord.Message | None = None
        for aid, name in choices:
            button = discord.ui.Button(label=name[:80], style=discord.ButtonStyle.secondary)
            button.callback = self.make_callback(aid)
            self.add_item(button)

    def make_callback(self, author_id: int):
        async def callback(interaction: discord.Interaction):
            if self.done:
                return await interaction.response.send_message("This round is over.", ephemeral=True)
            if interaction.user.id == self.correct:
                return await interaction.response.send_message("That's your own message, you can't guess!",
                                                               ephemeral=True)
            if interaction.user.id in self.tried:
                return await interaction.response.send_message("You already used your guess.", ephemeral=True)
            self.tried.add(interaction.user.id)
            if author_id != self.correct:
                return await interaction.response.send_message("Nope, not them.", ephemeral=True)
            self.done = True
            self.stop()
            await self.cog.win(self.guild_id, interaction.user.id)
            await interaction.response.edit_message(embed=self.reveal(f"{interaction.user.mention} got it!"),
                                                    view=self.disable(True))
            self.cog.running.discard(self.channel_id)
        return callback

    def disable(self, solved: bool):
        for child, (aid, _) in zip(self.children, self.choices):
            child.disabled = True
            if aid == self.correct:
                child.style = discord.ButtonStyle.success
        return self

    def reveal(self, line: str) -> discord.Embed:
        embed = discord.Embed(title="Who said it?", description=f"> {self.text}\n\nIt was <@{self.correct}>. {line}",
                              color=discord.Color.green())
        return embed

    async def on_timeout(self):
        if self.done:
            return
        self.done = True
        self.cog.running.discard(self.channel_id)
        if self.message:
            try:
                await self.message.edit(embed=self.reveal("Nobody got it."), view=self.disable(False))
            except discord.HTTPException:
                pass


class WhoSaid(commands.GroupCog, group_name="whosaid", group_description="Guess who wrote a recent message"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.running: set[int] = set()

    async def cog_load(self):
        await self.bot.db.execute("CREATE TABLE IF NOT EXISTS whosaid_scores (guild_id INTEGER, user_id INTEGER, "
                                  "wins INTEGER DEFAULT 0, PRIMARY KEY (guild_id, user_id))")
        await self.bot.db.commit()

    async def win(self, guild_id: int, user_id: int):
        await self.bot.db.execute(
            "INSERT INTO whosaid_scores (guild_id, user_id, wins) VALUES (?,?,1) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET wins=wins+1", (guild_id, user_id))
        await self.bot.db.commit()

    @app_commands.command(name="play", description="Show a random recent message from this channel, guess who wrote it")
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(read_message_history=True)
    async def play(self, interaction: discord.Interaction):
        if interaction.channel_id in self.running:
            return await interaction.response.send_message("A round is already running here.", ephemeral=True)
        self.running.add(interaction.channel_id)
        await interaction.response.defer()
        candidates = []
        try:
            async for m in interaction.channel.history(limit=150):
                if (m.author.bot or not isinstance(m.author, discord.Member) or m.attachments or m.embeds
                        or not 20 <= len(m.content) <= 200 or LINK_RE.search(m.content)):
                    continue
                candidates.append((m.author.id, m.author.display_name, m.content))
        except discord.HTTPException:
            self.running.discard(interaction.channel_id)
            return await interaction.followup.send("I couldn't read this channel's history.", ephemeral=True)
        picked = pick_round(candidates)
        if not picked:
            self.running.discard(interaction.channel_id)
            return await interaction.followup.send(
                "I need recent messages from at least 3 different people in this channel to play. Chat a bit more!",
                ephemeral=True)
        text, correct, choices = picked
        view = WhoSaidView(self, interaction.channel_id, interaction.guild_id, text, correct, choices)
        embed = discord.Embed(title="Who said it?", description=f"> {text}", color=discord.Color.purple())
        embed.set_footer(text="A random recent message from this channel. One guess each, 30 seconds.")
        view.message = await interaction.followup.send(embed=embed, view=view, wait=True,
                                                       allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="top", description="Best at guessing who said it")
    @app_commands.guild_only()
    async def top(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
                "SELECT user_id, wins FROM whosaid_scores WHERE guild_id=? ORDER BY wins DESC LIMIT 10",
                (interaction.guild_id,)) as cur:
            rows = await cur.fetchall()
        if not rows:
            return await interaction.response.send_message("No rounds won yet.", ephemeral=True)
        text = "\n".join(f"**{i}.** <@{u}> {w}" for i, (u, w) in enumerate(rows, 1))
        await interaction.response.send_message(embed=discord.Embed(title="Best detectives", description=text,
                                                                    color=discord.Color.purple()),
                                                allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot):
    for cog in (Trivia, Hangman, Scramble, TierList, WhoSaid):
        await bot.add_cog(cog(bot))
