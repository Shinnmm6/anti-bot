import asyncio
import base64
import functools
import json
import os
import random
import re
import shutil
from collections import deque
from dataclasses import dataclass

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

try:
    import yt_dlp
except ImportError:  # the bot still starts, /play just explains what is missing
    yt_dlp = None

try:
    import imageio_ffmpeg
except ImportError:
    imageio_ffmpeg = None

MAX_QUEUE = 100
IDLE_LEAVE_SECONDS = 300
SPOTIFY_RE = re.compile(r"open\.spotify\.com/(?:intl-[a-z]+/)?(track|album|playlist)/([A-Za-z0-9]+)")
URL_RE = re.compile(r"^https?://", re.I)
FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"


def ffmpeg_path() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    if imageio_ffmpeg:
        try:
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return None
    return None


def load_opus() -> bool:
    if discord.opus.is_loaded():
        return True
    for name in ("libopus.so.0", "libopus.so", "opus"):
        try:
            discord.opus.load_opus(name)
            return True
        except Exception:
            continue
    return False


def parse_spotify(text: str) -> tuple[str, str] | None:
    m = SPOTIFY_RE.search(text)
    return (m.group(1), m.group(2)) if m else None


def spotify_title_to_query(page_title: str) -> str:
    """'Song - song and lyrics by Artist | Spotify' -> 'Artist Song'."""
    t = page_title.replace(" | Spotify", "").strip()
    m = re.match(r"^(.*?) - (?:song(?: and lyrics)?) by (.*)$", t)
    return f"{m.group(2)} {m.group(1)}" if m else t


def fmt_duration(seconds) -> str:
    if not seconds:
        return "live"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


@dataclass
class Track:
    title: str
    query: str            # a link, or a search like ytsearch1:artist title
    requester: int
    duration: int | None = None
    web_url: str | None = None


class GuildState:
    def __init__(self):
        self.queue: deque[Track] = deque()
        self.current: Track | None = None
        self.loop = "off"          # off, track, queue
        self.volume = 0.5
        self.skip = False
        self.text_channel_id: int | None = None
        self.idle_task: asyncio.Task | None = None


class Music(commands.Cog):
    music = app_commands.Group(name="music", description="Control the music player", guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.states: dict[int, GuildState] = {}
        self.keep: dict[int, int] = {}          # guild -> voice channel it should always sit in
        self.has_opus = load_opus()
        self.spotify_token: tuple[str, float] | None = None
        self.cookie_file = None
        cookies = os.getenv("YOUTUBE_COOKIES")
        if cookies:
            self.cookie_file = "/tmp/yt_cookies.txt"
            with open(self.cookie_file, "w") as f:
                f.write(cookies)

    async def cog_load(self):
        await self.bot.db.execute("CREATE TABLE IF NOT EXISTS vc_247 (guild_id INTEGER PRIMARY KEY, channel_id INTEGER)")
        await self.bot.db.commit()
        async with self.bot.db.execute("SELECT guild_id, channel_id FROM vc_247") as cur:
            self.keep = {g: c for g, c in await cur.fetchall()}
        self.reconnect.start()

    async def cog_unload(self):
        self.reconnect.cancel()

    def state(self, guild_id: int) -> GuildState:
        return self.states.setdefault(guild_id, GuildState())

    # ------------------------------------------------------------------ staying in the channel 24/7

    @tasks.loop(seconds=30)
    async def reconnect(self):
        for gid, channel_id in list(self.keep.items()):
            guild = self.bot.get_guild(gid)
            channel = guild.get_channel(channel_id) if guild else None
            if guild is None or channel is None:
                continue
            vc = guild.voice_client
            if vc is not None and vc.is_connected():
                continue
            try:
                if vc is not None:
                    await vc.disconnect(force=True)
                await channel.connect(self_deaf=True)
            except (discord.ClientException, discord.HTTPException, asyncio.TimeoutError):
                pass  # try again in 30 seconds

    @reconnect.before_loop
    async def before_reconnect(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="join", description="Join a voice channel and stay there 24/7")
    @app_commands.describe(channel="The voice channel (default: the one you are in)")
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(connect=True, speak=True)
    async def join(self, interaction: discord.Interaction, channel: discord.VoiceChannel | discord.StageChannel | None = None):
        guild = interaction.guild
        channel = channel or (interaction.user.voice.channel if interaction.user.voice else None)
        if channel is None:
            return await interaction.response.send_message("Join a voice channel first, or pick one with the `channel` option.",
                                                           ephemeral=True)
        perms = channel.permissions_for(guild.me)
        if not (perms.connect and perms.speak):
            return await interaction.response.send_message(f"I need Connect and Speak in {channel.mention}.", ephemeral=True)
        vc = guild.voice_client
        if vc and vc.is_connected() and vc.channel.id != channel.id and (vc.is_playing() or vc.is_paused()) \
                and not interaction.user.guild_permissions.move_members:
            return await interaction.response.send_message("I'm playing music in another channel right now.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            if vc and vc.is_connected():
                await vc.move_to(channel)
            else:
                await channel.connect(self_deaf=True)
        except (discord.ClientException, discord.HTTPException, asyncio.TimeoutError) as e:
            return await interaction.followup.send(f"I couldn't join: {e}. (Voice needs the PyNaCl package installed.)",
                                                   ephemeral=True)
        self.keep[guild.id] = channel.id
        await self.bot.db.execute("INSERT OR REPLACE INTO vc_247 (guild_id, channel_id) VALUES (?,?)", (guild.id, channel.id))
        await self.bot.db.commit()
        await interaction.followup.send(f"Joined {channel.mention}. I'll stay here 24/7 and come back after a restart. "
                                        "Use `/leave` to send me away.", ephemeral=True)

    @app_commands.command(name="leave", description="Leave the voice channel and stop staying 24/7")
    @app_commands.guild_only()
    async def leave(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc is None:
            self.keep.pop(interaction.guild_id, None)
            return await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)
        if not (interaction.user.voice and interaction.user.voice.channel == vc.channel) \
                and not interaction.user.guild_permissions.move_members:
            return await interaction.response.send_message("Join my voice channel to send me away.", ephemeral=True)
        self.keep.pop(interaction.guild_id, None)
        await self.bot.db.execute("DELETE FROM vc_247 WHERE guild_id=?", (interaction.guild_id,))
        await self.bot.db.commit()
        st = self.state(interaction.guild_id)
        st.queue.clear()
        st.loop, st.skip = "off", True
        await vc.disconnect(force=True)
        await interaction.response.send_message("Left the voice channel.")

    # ------------------------------------------------------------------ finding songs

    def ydl_opts(self, **extra) -> dict:
        opts = {"quiet": True, "no_warnings": True, "format": "bestaudio/best", "noplaylist": True,
                "skip_download": True, "socket_timeout": 20}
        if self.cookie_file:
            opts["cookiefile"] = self.cookie_file
        opts.update(extra)
        return opts

    async def extract(self, query: str, **opts):
        def run():
            with yt_dlp.YoutubeDL(self.ydl_opts(**opts)) as ydl:
                return ydl.extract_info(query, download=False)
        return await asyncio.get_running_loop().run_in_executor(None, run)

    async def spotify_api_token(self) -> str | None:
        cid, secret = os.getenv("SPOTIFY_CLIENT_ID"), os.getenv("SPOTIFY_CLIENT_SECRET")
        if not (cid and secret):
            return None
        loop = asyncio.get_running_loop()
        if self.spotify_token and self.spotify_token[1] > loop.time():
            return self.spotify_token[0]
        auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        async with aiohttp.ClientSession() as s:
            async with s.post("https://accounts.spotify.com/api/token", data={"grant_type": "client_credentials"},
                              headers={"Authorization": f"Basic {auth}"}) as r:
                data = await r.json()
        self.spotify_token = (data["access_token"], loop.time() + data.get("expires_in", 3600) - 60)
        return self.spotify_token[0]

    async def spotify_names(self, kind: str, sid: str, url: str) -> list[str]:
        """Return 'Artist Title' search strings for a Spotify link."""
        token = None
        try:
            token = await self.spotify_api_token()
        except Exception:
            token = None
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            if token:
                h = {"Authorization": f"Bearer {token}"}
                if kind == "track":
                    async with s.get(f"https://api.spotify.com/v1/tracks/{sid}", headers=h) as r:
                        t = await r.json()
                    return [f"{t['artists'][0]['name']} {t['name']}"]
                path = f"albums/{sid}/tracks?limit=50" if kind == "album" else f"playlists/{sid}/tracks?limit=50"
                async with s.get(f"https://api.spotify.com/v1/{path}", headers=h) as r:
                    data = await r.json()
                out = []
                for item in data.get("items", []):
                    t = item.get("track") or item
                    if t and t.get("name"):
                        out.append(f"{t['artists'][0]['name']} {t['name']}")
                return out
            if kind != "track":
                raise ValueError("Spotify albums and playlists need SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET set. "
                                 "A single Spotify track link works without them.")
            async with s.get(f"https://open.spotify.com/{kind}/{sid}", headers={"User-Agent": "Mozilla/5.0"}) as r:
                html = await r.text()
        m = re.search(r"<title>(.*?)</title>", html, re.S)
        if not m:
            raise ValueError("I couldn't read that Spotify link.")
        return [spotify_title_to_query(m.group(1))]

    async def find_tracks(self, query: str, requester: int) -> list[Track]:
        query = query.strip()
        sp = parse_spotify(query)
        if sp:
            names = await self.spotify_names(sp[0], sp[1], query)
            return [Track(title=n, query=f"ytsearch1:{n}", requester=requester) for n in names[:50]]
        if URL_RE.match(query):
            info = await self.extract(query, extract_flat="in_playlist", noplaylist=False)
            if info.get("entries"):
                out = []
                for e in list(info["entries"])[:25]:
                    if not e:
                        continue
                    url = e.get("url") or e.get("webpage_url") or ""
                    if not URL_RE.match(url):
                        url = f"https://www.youtube.com/watch?v={e.get('id')}"
                    out.append(Track(title=e.get("title") or "Unknown", query=url, requester=requester,
                                     duration=e.get("duration"), web_url=url))
                return out
            return [Track(title=info.get("title") or "Unknown", query=info.get("webpage_url") or query,
                          requester=requester, duration=info.get("duration"), web_url=info.get("webpage_url") or query)]
        info = None
        for prefix in ("ytsearch1:", "scsearch1:"):   # YouTube first, SoundCloud as a backup
            try:
                info = await self.extract(prefix + query)
                if info and info.get("entries"):
                    break
            except Exception:
                info = None
        if not info or not info.get("entries"):
            raise ValueError("I couldn't find anything for that.")
        e = info["entries"][0]
        url = e.get("webpage_url") or e.get("url") or query
        return [Track(title=e.get("title") or query, query=url, requester=requester, duration=e.get("duration"), web_url=url)]

    async def stream_info(self, track: Track) -> dict:
        try:
            info = await self.extract(track.query)
        except Exception:
            if track.query.startswith("ytsearch1:"):   # YouTube failed, try SoundCloud
                info = await self.extract("scsearch1:" + track.query[len("ytsearch1:"):])
            else:
                raise
        if info.get("entries"):
            info = info["entries"][0]
        return info

    # ------------------------------------------------------------------ playing

    def make_source(self, url: str, volume: float, ffmpeg: str):
        opts = {"executable": ffmpeg, "before_options": FFMPEG_BEFORE, "options": "-vn"}
        if self.has_opus:
            return discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(url, **opts), volume=volume)
        return discord.FFmpegOpusAudio(url, **opts)

    async def say(self, guild: discord.Guild, text: str, embed: discord.Embed | None = None):
        ch = guild.get_channel(self.state(guild.id).text_channel_id or 0)
        if ch:
            try:
                await ch.send(text or None, embed=embed, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass

    async def play_next(self, guild: discord.Guild):
        st, vc = self.state(guild.id), guild.voice_client
        if vc is None or not vc.is_connected() or vc.is_playing() or vc.is_paused():
            return
        ffmpeg = ffmpeg_path()
        while st.queue:
            track = st.queue.popleft()
            try:
                if not ffmpeg:
                    raise RuntimeError("ffmpeg is not installed")
                info = await self.stream_info(track)
                source = self.make_source(info["url"], st.volume, ffmpeg)
            except Exception as e:
                await self.say(guild, f"I couldn't play **{track.title}**: {str(e)[:200]}")
                continue
            track.title = info.get("title") or track.title
            track.duration = info.get("duration") or track.duration
            track.web_url = info.get("webpage_url") or track.web_url
            st.current = track
            if st.idle_task:
                st.idle_task.cancel()
            vc.play(source, after=lambda err, t=track: asyncio.run_coroutine_threadsafe(self.after_play(guild, t, err), self.bot.loop))
            embed = discord.Embed(title="Now playing", description=f"[{track.title}]({track.web_url})" if track.web_url else track.title,
                                  color=discord.Color.green())
            embed.add_field(name="Length", value=fmt_duration(track.duration))
            embed.add_field(name="Requested by", value=f"<@{track.requester}>")
            await self.say(guild, "", embed)
            return
        st.current = None
        if guild.id not in self.keep:
            st.idle_task = asyncio.create_task(self.idle_leave(guild))

    async def after_play(self, guild: discord.Guild, track: Track, error):
        st = self.state(guild.id)
        st.current = None
        if error:
            print(f"Music playback error: {error!r}")
        if st.skip:
            st.skip = False
        elif st.loop == "track":
            st.queue.appendleft(track)
        elif st.loop == "queue":
            st.queue.append(track)
        await self.play_next(guild)

    async def idle_leave(self, guild: discord.Guild):
        try:
            await asyncio.sleep(IDLE_LEAVE_SECONDS)
        except asyncio.CancelledError:
            return
        vc = guild.voice_client
        if vc and not vc.is_playing() and not vc.is_paused() and guild.id not in self.keep:
            await vc.disconnect(force=True)

    def check_voice(self, interaction: discord.Interaction) -> str | None:
        vc = interaction.guild.voice_client
        if not interaction.user.voice or not interaction.user.voice.channel:
            return "Join a voice channel first."
        if vc and vc.is_connected() and vc.channel != interaction.user.voice.channel:
            return f"Join {vc.channel.mention} to control the music."
        return None

    @app_commands.command(name="play", description="Play a song: a title, a YouTube link or a Spotify link")
    @app_commands.describe(song="A song name, a YouTube link (video or playlist) or a Spotify link")
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(connect=True, speak=True)
    async def play(self, interaction: discord.Interaction, song: app_commands.Range[str, 2, 300]):
        if yt_dlp is None:
            return await interaction.response.send_message(
                "Music isn't installed on this bot yet. Add `yt-dlp`, `PyNaCl` and `imageio-ffmpeg` to requirements.txt.",
                ephemeral=True)
        if not ffmpeg_path():
            return await interaction.response.send_message("ffmpeg isn't available on this host, so I can't play audio.",
                                                           ephemeral=True)
        if not interaction.user.voice or not interaction.user.voice.channel:
            return await interaction.response.send_message("Join a voice channel first.", ephemeral=True)
        guild, st = interaction.guild, self.state(interaction.guild_id)
        vc = guild.voice_client
        if vc and vc.is_connected() and vc.channel != interaction.user.voice.channel:
            return await interaction.response.send_message(f"I'm in {vc.channel.mention}. Join it to queue songs.",
                                                           ephemeral=True)
        await interaction.response.defer()
        if not (vc and vc.is_connected()):
            try:
                await interaction.user.voice.channel.connect(self_deaf=True)
            except (discord.ClientException, discord.HTTPException, asyncio.TimeoutError) as e:
                return await interaction.followup.send(f"I couldn't join your voice channel: {e}. "
                                                       "(Voice needs the PyNaCl package installed.)")
        try:
            tracks = await self.find_tracks(song, interaction.user.id)
        except Exception as e:
            return await interaction.followup.send(f"Sorry, I couldn't load that: {str(e)[:300]}")
        room = MAX_QUEUE - len(st.queue)
        tracks = tracks[:max(0, room)]
        if not tracks:
            return await interaction.followup.send("The queue is full.")
        st.text_channel_id = interaction.channel_id
        st.queue.extend(tracks)
        if len(tracks) == 1:
            t = tracks[0]
            text = f"Added **{t.title}** to the queue ({fmt_duration(t.duration)})."
        else:
            text = f"Added **{len(tracks)}** songs to the queue."
        await interaction.followup.send(text, allowed_mentions=discord.AllowedMentions.none())
        await self.play_next(guild)

    # ------------------------------------------------------------------ controls

    @music.command(name="skip", description="Skip the current song")
    async def skip(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if err := self.check_voice(interaction):
            return await interaction.response.send_message(err, ephemeral=True)
        if not vc or not (vc.is_playing() or vc.is_paused()):
            return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        self.state(interaction.guild_id).skip = True
        vc.stop()
        await interaction.response.send_message("Skipped.")

    @music.command(name="pause", description="Pause the music")
    async def pause(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if err := self.check_voice(interaction):
            return await interaction.response.send_message(err, ephemeral=True)
        if not vc or not vc.is_playing():
            return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        vc.pause()
        await interaction.response.send_message("Paused.")

    @music.command(name="resume", description="Resume the music")
    async def resume(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if err := self.check_voice(interaction):
            return await interaction.response.send_message(err, ephemeral=True)
        if not vc or not vc.is_paused():
            return await interaction.response.send_message("Nothing is paused.", ephemeral=True)
        vc.resume()
        await interaction.response.send_message("Resumed.")

    @music.command(name="stop", description="Stop the music and clear the queue (I stay in the channel)")
    async def stop(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if err := self.check_voice(interaction):
            return await interaction.response.send_message(err, ephemeral=True)
        st = self.state(interaction.guild_id)
        st.queue.clear()
        st.loop, st.skip = "off", True
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
        await interaction.response.send_message("Stopped and cleared the queue.")

    @music.command(name="queue", description="See what is playing and what is next")
    async def queue(self, interaction: discord.Interaction):
        st = self.state(interaction.guild_id)
        if not st.current and not st.queue:
            return await interaction.response.send_message("The queue is empty. Use `/play`.", ephemeral=True)
        lines = []
        if st.current:
            lines.append(f"**Now:** {st.current.title} ({fmt_duration(st.current.duration)})")
        for i, t in enumerate(list(st.queue)[:10], 1):
            lines.append(f"`{i}.` {t.title}" + (f" ({fmt_duration(t.duration)})" if t.duration else ""))
        if len(st.queue) > 10:
            lines.append(f"...and {len(st.queue) - 10} more")
        embed = discord.Embed(title="Queue", description="\n".join(lines), color=discord.Color.blurple())
        embed.set_footer(text=f"Loop: {st.loop}")
        await interaction.response.send_message(embed=embed)

    @music.command(name="nowplaying", description="Show the current song")
    async def nowplaying(self, interaction: discord.Interaction):
        t = self.state(interaction.guild_id).current
        if not t:
            return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        await interaction.response.send_message(
            f"Now playing: [{t.title}]({t.web_url}) ({fmt_duration(t.duration)}), requested by <@{t.requester}>"
            if t.web_url else f"Now playing: {t.title}", allowed_mentions=discord.AllowedMentions.none())

    @music.command(name="volume", description="Set the volume from 1 to 100")
    async def volume(self, interaction: discord.Interaction, percent: app_commands.Range[int, 1, 100]):
        if err := self.check_voice(interaction):
            return await interaction.response.send_message(err, ephemeral=True)
        if not self.has_opus:
            return await interaction.response.send_message("Volume control needs the libopus library on the host.",
                                                           ephemeral=True)
        st, vc = self.state(interaction.guild_id), interaction.guild.voice_client
        st.volume = percent / 100
        if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = st.volume
        await interaction.response.send_message(f"Volume set to {percent}%.")

    @music.command(name="loop", description="Repeat the current song, the whole queue, or nothing")
    @app_commands.choices(mode=[app_commands.Choice(name="Off", value="off"), app_commands.Choice(name="Current song", value="track"),
                                app_commands.Choice(name="Whole queue", value="queue")])
    async def loop_cmd(self, interaction: discord.Interaction, mode: app_commands.Choice[str]):
        if err := self.check_voice(interaction):
            return await interaction.response.send_message(err, ephemeral=True)
        self.state(interaction.guild_id).loop = mode.value
        await interaction.response.send_message(f"Loop: **{mode.name}**.")

    @music.command(name="shuffle", description="Shuffle the queue")
    async def shuffle(self, interaction: discord.Interaction):
        if err := self.check_voice(interaction):
            return await interaction.response.send_message(err, ephemeral=True)
        st = self.state(interaction.guild_id)
        items = list(st.queue)
        random.shuffle(items)
        st.queue = deque(items)
        await interaction.response.send_message(f"Shuffled {len(items)} songs.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
