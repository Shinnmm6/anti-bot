import asyncio
import random
import re
import time

import discord
from discord import app_commands
from discord.ext import commands


# =====================================================================
# /drops : loot crates fall from the sky while people chat
# =====================================================================

RARITIES = [("Common", 60, 1), ("Rare", 28, 3), ("Epic", 10, 10), ("Legendary", 2, 50)]  # name, weight, score
LOOT = {
    "Common": ["Wooden Coin", "Shiny Pebble", "Lucky Button", "Old Key", "Bent Spoon", "Paper Crown"],
    "Rare": ["Silver Compass", "Glowing Mushroom", "Pocket Dragon Egg", "Moon Feather", "Tiny Telescope", "Jade Dice"],
    "Epic": ["Phoenix Ember", "Storm in a Jar", "Crystal Skull", "Golden Harp", "Shadow Cloak", "Star Map"],
    "Legendary": ["Crown of Ages", "Heart of the Mountain", "Sword of a Thousand Suns", "The Last Dragon Scale", "Orb of Everything"],
}
SCORES = {name: pts for name, _, pts in RARITIES}
MODES = {"relaxed": (90, 160), "normal": (45, 90), "frequent": (20, 45)}


def pick_loot(rng=random) -> tuple[str, str]:
    rarity = rng.choices([r[0] for r in RARITIES], weights=[r[1] for r in RARITIES])[0]
    return rarity, rng.choice(LOOT[rarity])


def next_target(mode: str, rng=random) -> int:
    lo, hi = MODES.get(mode, MODES["normal"])
    return rng.randint(lo, hi)


class CrateView(discord.ui.View):
    def __init__(self, cog, guild_id: int):
        super().__init__(timeout=60)
        self.cog, self.guild_id = cog, guild_id
        self.claimed = False
        self.message: discord.Message | None = None

    @discord.ui.button(label="Open the crate", style=discord.ButtonStyle.success)
    async def open(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.claimed:
            return await interaction.response.send_message("Someone else already opened it.", ephemeral=True)
        self.claimed = True
        self.stop()
        rarity, item = pick_loot()
        await self.cog.give(self.guild_id, interaction.user.id, item, rarity)
        self.cog.active.discard(self.guild_id)
        button.disabled = True
        embed = discord.Embed(title="Crate opened!", color=self.cog.color(rarity),
                              description=f"{interaction.user.mention} found a **{rarity}** item: **{item}**")
        await interaction.response.edit_message(embed=embed, view=self, allowed_mentions=discord.AllowedMentions.none())

    async def on_timeout(self):
        if self.claimed:
            return
        self.cog.active.discard(self.guild_id)
        if self.message:
            try:
                await self.message.edit(embed=discord.Embed(title="The crate crumbled", description="Nobody opened it in time.",
                                                            color=discord.Color.dark_grey()), view=None)
            except discord.HTTPException:
                pass


class Drops(commands.GroupCog, group_name="drops", group_description="Loot crates that fall while you chat"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config: dict[int, dict] = {}
        self.active: set[int] = set()
        self.counter: dict[int, int] = {}
        self.target: dict[int, int] = {}
        self.last_msg: dict[tuple[int, int], float] = {}
        self.last_drop: dict[int, float] = {}

    async def cog_load(self):
        db = self.bot.db
        await db.execute("CREATE TABLE IF NOT EXISTS drops_config (guild_id INTEGER PRIMARY KEY, channel_id INTEGER, mode TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS drops_bag (guild_id INTEGER, user_id INTEGER, item TEXT, rarity TEXT, "
                         "count INTEGER DEFAULT 0, PRIMARY KEY (guild_id, user_id, item))")
        await db.commit()
        async with db.execute("SELECT guild_id, channel_id, mode FROM drops_config") as cur:
            for gid, ch, mode in await cur.fetchall():
                self.config[gid] = {"channel_id": ch, "mode": mode}
                self.target[gid] = next_target(mode)

    @staticmethod
    def color(rarity: str) -> discord.Color:
        return {"Common": discord.Color.light_grey(), "Rare": discord.Color.blue(), "Epic": discord.Color.purple(),
                "Legendary": discord.Color.gold()}[rarity]

    async def give(self, guild_id: int, user_id: int, item: str, rarity: str):
        await self.bot.db.execute(
            "INSERT INTO drops_bag (guild_id, user_id, item, rarity, count) VALUES (?,?,?,?,1) "
            "ON CONFLICT(guild_id, user_id, item) DO UPDATE SET count=count+1", (guild_id, user_id, item, rarity))
        await self.bot.db.commit()

    @app_commands.command(name="setup", description="Choose the channel where loot crates fall (staff)")
    @app_commands.describe(channel="Where crates appear", frequency="How often crates fall as people chat")
    @app_commands.choices(frequency=[app_commands.Choice(name="Relaxed", value="relaxed"),
                                     app_commands.Choice(name="Normal", value="normal"),
                                     app_commands.Choice(name="Frequent", value="frequent")])
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup_cmd(self, interaction: discord.Interaction, channel: discord.TextChannel,
                        frequency: app_commands.Choice[str] | None = None):
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return await interaction.response.send_message(
                f"I need View Channel, Send Messages and Embed Links in {channel.mention}.", ephemeral=True)
        mode = frequency.value if frequency else "normal"
        await self.bot.db.execute("INSERT OR REPLACE INTO drops_config (guild_id, channel_id, mode) VALUES (?,?,?)",
                                  (interaction.guild_id, channel.id, mode))
        await self.bot.db.commit()
        self.config[interaction.guild_id] = {"channel_id": channel.id, "mode": mode}
        self.counter[interaction.guild_id] = 0
        self.target[interaction.guild_id] = next_target(mode)
        await interaction.response.send_message(f"Loot crates will fall in {channel.mention} ({mode}) as people chat there.",
                                                ephemeral=True)

    @app_commands.command(name="off", description="Stop the loot crates (staff)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def off(self, interaction: discord.Interaction):
        await self.bot.db.execute("DELETE FROM drops_config WHERE guild_id=?", (interaction.guild_id,))
        await self.bot.db.commit()
        self.config.pop(interaction.guild_id, None)
        await interaction.response.send_message("Crates are off. Everyone keeps what they collected.", ephemeral=True)

    @app_commands.command(name="bag", description="See what you have collected")
    @app_commands.guild_only()
    async def bag(self, interaction: discord.Interaction, member: discord.Member | None = None):
        target = member or interaction.user
        async with self.bot.db.execute("SELECT item, rarity, count FROM drops_bag WHERE guild_id=? AND user_id=?",
                                       (interaction.guild_id, target.id)) as cur:
            rows = await cur.fetchall()
        if not rows:
            return await interaction.response.send_message(
                f"{target.display_name} has nothing yet. Chat in the crate channel and be quick!", ephemeral=True)
        embed = discord.Embed(title=f"{target.display_name}'s bag", color=discord.Color.gold())
        total = 0
        for rarity, _, pts in reversed(RARITIES):
            mine = [(i, c) for i, r, c in rows if r == rarity]
            total += sum(c for _, c in mine) * pts
            if mine:
                embed.add_field(name=rarity, value="\n".join(f"{i} x{c}" for i, c in sorted(mine))[:1000], inline=False)
        embed.set_footer(text=f"Collection score: {total}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="top", description="The best collectors")
    @app_commands.guild_only()
    async def top(self, interaction: discord.Interaction):
        async with self.bot.db.execute("SELECT user_id, rarity, count FROM drops_bag WHERE guild_id=?",
                                       (interaction.guild_id,)) as cur:
            rows = await cur.fetchall()
        score: dict[int, int] = {}
        for uid, rarity, c in rows:
            score[uid] = score.get(uid, 0) + c * SCORES.get(rarity, 1)
        if not score:
            return await interaction.response.send_message("Nobody has opened a crate yet.", ephemeral=True)
        ranked = sorted(score.items(), key=lambda kv: -kv[1])[:10]
        text = "\n".join(f"**{i}.** <@{u}> {s} points" for i, (u, s) in enumerate(ranked, 1))
        await interaction.response.send_message(embed=discord.Embed(title="Top collectors", description=text,
                                                                    color=discord.Color.gold()),
                                                allowed_mentions=discord.AllowedMentions.none())

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot or len(message.content) < 3:
            return
        gid = message.guild.id
        cfg = self.config.get(gid)
        if not cfg or message.channel.id != cfg["channel_id"] or gid in self.active:
            return
        now = time.monotonic()
        key = (gid, message.author.id)
        if now - self.last_msg.get(key, 0) < 6:  # one person spamming doesn't speed it up
            return
        self.last_msg[key] = now
        self.counter[gid] = self.counter.get(gid, 0) + 1
        if self.counter[gid] < self.target.get(gid, 60) or now - self.last_drop.get(gid, 0) < 180:
            return
        self.counter[gid] = 0
        self.target[gid] = next_target(cfg["mode"])
        self.last_drop[gid] = now
        self.active.add(gid)
        view = CrateView(self, gid)
        embed = discord.Embed(title="A loot crate fell from the sky!", color=discord.Color.orange(),
                              description="The first person to open it keeps what's inside. You have 60 seconds.")
        try:
            view.message = await message.channel.send(embed=embed, view=view)
        except discord.HTTPException:
            self.active.discard(gid)


# =====================================================================
# /taboo : describe a word without saying it
# =====================================================================

CARDS = [
    ("pizza", ["cheese", "pepperoni", "italy", "slice"]), ("beach", ["sand", "ocean", "sun", "swim"]),
    ("dragon", ["fire", "wings", "scales", "fantasy"]), ("guitar", ["strings", "music", "band", "play"]),
    ("rainbow", ["colors", "rain", "sky", "arch"]), ("vampire", ["blood", "fangs", "dracula", "bite"]),
    ("robot", ["machine", "metal", "android", "technology"]), ("pirate", ["ship", "treasure", "sea", "parrot"]),
    ("snowman", ["snow", "winter", "carrot", "cold"]), ("birthday", ["cake", "party", "candles", "age"]),
    ("camera", ["photo", "picture", "lens", "flash"]), ("bicycle", ["wheels", "pedal", "ride", "bike"]),
    ("volcano", ["lava", "eruption", "mountain", "hot"]), ("astronaut", ["space", "rocket", "moon", "nasa"]),
    ("chocolate", ["sweet", "cocoa", "candy", "brown"]), ("dinosaur", ["extinct", "fossil", "big", "rex"]),
    ("umbrella", ["rain", "wet", "hold", "open"]), ("library", ["books", "read", "quiet", "borrow"]),
    ("wizard", ["magic", "wand", "spell", "hat"]), ("penguin", ["ice", "antarctica", "bird", "tuxedo"]),
    ("elevator", ["lift", "floors", "button", "up"]), ("lighthouse", ["ship", "light", "sea", "tower"]),
    ("sandwich", ["bread", "lunch", "ham", "between"]), ("skateboard", ["wheels", "trick", "ramp", "deck"]),
    ("hospital", ["doctor", "nurse", "sick", "patient"]), ("popcorn", ["movie", "butter", "corn", "snack"]),
    ("passport", ["travel", "country", "airport", "visa"]), ("headphones", ["music", "ears", "listen", "wireless"]),
]


def violates(text: str, word: str, taboo: list[str]) -> str | None:
    """Return the first forbidden word the text uses (also with an s on the end), or None."""
    for w in [word] + taboo:
        if re.search(rf"\b{re.escape(w)}(s|es)?\b", text.lower()):
            return w
    return None


def is_guess(text: str, word: str) -> bool:
    return any(t in (word, word + "s", word + "es") for t in re.findall(r"[a-z]+", text.lower()))


class Taboo(commands.GroupCog, group_name="taboo", group_description="Describe it without saying the forbidden words"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.rounds: dict[int, dict] = {}

    async def cog_load(self):
        await self.bot.db.execute("CREATE TABLE IF NOT EXISTS taboo_scores (guild_id INTEGER, user_id INTEGER, "
                                  "points INTEGER DEFAULT 0, PRIMARY KEY (guild_id, user_id))")
        await self.bot.db.commit()

    async def cog_unload(self):
        for r in self.rounds.values():
            r["task"].cancel()

    async def add_points(self, guild_id: int, user_id: int, pts: int):
        await self.bot.db.execute(
            "INSERT INTO taboo_scores (guild_id, user_id, points) VALUES (?,?,?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET points=points+?", (guild_id, user_id, pts, pts))
        await self.bot.db.commit()

    @app_commands.command(name="start", description="You describe a secret word, everyone else guesses it in chat")
    @app_commands.describe(seconds="Round length (default 90)")
    @app_commands.guild_only()
    async def start(self, interaction: discord.Interaction, seconds: app_commands.Range[int, 30, 180] = 90):
        if interaction.channel_id in self.rounds:
            return await interaction.response.send_message("A round is already running in this channel.", ephemeral=True)
        word, taboo = random.choice(CARDS)
        await interaction.response.send_message(
            f"Your word: **{word}**\nYou can't say it, or any of these: **{', '.join(taboo)}**.\n"
            f"Describe it in this channel for {seconds} seconds. Say a forbidden word and the round is over!", ephemeral=True)
        channel = interaction.channel
        state = {"word": word, "taboo": taboo, "describer": interaction.user.id, "guild": interaction.guild_id}
        self.rounds[channel.id] = state
        await channel.send(f"**Taboo!** {interaction.user.mention} is describing a secret word. Type your guesses in chat! "
                           f"{seconds} seconds.", allowed_mentions=discord.AllowedMentions(users=[interaction.user]))
        state["task"] = asyncio.create_task(self.timer(channel, state, seconds))

    async def timer(self, channel, state: dict, seconds: int):
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        if self.rounds.get(channel.id) is state:
            self.rounds.pop(channel.id, None)
            try:
                await channel.send(f"Time's up! The word was **{state['word']}**.")
            except discord.HTTPException:
                pass

    def end(self, channel_id: int):
        state = self.rounds.pop(channel_id, None)
        if state:
            state["task"].cancel()
        return state

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        state = self.rounds.get(message.channel.id)
        if not state:
            return
        if message.author.id == state["describer"]:
            bad = violates(message.content, state["word"], state["taboo"])
            if bad:
                self.end(message.channel.id)
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass
                await message.channel.send(f"**TABOO!** {message.author.mention} used a forbidden word. The word was "
                                           f"**{state['word']}**.", allowed_mentions=discord.AllowedMentions(users=[message.author]))
            return
        if is_guess(message.content, state["word"]):
            self.end(message.channel.id)
            await self.add_points(state["guild"], message.author.id, 2)
            await self.add_points(state["guild"], state["describer"], 1)
            await message.channel.send(
                f"{message.author.mention} guessed it! The word was **{state['word']}**. "
                f"+2 for the guesser and +1 for <@{state['describer']}>.",
                allowed_mentions=discord.AllowedMentions(users=[message.author]))

    @app_commands.command(name="stop", description="Cancel the round in this channel")
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction):
        state = self.rounds.get(interaction.channel_id)
        if not state:
            return await interaction.response.send_message("No round is running here.", ephemeral=True)
        if interaction.user.id != state["describer"] and not interaction.user.guild_permissions.manage_messages:
            return await interaction.response.send_message("Only the describer or a moderator can stop it.", ephemeral=True)
        self.end(interaction.channel_id)
        await interaction.response.send_message(f"Round cancelled. The word was **{state['word']}**.")

    @app_commands.command(name="top", description="Best taboo players")
    @app_commands.guild_only()
    async def top(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
                "SELECT user_id, points FROM taboo_scores WHERE guild_id=? ORDER BY points DESC LIMIT 10",
                (interaction.guild_id,)) as cur:
            rows = await cur.fetchall()
        if not rows:
            return await interaction.response.send_message("No points yet.", ephemeral=True)
        text = "\n".join(f"**{i}.** <@{u}> {p} points" for i, (u, p) in enumerate(rows, 1))
        await interaction.response.send_message(embed=discord.Embed(title="Taboo leaders", description=text,
                                                                    color=discord.Color.red()),
                                                allowed_mentions=discord.AllowedMentions.none())


# =====================================================================
# /superlative : the server votes for "most likely to..." awards
# =====================================================================

class VoteView(discord.ui.View):
    def __init__(self, cog, disabled: bool = False):
        super().__init__(timeout=None)
        self.cog = cog
        self.pick.disabled = disabled

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Vote for a member", min_values=1, max_values=1,
                       custom_id="super:vote")
    async def pick(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        await self.cog.vote(interaction, select.values[0])


class Superlative(commands.GroupCog, group_name="superlative", group_description="Vote for server awards"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        db = self.bot.db
        await db.execute("CREATE TABLE IF NOT EXISTS super_polls (message_id INTEGER PRIMARY KEY, guild_id INTEGER, "
                         "channel_id INTEGER, creator_id INTEGER, category TEXT, closed INTEGER DEFAULT 0)")
        await db.execute("CREATE TABLE IF NOT EXISTS super_votes (message_id INTEGER, voter_id INTEGER, nominee_id INTEGER, "
                         "PRIMARY KEY (message_id, voter_id))")
        await db.commit()
        self.bot.add_view(VoteView(self))

    @app_commands.command(name="start", description="Start a vote, for example: Funniest member")
    @app_commands.describe(category="The award, for example Funniest member or Most likely to be late")
    @app_commands.guild_only()
    async def start(self, interaction: discord.Interaction, category: app_commands.Range[str, 3, 100]):
        embed = discord.Embed(title=f"Award: {category}", color=discord.Color.gold(),
                              description="Who deserves it? Pick a member from the menu. One vote each, you can change it, "
                                          "and you can't vote for yourself. Results stay secret until the vote ends.")
        await interaction.response.send_message(embed=embed, view=VoteView(self))
        msg = await interaction.original_response()
        await self.bot.db.execute("INSERT INTO super_polls (message_id, guild_id, channel_id, creator_id, category) VALUES (?,?,?,?,?)",
                                  (msg.id, interaction.guild_id, msg.channel.id, interaction.user.id, category))
        await self.bot.db.commit()

    async def vote(self, interaction: discord.Interaction, nominee):
        async with self.bot.db.execute("SELECT closed FROM super_polls WHERE message_id=?", (interaction.message.id,)) as cur:
            row = await cur.fetchone()
        if not row or row[0]:
            return await interaction.response.send_message("This vote is closed.", ephemeral=True)
        if nominee.bot:
            return await interaction.response.send_message("Bots can't win awards. Pick a person.", ephemeral=True)
        if nominee.id == interaction.user.id:
            return await interaction.response.send_message("You can't vote for yourself.", ephemeral=True)
        await self.bot.db.execute(
            "INSERT INTO super_votes (message_id, voter_id, nominee_id) VALUES (?,?,?) "
            "ON CONFLICT(message_id, voter_id) DO UPDATE SET nominee_id=excluded.nominee_id",
            (interaction.message.id, interaction.user.id, nominee.id))
        await self.bot.db.commit()
        await interaction.response.send_message(f"You voted for **{nominee.display_name}**.", ephemeral=True)

    @app_commands.command(name="end", description="Close the vote and announce the winner")
    @app_commands.describe(message_id="The vote message ID (right click, Copy Message ID)")
    @app_commands.guild_only()
    async def end(self, interaction: discord.Interaction, message_id: str):
        if not message_id.isdigit():
            return await interaction.response.send_message("That isn't a message ID.", ephemeral=True)
        mid = int(message_id)
        async with self.bot.db.execute(
                "SELECT creator_id, channel_id, category, closed FROM super_polls WHERE message_id=? AND guild_id=?",
                (mid, interaction.guild_id)) as cur:
            row = await cur.fetchone()
        if not row:
            return await interaction.response.send_message("I don't know a vote with that ID.", ephemeral=True)
        creator, channel_id, category, closed = row
        if interaction.user.id != creator and not interaction.user.guild_permissions.manage_messages:
            return await interaction.response.send_message("Only the person who started it, or a moderator, can end it.",
                                                           ephemeral=True)
        if closed:
            return await interaction.response.send_message("It's already closed.", ephemeral=True)
        await self.bot.db.execute("UPDATE super_polls SET closed=1 WHERE message_id=?", (mid,))
        await self.bot.db.commit()
        async with self.bot.db.execute(
                "SELECT nominee_id, COUNT(*) c FROM super_votes WHERE message_id=? GROUP BY nominee_id ORDER BY c DESC LIMIT 5",
                (mid,)) as cur:
            ranked = await cur.fetchall()
        channel = interaction.guild.get_channel(channel_id)
        if ranked:
            lines = [f"**{i}.** <@{u}> {c} vote{'s' if c != 1 else ''}" for i, (u, c) in enumerate(ranked, 1)]
            tie = len(ranked) > 1 and ranked[0][1] == ranked[1][1]
            headline = ("It's a tie at the top!" if tie else f"And the winner is <@{ranked[0][0]}>!")
        else:
            lines, headline = ["Nobody voted."], "No winner this time."
        embed = discord.Embed(title=f"Award: {category}", description=headline + "\n\n" + "\n".join(lines), color=discord.Color.gold())
        embed.set_footer(text="Final results")
        try:
            await channel.get_partial_message(mid).edit(embed=embed, view=VoteView(self, disabled=True))
            await channel.send(f"The vote for **{category}** is over! {headline}",
                               allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=ranked[0][0])] if ranked else []))
        except (discord.HTTPException, AttributeError):
            pass
        await interaction.response.send_message("Done.", ephemeral=True)


# =====================================================================
# /decide : let the bot choose, roll, flip or predict
# =====================================================================

EIGHT_BALL = ["It is certain.", "Without a doubt.", "Yes, definitely.", "You may rely on it.", "Most likely.", "Signs point to yes.",
              "Reply hazy, try again.", "Ask again later.", "Better not tell you now.", "Cannot predict right now.",
              "Don't count on it.", "My reply is no.", "My sources say no.", "Very doubtful.", "Outlook not so good."]
DICE_RE = re.compile(r"^\s*(\d{1,2})?\s*d\s*(\d{1,4})\s*([+-]\s*\d{1,4})?\s*$", re.I)


def parse_dice(text: str) -> tuple[int, int, int] | None:
    m = DICE_RE.match(text)
    if not m:
        return None
    count, sides = int(m.group(1) or 1), int(m.group(2))
    mod = int(m.group(3).replace(" ", "")) if m.group(3) else 0
    return (count, sides, mod) if 1 <= count <= 20 and 2 <= sides <= 1000 else None


def split_options(text: str) -> list[str]:
    parts = re.split(r",|;|\s+or\s+|\|", text, flags=re.I)
    seen, out = set(), []
    for p in parts:
        p = p.strip()
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p[:80])
    return out


class Decide(commands.GroupCog, group_name="decide", group_description="Let luck choose for you"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="pick", description="Can't choose? List the options and I'll pick one")
    @app_commands.describe(options="Separate with commas or the word or, for example: pizza, burgers or sushi")
    @app_commands.guild_only()
    async def pick(self, interaction: discord.Interaction, options: app_commands.Range[str, 3, 400]):
        choices = split_options(options)
        if len(choices) < 2 or len(choices) > 20:
            return await interaction.response.send_message("Give me between 2 and 20 different options.", ephemeral=True)
        await interaction.response.send_message("Hmm, let me think...", allowed_mentions=discord.AllowedMentions.none())
        await asyncio.sleep(1.5)
        await interaction.edit_original_response(
            content=f"Options: {', '.join(choices)}\n\nI choose... **{random.choice(choices)}**!")

    @app_commands.command(name="8ball", description="Ask the magic 8 ball a yes or no question")
    @app_commands.guild_only()
    async def eightball(self, interaction: discord.Interaction, question: app_commands.Range[str, 3, 200]):
        await interaction.response.send_message(f"**{question}**\nThe 8 ball is shaking...",
                                                allowed_mentions=discord.AllowedMentions.none())
        await asyncio.sleep(1.5)
        await interaction.edit_original_response(content=f"**{question}**\n{random.choice(EIGHT_BALL)}")

    @app_commands.command(name="roll", description="Roll dice, for example 2d6 or d20+3")
    @app_commands.guild_only()
    async def roll(self, interaction: discord.Interaction, dice: str = "d6"):
        parsed = parse_dice(dice)
        if not parsed:
            return await interaction.response.send_message("Try something like `d20`, `2d6` or `3d8+2` (up to 20 dice).",
                                                           ephemeral=True)
        count, sides, mod = parsed
        rolls = [random.randint(1, sides) for _ in range(count)]
        total = sum(rolls) + mod
        shown = " + ".join(map(str, rolls)) + (f" {'+' if mod > 0 else '-'} {abs(mod)}" if mod else "")
        await interaction.response.send_message(f"{interaction.user.mention} rolled **{dice.strip()}**: {shown} = **{total}**",
                                                allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="flip", description="Flip a coin")
    @app_commands.guild_only()
    async def flip(self, interaction: discord.Interaction):
        await interaction.response.send_message("Flipping the coin...")
        await asyncio.sleep(1)
        await interaction.edit_original_response(content=f"It's **{random.choice(['heads', 'tails'])}**!")


# =====================================================================
# /rate : the bot rates anything
# =====================================================================

RATE_CATS = ["vibes", "charm", "chaos", "mystery", "energy", "style", "legend status", "snack potential", "drama",
             "comfort", "main character energy", "plot twist"]
RATE_REVIEWS = {
    5: ["Solid. Not a lot of people appreciate it, but I do.", "Underrated in its own quiet way."],
    6: ["Pretty good! Would give it a second look.", "Has potential and a very nice aura."],
    7: ["Honestly impressive. I'd recommend it to a friend.", "Reliable, charming and never boring."],
    8: ["Wow. This has real star quality.", "Hard to dislike. Easy to remember."],
    9: ["Top tier. The council of ratings approves.", "A rare gem. I'm genuinely excited."],
    10: ["Perfect. I have no notes. Frame it.", "Legendary. Future generations will talk about this."],
}


def rate_thing(thing: str) -> dict:
    rng = random.Random(" ".join(thing.lower().split()))
    overall = rng.choices([5, 6, 7, 8, 9, 10], weights=[8, 16, 28, 26, 14, 8])[0]
    cats = rng.sample(RATE_CATS, 3)
    return {"overall": overall, "review": rng.choice(RATE_REVIEWS[overall]),
            "subs": [(c, rng.randint(max(3, overall - 3), 10)) for c in cats]}


class Rate(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="rate", description="I rate anything out of 10 (always the same rating for the same thing)")
    @app_commands.describe(thing="Anything: a food, a game, a song, an idea...")
    @app_commands.guild_only()
    async def rate(self, interaction: discord.Interaction, thing: app_commands.Range[str, 2, 100]):
        r = rate_thing(thing)
        embed = discord.Embed(title=f"Rating: {thing}", description=f"**{r['overall']}/10**\n{r['review']}",
                              color=discord.Color.gold())
        for cat, score in r["subs"]:
            embed.add_field(name=cat.title(), value=f"{score}/10")
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot):
    for cog in (Drops, Taboo, Superlative, Decide, Rate):
        await bot.add_cog(cog(bot))
