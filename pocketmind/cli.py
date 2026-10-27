"""命令行入口。

常用：
  python -m pocketmind.cli init
  python -m pocketmind.cli chat        # 离线 mock；有 Key 时走真实 API
  python -m pocketmind.cli serve       # 本地 Web
  python -m pocketmind.cli simulate    # 3 天脚本模拟
  python -m pocketmind.cli eval        # 记忆召回探针
  python -m pocketmind.cli stats
"""

from __future__ import annotations

import json

import typer
from rich.console import Console
from rich.table import Table

from .config import Settings
from .pipeline import ChatEngine

app = typer.Typer(add_completion=False, help="PocketMind M1 本地实验台")
console = Console()


def _engine() -> ChatEngine:
    return ChatEngine(Settings())


@app.command()
def init() -> None:
    """初始化数据库/人格实体。"""
    engine = _engine()
    console.print(f"[green]初始化完成[/green] entity={engine.persona_card.entity_id} mock={engine.llm.is_mock}")
    console.print(json.dumps(engine.stats(), ensure_ascii=False, indent=2))


@app.command()
def chat() -> None:
    """交互式聊天（Ctrl+C / 输入 exit 退出）。"""
    engine = _engine()
    cid = engine.new_conversation()
    console.print(f"[green]开始对话[/green] mock={engine.llm.is_mock} 输入 exit 退出")
    while True:
        try:
            text = console.input("[bold cyan]你> [/bold cyan]").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text.lower() in {"exit", "quit", "q"}:
            break
        if not text:
            continue
        result = engine.send(text, cid)
        console.print(f"[bold yellow]{engine.persona_card.name}> [/bold yellow]{result.reply}")
        if result.used_memories:
            console.print(f"[dim]（想起 {len(result.used_memories)} 条记忆；新存 {result.saved_memories} 条）[/dim]")


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """启动本地 Web 实验台。"""
    import uvicorn

    from .web import create_app

    uvicorn.run(create_app(Settings()), host=host, port=port, log_level="info")


@app.command()
def simulate(days: int = 3) -> None:
    """跑 3 天脚本对话，验证记忆写入/召回。"""
    from .simulator import run_simulation

    report = run_simulation(_engine(), days=days)
    console.print(json.dumps(report, ensure_ascii=False, indent=2))


@app.command()
def evaluate() -> None:
    """跑记忆召回探针。"""
    from .evaluation import run_evaluation

    report = run_evaluation(_engine())
    console.print(json.dumps(report, ensure_ascii=False, indent=2))


@app.command()
def stats() -> None:
    """查看统计。"""
    engine = _engine()
    console.print(json.dumps(engine.stats(), ensure_ascii=False, indent=2))


@app.command()
def versions() -> None:
    """查看 schema / Prompt 模板 / embedding 版本。"""
    engine = _engine()
    from .prompt_store import template_meta

    data = {
        "schema_version": engine.db.schema_version(),
        "schema_migrations": engine.db.fetchall("SELECT * FROM schema_migrations ORDER BY version"),
        "prompt_template": template_meta(engine.settings),
        "embedding_model": engine.db.get_meta("embedding_model", ""),
        "embedding_dim": engine.db.get_meta("embedding_dim", "0"),
        "embedding_updated_at": engine.db.get_meta("embedding_updated_at", ""),
    }
    console.print(json.dumps(data, ensure_ascii=False, indent=2))


@app.command()
def reembed(limit: int = 0, dry_run: bool = False) -> None:
    """用当前 embedding 模型重算 active 记忆向量（换模型/维度后使用）。"""
    report = _engine().reembed_memories(limit=limit, dry_run=dry_run)
    console.print(json.dumps(report, ensure_ascii=False, indent=2))


@app.command()
def rebuild_scores(apply: bool = False) -> None:
    """按当前 base_score 公式重算已有记忆基础分；默认 dry-run。"""
    report = _engine().rebuild_scores(apply=apply)
    console.print(json.dumps(report, ensure_ascii=False, indent=2))


@app.command()
def memories(limit: int = 30) -> None:
    """列出活跃记忆。"""
    engine = _engine()
    table = Table(title="活跃记忆")
    table.add_column("分类")
    table.add_column("内容", overflow="fold")
    table.add_column("重要度")
    table.add_column("半衰期(天)")
    for m in engine.memories(limit=limit):
        table.add_row(m["category"], m["content"], f"{m['importance']:.2f}", f"{m['half_life_days']:.0f}")
    console.print(table)


@app.command()
def skills() -> None:
    """列出技能池（程序性记忆）。"""
    engine = _engine()
    rows = engine.skills.list(engine.persona_card.entity_id)
    table = Table(title="技能池")
    for name in ("名称", "触发条件", "熟练度", "调用", "半衰期(天)"):
        table.add_column(name)
    for s in rows:
        table.add_row(
            s["name"], s["trigger_conditions"], f"{s['proficiency']:.2f}",
            str(s["success_count"]), f"{s['half_life_days']:.0f}",
        )
    console.print(table)


@app.command()
def skill_add(
    name: str,
    triggers: str,
    steps: str = "",
    constraints: str = "",
) -> None:
    """手动添加技能；steps 用 | 分隔。"""
    engine = _engine()
    step_list = [s.strip() for s in steps.split("|") if s.strip()]
    skill = engine.skills.create(
        engine.persona_card.entity_id, name, triggers, step_list,
        constraints=constraints, source="manual_cli",
    )
    console.print(json.dumps(skill, ensure_ascii=False, indent=2))


@app.command()
def diary() -> None:
    """查看今天的日记。"""
    console.print(_engine().diary_today() or "今天还没写日记。")


if __name__ == "__main__":
    app()
