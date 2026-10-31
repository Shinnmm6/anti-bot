import difflib
import random
import re
import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

PINK = discord.Color.from_rgb(255, 105, 180)
LEVELS = [(0, "New spark"), (50, "Sweethearts"), (150, "Inseparable"), (400, "Soulmates"), (1000, "Legendary")]
DATE_COOLDOWN = 20 * 3600


def pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def love_level(points: int) -> str:
    return [name for need, name in LEVELS if points >= need][-1]


def days_together(since: int, now: int) -> int:
    return max(0, (now - since) // 86400)


def norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", "", text.lower()).split())


def same_answer(a: str, b: str) -> bool:
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if min(len(na), len(nb)) >= 3 and (na in nb or nb in na):
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= 0.8


async def get_couple(db, guild_id: int, user_id: int):
    async with db.execute(
            "SELECT user_a, user_b, since, love, last_date, last_note FROM couples "
            "WHERE guild_id=? AND (user_a=? OR user_b=?)", (guild_id, user_id, user_id)) as cur:
        return await cur.fetchone()


async def add_love(db, guild_id: int, a: int, b: int, points: int):
    x, y = pair(a, b)
    await db.execute("UPDATE couples SET love=love+? WHERE guild_id=? AND user_a=? AND user_b=?", (points, guild_id, x, y))
    await db.commit()


# =====================================================================
# /couple : proposals, status, breakup, leaderboard
# =====================================================================

class ProposalView(discord.ui.View):
    def __init__(self, cog, proposer: discord.Member, target: discord.Member):
        super().__init__(timeout=300)
        self.cog, self.proposer, self.target = cog, proposer, target
        self.message: discord.Message | None = None

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.target.id:
            return await interaction.response.send_message("Only the person who was asked can answer.", ephemeral=True)
        db, gid = self.cog.bot.db, interaction.guild_id
        if await get_couple(db, gid, self.proposer.id) or await get_couple(db, gid, self.target.id):
            self.stop()
            return await interaction.response.edit_message(content="One of you is already in a couple.", view=None)
        a, b = pair(self.proposer.id, self.target.id)
        await db.execute("INSERT INTO couples (guild_id, user_a, user_b, since) VALUES (?,?,?,?)", (gid, a, b, int(time.time())))
        await db.commit()
        self.stop()
        await interaction.response.edit_message(
            content=f"{self.proposer.mention} and {self.target.mention} are now a couple! See `/couple status`.", view=None,
            allowed_mentions=discord.AllowedMentions(users=[self.proposer, self.target]))

    @discord.ui.button(label="Not now", style=discord.ButtonStyle.secondary)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in (self.target.id, self.proposer.id):
            return await interaction.response.send_message("This isn't for you.", ephemeral=True)
        self.stop()
        await interaction.response.edit_message(content="No problem. Nobody has to answer yes.", view=None)

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(content="The question expired with no answer.", view=None)
            except discord.HTTPException:
                pass


class BreakupView(discord.ui.View):
    def __init__(self, cog, user_id: int):
        super().__init__(timeout=60)
        self.cog, self.user_id = cog, user_id

    @discord.ui.button(label="Yes, end it", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This isn't your choice.", ephemeral=True)
        c = await get_couple(self.cog.bot.db, interaction.guild_id, self.user_id)
        if c:
            await self.cog.bot.db.execute("DELETE FROM couples WHERE guild_id=? AND user_a=? AND user_b=?",
                                          (interaction.guild_id, c[0], c[1]))
            await self.cog.bot.db.commit()
        self.stop()
        await interaction.response.edit_message(content="It's over. Take care of yourself.", view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This isn't your choice.", ephemeral=True)
        self.stop()
        await interaction.response.edit_message(content="Nothing changed.", view=None)


class Couple(commands.GroupCog, group_name="couple", group_description="Couples, love points and sweet messages"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.db.execute(
            """CREATE TABLE IF NOT EXISTS couples (
                guild_id INTEGER, user_a INTEGER, user_b INTEGER, since INTEGER, love INTEGER DEFAULT 0,
                last_date INTEGER DEFAULT 0, last_note INTEGER DEFAULT 0, PRIMARY KEY (guild_id, user_a, user_b))""")
        await self.bot.db.commit()

    @app_commands.command(name="propose", description="Ask a member to be your partner (they can say no)")
    @app_commands.guild_only()
    async def propose(self, interaction: discord.Interaction, member: discord.Member):
        if member.bot or member.id == interaction.user.id:
            return await interaction.response.send_message("Pick another real member.", ephemeral=True)
        db = self.bot.db
        if await get_couple(db, interaction.guild_id, interaction.user.id):
            return await interaction.response.send_message("You're already in a couple.", ephemeral=True)
        if await get_couple(db, interaction.guild_id, member.id):
            return await interaction.response.send_message(f"{member.display_name} is already in a couple.", ephemeral=True)
        view = ProposalView(self, interaction.user, member)
        await interaction.response.send_message(
            f"{member.mention}, {interaction.user.mention} would like to be your partner. Do you accept?", view=view,
            allowed_mentions=discord.AllowedMentions(users=[member]))
        view.message = await interaction.original_response()

    @app_commands.command(name="status", description="See a couple's days together and love points")
    @app_commands.guild_only()
    async def status(self, interaction: discord.Interaction, member: discord.Member | None = None):
        target = member or interaction.user
        c = await get_couple(self.bot.db, interaction.guild_id, target.id)
        if not c:
            return await interaction.response.send_message(
                "You're not in a couple yet. Use `/couple propose`." if target.id == interaction.user.id
                else f"{target.display_name} isn't in a couple.", ephemeral=True)
        a, b, since, love, _, _ = c
        embed = discord.Embed(title="Couple", color=PINK, description=f"<@{a}> + <@{b}>")
        embed.add_field(name="Together", value=f"{days_together(since, int(time.time()))} days (since <t:{since}:D>)")
        embed.add_field(name="Love points", value=str(love))
        embed.add_field(name="Level", value=love_level(love))
        embed.set_footer(text="Earn love points with /datenight, /couplequiz, /mindmeld and /lovenote.")
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="breakup", description="End your couple (asks you to confirm)")
    @app_commands.guild_only()
    async def breakup(self, interaction: discord.Interaction):
        if not await get_couple(self.bot.db, interaction.guild_id, interaction.user.id):
            return await interaction.response.send_message("You're not in a couple.", ephemeral=True)
        await interaction.response.send_message("Are you sure you want to end it?", view=BreakupView(self, interaction.user.id),
                                                ephemeral=True)

    @app_commands.command(name="top", description="The couples with the most love points")
    @app_commands.guild_only()
    async def top(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
                "SELECT user_a, user_b, love, since FROM couples WHERE guild_id=? ORDER BY love DESC LIMIT 10",
                (interaction.guild_id,)) as cur:
            rows = await cur.fetchall()
        if not rows:
            return await interaction.response.send_message("No couples yet. Be the first with `/couple propose`.",
                                                           ephemeral=True)
        text = "\n".join(f"**{i}.** <@{a}> + <@{b}> {love} points ({love_level(love)})"
                         for i, (a, b, love, since) in enumerate(rows, 1))
        await interaction.response.send_message(embed=discord.Embed(title="Top couples", description=text, color=PINK),
                                                allowed_mentions=discord.AllowedMentions.none())


# =====================================================================
# /lovenote : a sweet message to your partner
# =====================================================================

class LoveNote(commands.GroupCog, group_name="lovenote", group_description="Send your partner a sweet message"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="send", description="Send a love note to your partner")
    @app_commands.describe(message="What you want to say", public="Post it in the channel (default) or send it by DM")
    @app_commands.guild_only()
    async def send(self, interaction: discord.Interaction, message: app_commands.Range[str, 2, 300], public: bool = True):
        c = await get_couple(self.bot.db, interaction.guild_id, interaction.user.id)
        if not c:
            return await interaction.response.send_message("Love notes are for couples. Start with `/couple propose`.",
                                                           ephemeral=True)
        partner_id = c[1] if c[0] == interaction.user.id else c[0]
        partner = interaction.guild.get_member(partner_id)
        if partner is None:
            return await interaction.response.send_message("I can't find your partner in this server.", ephemeral=True)
        embed = discord.Embed(description=message, color=PINK)
        embed.set_author(name=f"A note from {interaction.user.display_name} to {partner.display_name}")
        today = int(time.time()) // 86400
        bonus = ""
        if c[5] != today:
            await self.bot.db.execute("UPDATE couples SET last_note=?, love=love+1 WHERE guild_id=? AND user_a=? AND user_b=?",
                                      (today, interaction.guild_id, c[0], c[1]))
            await self.bot.db.commit()
            bonus = " +1 love point for today."
        if not public:
            try:
                await partner.send(embed=embed)
                return await interaction.response.send_message("Sent by DM." + bonus, ephemeral=True)
            except discord.HTTPException:
                pass  # DMs closed, post it in the channel instead
        await interaction.response.send_message(partner.mention, embed=embed,
                                                allowed_mentions=discord.AllowedMentions(users=[partner]))
        if bonus:
            await interaction.followup.send(bonus.strip(), ephemeral=True)


# =====================================================================
# /datenight : ideas, conversation starters, and love points
# =====================================================================

DATE_IDEAS = [
    "Cook the same recipe at the same time on a video call and compare.", "Watch a movie together and pause to guess what happens next.",
    "Build a playlist together with 5 songs each, then rate each other's picks.", "Play a co-op game and try to beat your best score.",
    "Take a long walk and photograph 3 things that remind you of each other.", "Bake something and give it a ridiculous name.",
    "Plan a dream trip together, with a budget and an itinerary.", "Do a puzzle or jigsaw together.",
    "Write each other a short letter and read them out loud.", "Have a picnic, indoors or outdoors.",
    "Learn 5 words in a new language together and use them all day.", "Try a new restaurant or a new street food.",
    "Draw each other in 2 minutes, no peeking, and laugh at the results.", "Have a game night with cards or board games.",
    "Watch the sunset or the sunrise together.", "Make a bucket list of 10 things to do together this year.",
    "Look at old photos and tell the story behind your favorites.", "Do a trivia night and let the loser pick the next date.",
    "Share your three happiest memories this year.", "Pick a random hobby and try it together for 30 minutes.",
    "Stargaze and make up your own constellations.", "Order the same dessert and rate it 1 to 10.",
    "Swap phones for 5 minutes and pick a new wallpaper for each other.", "Cook breakfast for dinner.",
    "Make a mini scrapbook of your favorite moments.", "Go to a bookshop or library and pick a book for each other.",
]
SPARKS = [
    "What is a small thing I do that makes your day better?", "What is your favorite memory of us so far?",
    "If we could live anywhere for a year, where would it be?", "What is something you want us to try together?",
    "What song reminds you of me?", "What is the best compliment you ever received?", "What is a dream you haven't told many people?",
    "What do you think we will be doing in five years?", "What was your first impression of me?",
    "What is your idea of a perfect lazy day?", "What is one thing you are proud of this month?",
    "What makes you feel most loved?", "If we had a theme song, what would it be?", "What is a tradition you want us to start?",
    "What is a fear you'd like me to know about?", "What is the funniest thing we have done together?",
    "What is something you admire about me?", "What is your comfort food, and why?", "What would our dream house look like?",
    "What is a skill you'd love to learn together?",
]


class DateDoneView(discord.ui.View):
    def __init__(self, cog, a: int, b: int, guild_id: int):
        super().__init__(timeout=6 * 3600)
        self.cog, self.a, self.b, self.guild_id = cog, a, b, guild_id
        self.pressed: set[int] = set()
        self.message: discord.Message | None = None

    @discord.ui.button(label="We did it!", style=discord.ButtonStyle.success)
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in (self.a, self.b):
            return await interaction.response.send_message("This date is for the couple.", ephemeral=True)
        self.pressed.add(interaction.user.id)
        if len(self.pressed) < 2:
            return await interaction.response.send_message("Noted. Your partner needs to press it too.", ephemeral=True)
        db = self.cog.bot.db
        c = await get_couple(db, self.guild_id, self.a)
        self.stop()
        button.disabled = True
        if not c:
            return await interaction.response.edit_message(content="You're not a couple anymore, so no points this time.",
                                                           view=self)
        if time.time() - c[4] < DATE_COOLDOWN:
            return await interaction.response.edit_message(
                content="Lovely! Date night points can be earned once every 20 hours, and you already did today.", view=self)
        await db.execute("UPDATE couples SET love=love+10, last_date=? WHERE guild_id=? AND user_a=? AND user_b=?",
                         (int(time.time()), self.guild_id, c[0], c[1]))
        await db.commit()
        await interaction.response.edit_message(content=f"<@{self.a}> and <@{self.b}> had their date night! **+10 love points.**",
                                                view=self, allowed_mentions=discord.AllowedMentions.none())

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


class DateNight(commands.GroupCog, group_name="datenight", group_description="Date ideas and conversation starters"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="idea", description="Get a date night idea (couples earn love points for doing it)")
    @app_commands.guild_only()
    async def idea(self, interaction: discord.Interaction):
        embed = discord.Embed(title="Date night idea", description=random.choice(DATE_IDEAS), color=PINK)
        c = await get_couple(self.bot.db, interaction.guild_id, interaction.user.id)
        if not c:
            embed.set_footer(text="Couples earn love points for doing it. Start with /couple propose.")
            return await interaction.response.send_message(embed=embed)
        embed.set_footer(text="When you've both done it, you both press the button. +10 love points, once per 20 hours.")
        view = DateDoneView(self, c[0], c[1], interaction.guild_id)
        await interaction.response.send_message(embed=embed, view=view)
        view.message = await interaction.original_response()

    @app_commands.command(name="spark", description="A question to start a good conversation")
    @app_commands.guild_only()
    async def spark(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=discord.Embed(title="Conversation spark", description=random.choice(SPARKS),
                                                                    color=PINK))


# =====================================================================
# /couplequiz : how well do you know each other?
# =====================================================================

TOPICS = ["favorite food", "favorite color", "dream travel place", "favorite movie or show", "go-to comfort snack",
          "favorite season", "favorite song or artist", "favorite animal", "favorite dessert", "dream job",
          "favorite game", "favorite drink", "favorite hobby", "favorite subject at school", "favorite holiday",
          "favorite fruit", "favorite place to relax", "favorite superhero or character", "first thing they do each morning",
          "favorite kind of weather"]


class QuizModal(discord.ui.Modal):
    def __init__(self, title: str, labels: list[str], on_done):
        super().__init__(title=title)
        self.on_done = on_done
        self.inputs = []
        for label in labels:
            item = discord.ui.TextInput(label=label[:45], max_length=60)
            self.inputs.append(item)
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        await self.on_done(interaction, [str(i) for i in self.inputs])


class GuessView(discord.ui.View):
    def __init__(self, cog, owner: discord.Member, guesser: discord.Member, topics: list[str], answers: list[str], guild_id: int):
        super().__init__(timeout=900)
        self.cog, self.owner, self.guesser = cog, owner, guesser
        self.topics, self.answers, self.guild_id = topics, answers, guild_id
        self.message: discord.Message | None = None

    @discord.ui.button(label="Guess their answers", style=discord.ButtonStyle.primary)
    async def guess(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.guesser.id:
            return await interaction.response.send_message(f"Only {self.guesser.display_name} can guess.", ephemeral=True)
        await interaction.response.send_modal(
            QuizModal(f"Guess {self.owner.display_name}'s answers", [f"Their {t}?" for t in self.topics], self.finish))

    async def finish(self, interaction: discord.Interaction, guesses: list[str]):
        self.stop()
        lines, score = [], 0
        for topic, real, guess in zip(self.topics, self.answers, guesses):
            ok = same_answer(real, guess)
            score += ok
            lines.append(f"**{'Right' if ok else 'Wrong'}** {topic}: they said *{real}*" + ("" if ok else f", you guessed *{guess}*"))
        verdict = ["Time to get to know each other better!", "A good start.", "Not bad at all.", "You know them well!",
                   "Impressive!", "Perfect! You really know them."][score]
        extra = ""
        c = await get_couple(self.cog.bot.db, self.guild_id, self.guesser.id)
        if c and {c[0], c[1]} == {self.guesser.id, self.owner.id} and score:
            await add_love(self.cog.bot.db, self.guild_id, self.guesser.id, self.owner.id, score * 2)
            extra = f"\n+{score * 2} love points for the couple."
        embed = discord.Embed(title=f"{self.guesser.display_name} knows {self.owner.display_name}: {score}/5",
                              description="\n".join(lines) + f"\n\n{verdict}{extra}", color=PINK)
        await interaction.response.edit_message(content=None, embed=embed, view=None,
                                                allowed_mentions=discord.AllowedMentions.none())

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(content="Nobody guessed in time.", view=None)
            except discord.HTTPException:
                pass


class CoupleQuiz(commands.GroupCog, group_name="couplequiz", group_description="How well do you know each other?"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="start", description="Answer 5 questions about yourself, then your partner guesses")
    @app_commands.describe(partner="Who will guess your answers")
    @app_commands.guild_only()
    async def start(self, interaction: discord.Interaction, partner: discord.Member):
        if partner.bot or partner.id == interaction.user.id:
            return await interaction.response.send_message("Pick another real member.", ephemeral=True)
        topics = random.sample(TOPICS, 5)

        async def done(modal_interaction: discord.Interaction, answers: list[str]):
            view = GuessView(self, interaction.user, partner, topics, answers, interaction.guild_id)
            await modal_interaction.response.send_message(
                f"{partner.mention}, {interaction.user.mention} answered 5 questions about themselves in secret. "
                "How well do you know them? Press the button to guess!", view=view,
                allowed_mentions=discord.AllowedMentions(users=[partner]))
            view.message = await modal_interaction.original_response()

        await interaction.response.send_modal(QuizModal("About you (keep it secret)", [f"Your {t}?" for t in topics], done))


# =====================================================================
# /mindmeld : try to say the same word at the same time
# =====================================================================

MAX_ROUNDS = 8


def meld_match(a: str, b: str) -> bool:
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    return min(len(na), len(nb)) >= 4 and difflib.SequenceMatcher(None, na, nb).ratio() >= 0.85


class WordModal(discord.ui.Modal, title="Your word"):
    word = discord.ui.TextInput(label="One word", max_length=30)

    def __init__(self, game):
        super().__init__()
        self.game = game

    async def on_submit(self, interaction: discord.Interaction):
        await self.game.submit(interaction, str(self.word))


class MeldView(discord.ui.View):
    def __init__(self, cog, a: discord.Member, b: discord.Member):
        super().__init__(timeout=600)
        self.cog, self.a, self.b = cog, a, b
        self.round = 1
        self.words: dict[int, str] = {}
        self.history: list[str] = []
        self.done = False
        self.message: discord.Message | None = None

    def text(self, note: str = "") -> str:
        past = "\n".join(self.history[-4:])
        who = " and ".join(f"{p.mention} {'(ready)' if p.id in self.words else ''}".strip() for p in (self.a, self.b))
        return (f"**Mind meld**, round {self.round}/{MAX_ROUNDS}: {who}\n"
                "Each of you secretly types one word. Try to type the same word!\n" + (f"{past}\n" if past else "") + note)

    @discord.ui.button(label="Enter my word", style=discord.ButtonStyle.primary)
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in (self.a.id, self.b.id):
            return await interaction.response.send_message("You're not in this game.", ephemeral=True)
        if self.done:
            return await interaction.response.send_message("The game is over.", ephemeral=True)
        if interaction.user.id in self.words:
            return await interaction.response.send_message("You already entered a word this round.", ephemeral=True)
        await interaction.response.send_modal(WordModal(self))

    async def submit(self, interaction: discord.Interaction, word: str):
        if self.done or interaction.user.id in self.words:
            return await interaction.response.send_message("Too late for that.", ephemeral=True)
        self.words[interaction.user.id] = word.strip()
        if len(self.words) < 2:
            return await interaction.response.edit_message(content=self.text(), view=self)
        wa, wb = self.words[self.a.id], self.words[self.b.id]
        self.history.append(f"Round {self.round}: {self.a.display_name} said **{wa}**, {self.b.display_name} said **{wb}**")
        self.words = {}
        if meld_match(wa, wb):
            self.done = True
            self.stop()
            note = f"\nMind meld! You both said **{wa}** in {self.round} round{'s' if self.round != 1 else ''}."
            note += await self.cog.record(interaction.guild_id, self.a.id, self.b.id, self.round)
            return await interaction.response.edit_message(content=self.text(note), view=None)
        if self.round >= MAX_ROUNDS:
            self.done = True
            self.stop()
            return await interaction.response.edit_message(content=self.text("\nOut of rounds. Better luck next time!"), view=None)
        self.round += 1
        await interaction.response.edit_message(content=self.text("Not the same. Think about what the other said and try again!"),
                                                view=self)

    async def on_timeout(self):
        if not self.done and self.message:
            try:
                await self.message.edit(content="The mind meld timed out.", view=None)
            except discord.HTTPException:
                pass


class MeldInvite(discord.ui.View):
    def __init__(self, cog, challenger: discord.Member, target: discord.Member):
        super().__init__(timeout=120)
        self.cog, self.challenger, self.target = cog, challenger, target
        self.message: discord.Message | None = None

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.target.id:
            return await interaction.response.send_message("Only the invited member can accept.", ephemeral=True)
        self.stop()
        game = MeldView(self.cog, self.challenger, self.target)
        await interaction.response.edit_message(content=game.text(), view=game, allowed_mentions=discord.AllowedMentions.none())
        game.message = await interaction.original_response()

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.secondary)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in (self.target.id, self.challenger.id):
            return await interaction.response.send_message("This isn't for you.", ephemeral=True)
        self.stop()
        await interaction.response.edit_message(content="Maybe next time.", view=None)

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(content="The invite expired.", view=None)
            except discord.HTTPException:
                pass


class MindMeld(commands.GroupCog, group_name="mindmeld", group_description="Say the same word at the same time"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.db.execute("CREATE TABLE IF NOT EXISTS meld_best (guild_id INTEGER, user_a INTEGER, user_b INTEGER, "
                                  "best INTEGER, plays INTEGER DEFAULT 0, PRIMARY KEY (guild_id, user_a, user_b))")
        await self.bot.db.commit()

    async def record(self, guild_id: int, a: int, b: int, rounds: int) -> str:
        x, y = pair(a, b)
        await self.bot.db.execute(
            "INSERT INTO meld_best (guild_id, user_a, user_b, best, plays) VALUES (?,?,?,?,1) "
            "ON CONFLICT(guild_id, user_a, user_b) DO UPDATE SET best=MIN(best, excluded.best), plays=plays+1",
            (guild_id, x, y, rounds))
        await self.bot.db.commit()
        c = await get_couple(self.bot.db, guild_id, a)
        if c and {c[0], c[1]} == {a, b}:
            await add_love(self.bot.db, guild_id, a, b, 5)
            return " +5 love points for the couple."
        return ""

    @app_commands.command(name="start", description="Challenge someone to think alike")
    @app_commands.guild_only()
    async def start(self, interaction: discord.Interaction, member: discord.Member):
        if member.bot or member.id == interaction.user.id:
            return await interaction.response.send_message("Pick another real member.", ephemeral=True)
        view = MeldInvite(self, interaction.user, member)
        await interaction.response.send_message(
            f"{member.mention}, {interaction.user.mention} wants to see if you think alike. Mind meld?", view=view,
            allowed_mentions=discord.AllowedMentions(users=[member]))
        view.message = await interaction.original_response()

    @app_commands.command(name="top", description="The pairs who think most alike")
    @app_commands.guild_only()
    async def top(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
                "SELECT user_a, user_b, best, plays FROM meld_best WHERE guild_id=? ORDER BY best ASC, plays DESC LIMIT 10",
                (interaction.guild_id,)) as cur:
            rows = await cur.fetchall()
        if not rows:
            return await interaction.response.send_message("No pair has matched yet.", ephemeral=True)
        text = "\n".join(f"**{i}.** <@{a}> + <@{b}> in {best} round{'s' if best != 1 else ''}" for i, (a, b, best, p) in enumerate(rows, 1))
        await interaction.response.send_message(embed=discord.Embed(title="Thinking alike", description=text, color=PINK),
                                                allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot):
    for cog in (Couple, LoveNote, DateNight, CoupleQuiz, MindMeld):
        await bot.add_cog(cog(bot))
