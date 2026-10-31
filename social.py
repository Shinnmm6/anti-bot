import asyncio
import random
import re
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from giveaway import parse_duration


# =====================================================================
# /lfg : looking for group, with slots that fill up
# =====================================================================

def lfg_status(closed: int, players: int, slots: int) -> str:
    if closed:
        return "closed"
    return "full" if players >= slots else "open"


def lfg_embed(game: str, slots: int, host_id: int, players: list[int], note: str, status: str,
              expires_at: int) -> discord.Embed:
    color = {"open": discord.Color.green(), "full": discord.Color.gold(), "closed": discord.Color.dark_grey()}[status]
    embed = discord.Embed(title=f"Looking for group: {game}"[:256], description=note or None, color=color)
    embed.add_field(name=f"Players ({len(players)}/{slots})",
                    value="\n".join(f"<@{u}>" + (" (host)" if u == host_id else "") for u in players) or "nobody",
                    inline=False)
    state = {"open": f"Open, {slots - len(players)} spot(s) left", "full": "Full, time to play",
             "closed": "Closed"}[status]
    embed.add_field(name="Status", value=f"{state}\nEnds <t:{expires_at}:R>", inline=False)
    return embed


class LfgView(discord.ui.View):
    def __init__(self, cog, join_disabled: bool = False, all_disabled: bool = False):
        super().__init__(timeout=None)
        self.cog = cog
        self.join.disabled = join_disabled or all_disabled
        self.leave.disabled = self.close.disabled = all_disabled

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, custom_id="lfg:join")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.on_join(interaction)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary, custom_id="lfg:leave")
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.on_leave(interaction)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, custom_id="lfg:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.on_close(interaction)


class Lfg(commands.GroupCog, group_name="lfg", group_description="Find people to play or work with"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        db = self.bot.db
        await db.execute(
            """CREATE TABLE IF NOT EXISTS lfg_posts (
                message_id INTEGER PRIMARY KEY, guild_id INTEGER, channel_id INTEGER, host_id INTEGER,
                game TEXT, slots INTEGER, note TEXT, closed INTEGER DEFAULT 0, expires_at INTEGER)""")
        await db.execute("CREATE TABLE IF NOT EXISTS lfg_players (message_id INTEGER, user_id INTEGER, "
                         "joined_at INTEGER, PRIMARY KEY (message_id, user_id))")
        await db.commit()
        self.bot.add_view(LfgView(self))
        self.expire.start()

    async def cog_unload(self):
        self.expire.cancel()

    async def load(self, message_id: int):
        async with self.bot.db.execute(
                "SELECT guild_id, channel_id, host_id, game, slots, note, closed, expires_at FROM lfg_posts "
                "WHERE message_id=?", (message_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            return None, []
        async with self.bot.db.execute("SELECT user_id FROM lfg_players WHERE message_id=? ORDER BY joined_at, rowid",
                                       (message_id,)) as cur:
            players = [r[0] for r in await cur.fetchall()]
        return row, players

    @app_commands.command(name="post", description="Look for people to join you")
    @app_commands.describe(game="What you want to do or play", slots="Total players you need, including you",
                           note="Extra details (rank, time, voice channel...)",
                           hours="How long the post stays open (default 2)")
    @app_commands.guild_only()
    async def post(self, interaction: discord.Interaction, game: app_commands.Range[str, 1, 80],
                   slots: app_commands.Range[int, 2, 25], note: app_commands.Range[str, 0, 300] = "",
                   hours: app_commands.Range[int, 1, 72] = 2):
        expires = int(time.time()) + hours * 3600
        players = [interaction.user.id]
        await interaction.response.send_message(
            embed=lfg_embed(game, slots, interaction.user.id, players, note, "open", expires), view=LfgView(self),
            allowed_mentions=discord.AllowedMentions.none())
        msg = await interaction.original_response()
        db = self.bot.db
        await db.execute("INSERT INTO lfg_posts (message_id, guild_id, channel_id, host_id, game, slots, note, "
                         "expires_at) VALUES (?,?,?,?,?,?,?,?)",
                         (msg.id, interaction.guild_id, msg.channel.id, interaction.user.id, game, slots, note, expires))
        await db.execute("INSERT INTO lfg_players (message_id, user_id, joined_at) VALUES (?,?,?)",
                         (msg.id, interaction.user.id, int(time.time())))
        await db.commit()

    def render(self, mid, row, players):
        guild_id, channel_id, host, game, slots, note, closed, expires = row
        status = lfg_status(closed, len(players), slots)
        view = LfgView(self, join_disabled=status != "open", all_disabled=bool(closed))
        return lfg_embed(game, slots, host, players, note, status, expires), view, status

    async def on_join(self, interaction: discord.Interaction):
        row, players = await self.load(interaction.message.id)
        if not row:
            return await interaction.response.send_message("I can't find that post anymore.", ephemeral=True)
        if row[6]:
            return await interaction.response.send_message("This post is closed.", ephemeral=True)
        if interaction.user.id in players:
            return await interaction.response.send_message("You're already in.", ephemeral=True)
        if len(players) >= row[4]:
            return await interaction.response.send_message("It's full.", ephemeral=True)
        await self.bot.db.execute("INSERT INTO lfg_players (message_id, user_id, joined_at) VALUES (?,?,?)",
                                  (interaction.message.id, interaction.user.id, int(time.time())))
        await self.bot.db.commit()
        players.append(interaction.user.id)
        embed, view, status = self.render(interaction.message.id, row, players)
        await interaction.response.edit_message(embed=embed, view=view)
        if status == "full":
            try:
                await interaction.channel.send(
                    "**" + row[3] + "** is full! " + " ".join(f"<@{u}>" for u in players) + " get together and play.",
                    allowed_mentions=discord.AllowedMentions(users=True))
            except discord.HTTPException:
                pass

    async def on_leave(self, interaction: discord.Interaction):
        row, players = await self.load(interaction.message.id)
        if not row:
            return await interaction.response.send_message("I can't find that post anymore.", ephemeral=True)
        if interaction.user.id == row[2]:
            return await interaction.response.send_message("You're the host. Press Close to end the post.",
                                                           ephemeral=True)
        if interaction.user.id not in players:
            return await interaction.response.send_message("You aren't in this group.", ephemeral=True)
        await self.bot.db.execute("DELETE FROM lfg_players WHERE message_id=? AND user_id=?",
                                  (interaction.message.id, interaction.user.id))
        await self.bot.db.commit()
        players.remove(interaction.user.id)
        embed, view, _ = self.render(interaction.message.id, row, players)
        await interaction.response.edit_message(embed=embed, view=view)

    async def on_close(self, interaction: discord.Interaction):
        row, players = await self.load(interaction.message.id)
        if not row:
            return await interaction.response.send_message("I can't find that post anymore.", ephemeral=True)
        if interaction.user.id != row[2] and not interaction.user.guild_permissions.manage_messages:
            return await interaction.response.send_message("Only the host or a moderator can close it.",
                                                           ephemeral=True)
        await self.bot.db.execute("UPDATE lfg_posts SET closed=1 WHERE message_id=?", (interaction.message.id,))
        await self.bot.db.commit()
        row = row[:6] + (1,) + row[7:]
        embed, view, _ = self.render(interaction.message.id, row, players)
        await interaction.response.edit_message(embed=embed, view=view)

    @tasks.loop(minutes=1)
    async def expire(self):
        async with self.bot.db.execute("SELECT message_id, channel_id FROM lfg_posts WHERE closed=0 AND expires_at<?",
                                       (int(time.time()),)) as cur:
            rows = await cur.fetchall()
        for mid, channel_id in rows:
            await self.bot.db.execute("UPDATE lfg_posts SET closed=1 WHERE message_id=?", (mid,))
            await self.bot.db.commit()
            row, players = await self.load(mid)
            channel = self.bot.get_channel(channel_id)
            if row is None or channel is None:
                continue
            embed, view, _ = self.render(mid, row, players)
            try:
                await channel.get_partial_message(mid).edit(embed=embed, view=view)
            except discord.HTTPException:
                pass

    @expire.before_loop
    async def before_expire(self):
        await self.bot.wait_until_ready()


# =====================================================================
# /secretsanta : sign up with a button, then draw and DM everyone
# =====================================================================

def santa_cycle(ids: list[int], rng=random) -> dict[int, int]:
    """One big loop (A gives to B, B to C ... last to A), so nobody gets themselves."""
    order = list(ids)
    rng.shuffle(order)
    return {order[i]: order[(i + 1) % len(order)] for i in range(len(order))}


def santa_embed(title: str, budget: str, count: int, drawn: bool = False) -> discord.Embed:
    desc = f"Budget: **{budget}**\n\n" if budget else ""
    desc += ("Names have been drawn. Check your DMs, or use `/secretsanta mine`." if drawn
             else "Press **Join** to take part. When the host draws, everyone is secretly given someone to give a gift to.")
    embed = discord.Embed(title=title, description=desc, color=discord.Color.red())
    embed.set_footer(text=f"{count} joined")
    return embed


class SantaView(discord.ui.View):
    def __init__(self, cog, disabled: bool = False):
        super().__init__(timeout=None)
        self.cog = cog
        self.join.disabled = self.leave.disabled = disabled

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, custom_id="santa:join")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.toggle(interaction, True)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary, custom_id="santa:leave")
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.toggle(interaction, False)


class SecretSanta(commands.GroupCog, group_name="secretsanta", group_description="Run a secret santa gift exchange"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        db = self.bot.db
        await db.execute(
            """CREATE TABLE IF NOT EXISTS santa_events (
                message_id INTEGER PRIMARY KEY, guild_id INTEGER, channel_id INTEGER, host_id INTEGER,
                title TEXT, budget TEXT, state TEXT DEFAULT 'open')""")
        await db.execute("CREATE TABLE IF NOT EXISTS santa_members (message_id INTEGER, user_id INTEGER, "
                         "PRIMARY KEY (message_id, user_id))")
        await db.execute("CREATE TABLE IF NOT EXISTS santa_pairs (message_id INTEGER, giver INTEGER, receiver INTEGER, "
                         "PRIMARY KEY (message_id, giver))")
        await db.commit()
        self.bot.add_view(SantaView(self))

    async def count(self, mid: int) -> int:
        async with self.bot.db.execute("SELECT COUNT(*) FROM santa_members WHERE message_id=?", (mid,)) as cur:
            return (await cur.fetchone())[0]

    async def open_event(self, guild_id: int):
        async with self.bot.db.execute(
                "SELECT message_id, channel_id, host_id, title, budget FROM santa_events "
                "WHERE guild_id=? AND state='open' ORDER BY message_id DESC LIMIT 1", (guild_id,)) as cur:
            return await cur.fetchone()

    @app_commands.command(name="open", description="Start a secret santa and let members sign up")
    @app_commands.describe(title="Name of the event", budget="Suggested gift budget, for example 10 dollars")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def open_cmd(self, interaction: discord.Interaction, title: app_commands.Range[str, 1, 80] = "Secret Santa",
                       budget: app_commands.Range[str, 0, 60] = ""):
        if await self.open_event(interaction.guild_id):
            return await interaction.response.send_message(
                "There is already an open secret santa. Use `/secretsanta draw` or `/secretsanta cancel` first.",
                ephemeral=True)
        await interaction.response.send_message(embed=santa_embed(title, budget, 0), view=SantaView(self))
        msg = await interaction.original_response()
        await self.bot.db.execute(
            "INSERT INTO santa_events (message_id, guild_id, channel_id, host_id, title, budget) VALUES (?,?,?,?,?,?)",
            (msg.id, interaction.guild_id, msg.channel.id, interaction.user.id, title, budget))
        await self.bot.db.commit()

    async def toggle(self, interaction: discord.Interaction, joining: bool):
        mid = interaction.message.id
        async with self.bot.db.execute("SELECT title, budget, state FROM santa_events WHERE message_id=?",
                                       (mid,)) as cur:
            row = await cur.fetchone()
        if not row:
            return await interaction.response.send_message("This event doesn't exist anymore.", ephemeral=True)
        if row[2] != "open":
            return await interaction.response.send_message("Names were already drawn.", ephemeral=True)
        if joining:
            await self.bot.db.execute("INSERT OR IGNORE INTO santa_members (message_id, user_id) VALUES (?,?)",
                                      (mid, interaction.user.id))
        else:
            await self.bot.db.execute("DELETE FROM santa_members WHERE message_id=? AND user_id=?",
                                      (mid, interaction.user.id))
        await self.bot.db.commit()
        await interaction.response.edit_message(embed=santa_embed(row[0], row[1], await self.count(mid)))
        await interaction.followup.send("You're in!" if joining else "You left.", ephemeral=True)

    @app_commands.command(name="draw", description="Draw names and send everyone their giftee in a DM")
    @app_commands.guild_only()
    async def draw(self, interaction: discord.Interaction):
        ev = await self.open_event(interaction.guild_id)
        if not ev:
            return await interaction.response.send_message("There is no open secret santa.", ephemeral=True)
        mid, channel_id, host, title, budget = ev
        if interaction.user.id != host and not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("Only the host or staff can draw.", ephemeral=True)
        async with self.bot.db.execute("SELECT user_id FROM santa_members WHERE message_id=?", (mid,)) as cur:
            ids = [r[0] for r in await cur.fetchall() if interaction.guild.get_member(r[0])]
        if len(ids) < 3:
            return await interaction.response.send_message(f"I need at least 3 people. Only {len(ids)} joined.",
                                                           ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        pairs = santa_cycle(ids)
        for giver, receiver in pairs.items():
            await self.bot.db.execute("INSERT OR REPLACE INTO santa_pairs (message_id, giver, receiver) VALUES (?,?,?)",
                                      (mid, giver, receiver))
        await self.bot.db.execute("UPDATE santa_events SET state='drawn' WHERE message_id=?", (mid,))
        await self.bot.db.commit()

        failed = []
        for giver, receiver in pairs.items():
            member = interaction.guild.get_member(giver)
            text = (f"**{title}** in {interaction.guild.name}\nYou are the Secret Santa for "
                    f"**{interaction.guild.get_member(receiver).display_name}**." + (f"\nBudget: {budget}" if budget else "")
                    + "\nKeep it secret!")
            try:
                await member.send(text)
            except discord.HTTPException:
                failed.append(member.mention)
        channel = interaction.guild.get_channel(channel_id)
        try:
            await channel.get_partial_message(mid).edit(embed=santa_embed(title, budget, len(ids), drawn=True),
                                                        view=SantaView(self, disabled=True))
        except (discord.HTTPException, AttributeError):
            pass
        note = f"Drawn! {len(ids)} people were sent their giftee."
        if failed:
            note += (" I couldn't DM " + ", ".join(failed) + ". They can use `/secretsanta mine` to see theirs.")
        await interaction.followup.send(note, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="mine", description="See who you are the Secret Santa for (only you see it)")
    @app_commands.guild_only()
    async def mine(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
                "SELECT p.receiver, e.title, e.budget FROM santa_pairs p JOIN santa_events e ON e.message_id=p.message_id "
                "WHERE e.guild_id=? AND p.giver=? ORDER BY p.message_id DESC LIMIT 1",
                (interaction.guild_id, interaction.user.id)) as cur:
            row = await cur.fetchone()
        if not row:
            return await interaction.response.send_message("You don't have a giftee. Join one with the button.",
                                                           ephemeral=True)
        receiver, title, budget = row
        await interaction.response.send_message(
            f"**{title}**: you are the Secret Santa for <@{receiver}>." + (f" Budget: {budget}." if budget else ""),
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="cancel", description="Cancel the open secret santa")
    @app_commands.guild_only()
    async def cancel(self, interaction: discord.Interaction):
        ev = await self.open_event(interaction.guild_id)
        if not ev:
            return await interaction.response.send_message("There is no open secret santa.", ephemeral=True)
        mid, channel_id, host = ev[0], ev[1], ev[2]
        if interaction.user.id != host and not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("Only the host or staff can cancel.", ephemeral=True)
        await self.bot.db.execute("DELETE FROM santa_events WHERE message_id=?", (mid,))
        await self.bot.db.execute("DELETE FROM santa_members WHERE message_id=?", (mid,))
        await self.bot.db.commit()
        channel = interaction.guild.get_channel(channel_id)
        try:
            await channel.get_partial_message(mid).edit(content="This secret santa was cancelled.", embed=None,
                                                        view=SantaView(self, disabled=True))
        except (discord.HTTPException, AttributeError):
            pass
        await interaction.response.send_message("Cancelled.", ephemeral=True)


# =====================================================================
# /habit : daily habit streaks
# =====================================================================

def next_streak(last: date | None, today: date, streak: int) -> tuple[int, str]:
    if last == today:
        return streak, "already"
    if last is not None and last == today - timedelta(days=1):
        return streak + 1, "continued"
    return 1, ("restarted" if last else "first")


def live_streak(last: date | None, today: date, streak: int) -> int:
    return streak if last is not None and (today - last).days <= 1 else 0


class Habit(commands.GroupCog, group_name="habit", group_description="Build daily streaks"):
    MAX_HABITS = 5

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.db.execute(
            """CREATE TABLE IF NOT EXISTS habits (
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, user_id INTEGER, name TEXT,
                streak INTEGER DEFAULT 0, best INTEGER DEFAULT 0, total INTEGER DEFAULT 0, last_day TEXT,
                utc_offset INTEGER DEFAULT 0, private INTEGER DEFAULT 0)""")
        await self.bot.db.commit()

    @staticmethod
    def local_today(offset: int) -> date:
        return (datetime.now(timezone.utc) + timedelta(hours=offset)).date()

    async def my_habit(self, interaction: discord.Interaction, name: str):
        async with self.bot.db.execute(
                "SELECT id, name, streak, best, total, last_day, utc_offset FROM habits "
                "WHERE guild_id=? AND user_id=? AND lower(name)=lower(?)",
                (interaction.guild_id, interaction.user.id, name)) as cur:
            return await cur.fetchone()

    async def names(self, interaction: discord.Interaction, current: str):
        async with self.bot.db.execute("SELECT name FROM habits WHERE guild_id=? AND user_id=?",
                                       (interaction.guild_id, interaction.user.id)) as cur:
            rows = await cur.fetchall()
        return [app_commands.Choice(name=r[0], value=r[0]) for r in rows if current.lower() in r[0].lower()][:25]

    @app_commands.command(name="add", description="Start tracking a daily habit")
    @app_commands.describe(name="For example: Read, Workout, Study", utc_offset="Your time zone in hours from UTC "
                           "(8 for the Philippines), so your day ends at your midnight",
                           private="Hide it from the public leaderboard")
    @app_commands.guild_only()
    async def add(self, interaction: discord.Interaction, name: app_commands.Range[str, 1, 40],
                  utc_offset: app_commands.Range[int, -12, 14] = 0, private: bool = False):
        async with self.bot.db.execute("SELECT COUNT(*) FROM habits WHERE guild_id=? AND user_id=?",
                                       (interaction.guild_id, interaction.user.id)) as cur:
            (n,) = await cur.fetchone()
        if await self.my_habit(interaction, name):
            return await interaction.response.send_message("You already track that habit.", ephemeral=True)
        if n >= self.MAX_HABITS:
            return await interaction.response.send_message(f"You can track up to {self.MAX_HABITS} habits.",
                                                           ephemeral=True)
        await self.bot.db.execute("INSERT INTO habits (guild_id, user_id, name, utc_offset, private) VALUES (?,?,?,?,?)",
                                  (interaction.guild_id, interaction.user.id, name, utc_offset, int(private)))
        await self.bot.db.commit()
        await interaction.response.send_message(f"Tracking **{name}**. Check in every day with `/habit done`.",
                                                ephemeral=True)

    @app_commands.command(name="done", description="Check in for today")
    @app_commands.guild_only()
    async def done(self, interaction: discord.Interaction, name: str):
        row = await self.my_habit(interaction, name)
        if not row:
            return await interaction.response.send_message("You don't track that habit. Use `/habit add`.",
                                                           ephemeral=True)
        hid, hname, streak, best, total, last_day, offset = row
        today = self.local_today(offset)
        last = date.fromisoformat(last_day) if last_day else None
        new, status = next_streak(last, today, streak)
        if status == "already":
            return await interaction.response.send_message(f"You already did **{hname}** today. Streak: **{streak}**.",
                                                           ephemeral=True)
        best = max(best, new)
        await self.bot.db.execute("UPDATE habits SET streak=?, best=?, total=total+1, last_day=? WHERE id=?",
                                  (new, best, today.isoformat(), hid))
        await self.bot.db.commit()
        text = f"**{hname}** done. Streak: **{new}** day{'s' if new != 1 else ''} (best {best})."
        if status == "restarted":
            text += " You missed a day, so the streak started over. Keep going!"
        await interaction.response.send_message(text, ephemeral=True)
        if new in (7, 30, 100, 365):
            await interaction.followup.send(f"{interaction.user.mention} reached a **{new} day** streak on {hname}!",
                                            allowed_mentions=discord.AllowedMentions(users=[interaction.user]))

    @done.autocomplete("name")
    async def done_ac(self, interaction: discord.Interaction, current: str):
        return await self.names(interaction, current)

    @app_commands.command(name="remove", description="Stop tracking a habit")
    @app_commands.guild_only()
    async def remove(self, interaction: discord.Interaction, name: str):
        row = await self.my_habit(interaction, name)
        if not row:
            return await interaction.response.send_message("You don't track that habit.", ephemeral=True)
        await self.bot.db.execute("DELETE FROM habits WHERE id=?", (row[0],))
        await self.bot.db.commit()
        await interaction.response.send_message(f"Stopped tracking **{row[1]}**.", ephemeral=True)

    @remove.autocomplete("name")
    async def remove_ac(self, interaction: discord.Interaction, current: str):
        return await self.names(interaction, current)

    @app_commands.command(name="list", description="See your habits and streaks")
    @app_commands.guild_only()
    async def list_cmd(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
                "SELECT name, streak, best, total, last_day, utc_offset FROM habits WHERE guild_id=? AND user_id=?",
                (interaction.guild_id, interaction.user.id)) as cur:
            rows = await cur.fetchall()
        if not rows:
            return await interaction.response.send_message("No habits yet. Start one with `/habit add`.",
                                                           ephemeral=True)
        lines = []
        for name, streak, best, total, last_day, offset in rows:
            today = self.local_today(offset)
            last = date.fromisoformat(last_day) if last_day else None
            lines.append(f"**{name}**: {live_streak(last, today, streak)} day streak, best {best}, "
                         f"{total} total. " + ("Done today." if last == today else "Not done today."))
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(name="board", description="The longest running streaks in the server")
    @app_commands.guild_only()
    async def board(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
                "SELECT user_id, name, streak, last_day, utc_offset FROM habits WHERE guild_id=? AND private=0",
                (interaction.guild_id,)) as cur:
            rows = await cur.fetchall()
        scored = []
        for uid, name, streak, last_day, offset in rows:
            live = live_streak(date.fromisoformat(last_day) if last_day else None, self.local_today(offset), streak)
            if live > 0:
                scored.append((live, uid, name))
        scored.sort(reverse=True)
        if not scored:
            return await interaction.response.send_message("No active streaks yet. Start one with `/habit add`.",
                                                           ephemeral=True)
        text = "\n".join(f"**{i}.** <@{u}> {name}: **{n}** days" for i, (n, u, name) in enumerate(scored[:10], 1))
        await interaction.response.send_message(embed=discord.Embed(title="Streak board", description=text,
                                                                    color=discord.Color.green()),
                                                allowed_mentions=discord.AllowedMentions.none())


# =====================================================================
# /wordchain : a channel where everyone continues the word chain
# =====================================================================

WORD_RE = re.compile(r"[a-z]{2,30}")


def check_word(word: str, last_word: str | None, last_user: int | None, user: int, used: bool) -> str | None:
    """Return why a word is not allowed, or None if it is fine."""
    if not WORD_RE.fullmatch(word):
        return "Send one word, letters only (at least 2)."
    if last_user == user:
        return "You went last. Wait for someone else."
    if last_word and word[0] != last_word[-1]:
        return f"Your word must start with **{last_word[-1].upper()}**."
    if used:
        return "That word was already used."
    return None


class WordChain(commands.GroupCog, group_name="wordchain", group_description="A word chain game channel"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config: dict[int, dict] = {}
        self.locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def cog_load(self):
        db = self.bot.db
        await db.execute("CREATE TABLE IF NOT EXISTS wordchain_config (guild_id INTEGER PRIMARY KEY, channel_id INTEGER, "
                         "last_word TEXT, last_user INTEGER, count INTEGER DEFAULT 0)")
        await db.execute("CREATE TABLE IF NOT EXISTS wordchain_used (guild_id INTEGER, word TEXT, "
                         "PRIMARY KEY (guild_id, word))")
        await db.execute("CREATE TABLE IF NOT EXISTS wordchain_scores (guild_id INTEGER, user_id INTEGER, "
                         "words INTEGER DEFAULT 0, PRIMARY KEY (guild_id, user_id))")
        await db.commit()
        async with db.execute("SELECT guild_id, channel_id, last_word, last_user, count FROM wordchain_config") as cur:
            for gid, ch, word, user, count in await cur.fetchall():
                self.config[gid] = {"channel_id": ch, "last_word": word, "last_user": user, "count": count}

    @app_commands.command(name="setup", description="Choose the word chain channel (staff)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup_cmd(self, interaction: discord.Interaction, channel: discord.TextChannel):
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.manage_messages):
            return await interaction.response.send_message(
                f"I need View Channel, Send Messages and Manage Messages in {channel.mention}.", ephemeral=True)
        await self.bot.db.execute("INSERT OR REPLACE INTO wordchain_config (guild_id, channel_id, count) VALUES (?,?,0)",
                                  (interaction.guild_id, channel.id))
        await self.bot.db.execute("DELETE FROM wordchain_used WHERE guild_id=?", (interaction.guild_id,))
        await self.bot.db.commit()
        self.config[interaction.guild_id] = {"channel_id": channel.id, "last_word": None, "last_user": None,
                                             "count": 0}
        await interaction.response.send_message(f"Word chain is live in {channel.mention}.", ephemeral=True)
        try:
            await channel.send("**Word chain!** Send one word. Each word must start with the last letter of the "
                               "previous word, no repeats, and you can't go twice in a row. Start with any word.")
        except discord.HTTPException:
            pass

    @app_commands.command(name="off", description="Turn the word chain off (staff)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def off(self, interaction: discord.Interaction):
        await self.bot.db.execute("DELETE FROM wordchain_config WHERE guild_id=?", (interaction.guild_id,))
        await self.bot.db.commit()
        self.config.pop(interaction.guild_id, None)
        await interaction.response.send_message("Word chain is off.", ephemeral=True)

    @app_commands.command(name="reset", description="Start a fresh chain with an empty used-words list (staff)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def reset(self, interaction: discord.Interaction):
        cfg = self.config.get(interaction.guild_id)
        if not cfg:
            return await interaction.response.send_message("Word chain isn't set up.", ephemeral=True)
        cfg.update(last_word=None, last_user=None, count=0)
        await self.bot.db.execute("UPDATE wordchain_config SET last_word=NULL, last_user=NULL, count=0 WHERE guild_id=?",
                                  (interaction.guild_id,))
        await self.bot.db.execute("DELETE FROM wordchain_used WHERE guild_id=?", (interaction.guild_id,))
        await self.bot.db.commit()
        await interaction.response.send_message("Fresh chain. Anyone can start with any word.", ephemeral=True)

    @app_commands.command(name="status", description="See the chain and the next letter")
    @app_commands.guild_only()
    async def status(self, interaction: discord.Interaction):
        cfg = self.config.get(interaction.guild_id)
        if not cfg:
            return await interaction.response.send_message("Word chain isn't set up.", ephemeral=True)
        nxt = f"Next word starts with **{cfg['last_word'][-1].upper()}**." if cfg["last_word"] else "Start with any word."
        await interaction.response.send_message(f"Channel: <#{cfg['channel_id']}>\nChain length: **{cfg['count']}**\n"
                                                f"Last word: **{cfg['last_word'] or 'none'}**\n{nxt}", ephemeral=True)

    @app_commands.command(name="top", description="Who added the most words")
    @app_commands.guild_only()
    async def top(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
                "SELECT user_id, words FROM wordchain_scores WHERE guild_id=? ORDER BY words DESC LIMIT 10",
                (interaction.guild_id,)) as cur:
            rows = await cur.fetchall()
        if not rows:
            return await interaction.response.send_message("No words yet.", ephemeral=True)
        text = "\n".join(f"**{i}.** <@{u}> {w} words" for i, (u, w) in enumerate(rows, 1))
        await interaction.response.send_message(embed=discord.Embed(title="Word chain leaders", description=text,
                                                                    color=discord.Color.blurple()),
                                                allowed_mentions=discord.AllowedMentions.none())

    async def reject(self, message: discord.Message, reason: str):
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        try:
            await message.channel.send(f"{message.author.mention} {reason}", delete_after=6,
                                       allowed_mentions=discord.AllowedMentions(users=[message.author]))
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        cfg = self.config.get(message.guild.id)
        if not cfg or message.channel.id != cfg["channel_id"]:
            return
        word = message.content.strip().lower()
        if isinstance(message.author, discord.Member) and message.author.guild_permissions.manage_messages \
                and not WORD_RE.fullmatch(word):
            return  # moderators can talk in the channel
        async with self.locks[message.guild.id]:
            used = False
            if WORD_RE.fullmatch(word):
                async with self.bot.db.execute("SELECT 1 FROM wordchain_used WHERE guild_id=? AND word=?",
                                               (message.guild.id, word)) as cur:
                    used = await cur.fetchone() is not None
            problem = check_word(word, cfg["last_word"], cfg["last_user"], message.author.id, used)
            if problem:
                return await self.reject(message, problem)
            cfg.update(last_word=word, last_user=message.author.id, count=cfg["count"] + 1)
            db = self.bot.db
            await db.execute("INSERT OR IGNORE INTO wordchain_used (guild_id, word) VALUES (?,?)",
                             (message.guild.id, word))
            await db.execute("INSERT INTO wordchain_scores (guild_id, user_id, words) VALUES (?,?,1) "
                             "ON CONFLICT(guild_id, user_id) DO UPDATE SET words=words+1",
                             (message.guild.id, message.author.id))
            await db.execute("UPDATE wordchain_config SET last_word=?, last_user=?, count=? WHERE guild_id=?",
                             (word, message.author.id, cfg["count"], message.guild.id))
            await db.commit()
        if cfg["count"] % 50 == 0:
            try:
                await message.channel.send(f"The chain just reached **{cfg['count']}** words! Next letter: "
                                           f"**{word[-1].upper()}**.")
            except discord.HTTPException:
                pass


# =====================================================================
# /remind : reminders in the channel or in your DMs
# =====================================================================

class Remind(commands.GroupCog, group_name="remind", group_description="Get reminded later"):
    MAX_PER_USER = 10

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.db.execute(
            """CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, channel_id INTEGER, user_id INTEGER,
                text TEXT, due INTEGER, dm INTEGER DEFAULT 0)""")
        await self.bot.db.commit()
        self.loop.start()

    async def cog_unload(self):
        self.loop.cancel()

    @app_commands.command(name="me", description="Remind me about something later")
    @app_commands.describe(when="For example 10m, 2h, 1d12h", text="What to remind you about",
                           dm="Send it to your DMs instead of this channel")
    @app_commands.guild_only()
    async def me(self, interaction: discord.Interaction, when: str, text: app_commands.Range[str, 1, 300],
                 dm: bool = False):
        seconds = parse_duration(when)
        if seconds is None or seconds < 10 or seconds > 90 * 86400:
            return await interaction.response.send_message(
                "Use a time between 10 seconds and 90 days, like `10m`, `2h` or `1d12h`.", ephemeral=True)
        async with self.bot.db.execute("SELECT COUNT(*) FROM reminders WHERE user_id=?", (interaction.user.id,)) as cur:
            (n,) = await cur.fetchone()
        if n >= self.MAX_PER_USER:
            return await interaction.response.send_message(
                f"You have {self.MAX_PER_USER} reminders already. Cancel one with `/remind cancel`.", ephemeral=True)
        due = int(time.time()) + seconds
        await self.bot.db.execute(
            "INSERT INTO reminders (guild_id, channel_id, user_id, text, due, dm) VALUES (?,?,?,?,?,?)",
            (interaction.guild_id, interaction.channel_id, interaction.user.id, text, due, int(dm)))
        await self.bot.db.commit()
        await interaction.response.send_message(f"Okay, I'll remind you <t:{due}:R>.", ephemeral=True)

    @app_commands.command(name="list", description="Your pending reminders")
    @app_commands.guild_only()
    async def list_cmd(self, interaction: discord.Interaction):
        async with self.bot.db.execute("SELECT id, text, due FROM reminders WHERE user_id=? ORDER BY due",
                                       (interaction.user.id,)) as cur:
            rows = await cur.fetchall()
        if not rows:
            return await interaction.response.send_message("You have no reminders.", ephemeral=True)
        await interaction.response.send_message(
            "\n".join(f"`#{i}` <t:{due}:R> {text[:80]}" for i, text, due in rows), ephemeral=True)

    @app_commands.command(name="cancel", description="Cancel a reminder")
    @app_commands.describe(reminder_id="The number shown in /remind list")
    @app_commands.guild_only()
    async def cancel(self, interaction: discord.Interaction, reminder_id: int):
        cur = await self.bot.db.execute("DELETE FROM reminders WHERE id=? AND user_id=?",
                                        (reminder_id, interaction.user.id))
        await self.bot.db.commit()
        await interaction.response.send_message("Cancelled." if cur.rowcount else "I couldn't find that reminder.",
                                                ephemeral=True)

    @tasks.loop(seconds=15)
    async def loop(self):
        async with self.bot.db.execute("SELECT id, channel_id, user_id, text, dm FROM reminders WHERE due<=?",
                                       (int(time.time()),)) as cur:
            rows = await cur.fetchall()
        for rid, channel_id, uid, text, dm in rows:
            await self.bot.db.execute("DELETE FROM reminders WHERE id=?", (rid,))
            await self.bot.db.commit()
            sent = False
            user = self.bot.get_user(uid)
            if dm:
                try:
                    user = user or await self.bot.fetch_user(uid)
                    await user.send(f"Reminder: {text}")
                    sent = True
                except discord.HTTPException:
                    pass
            if not sent:
                channel = self.bot.get_channel(channel_id)
                if channel is not None:
                    try:
                        await channel.send(f"<@{uid}> reminder: {text}",
                                           allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=uid)]))
                    except discord.HTTPException:
                        pass

    @loop.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    for cog in (Lfg, SecretSanta, Habit, WordChain, Remind):
        await bot.add_cog(cog(bot))
