import re

import discord
from discord import app_commands
from discord.ext import commands

from premium import premium_anywhere_only

HEX_RE = re.compile(r"^[0-9a-fA-F]{6}$")
MAX_NICKNAME = 32
MAX_TITLE = 100
MAX_BIO = 300


def parse_color(text: str) -> discord.Color | None:
    text = text.strip().lstrip("#")
    if text.lower() == "random":
        return discord.Color.random()
    if HEX_RE.fullmatch(text):
        return discord.Color(int(text, 16))
    attr = getattr(discord.Color, text.lower().replace(" ", "_"), None)
    if callable(attr):
        try:
            return attr()
        except TypeError:
            return None
    return None


class CustomProfile(commands.Cog):
    """/custom-profile: premium members set a nickname I apply in every mutual server, plus a title, color and bio
    shown on a profile card. Premium granted in any one server unlocks this everywhere I'm a member."""

    group = app_commands.Group(name="custom-profile", description="Customize your nickname and profile card (premium)")

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.db.execute(
            "CREATE TABLE IF NOT EXISTS custom_profiles (user_id INTEGER PRIMARY KEY, nickname TEXT, title TEXT, "
            "color INTEGER, bio TEXT)")
        await self.bot.db.commit()

    async def get_profile(self, user_id: int):
        async with self.bot.db.execute("SELECT nickname, title, color, bio FROM custom_profiles WHERE user_id=?",
                                       (user_id,)) as cur:
            return await cur.fetchone()

    async def apply_nickname(self, member: discord.Member, nickname: str | None) -> bool | None:
        """Try to set a member's nickname in one guild. True on success, False on a permission/hierarchy problem,
        None if there was nothing to do (already that nickname, or it's the guild owner, who the API can't rename)."""
        if member.id == member.guild.owner_id:
            return None
        if member.nick == nickname or (nickname is None and member.nick is None):
            return None
        me = member.guild.me
        if not me.guild_permissions.manage_nicknames or member.top_role >= me.top_role:
            return False
        try:
            await member.edit(nick=nickname, reason="/custom-profile")
            return True
        except discord.HTTPException:
            return False

    async def apply_everywhere(self, user: discord.abc.User, nickname: str | None) -> tuple[int, int]:
        """Apply (or clear) the nickname in every mutual guild. Returns (applied, skipped)."""
        applied = skipped = 0
        for guild in self.bot.guilds:
            member = guild.get_member(user.id)
            if member is None:
                continue
            result = await self.apply_nickname(member, nickname)
            if result is True:
                applied += 1
            elif result is False:
                skipped += 1
        return applied, skipped

    @group.command(name="set", description="Set your nickname (applied everywhere I'm a member), title, color and bio")
    @app_commands.describe(
        nickname=f"Your nickname in every server I'm in with you (max {MAX_NICKNAME} characters, leave out to keep it)",
        title="A short title shown on your profile card (leave out to keep it)",
        color="Hex like ffcc00, a color name like gold, or random (leave out to keep it)",
        bio=f"A short bio shown on your profile card (max {MAX_BIO} characters, leave out to keep it)")
    @premium_anywhere_only()
    async def set_profile(self, interaction: discord.Interaction, nickname: app_commands.Range[str, 1, MAX_NICKNAME] | None = None,
                          title: app_commands.Range[str, 1, MAX_TITLE] | None = None, color: str | None = None,
                          bio: app_commands.Range[str, 0, MAX_BIO] | None = None):
        col = None
        if color:
            col = parse_color(color)
            if col is None:
                return await interaction.response.send_message(
                    "I couldn't understand that color. Try a hex code like `ffcc00`, a name like `gold`, or `random`.",
                    ephemeral=True)

        old = await self.get_profile(interaction.user.id)
        new_nick = nickname if nickname is not None else (old[0] if old else None)
        new_title = title if title is not None else (old[1] if old else None)
        new_color = col.value if col is not None else (old[2] if old else None)
        new_bio = bio.replace("\\n", "\n") if bio is not None else (old[3] if old else None)

        await self.bot.db.execute(
            "INSERT INTO custom_profiles (user_id, nickname, title, color, bio) VALUES (?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET nickname=excluded.nickname, title=excluded.title, "
            "color=excluded.color, bio=excluded.bio",
            (interaction.user.id, new_nick, new_title, new_color, new_bio))
        await self.bot.db.commit()

        text = "Saved your custom profile."
        if nickname is not None:
            await interaction.response.defer(ephemeral=True)
            applied, skipped = await self.apply_everywhere(interaction.user, new_nick)
            text += f" Nickname applied in {applied} server(s)."
            if skipped:
                text += f" Couldn't change it in {skipped} server(s) (missing permission or role order there)."
            return await interaction.followup.send(text, ephemeral=True)
        await interaction.response.send_message(text, ephemeral=True)

    @group.command(name="clear", description="Clear your custom nickname, title, color and bio")
    @premium_anywhere_only()
    async def clear_profile(self, interaction: discord.Interaction):
        await self.bot.db.execute("DELETE FROM custom_profiles WHERE user_id=?", (interaction.user.id,))
        await self.bot.db.commit()
        await interaction.response.defer(ephemeral=True)
        applied, skipped = await self.apply_everywhere(interaction.user, None)
        text = f"Cleared. Nickname reset in {applied} server(s)."
        if skipped:
            text += f" Couldn't reset it in {skipped} server(s) (missing permission or role order there)."
        await interaction.followup.send(text, ephemeral=True)

    @group.command(name="show", description="Show a member's custom profile card")
    async def show_profile(self, interaction: discord.Interaction, member: discord.Member | None = None):
        member = member or interaction.user
        row = await self.get_profile(member.id)
        if not row or not any(row):
            return await interaction.response.send_message(f"{member.display_name} hasn't set a custom profile.",
                                                           ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        nickname, title, color, bio = row
        embed = discord.Embed(color=discord.Color(color) if color else discord.Color.blurple())
        name = member.display_name + (f" — {title}" if title else "")
        embed.set_author(name=name, icon_url=member.display_avatar.url)
        if bio:
            embed.description = bio
        embed.set_footer(text="Custom profile" + (" • nickname applies in every server I'm in with them" if nickname else ""))
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """A premium member joining a new mutual server gets their saved nickname automatically, where possible."""
        from premium import has_premium_anywhere
        row = await self.get_profile(member.id)
        if not row or not row[0]:
            return
        if not await has_premium_anywhere(self.bot, member.id):
            return
        await self.apply_nickname(member, row[0])


async def setup(bot: commands.Bot):
    await bot.add_cog(CustomProfile(bot))
