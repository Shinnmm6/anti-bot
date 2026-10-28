import discord
from discord import app_commands
from discord.ext import commands

PAGES: dict[str, str] = {
    "Overview": (
        "This bot protects your server with moderation tools, auto-mod, anti-raid, anti-nuke, "
        "member verification, anti-link and logging.\n\n"
        "**Quick start (server owner)**\n"
        "1. `/auditlog set` to pick a staff-only log channel\n"
        "2. Move the bot's role as HIGH as possible (above roles it must moderate)\n"
        "3. `/verify setup` to turn on member verification (and lockdown)\n"
        "4. `/antilink on` to block links from non-trusted people\n"
        "5. `/antinuke trust` and `/antilink trust` for people you fully trust\n\n"
        "Use the dropdown below to read about each feature. Only people with *Manage Server* "
        "(and the owner) can open this help."),
    "Moderation commands": (
        "`/kick member reason`: kick (needs Kick Members)\n"
        "`/ban member reason`: ban (needs Ban Members)\n"
        "`/timeout member minutes reason`: timeout up to 28 days (needs Moderate Members)\n"
        "`/warn member reason`: add a warning. 3 warnings = automatic 1-hour timeout\n"
        "`/warnings member`: list a member's last 10 warnings\n"
        "`/purge amount`: delete 1 to 100 recent messages (needs Manage Messages)\n\n"
        "The bot and the moderator must both outrank the target. Nobody can act on the owner or on themselves."),
    "Auto-mod & anti-raid": (
        "**Auto-mod** (staff with Manage Messages are exempt)\n"
        "- 5 messages in 5 seconds: message deleted + 10 min timeout\n"
        "- 5 or more mentions in one message: deleted + 10 min timeout\n\n"
        "**Anti-raid**\n"
        "- 8 joins within 10 seconds starts raid mode for 2 minutes\n"
        "- During raid mode, accounts younger than 7 days are kicked on join\n\n"
        "Invites and links are handled by Anti-link."),
    "Anti-nuke": (
        "Watches the audit log for destructive actions by people who are NOT trusted.\n\n"
        "**Limits (per 10 seconds):** 3 channel deletes, 3 role deletes, 4 bans, 5 kicks, "
        "5 channel creates, 5 role creates, 3 webhook creates.\n"
        "2 or more actions in a burst posts a *Suspicious activity* log. Hitting the limit strips "
        "the person's dangerous roles (bots are kicked) and re-creates deleted channels.\n"
        "Also: untrusted bot invites are kicked, and dangerous permissions granted to a role by an "
        "untrusted person are reverted.\n\n"
        "**Commands (server owner only)**\n"
        "`/antinuke trust user`, `/antinuke untrust user`, `/antinuke status`\n"
        "The owner and the bot are always trusted. The bot needs View Audit Log, Manage Roles, "
        "Manage Channels, Ban and Kick, and a high role."),
    "Verification": (
        "Every new member must pass a random challenge (a math question, a code to copy, or typing yes) "
        "to get the member role.\n\n"
        "`/verify setup member_role verify_channel [timeout_minutes] [lockdown] [grant_existing]`\n"
        "- Posts the Verify button in the verify channel\n"
        "- `lockdown` (default on): unverified people can only see the verify channel\n"
        "- `grant_existing` (default on): gives the role to current members (up to 250)\n"
        "- Members who don't verify within the timeout (default 15 min) are kicked; 5 wrong answers = kick\n"
        "`/verify status`: show settings\n"
        "`/verify disable`: turn it off (restores @everyone View Channels if lockdown changed it)\n\n"
        "The bot's role must be above the member role and have View Channels itself."),
    "Anti-link": (
        "Deletes links from anyone who isn't trusted. Edited messages are checked too. "
        "3 blocked messages in 5 minutes = 10 min timeout.\n\n"
        "**Who can post links:** the server owner, everyone in `/antinuke trust`, and everyone in "
        "`/antilink trust`. Admins and mods are NOT exempt unless trusted.\n\n"
        "`/antilink on` / `/antilink off`: master switch (starts OFF)\n"
        "`/antilink toggle option enabled`: choose what to block: Discord invites, Discord CDN links, "
        "all other links (https, www, bare domains like example.com), file/image uploads (off by default)\n"
        "`/antilink allow domain` / `/antilink unallow domain`: domains everyone can post (try tenor.com so GIFs work)\n"
        "`/antilink trust user` / `/antilink untrust user`: owner only\n"
        "`/antilink status`: show everything"),
    "Logging": (
        "`/auditlog set channel`: pick where logs go (make it staff-only)\n"
        "`/auditlog status` and `/auditlog off`\n\n"
        "**Logged:** verifications and failed attempts, kicks/bans/unbans/timeouts (including ones done in "
        "Discord's own menus or by other bots), blocked links, automod hits, raid mode, anti-nuke warnings "
        "and triggers, message edits and deletes.\n"
        "Needs the View Audit Log permission."),
}


def make_embed(name: str) -> discord.Embed:
    return discord.Embed(title=f"Help: {name}", description=PAGES[name], color=discord.Color.blurple())


class HelpSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Pick a section",
            options=[discord.SelectOption(label=name, value=name) for name in PAGES])

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=make_embed(self.values[0]), view=self.view)


class HelpView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(HelpSelect())


class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="help", description="See every command and how the bot works")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def help_command(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=make_embed("Overview"), view=HelpView(), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(HelpCog(bot))
