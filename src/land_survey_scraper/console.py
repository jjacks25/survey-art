from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

_console = Console()


def get_console() -> Console:
    return _console


def step(msg: str, **kwargs: Any) -> None:
    """Print a step label (cyan)."""
    _console.print(f"[cyan]→[/] [bold]{msg}[/]", **kwargs)


def success(msg: str, **kwargs: Any) -> None:
    """Print a success message (green)."""
    _console.print(f"[green]✓[/] {msg}", **kwargs)


def run_cost(cost_usd: float, input_tokens: int, output_tokens: int) -> None:
    """Print the total LLM cost and token breakdown for the run."""
    total = input_tokens + output_tokens
    _console.print(
        Panel(
            f"[bold green]${cost_usd:.4f}[/]  "
            f"[dim]{total:,} tokens "
            f"({input_tokens:,} in / {output_tokens:,} out)[/]",
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


def county_resolved(name: str, state: str, via: str = "address") -> None:
    """Print the resolved county with a small panel."""
    _console.print(
        Panel(
            f"[bold]{name}[/], [bold]{state}[/]\n[dim]resolved via {via}[/]",
            title="[cyan]County[/]",
            border_style="cyan",
        )
    )


def site_found(base_url: str, search_path: str) -> None:
    """Print the property records site being used."""
    url = f"{base_url.rstrip('/')}{search_path}".strip("/")
    _console.print(
        Panel(f"[bold]{url}[/]", title="[cyan]Property records site[/]", border_style="cyan")
    )


def discovery_start(county_name: str, state: str) -> None:
    """Print that we're searching the web for the county site."""
    _console.print(
        f"[cyan]→[/] [bold]Searching web[/] for [bold]{county_name}[/], [bold]{state}[/] "
        "property records site..."
    )


def discovery_found(url: str) -> None:
    """Print that we found a site via search."""
    _console.print(f"[green]✓[/] Found candidate: [bold]{url}[/]")


def browser_search_start(address: str) -> None:
    """Print that we're using the browser to search for the property."""
    _console.print(
        f"[cyan]→[/] [bold]Searching[/] for property: [bold]{address}[/] (browser)..."
    )


def property_page_found(url: str) -> None:
    """Print that we found the property page."""
    _console.print(f"[green]✓[/] Property page: [bold]{url}[/]")


def crawl_start(max_pages: int, max_depth: int) -> None:
    """Print crawl parameters."""
    _console.print(
        f"[cyan]→[/] [bold]Crawling[/] (max [bold]{max_pages}[/] pages, depth [bold]{max_depth}[/])"
    )


def crawl_done(pages_visited: int, doc_count: int) -> None:
    """Print crawl result."""
    _console.print(
        f"[green]✓[/] Visited [bold]{pages_visited}[/] page(s), "
        f"found [bold]{doc_count}[/] document(s)"
    )


def download_start(count: int, dest: str) -> None:
    """Print download start."""
    _console.print(f"[cyan]→[/] [bold]Downloading[/] {count} file(s) to [bold]{dest}[/]")


def download_done(saved: list | int, dest: str) -> None:
    """Print download result. saved can be list of paths or count."""
    if isinstance(saved, list):
        count = len(saved)
    else:
        count = saved
    _console.print(f"[green]✓[/] Saved [bold]{count}[/] file(s) to [bold]{dest}[/]")


def files_table(paths: list, dest_dir: str | Path) -> None:
    """Print a table of saved files (relative to dest_dir)."""
    dest = Path(dest_dir)
    table = Table(title=f"Saved to {dest}")
    table.add_column("File", style="green")
    for p in paths:
        p_path = Path(p) if not isinstance(p, Path) else p
        try:
            rel = p_path.relative_to(dest)
        except ValueError:
            rel = p_path.name
        table.add_row(str(rel))
    _console.print(table)


def progress_spinner(description: str):
    """Context manager that shows a spinner with the given description."""
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=_console,
    )


def track_downloads(items: list, description: str = "Downloading..."):
    """Iterate with rich progress bar (for download steps)."""
    from rich.progress import track

    return track(items, description=description, console=_console)
