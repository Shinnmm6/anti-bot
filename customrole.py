import re

import discord
from discord import app_commands
from discord.ext import commands

from greetings import read_attachment, read_image_url  # same image rules as /welcome
from wordfilter import normalize  # so role names obey the word filter too

MAX_NAME_LENGTH = 32
ICON_MAX_BYTES = 256 * 1024  # Discord's limit for role icons
HEX_RE = re.compile(r"^(?:#|0x)?([0-9a-fA-F]{6})$")

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

    emoji = discord.ui.TextInput(label="Emoji (leave empty to remove the icon)", required=False, max_length=10)
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
                         "anchor_role_id INTEGER, boosters INTEGER DEFAULT 1)")
        await db.commit()
        async with db.execute("SELECT guild_id, user_id, role_id FROM custom_roles") as cur:
            for guild_id, user_id, role_id in await cur.fetchall():
                self.owned[(guild_id, user_id)] = role_id
        async with db.execute("SELECT guild_id, role_id FROM custom_role_access") as cur:
            for guild_id, role_id in await cur.fetchall():
                self.access.setdefault(guild_id, set()).add(role_id)
        async with db.execute("SELECT guild_id, anchor_role_id, boosters FROM custom_role_settings") as cur:
            for guild_id, anchor, boosters in await cur.fetchall():
                self.settings[guild_id] = {"anchor": anchor, "boosters": bool(boosters)}
        self.bot.add_view(RolePanel(self))
        self.bot.add_view(OldRolePanel())

    # ---------- helpers ----------

    def cfg(self, guild_id: int) -> dict:
        return self.settings.get(guild_id, {"anchor": None, "boosters": True})

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
            "INSERT OR REPLACE INTO custom_role_settings (guild_id, anchor_role_id, boosters) VALUES (?,?,?)",
            (guild_id, cfg["anchor"], int(cfg["boosters"])))
        await self.bot.db.commit()
        self.settings[guild_id] = cfg

    async def forget(self, guild_id: int, user_id: int):
        self.owned.pop((guild_id, user_id), None)
        await self.bot.db.execute("DELETE FROM custom_roles WHERE guild_id=? AND user_id=?", (guild_id, user_id))
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

    async def create_for(self, member: discord.Member, name: str, value: int | None) -> discord.Role:
        guild = member.guild
        role = await guild.create_role(
            name=name, colour=discord.Colour(value or 0), permissions=discord.Permissions.none(),
            hoist=False, mentionable=False, reason=f"Custom role for {member}")
        # Put it just under the anchor role (default: just under my own top role) so its color shows.
        anchor_id = self.cfg(guild.id)["anchor"]
        anchor = guild.get_role(anchor_id) if anchor_id else None
        top = min(anchor.position if anchor else guild.me.top_role.position, guild.me.top_role.position)
        try:
            await role.edit(position=max(1, top - 1))
        except discord.HTTPException:
            pass  # the role still works, it just stays near the bottom of the list
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
                          "small emoji or image next to it (the server needs Boost Level 2 for icons).",
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
        if action in ("change", "icon") and own is None:
            return await interaction.response.send_message(
                "You don't have a custom role yet. Press **Create Role** first.", ephemeral=True)
        if action == "icon":
            if "ROLE_ICONS" not in guild.features:
                return await interaction.response.send_message(
                    "Role icons need this server to reach Boost Level 2.", ephemeral=True)
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
    @app_commands.describe(emoji="An emoji to use as the icon", image="Or upload an image (square, under 256 KB)",
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
        if "ROLE_ICONS" not in guild.features:
            return await interaction.response.send_message(
                "Role icons need this server to reach Boost Level 2.", ephemeral=True)
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
                if emoji.isascii():
                    raise ValueError("That isn't an emoji. Paste a single emoji, or use an image instead.")
                value = emoji
            elif image is not None or url:
                value, _ext = await read_attachment(image) if image is not None else await read_image_url(url)
                if len(value) > ICON_MAX_BYTES:
                    raise ValueError("That image is bigger than 256 KB. Use a smaller, square image.")
        except ValueError as e:
            return await interaction.followup.send(str(e), ephemeral=True)
        try:
            await own.edit(display_icon=value, reason=f"Role icon changed by {member}")
        except discord.Forbidden:
            return await interaction.followup.send(
                "I don't have permission to edit that role. Ask an admin to move my role higher.", ephemeral=True)
        except discord.HTTPException:
            return await interaction.followup.send(
                "Discord refused that icon. Use one emoji, or a square PNG or JPG that is at least 64x64 "
                "and under 256 KB.", ephemeral=True)
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

    @app_commands.command(name="position", description="Place new custom roles just under this role (so their colors show)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def position(self, interaction: discord.Interaction, role: discord.Role):
        if role >= interaction.guild.me.top_role:
            return await interaction.response.send_message(
                "Pick a role below my highest role.", ephemeral=True)
        await self.save_settings(interaction.guild_id, anchor=role.id)
        await interaction.response.send_message(
            f"New custom roles will be placed just under {role.mention}. Existing ones don't move.",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="status", description="Show who can make custom roles and how many exist")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def status(self, interaction: discord.Interaction):
        guild = interaction.guild
        cfg = self.cfg(guild.id)
        allowed = ", ".join(f"<@&{r}>" for r in sorted(self.access.get(guild.id, set()))) or "none"
        anchor = f"<@&{cfg['anchor']}>" if cfg["anchor"] else "just under my top role"
        count = sum(1 for g, _ in self.owned if g == guild.id)
        await interaction.response.send_message(
            f"Boosters allowed: **{'yes' if cfg['boosters'] else 'no'}**\nAllowed roles: {allowed}\n"
            f"New roles go: {anchor}\nCustom roles in use: **{count}**",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    # ---------- cleanup ----------

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if (after.guild.id, after.id) in self.owned and not self.eligible(after):
            await self.delete_custom(after.guild, after.id, "No longer eligible for a custom role")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if (member.guild.id, member.id) in self.owned:
            await self.delete_custom(member.guild, member.id, "Member left the server")

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
