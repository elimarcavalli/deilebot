"""HelpCog — `/help` que recursa em groups e mostra subcomandos.

Bug fix: a versão anterior só listava `tree.get_commands()` no nível 0,
deixando subcomandos de groups (`/dlq list`, `/sessions purge` etc.)
invisíveis. Esta versão recursa em `app_commands.Group`, agrupa por
categoria deduzida do cog, e mostra descrição.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Tuple

import discord
from discord import app_commands
from discord.ext import commands


def _walk(cmd, prefix: str = "") -> Iterable[Tuple[str, str]]:
    """Yield (name, description) for cmd and all sub-commands of groups."""
    name = f"{prefix}{cmd.name}".strip()
    if isinstance(cmd, app_commands.Group):
        for sub in cmd.commands:
            yield from _walk(sub, prefix=f"{name} ")
    else:
        desc = getattr(cmd, "description", "") or ""
        yield name, desc


def _categorize(name: str) -> str:
    """Deduce category by command name prefix or known set."""
    head = name.split(" ", 1)[0]
    table = {
        "deile": "Agente",
        "ideia": "Agente",
        "agendar": "Agendamento",
        "agendamentos": "Agendamento",
        "cancelar": "Agendamento",
        "status": "Sistema",
        "ping": "Sistema",
        "help": "Sistema",
        "capabilities": "Sistema",
        "forget": "Privacidade",
        "forget_me": "Privacidade",
        "sessions": "Admin",
        "dlq": "Admin",
        "metrics": "Admin",
    }
    return table.get(head, "Outros")


class HelpCog(commands.Cog):
    def __init__(self, bot: Any):
        self.bot = bot

    @commands.hybrid_command(name="help", description="Lista comandos do bot")
    async def help(self, ctx: commands.Context):
        # 1. Walk slash tree (recursive into groups)
        slash_pairs: List[Tuple[str, str]] = []
        for cmd in self.bot.tree.get_commands():
            slash_pairs.extend(_walk(cmd))

        # 2. Group by category
        by_cat: Dict[str, List[Tuple[str, str]]] = {}
        for name, desc in slash_pairs:
            by_cat.setdefault(_categorize(name), []).append((name, desc))

        # 3. Render embed
        embed = discord.Embed(
            title="📚 DEILE — Comandos disponíveis",
            color=0x5865F2,
            description=(
                "Use `/<comando>` para invocar. Em DM, é só falar — sem prefixo. "
                "Mande imagens, peça reações, fixar mensagens, ou tarefas de código."
            ),
        )
        order = ["Agente", "Sistema", "Agendamento", "Privacidade", "Admin", "Outros"]
        for cat in order:
            items = by_cat.get(cat, [])
            if not items:
                continue
            text = "\n".join(f"`/{n}` — {d}" for n, d in sorted(items))
            if len(text) > 1024:
                text = text[:1020] + "…"
            embed.add_field(name=cat, value=text, inline=False)

        # 4. Prefix commands (legacy)
        prefix_cmds = [c for c in self.bot.commands if c.name != "help"]
        if prefix_cmds:
            text = "\n".join(
                f"`{self.bot.command_prefix}{c.name}` — {c.help or c.description or ''}"
                for c in prefix_cmds
            )
            if text:
                embed.add_field(name=f"Prefixo ({self.bot.command_prefix})", value=text[:1024], inline=False)

        embed.set_footer(text="Em DM, mande mensagem direta — bot decide tool/dispatch sozinho")
        await ctx.send(embed=embed)
