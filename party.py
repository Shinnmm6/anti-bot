import random
import re
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks


# =====================================================================
# /story : build a story together, one sentence at a time
# =====================================================================

def story_text(lines: list[str], limit: int = 3900) -> str:
    text = " ".join(lines)
    return text if len(text) <= limit else "..." + text[-limit:]


class Story(commands.GroupCog, group_name="story", group_description="Write a story together, one sentence each"):
    MAX_LINES = 100

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        db = self.bot.db
        await db.execute("CREATE TABLE IF NOT EXISTS stories (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, "
                         "channel_id INTEGER, title TEXT, starter_id INTEGER, last_user INTEGER, state TEXT DEFAULT 'open')")
        await db.execute("CREATE TABLE IF NOT EXISTS story_lines (id INTEGER PRIMARY KEY AUTOINCREMENT, story_id INTEGER, "
                         "user_id INTEGER, text TEXT)")
        await db.commit()

    async def active(self, guild_id: int):
        async with self.bot.db.execute(
                "SELECT id, channel_id, title, starter_id, last_user FROM stories WHERE guild_id=? AND state='open' "
                "ORDER BY id DESC LIMIT 1", (guild_id,)) as cur:
            return await cur.fetchone()

    async def lines(self, story_id: int):
        async with self.bot.db.execute("SELECT user_id, text FROM story_lines WHERE story_id=? ORDER BY id",
                                       (story_id,)) as cur:
            return await cur.fetchall()

    @app_commands.command(name="start", description="Start a new story for everyone to continue")
    @app_commands.describe(title="Name of the story", opening="The first sentence")
    @app_commands.guild_only()
    async def start(self, interaction: discord.Interaction, title: app_commands.Range[str, 1, 80],
                    opening: app_commands.Range[str, 5, 250]):
        if await self.active(interaction.guild_id):
            return await interaction.response.send_message(
                "A story is already running. Add to it with `/story add` or end it with `/story finish`.",
                ephemeral=True)
        cur = await self.bot.db.execute(
            "INSERT INTO stories (guild_id, channel_id, title, starter_id, last_user) VALUES (?,?,?,?,?)",
            (interaction.guild_id, interaction.channel_id, title, interaction.user.id, interaction.user.id))
        await self.bot.db.execute("INSERT INTO story_lines (story_id, user_id, text) VALUES (?,?,?)",
                                  (cur.lastrowid, interaction.user.id, opening))
        await self.bot.db.commit()
        embed = discord.Embed(title=f"New story: {title}", description=opening, color=discord.Color.purple())
        embed.set_footer(text="Continue it with /story add. One sentence at a time, and you can't go twice in a row.")
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="add", description="Add the next sentence to the story")
    @app_commands.guild_only()
    async def add(self, interaction: discord.Interaction, sentence: app_commands.Range[str, 5, 250]):
        story = await self.active(interaction.guild_id)
        if not story:
            return await interaction.response.send_message("No story is running. Start one with `/story start`.",
                                                           ephemeral=True)
        sid, channel_id, title, starter, last_user = story
        if last_user == interaction.user.id:
            return await interaction.response.send_message("You wrote the last sentence. Let someone else continue.",
                                                           ephemeral=True)
        await self.bot.db.execute("INSERT INTO story_lines (story_id, user_id, text) VALUES (?,?,?)",
                                  (sid, interaction.user.id, sentence))
        await self.bot.db.execute("UPDATE stories SET last_user=? WHERE id=?", (interaction.user.id, sid))
        await self.bot.db.commit()
        rows = await self.lines(sid)
        await interaction.response.send_message(
            f"**{interaction.user.display_name}** adds (sentence {len(rows)}): {sentence}",
            allowed_mentions=discord.AllowedMentions.none())
        if len(rows) >= self.MAX_LINES:
            await self.publish(interaction.channel, sid, title, rows, "The story reached its limit and is finished!")

    async def publish(self, channel, sid: int, title: str, rows, note: str):
        await self.bot.db.execute("UPDATE stories SET state='done' WHERE id=?", (sid,))
        await self.bot.db.commit()
        writers = sorted({u for u, _ in rows})
        embed = discord.Embed(title=f"{title}", description=story_text([t for _, t in rows]),
                              color=discord.Color.purple())
        embed.set_footer(text=f"{len(rows)} sentences by {len(writers)} writers")
        embed.add_field(name="Writers", value=" ".join(f"<@{u}>" for u in writers)[:1000], inline=False)
        try:
            await channel.send(note, embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

    @app_commands.command(name="read", description="Read the story so far")
    @app_commands.guild_only()
    async def read(self, interaction: discord.Interaction):
        story = await self.active(interaction.guild_id)
        if not story:
            return await interaction.response.send_message("No story is running.", ephemeral=True)
        rows = await self.lines(story[0])
        embed = discord.Embed(title=story[2], description=story_text([t for _, t in rows]), color=discord.Color.purple())
        embed.set_footer(text=f"{len(rows)} sentences so far")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="finish", description="End the story and post it (starter or a moderator)")
    @app_commands.guild_only()
    async def finish(self, interaction: discord.Interaction):
        story = await self.active(interaction.guild_id)
        if not story:
            return await interaction.response.send_message("No story is running.", ephemeral=True)
        sid, channel_id, title, starter, _ = story
        if interaction.user.id != starter and not interaction.user.guild_permissions.manage_messages:
            return await interaction.response.send_message("Only the person who started it, or a moderator, can finish.",
                                                           ephemeral=True)
        rows = await self.lines(sid)
        await interaction.response.send_message("Finishing the story...", ephemeral=True)
        await self.publish(interaction.channel, sid, title, rows, "The story is finished! Here it is:")


# =====================================================================
# /potato : hot potato with a secret fuse
# =====================================================================

class Potato(commands.GroupCog, group_name="potato", group_description="Hot potato with a secret timer"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        db = self.bot.db
        await db.execute("CREATE TABLE IF NOT EXISTS potato_games (guild_id INTEGER PRIMARY KEY, channel_id INTEGER, "
                         "holder_id INTEGER, prev_id INTEGER, explode_at INTEGER, passes INTEGER DEFAULT 0)")
        await db.execute("CREATE TABLE IF NOT EXISTS potato_burns (guild_id INTEGER, user_id INTEGER, "
                         "burns INTEGER DEFAULT 0, PRIMARY KEY (guild_id, user_id))")
        await db.commit()
        self.fuse.start()

    async def cog_unload(self):
        self.fuse.cancel()

    @app_commands.command(name="start", description="Start a hot potato. You hold it first, pass it fast!")
    @app_commands.guild_only()
    async def start(self, interaction: discord.Interaction):
        async with self.bot.db.execute("SELECT 1 FROM potato_games WHERE guild_id=?", (interaction.guild_id,)) as cur:
            if await cur.fetchone():
                return await interaction.response.send_message("A potato is already going around.", ephemeral=True)
        explode = int(time.time()) + random.randint(25, 120)
        await self.bot.db.execute(
            "INSERT INTO potato_games (guild_id, channel_id, holder_id, prev_id, explode_at) VALUES (?,?,?,?,?)",
            (interaction.guild_id, interaction.channel_id, interaction.user.id, 0, explode))
        await self.bot.db.commit()
        await interaction.response.send_message(
            f"A hot potato appears and {interaction.user.mention} is holding it! Pass it with `/potato pass`. "
            "Nobody knows when it explodes.", allowed_mentions=discord.AllowedMentions(users=[interaction.user]))

    @app_commands.command(name="pass", description="Pass the potato to someone else, quick!")
    @app_commands.guild_only()
    async def pass_cmd(self, interaction: discord.Interaction, member: discord.Member):
        async with self.bot.db.execute("SELECT holder_id, prev_id, passes FROM potato_games WHERE guild_id=?",
                                       (interaction.guild_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            return await interaction.response.send_message("There is no potato. Start one with `/potato start`.",
                                                           ephemeral=True)
        holder, prev, passes = row
        if interaction.user.id != holder:
            return await interaction.response.send_message("You don't have the potato.", ephemeral=True)
        if member.bot or member.id == holder:
            return await interaction.response.send_message("Pass it to another real person.", ephemeral=True)
        if member.id == prev:
            return await interaction.response.send_message("No passing it straight back!", ephemeral=True)
        await self.bot.db.execute("UPDATE potato_games SET holder_id=?, prev_id=?, passes=passes+1 WHERE guild_id=?",
                                  (member.id, holder, interaction.guild_id))
        await self.bot.db.commit()
        await interaction.response.send_message(
            f"{interaction.user.mention} tosses the potato to {member.mention}! Pass #{passes + 1}. Quick!",
            allowed_mentions=discord.AllowedMentions(users=[interaction.user, member]))

    @app_commands.command(name="status", description="Who is holding the potato?")
    @app_commands.guild_only()
    async def status(self, interaction: discord.Interaction):
        async with self.bot.db.execute("SELECT holder_id, passes FROM potato_games WHERE guild_id=?",
                                       (interaction.guild_id,)) as cur:
            row = await cur.fetchone()
        await interaction.response.send_message(
            f"<@{row[0]}> is holding the potato after {row[1]} passes." if row else "No potato right now.",
            allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="top", description="Who gets burned the most")
    @app_commands.guild_only()
    async def top(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
                "SELECT user_id, burns FROM potato_burns WHERE guild_id=? ORDER BY burns DESC LIMIT 10",
                (interaction.guild_id,)) as cur:
            rows = await cur.fetchall()
        if not rows:
            return await interaction.response.send_message("Nobody has been burned yet.", ephemeral=True)
        text = "\n".join(f"**{i}.** <@{u}> burned {b} time{'s' if b != 1 else ''}" for i, (u, b) in enumerate(rows, 1))
        await interaction.response.send_message(embed=discord.Embed(title="Hall of burns", description=text,
                                                                    color=discord.Color.orange()),
                                                allowed_mentions=discord.AllowedMentions.none())

    @tasks.loop(seconds=3)
    async def fuse(self):
        async with self.bot.db.execute(
                "SELECT guild_id, channel_id, holder_id, passes FROM potato_games WHERE explode_at<=?",
                (int(time.time()),)) as cur:
            rows = await cur.fetchall()
        for gid, channel_id, holder, passes in rows:
            await self.bot.db.execute("DELETE FROM potato_games WHERE guild_id=?", (gid,))
            await self.bot.db.execute(
                "INSERT INTO potato_burns (guild_id, user_id, burns) VALUES (?,?,1) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET burns=burns+1", (gid, holder))
            await self.bot.db.commit()
            channel = self.bot.get_channel(channel_id)
            if channel:
                try:
                    await channel.send(f"BOOM! The potato exploded in <@{holder}>'s hands after {passes} passes!",
                                       allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=holder)]))
                except discord.HTTPException:
                    pass

    @fuse.before_loop
    async def before_fuse(self):
        await self.bot.wait_until_ready()


# =====================================================================
# /riddle : staff post a riddle, first correct answer wins
# =====================================================================

def normalize(text: str) -> str:
    text = re.sub(r"[^a-z0-9 ]", "", text.lower())
    text = re.sub(r"^(a|an|the) ", "", text.strip())
    return " ".join(text.split())


def is_correct(guess: str, answers: str) -> bool:
    g = normalize(guess)
    return bool(g) and any(g == normalize(a) for a in answers.split("|"))


class AnswerModal(discord.ui.Modal, title="Your answer"):
    answer = discord.ui.TextInput(label="What is it?", max_length=100)

    def __init__(self, cog, message_id: int):
        super().__init__()
        self.cog, self.message_id = cog, message_id

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.check_answer(interaction, self.message_id, str(self.answer))


class RiddleView(discord.ui.View):
    def __init__(self, cog, disabled: bool = False):
        super().__init__(timeout=None)
        self.cog = cog
        self.answer.disabled = disabled

    @discord.ui.button(label="Answer", style=discord.ButtonStyle.primary, custom_id="riddle:answer")
    async def answer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AnswerModal(self.cog, interaction.message.id))


class RiddleForm(discord.ui.Modal, title="Post a riddle"):
    riddle = discord.ui.TextInput(label="The riddle", style=discord.TextStyle.paragraph, max_length=800)
    answer = discord.ui.TextInput(label="Answer (use | between accepted answers)", max_length=100)

    def __init__(self, cog, channel: discord.TextChannel):
        super().__init__()
        self.cog, self.channel = cog, channel

    async def on_submit(self, interaction: discord.Interaction):
        embed = discord.Embed(title="Riddle", description=str(self.riddle), color=discord.Color.gold())
        embed.set_footer(text="Press Answer. The first correct answer wins. Wrong answers are private.")
        try:
            msg = await self.channel.send(embed=embed, view=RiddleView(self.cog))
        except discord.HTTPException:
            return await interaction.response.send_message("I couldn't post in that channel.", ephemeral=True)
        await self.cog.bot.db.execute(
            "INSERT INTO riddles (message_id, guild_id, channel_id, answer, setter_id) VALUES (?,?,?,?,?)",
            (msg.id, interaction.guild_id, self.channel.id, str(self.answer), interaction.user.id))
        await self.cog.bot.db.commit()
        await interaction.response.send_message(f"Posted in {self.channel.mention}. The answer stays hidden.",
                                                ephemeral=True)


class Riddle(commands.GroupCog, group_name="riddle", group_description="Riddles with a hidden answer"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.last_try: dict[tuple[int, int], float] = {}

    async def cog_load(self):
        db = self.bot.db
        await db.execute("CREATE TABLE IF NOT EXISTS riddles (message_id INTEGER PRIMARY KEY, guild_id INTEGER, "
                         "channel_id INTEGER, answer TEXT, setter_id INTEGER, solved_by INTEGER)")
        await db.execute("CREATE TABLE IF NOT EXISTS riddle_scores (guild_id INTEGER, user_id INTEGER, "
                         "wins INTEGER DEFAULT 0, PRIMARY KEY (guild_id, user_id))")
        await db.commit()
        self.bot.add_view(RiddleView(self))

    @app_commands.command(name="post", description="Post a riddle (opens a private form for the answer)")
    @app_commands.describe(channel="Where to post it (default: here)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_messages=True)
    async def post(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        await interaction.response.send_modal(RiddleForm(self, channel or interaction.channel))

    async def check_answer(self, interaction: discord.Interaction, message_id: int, guess: str):
        key = (message_id, interaction.user.id)
        now = time.monotonic()
        if now - self.last_try.get(key, 0) < 5:
            return await interaction.response.send_message("Slow down, wait a few seconds between tries.", ephemeral=True)
        self.last_try[key] = now
        async with self.bot.db.execute("SELECT answer, solved_by FROM riddles WHERE message_id=?", (message_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            return await interaction.response.send_message("I can't find that riddle anymore.", ephemeral=True)
        answers, solved = row
        if solved:
            return await interaction.response.send_message("Someone already solved it.", ephemeral=True)
        if not is_correct(guess, answers):
            return await interaction.response.send_message("Not quite. Try again.", ephemeral=True)
        cur = await self.bot.db.execute("UPDATE riddles SET solved_by=? WHERE message_id=? AND solved_by IS NULL",
                                        (interaction.user.id, message_id))
        await self.bot.db.commit()
        if not cur.rowcount:
            return await interaction.response.send_message("Someone just beat you to it.", ephemeral=True)
        await self.bot.db.execute(
            "INSERT INTO riddle_scores (guild_id, user_id, wins) VALUES (?,?,1) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET wins=wins+1", (interaction.guild_id, interaction.user.id))
        await self.bot.db.commit()
        shown = answers.split("|")[0].strip()
        await interaction.response.send_message("Correct! You solved it.", ephemeral=True)
        try:
            msg = await interaction.channel.fetch_message(message_id)
            embed = msg.embeds[0] if msg.embeds else discord.Embed(title="Riddle")
            embed.color = discord.Color.green()
            embed.add_field(name="Solved", value=f"{interaction.user.mention} answered **{shown}**", inline=False)
            await msg.edit(embed=embed, view=RiddleView(self, disabled=True))
        except discord.HTTPException:
            pass
        try:
            await interaction.channel.send(f"{interaction.user.mention} solved the riddle! The answer was **{shown}**.",
                                           allowed_mentions=discord.AllowedMentions(users=[interaction.user]))
        except discord.HTTPException:
            pass

    @app_commands.command(name="reveal", description="Give up and show the answer (staff)")
    @app_commands.describe(message_id="The riddle message ID")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_messages=True)
    async def reveal(self, interaction: discord.Interaction, message_id: str):
        if not message_id.isdigit():
            return await interaction.response.send_message("That isn't a message ID.", ephemeral=True)
        async with self.bot.db.execute("SELECT answer, solved_by, channel_id FROM riddles WHERE message_id=? AND guild_id=?",
                                       (int(message_id), interaction.guild_id)) as cur:
            row = await cur.fetchone()
        if not row:
            return await interaction.response.send_message("I don't know a riddle with that ID.", ephemeral=True)
        answers, solved, channel_id = row
        if solved:
            return await interaction.response.send_message("It's already solved.", ephemeral=True)
        await self.bot.db.execute("UPDATE riddles SET solved_by=0 WHERE message_id=?", (int(message_id),))
        await self.bot.db.commit()
        shown = answers.split("|")[0].strip()
        channel = interaction.guild.get_channel(channel_id)
        try:
            msg = await channel.fetch_message(int(message_id))
            embed = msg.embeds[0]
            embed.color = discord.Color.dark_grey()
            embed.add_field(name="Nobody got it", value=f"The answer was **{shown}**", inline=False)
            await msg.edit(embed=embed, view=RiddleView(self, disabled=True))
        except (discord.HTTPException, AttributeError, IndexError):
            pass
        await interaction.response.send_message(f"Revealed: **{shown}**.", ephemeral=True)

    @app_commands.command(name="top", description="Best riddle solvers")
    @app_commands.guild_only()
    async def top(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
                "SELECT user_id, wins FROM riddle_scores WHERE guild_id=? ORDER BY wins DESC LIMIT 10",
                (interaction.guild_id,)) as cur:
            rows = await cur.fetchall()
        if not rows:
            return await interaction.response.send_message("No riddles solved yet.", ephemeral=True)
        text = "\n".join(f"**{i}.** <@{u}> {w} solved" for i, (u, w) in enumerate(rows, 1))
        await interaction.response.send_message(embed=discord.Embed(title="Riddle masters", description=text,
                                                                    color=discord.Color.gold()),
                                                allowed_mentions=discord.AllowedMentions.none())


# =====================================================================
# /numberhunt : find the secret number with hot and cold hints
# =====================================================================

def hint(guess: int, secret: int, top: int) -> str:
    if guess == secret:
        return "correct"
    gap = abs(guess - secret) / top
    heat = "burning hot" if gap <= 0.02 else "warm" if gap <= 0.08 else "cool" if gap <= 0.25 else "freezing"
    return f"{heat}, go {'higher' if guess < secret else 'lower'}"


class NumberHunt(commands.GroupCog, group_name="numberhunt", group_description="Guess the secret number"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.last: dict[tuple[int, int], float] = {}

    async def cog_load(self):
        db = self.bot.db
        await db.execute("CREATE TABLE IF NOT EXISTS hunt_games (guild_id INTEGER PRIMARY KEY, channel_id INTEGER, "
                         "secret INTEGER, top INTEGER, guesses INTEGER DEFAULT 0)")
        await db.execute("CREATE TABLE IF NOT EXISTS hunt_scores (guild_id INTEGER, user_id INTEGER, "
                         "wins INTEGER DEFAULT 0, PRIMARY KEY (guild_id, user_id))")
        await db.commit()

    @app_commands.command(name="start", description="Pick a secret number for the server to find")
    @app_commands.describe(highest="The secret is between 1 and this number (default 100)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_messages=True)
    async def start(self, interaction: discord.Interaction, highest: app_commands.Range[int, 10, 100000] = 100):
        async with self.bot.db.execute("SELECT 1 FROM hunt_games WHERE guild_id=?", (interaction.guild_id,)) as cur:
            if await cur.fetchone():
                return await interaction.response.send_message("A hunt is already running. Stop it first.",
                                                               ephemeral=True)
        await self.bot.db.execute("INSERT INTO hunt_games (guild_id, channel_id, secret, top) VALUES (?,?,?,?)",
                                  (interaction.guild_id, interaction.channel_id, random.randint(1, highest), highest))
        await self.bot.db.commit()
        await interaction.response.send_message(
            f"**Number hunt!** I'm thinking of a number from 1 to **{highest}**. Use `/numberhunt guess` and I'll "
            "tell you how hot or cold you are. First to find it wins!")

    @app_commands.command(name="guess", description="Guess the secret number")
    @app_commands.guild_only()
    async def guess(self, interaction: discord.Interaction, number: int):
        async with self.bot.db.execute("SELECT secret, top, guesses FROM hunt_games WHERE guild_id=?",
                                       (interaction.guild_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            return await interaction.response.send_message("No hunt is running right now.", ephemeral=True)
        secret, top, guesses = row
        if not 1 <= number <= top:
            return await interaction.response.send_message(f"Pick a number from 1 to {top}.", ephemeral=True)
        key = (interaction.guild_id, interaction.user.id)
        if time.monotonic() - self.last.get(key, 0) < 4:
            return await interaction.response.send_message("Easy, wait a few seconds between guesses.", ephemeral=True)
        self.last[key] = time.monotonic()
        result = hint(number, secret, top)
        if result != "correct":
            await self.bot.db.execute("UPDATE hunt_games SET guesses=guesses+1 WHERE guild_id=?", (interaction.guild_id,))
            await self.bot.db.commit()
            return await interaction.response.send_message(f"{interaction.user.display_name} guessed **{number}**: {result}.",
                                                           allowed_mentions=discord.AllowedMentions.none())
        cur = await self.bot.db.execute("DELETE FROM hunt_games WHERE guild_id=?", (interaction.guild_id,))
        if not cur.rowcount:
            return await interaction.response.send_message("Someone just found it first.", ephemeral=True)
        await self.bot.db.execute(
            "INSERT INTO hunt_scores (guild_id, user_id, wins) VALUES (?,?,1) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET wins=wins+1", (interaction.guild_id, interaction.user.id))
        await self.bot.db.commit()
        await interaction.response.send_message(
            f"{interaction.user.mention} found it! The number was **{secret}**, after {guesses + 1} guesses in total.",
            allowed_mentions=discord.AllowedMentions(users=[interaction.user]))

    @app_commands.command(name="stop", description="Stop the current hunt (staff)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_messages=True)
    async def stop(self, interaction: discord.Interaction):
        cur = await self.bot.db.execute("DELETE FROM hunt_games WHERE guild_id=?", (interaction.guild_id,))
        await self.bot.db.commit()
        await interaction.response.send_message("Hunt stopped." if cur.rowcount else "No hunt is running.",
                                                ephemeral=True)

    @app_commands.command(name="top", description="Best number hunters")
    @app_commands.guild_only()
    async def top(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
                "SELECT user_id, wins FROM hunt_scores WHERE guild_id=? ORDER BY wins DESC LIMIT 10",
                (interaction.guild_id,)) as cur:
            rows = await cur.fetchall()
        if not rows:
            return await interaction.response.send_message("No hunts won yet.", ephemeral=True)
        text = "\n".join(f"**{i}.** <@{u}> {w} win{'s' if w != 1 else ''}" for i, (u, w) in enumerate(rows, 1))
        await interaction.response.send_message(embed=discord.Embed(title="Number hunters", description=text,
                                                                    color=discord.Color.blurple()),
                                                allowed_mentions=discord.AllowedMentions.none())


# =====================================================================
# /truthdare : truth or dare with built in and custom questions
# =====================================================================

TRUTHS = [
    "What is the most embarrassing song on your playlist?", "What is a food you secretly love that others find weird?",
    "What is the longest you have gone without sleep?", "What is your most useless talent?",
    "What was the last thing you searched for online?", "What is the silliest thing you are afraid of?",
    "If you could swap lives with anyone here for a day, who and why?", "What is a habit you have that nobody knows about?",
    "What is the worst gift you ever received?", "What is a movie you cried at?",
    "What is your most irrational pet peeve?", "Which app do you spend the most time on, and how many hours?",
    "What is the funniest thing that ever happened to you at school or work?", "What is one thing you would change about yourself?",
    "What is your guilty pleasure show?", "What nickname did you hate as a kid?",
    "What is the most spontaneous thing you have done?", "Who here would you want next to you in a zombie apocalypse?",
    "What is a lie you told that got out of hand?", "What is the weirdest dream you remember?",
]
DARES = [
    "Change your nickname in this server to something silly for one hour.", "Send the last emoji you used five times in a row.",
    "Write a two line poem about the person who ran this command.", "Talk in rhymes for your next five messages.",
    "Describe your day like a movie trailer.", "Type your next message using only your elbows.",
    "Send a voice message saying a tongue twister three times.", "Compliment the last three people who spoke in this channel.",
    "Do ten jumping jacks and say 'done' when finished.", "Share the oldest photo on your phone that you are comfortable showing.",
    "Speak only in questions for the next three messages.", "Draw something with your eyes closed and describe it.",
    "Make up a one sentence ad for a toothbrush.", "Say the alphabet backwards as fast as you can and post your time.",
    "Give a dramatic reading of the last message you sent.", "Sing the first line of any song in the chat in text, and let people guess it.",
    "Change your profile picture to a potato for one hour.", "Tell the group a joke. It has to be a bad one.",
    "Reply to the next three messages as a pirate.", "Post a screenshot of your home screen.",
]


def pick_prompt(kind: str, custom: list[tuple[str, str]], rng=random) -> tuple[str, str]:
    if kind == "random":
        kind = rng.choice(["truth", "dare"])
    pool = (TRUTHS if kind == "truth" else DARES) + [t for k, t in custom if k == kind]
    return kind, rng.choice(pool)


class TruthDare(commands.GroupCog, group_name="truthdare", group_description="Truth or dare for the chat"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.db.execute("CREATE TABLE IF NOT EXISTS td_custom (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                                  "guild_id INTEGER, kind TEXT, text TEXT)")
        await self.bot.db.commit()

    @app_commands.command(name="ask", description="Get a truth or a dare")
    @app_commands.describe(kind="Truth, dare or surprise me", member="Who has to do it (default: you)")
    @app_commands.choices(kind=[app_commands.Choice(name="Truth", value="truth"), app_commands.Choice(name="Dare", value="dare"),
                                app_commands.Choice(name="Surprise me", value="random")])
    @app_commands.guild_only()
    async def ask(self, interaction: discord.Interaction, kind: app_commands.Choice[str] | None = None,
                  member: discord.Member | None = None):
        async with self.bot.db.execute("SELECT kind, text FROM td_custom WHERE guild_id=?", (interaction.guild_id,)) as cur:
            custom = await cur.fetchall()
        chosen, text = pick_prompt(kind.value if kind else "random", custom)
        target = member or interaction.user
        embed = discord.Embed(title=chosen.title(), description=text,
                              color=discord.Color.blue() if chosen == "truth" else discord.Color.red())
        await interaction.response.send_message(f"{target.mention}, your turn!", embed=embed,
                                                allowed_mentions=discord.AllowedMentions(users=[target]))

    @app_commands.command(name="add", description="Add your own truth or dare to the pool (staff)")
    @app_commands.choices(kind=[app_commands.Choice(name="Truth", value="truth"), app_commands.Choice(name="Dare", value="dare")])
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def add(self, interaction: discord.Interaction, kind: app_commands.Choice[str],
                  text: app_commands.Range[str, 5, 200]):
        async with self.bot.db.execute("SELECT COUNT(*) FROM td_custom WHERE guild_id=?", (interaction.guild_id,)) as cur:
            (n,) = await cur.fetchone()
        if n >= 100:
            return await interaction.response.send_message("You already have 100 custom ones. Remove some first.",
                                                           ephemeral=True)
        cur = await self.bot.db.execute("INSERT INTO td_custom (guild_id, kind, text) VALUES (?,?,?)",
                                        (interaction.guild_id, kind.value, text))
        await self.bot.db.commit()
        await interaction.response.send_message(f"Added as #{cur.lastrowid}.", ephemeral=True)

    @app_commands.command(name="list", description="Your custom truths and dares (staff)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def list_cmd(self, interaction: discord.Interaction):
        async with self.bot.db.execute("SELECT id, kind, text FROM td_custom WHERE guild_id=? ORDER BY id LIMIT 25",
                                       (interaction.guild_id,)) as cur:
            rows = await cur.fetchall()
        await interaction.response.send_message(
            "\n".join(f"`#{i}` {k}: {t[:90]}" for i, k, t in rows) or "No custom ones yet.", ephemeral=True)

    @app_commands.command(name="remove", description="Remove a custom truth or dare (staff)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def remove(self, interaction: discord.Interaction, number: int):
        cur = await self.bot.db.execute("DELETE FROM td_custom WHERE id=? AND guild_id=?", (number, interaction.guild_id))
        await self.bot.db.commit()
        await interaction.response.send_message("Removed." if cur.rowcount else "I couldn't find that number.",
                                                ephemeral=True)


async def setup(bot: commands.Bot):
    for cog in (Story, Potato, Riddle, NumberHunt, TruthDare):
        await bot.add_cog(cog(bot))
