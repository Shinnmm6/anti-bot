import asyncio
import os
import re
from datetime import datetime, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

MAX_COMMITS = 15
DISPLAY_AUTHOR = "v0zv2"  # shown instead of the real Git author name
# Public updates channel. If it is an Announcement channel, each update is also published
# (crossposted) so every server following it receives it. Override with PUBLIC_CHANNEL_ID.
DEFAULT_REPO_OWNER = "Shinnmm6"  # used by /changelogs when Railway doesn't provide repo info
DEFAULT_REPO_NAME = "anti-bot"
PUBLIC_CHANNEL_ID = int(os.getenv("PUBLIC_CHANNEL_ID", "1564952852810702970") or 0)
GITHUB_TIMEOUT = aiohttp.ClientTimeout(total=10)


# Commit messages like "Update bot.py" are shown with the slash commands that file provides.
FILE_COMMANDS = {
    "bot": "/kick /ban /timeout /warn /warnings /purge",
    "antilink": "/antilink",
    "antinuke": "/antinuke",
    "audit_logs": "/auditlog",
    "changelog": "/changelog /changelogs",
    "help_cmd": "/help",
    "server_profile": "/server",
    "shop": "/buy",
    "modtools": "/unban /clearwarnings /slowmode /lock /unlock /userinfo",
    "greetings": "/welcome /bye /booster",
    "wordfilter": "/filter",
    "partnership": "/partnership",
    "tickets": "/ticket",
    "leveling": "/leveling",
    "invites": "/invites",
    "giveaway": "/giveaway",
    "customrole": "/role",
    "community": "/profile /confess /capsule /kudos /daily",
    "surprise": "/surprise",
    "mrbeastpot": "/mrbeastpot",
    "fun": "/wyr, /pairup, /vibe, /duel, /birthday",
    "social": "/lfg, /secretsanta, /habit, /wordchain, /remind",
    "verification": "/verify",
}


def friendly_message(message: str) -> str:
    """Replace file names like bot.py with the commands they contain (unknown files are left alone)."""
    return re.sub(r"\b(\w+)\.py\b", lambda m: FILE_COMMANDS.get(m.group(1), m.group(0)), message)


def current_build() -> dict | None:
    """Read the deploy info Railway injects when it deploys from a GitHub repo."""
    sha = os.getenv("RAILWAY_GIT_COMMIT_SHA")
    if not sha:
        return None
    return {
        "sha": sha,
        "message": os.getenv("RAILWAY_GIT_COMMIT_MESSAGE", ""),
        "author": os.getenv("RAILWAY_GIT_AUTHOR", "") or os.getenv("RAILWAY_GIT_AUTHOR_NAME", ""),
        "branch": os.getenv("RAILWAY_GIT_BRANCH", ""),
        "owner": os.getenv("RAILWAY_GIT_REPO_OWNER", ""),
        "repo": os.getenv("RAILWAY_GIT_REPO_NAME", ""),
    }


async def fetch_commits(build: dict, old_sha: str | None) -> list[tuple[str, str, str]]:
    """Commits between old_sha and the current build as (sha, first line, author), oldest first.

    Returns [] on any problem (private repo, force-push, rate limit, no network);
    the caller then falls back to the single commit message Railway gave us.
    """
    if not (old_sha and build["owner"] and build["repo"]):
        return []
    url = (f"https://api.github.com/repos/{build['owner']}/{build['repo']}"
           f"/compare/{old_sha}...{build['sha']}")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "anti-bot-changelog"}
    if token := os.getenv("GITHUB_TOKEN"):  # optional: only needed for a private repo
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with aiohttp.ClientSession(timeout=GITHUB_TIMEOUT, headers=headers) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return []

    commits = []
    for c in data.get("commits", []):
        message = (c.get("commit", {}).get("message") or "").split("\n")[0]
        author = (c.get("author") or {}).get("login") or c.get("commit", {}).get("author", {}).get("name", "")
        commits.append((c["sha"], message, author))
    return commits


def build_embed(build: dict, commits: list[tuple[str, str, str]]) -> discord.Embed:
    if not commits:
        commits = [(build["sha"], build["message"].split("\n")[0], build["author"])]

    def link(sha: str) -> str:
        return f"`{sha[:7]}`"  # plain text on purpose, no GitHub link

    shown = commits[-MAX_COMMITS:][::-1]  # newest first
    lines = []
    for sha, message, _author in shown:  # real Git author is ignored on purpose
        lines.append(f"{link(sha)} {friendly_message(message)[:150] or 'No message'} — {DISPLAY_AUTHOR}")
    hidden = len(commits) - len(shown)
    if hidden > 0:
        lines.append(f"…and {hidden} older commit{'s' if hidden != 1 else ''}")

    embed = discord.Embed(title="Bot updated logs", description="\n".join(lines)[:4000],
                          color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
    footer = " • ".join(p for p in (build["branch"], build["sha"][:7]) if p)
    if footer:
        embed.set_footer(text=footer)
    return embed


class Changelog(commands.GroupCog, group_name="changelog",
                group_description="Post a changelog when the bot is redeployed"):
    """On startup, if Railway's commit SHA differs from the last one announced, post the new commits.

    Needs the bot to be deployed from a GitHub repo on Railway (that's what sets RAILWAY_GIT_*),
    and DB_PATH on a mounted volume so the last announced SHA survives redeploys.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.channels: dict[int, int] = {}
        self._announced = False

    async def cog_load(self):
        db = self.bot.db
        await db.execute("CREATE TABLE IF NOT EXISTS changelog_state (key TEXT PRIMARY KEY, value TEXT)")
        await db.execute(
            "CREATE TABLE IF NOT EXISTS changelog_channels (guild_id INTEGER PRIMARY KEY, channel_id INTEGER)")
        await db.commit()
        async with db.execute("SELECT guild_id, channel_id FROM changelog_channels") as cur:
            for guild_id, channel_id in await cur.fetchall():
                self.channels[guild_id] = channel_id

    # ---------- helpers ----------

    async def get_state(self, key: str) -> str | None:
        async with self.bot.db.execute("SELECT value FROM changelog_state WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def set_state(self, key: str, value: str):
        await self.bot.db.execute("INSERT OR REPLACE INTO changelog_state (key, value) VALUES (?, ?)",
                                  (key, value))
        await self.bot.db.commit()

    def channel_for(self, guild: discord.Guild):
        """Changelog channel if set, otherwise the log channel."""
        channel_id = (self.channels.get(guild.id)
                      or self.bot.log_channels.get(guild.id)
                      or int(os.getenv("LOG_CHANNEL_ID", "0") or 0))
        return guild.get_channel(channel_id) if channel_id else None

    async def announce(self):
        build = current_build()
        if not build:
            print("changelog: RAILWAY_GIT_COMMIT_SHA not set (not a GitHub deploy?), skipping")
            return
        old_sha = await self.get_state("last_sha")
        if old_sha == build["sha"]:
            return
        # Save first so a crash loop can't announce the same deploy over and over.
        await self.set_state("last_sha", build["sha"])
        if old_sha is None:
            print(f"changelog: first run, remembering {build['sha'][:7]} without announcing")
            return

        embed = build_embed(build, await fetch_commits(build, old_sha))

        targets = []
        public = await self.get_public_channel()
        if public is not None:
            targets.append(public)
        for guild in self.bot.guilds:
            channel = self.channel_for(guild)
            if channel is not None and all(channel.id != t.id for t in targets):
                targets.append(channel)

        for channel in targets:
            try:
                message = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                continue  # missing permissions or channel deleted
            if getattr(channel, "is_news", lambda: False)():
                try:
                    await message.publish()  # crosspost to servers following this announcement channel
                except discord.HTTPException:
                    pass

    async def get_public_channel(self):
        if not PUBLIC_CHANNEL_ID:
            return None
        channel = self.bot.get_channel(PUBLIC_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(PUBLIC_CHANNEL_ID)
            except discord.HTTPException:
                print(f"changelog: can't access public channel {PUBLIC_CHANNEL_ID}")
                return None
        return channel

    # ---------- listener ----------

    @commands.Cog.listener()
    async def on_ready(self):
        if self._announced:  # on_ready fires again after reconnects
            return
        self._announced = True
        await self.announce()

    # ---------- /changelog commands ----------

    @app_commands.command(name="set", description="Choose the channel where update announcements are posted")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return await interaction.response.send_message(
                f"I need View Channel, Send Messages and Embed Links in {channel.mention}.", ephemeral=True)
        await self.bot.db.execute(
            "INSERT OR REPLACE INTO changelog_channels (guild_id, channel_id) VALUES (?, ?)",
            (interaction.guild_id, channel.id))
        await self.bot.db.commit()
        self.channels[interaction.guild_id] = channel.id
        await interaction.response.send_message(
            f"Update announcements will be posted in {channel.mention}.", ephemeral=True)

    @app_commands.command(name="off", description="Use the log channel for announcements again")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def off(self, interaction: discord.Interaction):
        await self.bot.db.execute("DELETE FROM changelog_channels WHERE guild_id=?", (interaction.guild_id,))
        await self.bot.db.commit()
        self.channels.pop(interaction.guild_id, None)
        await interaction.response.send_message(
            "Dedicated changelog channel removed. Announcements go to the log channel (if one is set).",
            ephemeral=True)

    @app_commands.command(name="status", description="Show the changelog channel and the running version")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def status(self, interaction: discord.Interaction):
        channel = self.channel_for(interaction.guild)
        build = current_build()
        where = channel.mention if channel else "nowhere yet (use `/changelog set` or `/auditlog set`)"
        version = (f"`{build['sha'][:7]}` on `{build['branch'] or '?'}`" if build
                   else "unknown (Railway isn't providing Git info, so it must not be a GitHub deploy)")
        await interaction.response.send_message(
            f"Announcements go to: {where}\nRunning version: {version}", ephemeral=True)

    @app_commands.command(name="preview", description="Show what an update announcement looks like (only you see it)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def preview(self, interaction: discord.Interaction):
        build = current_build()
        if not build:
            return await interaction.response.send_message(
                "No Railway Git info found, so there's nothing to preview. "
                "This only works when Railway deploys the bot from GitHub.", ephemeral=True)
        await interaction.response.send_message(embed=build_embed(build, []), ephemeral=True)


async def fetch_recent_commits(owner: str, repo: str) -> list[tuple[str, str, str]]:
    """Latest commits on the default branch as (sha, first line, author), oldest first. [] on any problem."""
    url = f"https://api.github.com/repos/{owner}/{repo}/commits?per_page={MAX_COMMITS}"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "anti-bot-changelog"}
    if token := os.getenv("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with aiohttp.ClientSession(timeout=GITHUB_TIMEOUT, headers=headers) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return []
    commits = [(c["sha"], (c.get("commit", {}).get("message") or "").split("\n")[0], "") for c in data]
    return commits[::-1]  # GitHub returns newest first


class PublicChangelog(commands.Cog):
    """/changelogs: anyone can run it and the reply is visible to the whole channel."""

    @app_commands.command(name="changelogs", description="Show the latest bot updates")
    async def changelogs(self, interaction: discord.Interaction):
        await interaction.response.defer()  # not ephemeral, so everyone in the channel can see it
        deploy = current_build() or {}
        owner = deploy.get("owner") or DEFAULT_REPO_OWNER
        repo = deploy.get("repo") or DEFAULT_REPO_NAME
        commits = await fetch_recent_commits(owner, repo)
        if not commits:
            return await interaction.followup.send("Couldn't load the updates right now. Try again later.")
        build = {"sha": commits[-1][0], "message": commits[-1][1], "author": "",
                 "branch": deploy.get("branch") or "main", "owner": owner, "repo": repo}
        await interaction.followup.send(embed=build_embed(build, commits),
                                        allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot):
    await bot.add_cog(Changelog(bot))
    await bot.add_cog(PublicChangelog())
