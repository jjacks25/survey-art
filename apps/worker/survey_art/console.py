"""Rich terminal output for the CLI (`pipeline.run_async(quiet=False)`)."""

from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

_console = Console()


def run_cost(cost_usd: float, input_tokens: int, output_tokens: int) -> None:
    """Print the total LLM cost and token breakdown for the run."""
    total = input_tokens + output_tokens
    _console.print(
        Panel(
            f"[bold green]${cost_usd:.4f}[/]  "
            f"[dim]{total:,} tokens ({input_tokens:,} in / {output_tokens:,} out)[/]",
            title="[bold]Run Cost[/]",
            border_style="green",
            expand=False,
        )
    )


def model_banner(model: str) -> None:
    """Print the active LLM model prominently."""
    _console.print(
        Panel(
            f"[bold yellow]{model}[/]",
            title="[bold]LLM Model[/]",
            border_style="yellow",
            expand=False,
        )
    )


def county_resolved(name: str, state: str) -> None:
    """Print the resolved county."""
    _console.print(
        Panel(f"[bold]{name}[/], [bold]{state}[/]", title="[cyan]County[/]", border_style="cyan")
    )


def files_table(paths: list[Path], dest_dir: Path) -> None:
    """Print a table of saved files, relative to `dest_dir`."""
    table = Table(title=f"Saved {len(paths)} file(s) to {dest_dir}")
    table.add_column("File", style="green")
    for p in paths:
        table.add_row(str(p.relative_to(dest_dir) if p.is_relative_to(dest_dir) else p.name))
    _console.print(table)
