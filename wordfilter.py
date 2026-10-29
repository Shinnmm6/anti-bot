import re
import unicodedata
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

MAX_WORDS = 200
MAX_WORD_LENGTH = 50
TIMEOUT_MINUTES = 10
ACTIONS = {"delete": "Delete the message", "warn": "Delete and add a warning",
           "timeout": f"Delete and time out for {TIMEOUT_MINUTES} min"}
ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿"))


def normalize(text: str) -> str:
    """Lowercase, fold fancy letters (ｆｕｌｌｗｉｄｔｈ, accents) and drop invisible characters."""
    text = unicodedata.normalize("NFKC", text).translate(ZERO_WIDTH)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.casefold()


def pattern_for(word: str) -> str:
    """A whole-word pattern. `*` is a wildcard, so `bad*` also catches `badword`."""
    body = re.escape(normalize(word)).replace(r"\*", r"\w*")
    return rf"(?<!\w){body}(?!\w)"


def compile_words(words: set[str]):
    if not words:
        return None
    return re.compile("|".join(pattern_for(w) for w in sorted(words)))


def split_words(text: str) -> list[str]:
    """'bad, worse  ,worst' -> ['bad', 'worse', 'worst']. Words can contain spaces (phrases)."""
    seen, out = set(), []
    for part in text.split(","):
        word = " ".join(normalize(part).split())
        if word and word not in seen:
            seen.add(word)
            out.append(word)
    return out


class WordFilter(commands.GroupCog, group_name="filter",
                 group_description="Block words you don't want in your server"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.words: dict[int, set[str]] = {}
        self.regex: dict[int, re.Pattern | None] = {}
        self.action: dict[int, str] = {}

    async def cog_load(self):
        db = self.bot.db
        await db.execute("CREATE TABLE IF NOT EXISTS filter_words (guild_id INTEGER, word TEXT, "
                         "PRIMARY KEY (guild_id, word))")
        await db.execute("CREATE TABLE IF NOT EXISTS filter_config (guild_id INTEGER PRIMARY KEY, action TEXT)")
        await db.commit()
        async with db.execute("SELECT guild_id, word FROM filter_words") as cur:
            for guild_id, word in await cur.fetchall():
                self.words.setdefault(guild_id, set()).add(word)
        for guild_id, words in self.words.items():
            self.regex[guild_id] = compile_words(words)
        async with db.execute("SELECT guild_id, action FROM filter_config") as cur:
            for guild_id, action in await cur.fetchall():
                self.action[guild_id] = action

    def refresh(self, guild_id: int):
        self.regex[guild_id] = compile_words(self.words.get(guild_id, set()))

    # ---------- /filter commands (replies are private so the word list isn't shown to everyone) ----------

    @app_commands.command(name="add", description="Add words to the filter (separate several with commas)")
    @app_commands.describe(words="Words or phrases, e.g. badword, another one, bad* (* matches any ending)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def add(self, interaction: discord.Interaction, words: str):
        # a word made only of * would match every message, so it needs at least one real character
        new = [w for w in split_words(words) if len(w) <= MAX_WORD_LENGTH and w.replace("*", "").strip()]
        if not new:
            return await interaction.response.send_message(
                f"Give me at least one word (up to {MAX_WORD_LENGTH} characters each).", ephemeral=True)
        current = self.words.setdefault(interaction.guild_id, set())
        fresh = [w for w in new if w not in current]
        if len(current) + len(fresh) > MAX_WORDS:
            return await interaction.response.send_message(
                f"The filter can hold {MAX_WORDS} words. Remove some first.", ephemeral=True)
        for w in fresh:
            await self.bot.db.execute("INSERT OR IGNORE INTO filter_words (guild_id, word) VALUES (?, ?)",
                                      (interaction.guild_id, w))
        await self.bot.db.commit()
        current.update(fresh)
        self.refresh(interaction.guild_id)
        await interaction.response.send_message(
            f"Added **{len(fresh)}** word{'s' if len(fresh) != 1 else ''}"
            + (f" ({len(new) - len(fresh)} already there)." if len(new) != len(fresh) else ".")
            + f" The filter now has {len(current)}.", ephemeral=True)
        await self.bot.log(interaction.guild, "Filter updated",
                           f"{len(fresh)} word(s) added by {interaction.user}")

    @app_commands.command(name="remove", description="Remove words from the filter (separate several with commas)")
    @app_commands.describe(words="The words to remove")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def remove(self, interaction: discord.Interaction, words: str):
        current = self.words.setdefault(interaction.guild_id, set())
        gone = [w for w in split_words(words) if w in current]
        if not gone:
            return await interaction.response.send_message("None of those words are in the filter.",
                                                           ephemeral=True)
        for w in gone:
            await self.bot.db.execute("DELETE FROM filter_words WHERE guild_id=? AND word=?",
                                      (interaction.guild_id, w))
        await self.bot.db.commit()
        current.difference_update(gone)
        self.refresh(interaction.guild_id)
        await interaction.response.send_message(
            f"Removed **{len(gone)}** word{'s' if len(gone) != 1 else ''}.", ephemeral=True)
        await self.bot.log(interaction.guild, "Filter updated",
                           f"{len(gone)} word(s) removed by {interaction.user}")

    @app_commands.command(name="list", description="Show the filtered words and the current action (only you see it)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def list_words(self, interaction: discord.Interaction):
        words = sorted(self.words.get(interaction.guild_id, set()))
        action = self.action.get(interaction.guild_id, "delete")
        if not words:
            return await interaction.response.send_message(
                "The filter is empty. Add words with `/filter add`.", ephemeral=True)
        text = ", ".join(words)
        if len(text) > 1700:
            text = text[:1700] + "…"
        await interaction.response.send_message(
            f"**Filtered words ({len(words)})**\n||{text}||\n**Action:** {ACTIONS[action]}", ephemeral=True)

    @app_commands.command(name="action", description="Choose what happens when someone uses a filtered word")
    @app_commands.choices(action=[app_commands.Choice(name=label, value=key) for key, label in ACTIONS.items()])
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_action(self, interaction: discord.Interaction, action: app_commands.Choice[str]):
        await self.bot.db.execute("INSERT OR REPLACE INTO filter_config (guild_id, action) VALUES (?, ?)",
                                  (interaction.guild_id, action.value))
        await self.bot.db.commit()
        self.action[interaction.guild_id] = action.value
        await interaction.response.send_message(f"Filtered messages: **{ACTIONS[action.value]}**.", ephemeral=True)

    @app_commands.command(name="clear", description="Remove every word from the filter")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def clear(self, interaction: discord.Interaction):
        await self.bot.db.execute("DELETE FROM filter_words WHERE guild_id=?", (interaction.guild_id,))
        await self.bot.db.commit()
        removed = len(self.words.pop(interaction.guild_id, set()))
        self.refresh(interaction.guild_id)
        await interaction.response.send_message(f"Removed all {removed} words.", ephemeral=True)
        await self.bot.log(interaction.guild, "Filter cleared", f"{removed} word(s) removed by {interaction.user}")

    # ---------- enforcement ----------

    async def check(self, message: discord.Message):
        if message.author.bot or not message.guild or not isinstance(message.author, discord.Member):
            return
        pattern = self.regex.get(message.guild.id)
        if pattern is None:
            return
        if message.author.guild_permissions.manage_messages:  # staff are exempt, same as auto-mod
            return
        if not pattern.search(normalize(message.content)):
            return

        try:
            await message.delete()
        except discord.HTTPException:
            return  # can't delete it (missing permission), so don't punish either
        action = self.action.get(message.guild.id, "delete")
        note = "that word isn't allowed here."
        if action == "warn":
            await self.bot.db.execute(
                "INSERT INTO warnings (guild_id, user_id, mod_id, reason, created_at) VALUES (?,?,?,?,?)",
                (message.guild.id, message.author.id, self.bot.user.id, "Used a filtered word",
                 datetime.now(timezone.utc).isoformat()))
            await self.bot.db.commit()
            note = "that word isn't allowed here. You've been warned."
        elif action == "timeout":
            try:
                await message.author.timeout(timedelta(minutes=TIMEOUT_MINUTES), reason="Word filter")
                note = f"that word isn't allowed here. You're timed out for {TIMEOUT_MINUTES} min."
            except discord.HTTPException:
                pass
        try:
            await message.channel.send(
                f"{message.author.mention}, {note}", delete_after=6,
                allowed_mentions=discord.AllowedMentions(users=[message.author]))
        except discord.HTTPException:
            pass
        snippet = message.content[:300].replace("```", "'''")
        await self.bot.log(message.guild, "Word filter",
                           f"{message.author} in {message.channel.mention} ({action})\n```{snippet}```")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        await self.check(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.content != after.content:  # catch people editing a bad word in afterwards
            await self.check(after)


async def setup(bot: commands.Bot):
    await bot.add_cog(WordFilter(bot))
