import re

import discord
from discord import app_commands
from discord.ext import commands

from wordfilter import normalize  # so role names obey the word filter too

MAX_NAME_LENGTH = 32
HEX_RE = re.compile(r"^(?:#|0x)?([0-9a-fA-F]{6})$")


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
    @app_commands.describe(name="The role name", color="Hex color such as #ff66aa (optional)")
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def custom(self, interaction: discord.Interaction, name: str, color: str | None = None):
        member, guild = interaction.user, interaction.guild
        if not self.eligible(member):
            return await interaction.response.send_message(
                "Custom roles are for server boosters and members with a selected role. "
                "Ask the staff if you think you should have access.", ephemeral=True)

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
            match = HEX_RE.match(color.strip())
            if not match:
                return await interaction.response.send_message(
                    "The color must be a hex code like `#ff66aa`.", ephemeral=True)
            value = int(match.group(1), 16)

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
        await interaction.followup.send(f"Your custom role is ready and has been given to you: {role.mention}",
                                        ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
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
        await interaction.response.send_message(f"Members with {role.mention} can now use `/role custom`.",
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
