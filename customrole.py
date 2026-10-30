import asyncio
import io
import re

import discord
from discord import app_commands
from discord.ext import commands

from greetings import read_attachment, read_image_url  # same image rules as /welcome
from wordfilter import normalize  # so role names obey the word filter too

try:
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = 25_000_000  # refuse huge images instead of eating all the memory
except ImportError:  # Pillow missing: icons are then sent exactly as uploaded
    Image = ImageOps = None

MAX_NAME_LENGTH = 32
CREW_DEFAULT = 3  # friends a member can share their custom role with (staff can change it with /role crewsize)
ICON_SIZE = 128  # role icons are shown tiny, so a 128x128 PNG is plenty and far below Discord's 256 KB limit


def to_icon_png(raw: bytes) -> bytes:
    """Turn any picture (PNG, JPG, GIF, WebP, any shape or size) into the square PNG Discord accepts."""
    if Image is None:
        return raw
    try:
        with Image.open(io.BytesIO(raw)) as img:
            if img.width * img.height > Image.MAX_IMAGE_PIXELS:  # Pillow itself only refuses at double this
                raise ValueError("too many pixels")
            img.seek(0)  # animated GIF/WebP: use the first frame
            img = ImageOps.exif_transpose(img).convert("RGBA")
            side = min(img.size)  # crop the middle square so nothing gets stretched
            left, top = (img.width - side) // 2, (img.height - side) // 2
            img = img.crop((left, top, left + side, top + side)).resize((ICON_SIZE, ICON_SIZE), Image.LANCZOS)
            out = io.BytesIO()
            img.save(out, format="PNG", optimize=True)
            return out.getvalue()
    except Exception:
        raise ValueError("I couldn't read that image. Try a normal PNG or JPG.")
HEX_RE = re.compile(r"^(?:#|0x)?([0-9a-fA-F]{6})$")
CUSTOM_EMOJI_RE = re.compile(r"^<a?:\w{2,32}:(\d{17,20})>$")  # what Discord pastes for a custom emoji
EMOJI_NAME_RE = re.compile(r"^:(\w{2,32}):$")                  # :name: of one of THIS server's emojis


def custom_emoji_id(text: str, guild_emojis=()) -> int | None:
    """The id of a custom emoji given as <:name:id>, <a:name:id>, or :name: (a name from this server)."""
    m = CUSTOM_EMOJI_RE.match(text)
    if m:
        return int(m.group(1))
    m = EMOJI_NAME_RE.match(text)
    if m:
        for e in guild_emojis:
            if e.name.lower() == m.group(1).lower():
                return e.id
    return None


# Ready-made colors, so nobody has to know a hex code. Discord lists at most 25 options in a dropdown.
COLORS = {
    "Red": 0xE74C3C, "Dark Red": 0x992D22, "Orange": 0xE67E22, "Peach": 0xFFB38A, "Gold": 0xF1C40F,
    "Yellow": 0xFFEB3B, "Lime": 0xA3E635, "Green": 0x2ECC71, "Dark Green": 0x1F8B4C, "Teal": 0x1ABC9C,
    "Cyan": 0x22D3EE, "Sky Blue": 0x3498DB, "Blue": 0x2563EB, "Dark Blue": 0x1F3A93, "Purple": 0x9B59B6,
    "Violet": 0x7C3AED, "Magenta": 0xE91E8C, "Pink": 0xFF66AA, "Hot Pink": 0xFF1493, "Rose": 0xFB7185,
    "Brown": 0x8B5A2B, "White": 0xFFFFFF, "Grey": 0x95A5A6, "Dark Grey": 0x23272A,
}
COLOR_LOOKUP = {name.lower().replace(" ", ""): value for name, value in COLORS.items()}


def parse_color(text: str) -> int | None:
    """A color name ('hot pink', 'Sky-Blue') or a hex code ('#ff66aa'). None if it is neither."""
    text = text.strip()
    key = re.sub(r"[\s_-]+", "", text.lower())
    if key in COLOR_LOOKUP:
        return COLOR_LOOKUP[key]
    match = HEX_RE.match(text)
    return int(match.group(1), 16) if match else None


class RoleForm(discord.ui.Modal, title="Create your custom role"):
    """The pop-up form opened by the panel buttons."""

    name = discord.ui.TextInput(label="Role name", placeholder="Pick a name", min_length=1, max_length=MAX_NAME_LENGTH)
    color = discord.ui.TextInput(label="Color (optional)", placeholder="pink, sky blue, or a hex code like #ff66aa",
                                 required=False, max_length=20)

    def __init__(self, cog: "CustomRoles", current: discord.Role | None):
        super().__init__()
        self.cog = cog
        if current is not None:  # editing: start from what they have now
            self.name.default = current.name
            if current.colour.value:
                self.color.default = f"#{current.colour.value:06x}"

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.apply(interaction, str(self.name.value), str(self.color.value).strip() or None)


class ColorSelect(discord.ui.Select):
    def __init__(self, cog: "CustomRoles"):
        super().__init__(placeholder="Pick a color", min_values=1, max_values=1,
                         options=[discord.SelectOption(label=name, value=name, description=f"#{value:06x}")
                                  for name, value in COLORS.items()])
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        await self.cog.set_color(interaction, self.values[0])


class ColorMenu(discord.ui.View):
    """A color dropdown (shown after creating a role, and by the Change Role button)."""

    def __init__(self, cog: "CustomRoles", show_rename: bool):
        super().__init__(timeout=180)
        self.cog = cog
        self.add_item(ColorSelect(cog))
        if show_rename:
            button = discord.ui.Button(label="Rename or type a color", style=discord.ButtonStyle.secondary)
            button.callback = self.rename
            self.add_item(button)

    async def rename(self, interaction: discord.Interaction):
        own_id = self.cog.owned.get((interaction.guild_id, interaction.user.id))
        own = interaction.guild.get_role(own_id) if own_id else None
        await interaction.response.send_modal(RoleForm(self.cog, own))


class IconForm(discord.ui.Modal, title="Role icon"):
    """Pop-up for the Role Icon button. Leave both boxes empty to remove the icon."""

    emoji = discord.ui.TextInput(label="Emoji (leave empty to remove the icon)", required=False, max_length=64,
                                 placeholder="A normal emoji, or a custom emoji from any server")
    url = discord.ui.TextInput(label="Or an image link", required=False, max_length=500,
                               placeholder="Paste an image link, e.g. a Discord CDN link")

    def __init__(self, cog: "CustomRoles"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.set_icon(interaction, str(self.emoji.value), None, str(self.url.value).strip() or None)


class RolePanel(discord.ui.View):
    """The two buttons under the panel message. Persistent: they keep working after the bot restarts."""

    def __init__(self, cog: "CustomRoles"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Create Role", style=discord.ButtonStyle.primary, custom_id="role:create")
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.panel_click(interaction, "create")

    @discord.ui.button(label="Change Role", style=discord.ButtonStyle.secondary, custom_id="role:change")
    async def change(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.panel_click(interaction, "change")

    @discord.ui.button(label="Role Icon", style=discord.ButtonStyle.secondary, custom_id="role:icon")
    async def icon(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.panel_click(interaction, "icon")

    @discord.ui.button(label="Share Role", style=discord.ButtonStyle.secondary, custom_id="role:share")
    async def share(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.panel_click(interaction, "share")

    @discord.ui.button(label="Gallery", style=discord.ButtonStyle.success, custom_id="role:gallery")
    async def gallery(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.open_gallery(interaction)  # open to everyone, not only boosters


class CrewMenu(discord.ui.View):
    """Opened by the Share Role button: pick a friend to give your role to, or to take it back from."""

    def __init__(self, cog: "CustomRoles"):
        super().__init__(timeout=180)
        self.cog = cog

    async def friend(self, interaction: discord.Interaction, picked) -> discord.Member | None:
        member = interaction.guild.get_member(picked.id)
        if member is None:
            await interaction.response.send_message("I can't find that member in this server.", ephemeral=True)
        return member

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Pick a friend to share your role with",
                       min_values=1, max_values=1)
    async def add_friend(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        member = await self.friend(interaction, select.values[0])
        if member is not None:
            await self.cog.do_share(interaction, member)

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Pick a friend to take your role back from",
                       min_values=1, max_values=1)
    async def remove_friend(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        member = await self.friend(interaction, select.values[0])
        if member is not None:
            await self.cog.do_unshare(interaction, member)


class OldRolePanel(discord.ui.View):
    """Keeps panels posted before the buttons were renamed from failing; they just ask for a new panel."""

    def __init__(self):
        super().__init__(timeout=None)

    async def outdated(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "This panel is out of date. Ask the staff to post a new one with `/role setup`.", ephemeral=True)

    @discord.ui.button(label="Booster", style=discord.ButtonStyle.primary, custom_id="role:booster")
    async def booster(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.outdated(interaction)

    @discord.ui.button(label="Selected role", style=discord.ButtonStyle.secondary, custom_id="role:selected")
    async def selected(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.outdated(interaction)


def rank_entries(entries: list[dict]) -> list[dict]:
    """Most liked first; roles with the same number of likes are listed alphabetically."""
    return sorted(entries, key=lambda e: (-e["likes"], e["name"].lower()))


class GalleryView(discord.ui.View):
    """Browse everyone's custom roles and like them. Shown privately to whoever opened it."""

    def __init__(self, cog: "CustomRoles", guild: discord.Guild, viewer: discord.Member):
        super().__init__(timeout=300)
        self.cog, self.guild, self.viewer = cog, guild, viewer
        self.index = 0
        self.focus: int | None = None   # owner id to keep on screen when the ranking changes after a like
        self.entries: list[dict] = []

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.viewer.id

    async def refresh(self) -> discord.Embed:
        self.entries = await self.cog.gallery_entries(self.guild)
        if self.focus is not None:
            for i, e in enumerate(self.entries):
                if e["owner_id"] == self.focus:
                    self.index = i
        self.index = max(0, min(self.index, len(self.entries) - 1))
        entry = self.entries[self.index] if self.entries else None
        self.focus = entry["owner_id"] if entry else None
        self.prev_button.disabled = self.index <= 0
        self.next_button.disabled = self.index >= len(self.entries) - 1
        self.like_button.disabled = entry is None or entry["owner_id"] == self.viewer.id
        if entry:
            verb = "Unlike" if self.viewer.id in entry["likers"] else "Like"
            self.like_button.label = f"{verb} ({entry['likes']})"
        return self.cog.gallery_embed(entry, self.index, len(self.entries))

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index, self.focus = self.index - 1, None
        await interaction.response.edit_message(embed=await self.refresh(), view=self)

    @discord.ui.button(label="Like", style=discord.ButtonStyle.primary)
    async def like_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        entry = self.entries[self.index] if self.entries else None
        if entry and entry["owner_id"] != self.viewer.id:
            db, gid, owner = self.cog.bot.db, self.guild.id, entry["owner_id"]
            cur = await db.execute("DELETE FROM role_likes WHERE guild_id=? AND role_owner_id=? AND voter_id=?",
                                   (gid, owner, self.viewer.id))
            if not cur.rowcount:  # not liked yet, so this press is a like
                await db.execute("INSERT OR IGNORE INTO role_likes (guild_id, role_owner_id, voter_id) VALUES (?,?,?)",
                                 (gid, owner, self.viewer.id))
            await db.commit()
        await interaction.response.edit_message(embed=await self.refresh(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index, self.focus = self.index + 1, None
        await interaction.response.edit_message(embed=await self.refresh(), view=self)


class CustomRoles(commands.GroupCog, group_name="role", group_description="Custom roles for boosters and selected roles"):
    """Eligible members make ONE cosmetic role (a name and a color, no permissions) that is given to them.

    Eligible means: boosting the server (can be switched off), having a role added with /role allow, or administrator.
    The role is deleted again if they stop being eligible or leave the server.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.owned: dict[tuple[int, int], int] = {}   # (guild_id, user_id) -> role_id
        self.access: dict[int, set[int]] = {}          # guild_id -> role ids that may use /role custom
        self.settings: dict[int, dict] = {}            # guild_id -> {"anchor": role_id | None, "boosters": bool}

    async def cog_load(self):
        db = self.bot.db
        await db.execute("CREATE TABLE IF NOT EXISTS custom_roles (guild_id INTEGER, user_id INTEGER, role_id INTEGER, "
                         "PRIMARY KEY (guild_id, user_id))")
        await db.execute("CREATE TABLE IF NOT EXISTS custom_role_access (guild_id INTEGER, role_id INTEGER, "
                         "PRIMARY KEY (guild_id, role_id))")
        await db.execute("CREATE TABLE IF NOT EXISTS custom_role_settings (guild_id INTEGER PRIMARY KEY, "
                         "anchor_role_id INTEGER, boosters INTEGER DEFAULT 1, anchor_side TEXT DEFAULT 'below', "
                         f"crew_max INTEGER DEFAULT {CREW_DEFAULT})")
        for column in ("anchor_side TEXT DEFAULT 'below'", f"crew_max INTEGER DEFAULT {CREW_DEFAULT}"):
            try:  # databases made before these columns existed need them added
                await db.execute(f"ALTER TABLE custom_role_settings ADD COLUMN {column}")
            except Exception:
                pass  # the column is already there
        await db.execute("CREATE TABLE IF NOT EXISTS role_crew (guild_id INTEGER, owner_id INTEGER, member_id INTEGER, "
                         "PRIMARY KEY (guild_id, owner_id, member_id))")
        await db.execute("CREATE TABLE IF NOT EXISTS role_likes (guild_id INTEGER, role_owner_id INTEGER, "
                         "voter_id INTEGER, PRIMARY KEY (guild_id, role_owner_id, voter_id))")
        await db.commit()
        async with db.execute("SELECT guild_id, user_id, role_id FROM custom_roles") as cur:
            for guild_id, user_id, role_id in await cur.fetchall():
                self.owned[(guild_id, user_id)] = role_id
        async with db.execute("SELECT guild_id, role_id FROM custom_role_access") as cur:
            for guild_id, role_id in await cur.fetchall():
                self.access.setdefault(guild_id, set()).add(role_id)
        async with db.execute("SELECT guild_id, anchor_role_id, boosters, anchor_side, crew_max "
                              "FROM custom_role_settings") as cur:
            for guild_id, anchor, boosters, side, crew_max in await cur.fetchall():
                self.settings[guild_id] = {"anchor": anchor, "boosters": bool(boosters), "side": side or "below",
                                           "crew_max": CREW_DEFAULT if crew_max is None else crew_max}
        self.bot.add_view(RolePanel(self))
        self.bot.add_view(OldRolePanel())

    # ---------- helpers ----------

    def cfg(self, guild_id: int) -> dict:
        return self.settings.get(guild_id, {"anchor": None, "boosters": True, "side": "below",
                                            "crew_max": CREW_DEFAULT})

    def eligible(self, member: discord.Member) -> bool:
        if member.guild_permissions.administrator:
            return True
        if self.cfg(member.guild.id)["boosters"] and member.premium_since is not None:
            return True
        allowed = self.access.get(member.guild.id, set())
        return any(r.id in allowed for r in member.roles)

    async def save_settings(self, guild_id: int, **changes):
        cfg = dict(self.cfg(guild_id), **changes)
        await self.bot.db.execute(
            "INSERT OR REPLACE INTO custom_role_settings (guild_id, anchor_role_id, boosters, anchor_side, crew_max) "
            "VALUES (?,?,?,?,?)", (guild_id, cfg["anchor"], int(cfg["boosters"]), cfg["side"], cfg["crew_max"]))
        await self.bot.db.commit()
        self.settings[guild_id] = cfg

    async def forget(self, guild_id: int, user_id: int):
        self.owned.pop((guild_id, user_id), None)
        await self.bot.db.execute("DELETE FROM custom_roles WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        # the role is gone, so its crew and its likes go with it
        await self.bot.db.execute("DELETE FROM role_crew WHERE guild_id=? AND owner_id=?", (guild_id, user_id))
        await self.bot.db.execute("DELETE FROM role_likes WHERE guild_id=? AND role_owner_id=?", (guild_id, user_id))
        await self.bot.db.commit()

    async def delete_custom(self, guild: discord.Guild, user_id: int, reason: str):
        role_id = self.owned.get((guild.id, user_id))
        role = guild.get_role(role_id) if role_id else None
        if role is not None:
            try:
                await role.delete(reason=reason)
            except discord.HTTPException:
                pass
        await self.forget(guild.id, user_id)

    # ---------- /role custom ----------

    @app_commands.command(name="custom", description="Create or change your own custom role (name and color only)")
    @app_commands.describe(name="The role name", color="A color name like pink or sky blue, or a hex code (optional)")
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def custom(self, interaction: discord.Interaction, name: str, color: str | None = None):
        await self.apply(interaction, name, color)

    async def apply(self, interaction: discord.Interaction, name: str, color: str | None):
        """Create or update the member's role. Shared by /role custom and the panel's pop-up form."""
        member, guild = interaction.user, interaction.guild
        if not self.eligible(member):
            return await interaction.response.send_message(
                "Custom roles are for server boosters and members with a selected role. "
                "Ask the staff if you think you should have access.", ephemeral=True)
        if not guild.me.guild_permissions.manage_roles:
            return await interaction.response.send_message(
                "I need the Manage Roles permission to do that. Ask an admin to give it to me.", ephemeral=True)

        name = " ".join(name.split())
        if not name or len(name) > MAX_NAME_LENGTH:
            return await interaction.response.send_message(
                f"The role name must be between 1 and {MAX_NAME_LENGTH} characters.", ephemeral=True)
        if "@" in name:
            return await interaction.response.send_message("The role name can't contain the @ symbol.", ephemeral=True)
        wf = self.bot.get_cog("WordFilter")
        if wf is not None:
            pattern = wf.regex.get(guild.id)
            if pattern is not None and pattern.search(normalize(name)):
                return await interaction.response.send_message("That name isn't allowed here. Pick another one.",
                                                               ephemeral=True)

        value = None
        if color:
            value = parse_color(color)
            if value is None:
                return await interaction.response.send_message(
                    "I don't know that color. Use a name like `pink` or `sky blue`, a hex code like `#ff66aa`, "
                    "or leave it empty and pick one from the dropdown afterwards.", ephemeral=True)

        own_id = self.owned.get((guild.id, member.id))
        own = guild.get_role(own_id) if own_id else None
        if any(r.name.lower() == name.lower() and (own is None or r.id != own.id) for r in guild.roles):
            return await interaction.response.send_message(
                "A role with that name already exists. Pick a different name.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        try:
            if own is not None:
                changes = {"name": name}
                if value is not None:
                    changes["colour"] = discord.Colour(value)
                await own.edit(**changes, reason=f"Custom role updated by {member}")
                return await interaction.followup.send(f"Updated your role: {own.mention}", ephemeral=True,
                                                       allowed_mentions=discord.AllowedMentions.none())
            role = await self.create_for(member, name, value)
        except discord.Forbidden:
            return await interaction.followup.send(
                "I don't have permission to do that. Ask an admin to give me Manage Roles and move my role higher.",
                ephemeral=True)
        except discord.HTTPException:
            return await interaction.followup.send("Discord refused that. The server may be at its role limit.",
                                                   ephemeral=True)
        extra = {}
        text = f"Your custom role is ready and has been given to you: {role.mention}"
        if value is None:  # no color was chosen yet, so offer the dropdown
            text += "\nPick a color for it below, or ignore this to keep it plain."
            extra["view"] = ColorMenu(self, show_rename=False)
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none(), **extra)
        await self.bot.log(guild, "Custom role created", f"{member} created the role **{discord.utils.escape_markdown(name)}**")

    async def place(self, guild: discord.Guild, role: discord.Role) -> bool:
        """Move a custom role to where /role position says: the very bottom, or just above/below a chosen role.

        Uses fresh positions from Discord, because moving one role shifts the others. Returns True if it moved.
        """
        cfg = self.cfg(guild.id)
        fresh = {r.id: r for r in await guild.fetch_roles()}
        current = fresh[role.id].position if role.id in fresh else role.position
        anchor = fresh.get(cfg["anchor"]) if cfg["anchor"] else None
        if anchor is None:
            target = 1
        else:
            a = anchor.position
            if cfg["side"] == "above":
                target = a + 1 if current > a else a   # moving down or up changes where the gap opens
            else:
                target = a if current > a else a - 1
            top = fresh[guild.me.top_role.id].position if guild.me.top_role.id in fresh else guild.me.top_role.position
            target = max(1, min(target, top - 1))      # I can only place roles below my own top role
        if target == current:
            return False
        await role.edit(position=target, reason="Custom role placement")
        return True

    async def create_for(self, member: discord.Member, name: str, value: int | None) -> discord.Role:
        guild = member.guild
        role = await guild.create_role(
            name=name, colour=discord.Colour(value or 0), permissions=discord.Permissions.none(),
            hoist=False, mentionable=False, reason=f"Custom role for {member}")
        try:
            await self.place(guild, role)
        except discord.HTTPException:
            pass  # the role still works, it just stays at the bottom of the list
        try:
            await member.add_roles(role, reason="Custom role")
        except discord.HTTPException:
            await role.delete(reason="Could not give the custom role to the member")
            raise
        self.owned[(guild.id, member.id)] = role.id
        await self.bot.db.execute("INSERT OR REPLACE INTO custom_roles (guild_id, user_id, role_id) VALUES (?,?,?)",
                                  (guild.id, member.id, role.id))
        await self.bot.db.commit()
        return role

    # ---------- /role setup and the panel buttons ----------

    @app_commands.command(name="setup", description="Post the custom role panel (Booster and Selected role buttons)")
    @app_commands.describe(channel="Where to post the panel",
                           selected_role="Members with this role can use the Selected role button "
                                         "(add more later with /role allow)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def setup_cmd(self, interaction: discord.Interaction, channel: discord.TextChannel,
                        selected_role: discord.Role | None = None):
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return await interaction.response.send_message(
                f"I need View Channel, Send Messages and Embed Links in {channel.mention}.", ephemeral=True)
        if selected_role is not None:
            if selected_role.is_default():
                return await interaction.response.send_message("Pick a specific role, not @everyone.", ephemeral=True)
            await self.bot.db.execute("INSERT OR IGNORE INTO custom_role_access (guild_id, role_id) VALUES (?,?)",
                                      (interaction.guild_id, selected_role.id))
            await self.bot.db.commit()
            self.access.setdefault(interaction.guild_id, set()).add(selected_role.id)

        try:
            await channel.send(embed=self.panel_embed(interaction.guild), view=RolePanel(self))
        except discord.HTTPException:
            return await interaction.response.send_message("I couldn't post in that channel.", ephemeral=True)
        note = ""
        if not self.access.get(interaction.guild_id):
            note = " The Selected role button won't work until you pick a role with `/role allow` or `selected_role`."
        await interaction.response.send_message(f"Panel posted in {channel.mention}.{note}", ephemeral=True)

    def allowed_roles(self, guild: discord.Guild) -> list[discord.Role]:
        roles = [guild.get_role(r) for r in self.access.get(guild.id, set())]
        return sorted((r for r in roles if r is not None), key=lambda r: -r.position)

    def panel_embed(self, guild: discord.Guild) -> discord.Embed:
        lines = []
        if self.cfg(guild.id)["boosters"]:
            lines.append("**Booster**: for members who boost this server.")
        for role in self.allowed_roles(guild):
            name = discord.utils.escape_markdown(role.name)
            lines.append(f"**{name} role**: for members who have the {name} role.")
        return discord.Embed(
            title="Create your custom role",
            description="Make your own role with a name and a color of your choice. It has no permissions, "
                        "it only shows next to your name.\n\n" + "\n".join(lines)
                        + "\n\nPress **Create Role** to make your role, then pick its color from a list. "
                          "Press **Change Role** to pick a new color or rename it, or **Role Icon** to add a "
                          "small emoji or image next to it (the server needs Boost Level 2 for icons). "
                          "**Share Role** gives your role to a few friends, and **Gallery** lets anyone browse "
                          "all custom roles and like their favorites.",
            color=discord.Color(0xF47FFF))

    def who_can_text(self, guild: discord.Guild) -> str:
        groups = []
        if self.cfg(guild.id)["boosters"]:
            groups.append("server boosters")
        groups += [f"members with the {discord.utils.escape_markdown(r.name)} role" for r in self.allowed_roles(guild)]
        return " and ".join([", ".join(groups[:-1]), groups[-1]] if len(groups) > 1 else groups) or "nobody yet"

    async def panel_click(self, interaction: discord.Interaction, action: str):
        member, guild = interaction.user, interaction.guild
        allowed = self.access.get(guild.id, set())
        is_booster = self.cfg(guild.id)["boosters"] and member.premium_since is not None
        if not (is_booster or any(r.id in allowed for r in member.roles)):
            return await interaction.response.send_message(
                f"This is only for {self.who_can_text(guild)}.", ephemeral=True)

        own_id = self.owned.get((guild.id, member.id))
        own = guild.get_role(own_id) if own_id else None
        if action == "create" and own is not None:
            return await interaction.response.send_message(
                f"You already have a custom role: {own.mention}. Press **Change Role** to rename or recolor it.",
                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        if action in ("change", "icon", "share") and own is None:
            return await interaction.response.send_message(
                "You don't have a custom role yet. Press **Create Role** first.", ephemeral=True)
        if action == "share":
            crew, limit = await self.crew_ids(guild.id, member.id), self.cfg(guild.id)["crew_max"]
            if limit <= 0:
                return await interaction.response.send_message("Sharing roles is turned off on this server.",
                                                               ephemeral=True)
            names = ", ".join(f"<@{m}>" for m in crew) or "nobody yet"
            return await interaction.response.send_message(
                f"Your crew ({len(crew)}/{limit}): {names}\nUse the top menu to give your role to a friend, "
                "or the bottom menu to take it back from someone.", view=CrewMenu(self), ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none())
        if action == "icon":
            return await interaction.response.send_modal(IconForm(self))
        if action == "change":
            return await interaction.response.send_message(
                "What would you like to change? Pick a new color from the dropdown, or press the button "
                "to rename your role or type your own color.", view=ColorMenu(self, show_rename=True), ephemeral=True)
        await interaction.response.send_modal(RoleForm(self, None))

    async def set_color(self, interaction: discord.Interaction, color_name: str):
        """Called when someone picks a color in the dropdown."""
        guild, member = interaction.guild, interaction.user
        own_id = self.owned.get((guild.id, member.id))
        own = guild.get_role(own_id) if own_id else None
        if own is None or not self.eligible(member):
            return await interaction.response.edit_message(content="You don't have a custom role right now.", view=None)
        try:
            await own.edit(colour=discord.Colour(COLORS[color_name]), reason=f"Color changed by {member}")
        except discord.HTTPException:
            return await interaction.response.edit_message(
                content="I couldn't change the color. Ask an admin to move my role higher.", view=None)
        await interaction.response.edit_message(content=f"Your role color is now **{color_name}**.", view=None)

    @custom.autocomplete("color")
    async def color_autocomplete(self, interaction: discord.Interaction, current: str):
        key = current.lower().replace(" ", "")
        names = [n for n in COLORS if key in n.lower().replace(" ", "")]
        return [app_commands.Choice(name=n, value=n) for n in names[:25]]

    # ---------- role icon ----------

    @app_commands.command(name="icon", description="Put an emoji or image next to your custom role (leave empty to remove)")
    @app_commands.describe(emoji="An emoji to use as the icon (normal, or a custom emoji from any server)",
                           image="Or upload an image (any picture works)",
                           image_url="Or paste an image link (a Discord CDN link works)")
    @app_commands.guild_only()
    async def icon_cmd(self, interaction: discord.Interaction, emoji: str | None = None,
                       image: discord.Attachment | None = None, image_url: str | None = None):
        await self.set_icon(interaction, emoji, image, image_url)

    async def set_icon(self, interaction: discord.Interaction, emoji: str | None,
                       image: discord.Attachment | None, url: str | None):
        """Shared by /role icon and the Role Icon button. Nothing given at all means remove the icon."""
        member, guild = interaction.user, interaction.guild
        own_id = self.owned.get((guild.id, member.id))
        own = guild.get_role(own_id) if own_id else None
        if not self.eligible(member):
            return await interaction.response.send_message(
                "Custom roles are for server boosters and members with a selected role.", ephemeral=True)
        if own is None:
            return await interaction.response.send_message(
                "You don't have a custom role yet. Create one first.", ephemeral=True)
        if not guild.me.guild_permissions.manage_roles:
            return await interaction.response.send_message(
                "I need the Manage Roles permission to do that.", ephemeral=True)
        emoji = (emoji or "").strip() or None
        url = (url or "").strip() or None
        if sum(bool(x) for x in (emoji, image, url)) > 1:
            return await interaction.response.send_message(
                "Use only one: an emoji, an image upload or an image link.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        value = None  # None removes the icon
        try:
            if emoji:
                emoji_id = custom_emoji_id(emoji, guild.emojis)
                if emoji_id:
                    # Discord only allows normal emoji as an icon, so use the custom emoji's picture instead
                    try:
                        raw, _ext = await read_image_url(f"https://cdn.discordapp.com/emojis/{emoji_id}.png?size=128")
                    except ValueError:
                        raise ValueError("I couldn't load that custom emoji. It may have been deleted.")
                    value = await asyncio.to_thread(to_icon_png, raw)
                elif emoji.isascii():
                    raise ValueError("That isn't an emoji. Paste a single emoji (normal or custom), "
                                     "or use an image instead.")
                else:
                    value = emoji
            elif image is not None or url:
                raw, _ext = await read_attachment(image) if image is not None else await read_image_url(url)
                value = await asyncio.to_thread(to_icon_png, raw)  # square 128x128 PNG, whatever was sent
        except ValueError as e:
            return await interaction.followup.send(str(e), ephemeral=True)
        try:
            await own.edit(display_icon=value, reason=f"Role icon changed by {member}")
        except discord.Forbidden as e:
            print(f"role icon forbidden in {guild.id}: status={e.status} code={e.code} text={e.text}")
            return await interaction.followup.send(
                f"Discord said I'm not allowed to edit that role ({e.text or 'no details'}). "
                "Ask an admin to check that my role is above your custom role and has Manage Roles.", ephemeral=True)
        except discord.HTTPException as e:
            print(f"role icon failed in {guild.id}: status={e.status} code={e.code} text={e.text}")
            hint = ""
            if "ROLE_ICONS" not in guild.features and guild.premium_tier < 2:
                hint = " This server doesn't look like it has role icons unlocked (Boost Level 2)."
            return await interaction.followup.send(
                f"Discord refused the icon: {e.text or 'no details'}.{hint} "
                "If you used an image, try a different one, or use an emoji instead.", ephemeral=True)
        await interaction.followup.send("Removed your role icon." if value is None else "Your role icon was updated.",
                                        ephemeral=True)

    # ---------- /role remove ----------

    @app_commands.command(name="remove", description="Delete your custom role")
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def remove(self, interaction: discord.Interaction):
        if (interaction.guild_id, interaction.user.id) not in self.owned:
            return await interaction.response.send_message("You don't have a custom role.", ephemeral=True)
        await self.delete_custom(interaction.guild, interaction.user.id, f"Removed by {interaction.user}")
        await interaction.response.send_message("Your custom role was deleted.", ephemeral=True)

    # ---------- admin settings ----------

    @app_commands.command(name="allow", description="Let members with this role make a custom role")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def allow(self, interaction: discord.Interaction, role: discord.Role):
        if role.is_default():
            return await interaction.response.send_message(
                "That would allow everyone. Pick a specific role.", ephemeral=True)
        await self.bot.db.execute("INSERT OR IGNORE INTO custom_role_access (guild_id, role_id) VALUES (?,?)",
                                  (interaction.guild_id, role.id))
        await self.bot.db.commit()
        self.access.setdefault(interaction.guild_id, set()).add(role.id)
        await interaction.response.send_message(
            f"Members with {role.mention} can now make a custom role. "
            "Run `/role setup` again to post a panel that lists this role.",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="disallow", description="Stop letting a role make custom roles")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def disallow(self, interaction: discord.Interaction, role: discord.Role):
        await self.bot.db.execute("DELETE FROM custom_role_access WHERE guild_id=? AND role_id=?",
                                  (interaction.guild_id, role.id))
        await self.bot.db.commit()
        self.access.get(interaction.guild_id, set()).discard(role.id)
        await interaction.response.send_message(
            f"{role.mention} can no longer be used for custom roles. Existing custom roles are removed the next "
            "time that member's roles change.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="boosters", description="Choose whether server boosters can make a custom role")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def boosters(self, interaction: discord.Interaction, enabled: bool):
        await self.save_settings(interaction.guild_id, boosters=enabled)
        await interaction.response.send_message(
            "Server boosters can now make a custom role." if enabled else
            "Server boosters can no longer make a custom role (only the allowed roles can).", ephemeral=True)

    @app_commands.command(name="position", description="Choose where custom roles sit (leave empty for the very bottom)")
    @app_commands.describe(role="The role to place custom roles next to. Leave empty to put them at the bottom",
                           placement="Above or below that role (default: below)")
    @app_commands.choices(placement=[app_commands.Choice(name="Below the role", value="below"),
                                     app_commands.Choice(name="Above the role", value="above")])
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def position(self, interaction: discord.Interaction, role: discord.Role | None = None,
                       placement: app_commands.Choice[str] | None = None):
        guild = interaction.guild
        side = placement.value if placement else "below"
        if role is not None:
            top = guild.me.top_role
            if role.position > top.position or (side == "above" and role.position >= top.position):
                return await interaction.response.send_message(
                    "I can only place roles below my own highest role. Pick a lower role, or move my role up.",
                    ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        await self.save_settings(guild.id, anchor=role.id if role else None, side=side)

        moved = 0
        for (gid, uid), role_id in list(self.owned.items()):
            existing = guild.get_role(role_id) if gid == guild.id else None
            if existing is None:
                continue
            try:
                if await self.place(guild, existing):
                    moved += 1
            except discord.HTTPException:
                pass
        where = f"just {side} {role.mention}" if role else "at the very bottom of the role list"
        await interaction.followup.send(
            f"Custom roles now go {where}." + (f" Moved {moved} existing role{'s' if moved != 1 else ''}." if moved else ""),
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="status", description="Show who can make custom roles and how many exist")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def status(self, interaction: discord.Interaction):
        guild = interaction.guild
        cfg = self.cfg(guild.id)
        allowed = ", ".join(f"<@&{r}>" for r in sorted(self.access.get(guild.id, set()))) or "none"
        anchor = f"just {cfg['side']} <@&{cfg['anchor']}>" if cfg["anchor"] else "at the very bottom"
        count = sum(1 for g, _ in self.owned if g == guild.id)
        await interaction.response.send_message(
            f"Boosters allowed: **{'yes' if cfg['boosters'] else 'no'}**\nAllowed roles: {allowed}\n"
            f"New roles go: {anchor}\nCustom roles in use: **{count}**",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    # ---------- Role Crew: share your role with a few friends ----------

    def own_role(self, guild: discord.Guild, user_id: int) -> discord.Role | None:
        role_id = self.owned.get((guild.id, user_id))
        return guild.get_role(role_id) if role_id else None

    async def crew_ids(self, guild_id: int, owner_id: int) -> list[int]:
        async with self.bot.db.execute("SELECT member_id FROM role_crew WHERE guild_id=? AND owner_id=?",
                                       (guild_id, owner_id)) as cur:
            return [r[0] for r in await cur.fetchall()]

    @app_commands.command(name="share", description="Share your custom role with a friend (they join your crew)")
    @app_commands.describe(member="The friend who gets your role")
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def share(self, interaction: discord.Interaction, member: discord.Member):
        await self.do_share(interaction, member)

    async def do_share(self, interaction: discord.Interaction, member: discord.Member):
        """Shared by /role share and the friend picker on the panel."""
        guild, owner = interaction.guild, interaction.user
        if not guild.me.guild_permissions.manage_roles:
            return await interaction.response.send_message("I need the Manage Roles permission to do that.",
                                                           ephemeral=True)
        own = self.own_role(guild, owner.id)
        limit = self.cfg(guild.id)["crew_max"]
        if own is None or not self.eligible(owner):
            return await interaction.response.send_message("You need your own custom role first.", ephemeral=True)
        if limit <= 0:
            return await interaction.response.send_message("Sharing roles is turned off on this server.", ephemeral=True)
        if member.bot or member.id == owner.id:
            return await interaction.response.send_message("Pick a friend, not yourself or a bot.", ephemeral=True)
        crew = await self.crew_ids(guild.id, owner.id)
        if member.id in crew:
            return await interaction.response.send_message(f"{member.mention} is already in your crew.",
                                                           ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        if len(crew) >= limit:
            return await interaction.response.send_message(
                f"Your crew is full ({limit} friends). Use `/role unshare` to make room.", ephemeral=True)
        try:
            await member.add_roles(own, reason=f"Shared by {owner}")
        except discord.HTTPException:
            return await interaction.response.send_message(
                "I couldn't give them the role. Ask an admin to check that my role is above custom roles.",
                ephemeral=True)
        await self.bot.db.execute("INSERT OR IGNORE INTO role_crew (guild_id, owner_id, member_id) VALUES (?,?,?)",
                                  (guild.id, owner.id, member.id))
        await self.bot.db.commit()
        await interaction.response.send_message(
            f"{member.mention} is now in your crew and has {own.mention} ({len(crew) + 1}/{limit}). "
            "If you lose your custom role, they lose it too.",
            allowed_mentions=discord.AllowedMentions(users=[member]))

    @app_commands.command(name="unshare", description="Take your custom role back from a friend")
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def unshare(self, interaction: discord.Interaction, member: discord.Member):
        await self.do_unshare(interaction, member)

    async def do_unshare(self, interaction: discord.Interaction, member: discord.Member):
        """Shared by /role unshare and the friend picker on the panel."""
        cur = await self.bot.db.execute("DELETE FROM role_crew WHERE guild_id=? AND owner_id=? AND member_id=?",
                                        (interaction.guild_id, interaction.user.id, member.id))
        await self.bot.db.commit()
        if not cur.rowcount:
            return await interaction.response.send_message(
                f"{member.mention} isn't in your crew.", ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none())
        own = self.own_role(interaction.guild, interaction.user.id)
        if own is not None:
            try:
                await member.remove_roles(own, reason=f"Unshared by {interaction.user}")
            except discord.HTTPException:
                pass
        await interaction.response.send_message(f"Removed {member.mention} from your crew.",
                                                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="leave", description="Leave someone's crew and give back their role")
    @app_commands.describe(owner="Whose crew to leave")
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def leave(self, interaction: discord.Interaction, owner: discord.Member):
        cur = await self.bot.db.execute("DELETE FROM role_crew WHERE guild_id=? AND owner_id=? AND member_id=?",
                                        (interaction.guild_id, owner.id, interaction.user.id))
        await self.bot.db.commit()
        if not cur.rowcount:
            return await interaction.response.send_message(f"You're not in {owner.display_name}'s crew.",
                                                           ephemeral=True)
        role = self.own_role(interaction.guild, owner.id)
        if role is not None:
            try:
                await interaction.user.remove_roles(role, reason="Left the crew")
            except discord.HTTPException:
                pass
        await interaction.response.send_message(f"You left {owner.display_name}'s crew.", ephemeral=True)

    @app_commands.command(name="crew", description="See who is in someone's crew")
    @app_commands.guild_only()
    async def crew(self, interaction: discord.Interaction, member: discord.Member | None = None):
        member, gid = member or interaction.user, interaction.guild_id
        mine = await self.crew_ids(gid, member.id)
        async with self.bot.db.execute("SELECT owner_id FROM role_crew WHERE guild_id=? AND member_id=?",
                                       (gid, member.id)) as cur:
            joined = [r[0] for r in await cur.fetchall()]
        limit = self.cfg(gid)["crew_max"]
        own = self.own_role(interaction.guild, member.id)
        lines = [f"**{discord.utils.escape_markdown(member.display_name)}'s crew** ({len(mine)}/{limit})"
                 + (f" with {own.mention}" if own else "")]
        lines.append(", ".join(f"<@{m}>" for m in mine) if mine else "Nobody yet.")
        if joined:
            lines.append("\n**In the crew of:** " + ", ".join(f"<@{o}>" for o in joined))
        await interaction.response.send_message("\n".join(lines), ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="crewsize", description="Set how many friends each member can share their role with")
    @app_commands.describe(amount="0 turns sharing off. Default is 3")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def crewsize(self, interaction: discord.Interaction, amount: app_commands.Range[int, 0, 10]):
        await self.save_settings(interaction.guild_id, crew_max=amount)
        await interaction.response.send_message(
            "Sharing custom roles is now **off**." if amount == 0 else
            f"Members can now share their custom role with up to **{amount}** friend{'s' if amount != 1 else ''}. "
            "Crews that are already bigger keep their members.", ephemeral=True)

    # ---------- Role Gallery: browse and like everyone's roles ----------

    async def gallery_entries(self, guild: discord.Guild) -> list[dict]:
        async with self.bot.db.execute("SELECT role_owner_id, voter_id FROM role_likes WHERE guild_id=?",
                                       (guild.id,)) as cur:
            likes: dict[int, set[int]] = {}
            for owner_id, voter_id in await cur.fetchall():
                likes.setdefault(owner_id, set()).add(voter_id)
        async with self.bot.db.execute("SELECT owner_id, COUNT(*) FROM role_crew WHERE guild_id=? GROUP BY owner_id",
                                       (guild.id,)) as cur:
            crews = dict(await cur.fetchall())
        entries = []
        for (gid, uid), role_id in self.owned.items():
            role = guild.get_role(role_id) if gid == guild.id else None
            if role is None:
                continue
            likers = likes.get(uid, set())
            entries.append({"owner_id": uid, "role": role, "name": role.name, "likes": len(likers),
                            "likers": likers, "crew": crews.get(uid, 0)})
        return rank_entries(entries)

    def gallery_embed(self, entry: dict | None, index: int, total: int) -> discord.Embed:
        if entry is None:
            return discord.Embed(
                title="No custom roles yet",
                description="Nobody has made one yet. Boosters and selected-role members can start with the "
                            "Create Role button.", color=discord.Color.blurple())
        role = entry["role"]
        embed = discord.Embed(
            title=discord.utils.escape_markdown(role.name),
            description=f"Made by <@{entry['owner_id']}>\nLikes: **{entry['likes']}**\nCrew: **{entry['crew']}** friends",
            color=role.colour if role.colour.value else discord.Color.blurple())
        if role.icon:
            embed.set_thumbnail(url=role.icon.url)
        embed.set_footer(text=f"Role {index + 1} of {total} - ranked by likes")
        return embed

    @app_commands.command(name="gallery", description="Browse everyone's custom roles and like your favorites")
    @app_commands.guild_only()
    async def gallery(self, interaction: discord.Interaction):
        await self.open_gallery(interaction)

    async def open_gallery(self, interaction: discord.Interaction):
        """Shared by /role gallery and the Gallery button. Anyone can open it."""
        view = GalleryView(self, interaction.guild, interaction.user)
        embed = await view.refresh()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    # ---------- cleanup ----------

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if (after.guild.id, after.id) in self.owned and not self.eligible(after):
            await self.delete_custom(after.guild, after.id, "No longer eligible for a custom role")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if (member.guild.id, member.id) in self.owned:
            await self.delete_custom(member.guild, member.id, "Member left the server")
        await self.bot.db.execute("DELETE FROM role_crew WHERE guild_id=? AND member_id=?",
                                  (member.guild.id, member.id))  # they no longer take up a crew spot
        await self.bot.db.commit()

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        for key, role_id in list(self.owned.items()):
            if role_id == role.id:
                await self.forget(*key)  # someone deleted it by hand
        if role.id in self.access.get(role.guild.id, set()):
            self.access[role.guild.id].discard(role.id)
            await self.bot.db.execute("DELETE FROM custom_role_access WHERE guild_id=? AND role_id=?",
                                      (role.guild.id, role.id))
            await self.bot.db.commit()


async def setup(bot: commands.Bot):
    await bot.add_cog(CustomRoles(bot))
