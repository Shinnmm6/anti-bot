import discord
from discord import app_commands
from discord.ext import commands


def is_allowed(owner_id: int, is_admin: bool, user_id: int, role_ids: list[int],
               allowed_users: set[int], allowed_roles: set[int]) -> bool:
    """With an empty list everyone may use commands. Otherwise only the owner, admins and listed people/roles."""
    if not allowed_users and not allowed_roles:
        return True
    if user_id == owner_id or is_admin:
        return True
    return user_id in allowed_users or any(r in allowed_roles for r in role_ids)


class Access(commands.Cog):
    """Server owner picks who may use the bot's commands."""

    access = app_commands.Group(name="access", description="Choose who can use the bot's commands (server owner only)",
                                guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.users: dict[int, set[int]] = {}
        self.roles: dict[int, set[int]] = {}
        self.old_check = None

    async def cog_load(self):
        db = self.bot.db
        await db.execute("CREATE TABLE IF NOT EXISTS access_list (guild_id INTEGER, kind TEXT, target_id INTEGER, "
                         "PRIMARY KEY (guild_id, kind, target_id))")
        await db.commit()
        async with db.execute("SELECT guild_id, kind, target_id FROM access_list") as cur:
            for gid, kind, tid in await cur.fetchall():
                (self.users if kind == "user" else self.roles).setdefault(gid, set()).add(tid)
        self.old_check = self.bot.tree.interaction_check
        self.bot.tree.interaction_check = self.check

    async def cog_unload(self):
        if self.old_check:
            self.bot.tree.interaction_check = self.old_check

    async def check(self, interaction: discord.Interaction) -> bool:
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            return True
        m = interaction.user
        ok = is_allowed(guild.owner_id, m.guild_permissions.administrator, m.id, [r.id for r in m.roles],
                        self.users.get(guild.id, set()), self.roles.get(guild.id, set()))
        if not ok:
            raise app_commands.CheckFailure("The server owner has limited who can use my commands, and you're not on the list.")
        return True

    async def _owner_only(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != interaction.guild.owner_id:
            await interaction.response.send_message("Only the server owner can change this.", ephemeral=True)
            return False
        return True

    async def _add(self, interaction, kind: str, target):
        if not await self._owner_only(interaction):
            return
        gid = interaction.guild_id
        await self.bot.db.execute("INSERT OR IGNORE INTO access_list VALUES (?,?,?)", (gid, kind, target.id))
        await self.bot.db.commit()
        (self.users if kind == "user" else self.roles).setdefault(gid, set()).add(target.id)
        await interaction.response.send_message(
            f"{target.mention} can now use my commands. Commands are now **limited to the list** "
            "(plus you and admins). Use `/access off` to open them to everyone again.",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def _remove(self, interaction, kind: str, target):
        if not await self._owner_only(interaction):
            return
        gid = interaction.guild_id
        await self.bot.db.execute("DELETE FROM access_list WHERE guild_id=? AND kind=? AND target_id=?", (gid, kind, target.id))
        await self.bot.db.commit()
        (self.users if kind == "user" else self.roles).get(gid, set()).discard(target.id)
        left = bool(self.users.get(gid) or self.roles.get(gid))
        note = "" if left else " The list is now empty, so **everyone** can use commands again."
        await interaction.response.send_message(f"Removed {target.mention}.{note}", ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @access.command(name="allow_role", description="Let a role use my commands")
    async def allow_role(self, interaction: discord.Interaction, role: discord.Role):
        await self._add(interaction, "role", role)

    @access.command(name="allow_user", description="Let one person use my commands")
    async def allow_user(self, interaction: discord.Interaction, user: discord.Member):
        await self._add(interaction, "user", user)

    @access.command(name="remove_role", description="Take a role off the list")
    async def remove_role(self, interaction: discord.Interaction, role: discord.Role):
        await self._remove(interaction, "role", role)

    @access.command(name="remove_user", description="Take a person off the list")
    async def remove_user(self, interaction: discord.Interaction, user: discord.Member):
        await self._remove(interaction, "user", user)

    @access.command(name="list", description="See who can use my commands")
    async def list_cmd(self, interaction: discord.Interaction):
        if not await self._owner_only(interaction):
            return
        gid = interaction.guild_id
        users, roles = self.users.get(gid, set()), self.roles.get(gid, set())
        if not users and not roles:
            return await interaction.response.send_message("Everyone can use my commands (the list is empty).", ephemeral=True)
        text = ("Only these can use my commands (plus you and admins):\n"
                + "\n".join([f"Role: <@&{r}>" for r in roles] + [f"User: <@{u}>" for u in users]))
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @access.command(name="off", description="Clear the list so everyone can use my commands again")
    async def off(self, interaction: discord.Interaction):
        if not await self._owner_only(interaction):
            return
        gid = interaction.guild_id
        await self.bot.db.execute("DELETE FROM access_list WHERE guild_id=?", (gid,))
        await self.bot.db.commit()
        self.users.pop(gid, None)
        self.roles.pop(gid, None)
        await interaction.response.send_message("The list is cleared. Everyone can use my commands again.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Access(bot))
