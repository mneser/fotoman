"""Typer CLI for reclaim."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from reclaim import __version__
from reclaim.jobs import delete_job, list_jobs
from reclaim.normalize import inspect_input, normalize
from reclaim.paths import client_secrets_path, config_dir, default_watch_dir
from reclaim.place import DedupMode, Layout
from reclaim.platforms import wizard_for
from reclaim.util import ExitCode, open_url
from reclaim.watch import run_watch_and_normalize

app = typer.Typer(
    name="reclaim",
    help="Get media off Google / Facebook / Instagram onto disk for Fotoman.",
    no_args_is_help=True,
)
google_app = typer.Typer(
    help="Google Photos via Takeout (preferred). Legacy Library API pull is limited."
)
app.add_typer(google_app, name="google")
facebook_app = typer.Typer(help="Facebook Download Your Information assist.")
app.add_typer(facebook_app, name="facebook")
instagram_app = typer.Typer(help="Instagram data download assist.")
app.add_typer(instagram_app, name="instagram")

console = Console()


def _exit(code: ExitCode | int) -> None:
    raise typer.Exit(int(code))


def _parse_dedup(value: str) -> DedupMode:
    try:
        return DedupMode(value)
    except ValueError:
        console.print(f"[red]Invalid --dedup {value!r}; use skip|hardlink|symlink[/red]")
        _exit(ExitCode.USAGE)
        raise  # unreachable


def _parse_layout(value: str) -> Layout:
    try:
        return Layout(value)
    except ValueError:
        console.print(f"[red]Invalid --layout {value!r}; use albums|date|flat[/red]")
        _exit(ExitCode.USAGE)
        raise


def _print_checklist(lines: list[str]) -> None:
    for i, line in enumerate(lines, 1):
        console.print(f"  {i}. {line}")


def _run_fetch_wizard(
    platform: str,
    out: Path,
    watch_dir: Path | None,
    wait_hours: float,
    layout: str,
    dedup: str,
    open_browser: bool,
) -> None:
    wiz = wizard_for(platform)
    watch = (watch_dir or default_watch_dir()).expanduser().resolve()
    out = out.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    watch.mkdir(parents=True, exist_ok=True)

    console.print(f"[bold]{platform} export assist[/bold]")
    console.print(f"Output: {out}")
    console.print(f"Watch:  {watch}")
    console.print()
    console.print("[bold]Checklist[/bold]")
    _print_checklist(wiz.checklist)
    console.print()
    if open_browser:
        console.print(f"Opening {wiz.url}")
        open_url(wiz.url)
    else:
        console.print(f"Open: {wiz.url}")

    started = time.time()
    code = run_watch_and_normalize(
        platform=wiz.platform,
        source=wiz.source,
        out=out,
        watch_dir=watch,
        patterns=wiz.patterns,
        wait_hours=wait_hours,
        layout=_parse_layout(layout),
        dedup=_parse_dedup(dedup),
        started_mtime=started,
        progress=lambda m: console.print(m),
    )
    if code == ExitCode.PENDING:
        console.print(
            f"[yellow]{platform} fetch: waiting on external export "
            f"(re-run later / reclaim status)[/yellow]"
        )
    _exit(code)


# ── google ─────────────────────────────────────────────────────────────


@google_app.command("auth")
def google_auth(
    client_secrets: Optional[Path] = typer.Option(
        None,
        "--client-secrets",
        help=f"OAuth desktop client JSON (default: {client_secrets_path()})",
    ),
) -> None:
    """Legacy OAuth for Library API (cannot read your full library after Mar 2025)."""
    from reclaim.google_auth import run_auth

    console.print(
        "[yellow]Note:[/yellow] Google no longer allows library-wide Photos download "
        "via API. Prefer [cyan]reclaim google takeout[/cyan] (no OAuth)."
    )
    _exit(run_auth(client_secrets))


@google_app.command("logout")
def google_logout() -> None:
    """Delete stored Google refresh token (legacy API path)."""
    from reclaim.google_auth import logout

    _exit(logout())


@google_app.command("pull")
def google_pull(
    out: Path = typer.Option(..., "-o", "--out", help="Output directory"),
    since: Optional[str] = typer.Option(
        None, "--since", help="Only items on/after YYYY-MM-DD"
    ),
    no_videos: bool = typer.Option(False, "--no-videos", help="Skip videos"),
    dedup: str = typer.Option("hardlink", "--dedup", help="skip|hardlink|symlink"),
    layout: str = typer.Option("albums", "--layout", help="albums|date|flat"),
    force: bool = typer.Option(
        False,
        "--force",
        help="Proceed despite library-read being unavailable for existing Photos",
    ),
) -> None:
    """Legacy Library API pull — cannot download your existing Photos library."""
    from reclaim.google_pull import pull

    console.print(
        "[bold yellow]Google Photos Library API cannot read your existing library "
        "(policy change 31 Mar 2025).[/bold yellow]\n"
        "Use [cyan]reclaim google takeout -o OUT[/cyan] instead."
    )
    if not force:
        console.print("Pass [cyan]--force[/cyan] only to experiment with the legacy API.")
        _exit(ExitCode.USAGE)

    since_dt: datetime | None = None
    if since:
        try:
            since_dt = datetime.strptime(since, "%Y-%m-%d")
        except ValueError:
            console.print("[red]--since must be YYYY-MM-DD[/red]")
            _exit(ExitCode.USAGE)

    stats, code = pull(
        out,
        since=since_dt,
        include_videos=not no_videos,
        dedup=_parse_dedup(dedup),
        layout=_parse_layout(layout),
        progress=lambda m: console.print(m),
    )
    console.print(
        f"google pull: {stats.downloaded} downloaded, "
        f"{stats.skipped_existing} skipped_existing, "
        f"{stats.errors} errors"
    )
    console.print(f"output: {out.expanduser().resolve()}")
    _exit(code)


@google_app.command("takeout")
def google_takeout(
    out: Path = typer.Option(..., "-o", "--out", help="Output directory"),
    watch_dir: Optional[Path] = typer.Option(
        None, "--watch-dir", help="Where zip downloads land (default ~/Downloads)"
    ),
    wait_hours: float = typer.Option(
        0.0,
        "--wait-hours",
        help="Hours to poll for zip (0 = single scan then exit pending)",
    ),
    dedup: str = typer.Option("hardlink", "--dedup"),
    layout: str = typer.Option("albums", "--layout"),
    no_open: bool = typer.Option(False, "--no-open", help="Do not open browser"),
) -> None:
    """Preferred: open Takeout, watch for Photos zip(s), normalize into -o."""
    _run_fetch_wizard(
        "google_takeout",
        out,
        watch_dir,
        wait_hours,
        layout,
        dedup,
        open_browser=not no_open,
    )


# ── facebook / instagram ───────────────────────────────────────────────


@facebook_app.command("fetch")
def facebook_fetch(
    out: Path = typer.Option(..., "-o", "--out", help="Output directory"),
    watch_dir: Optional[Path] = typer.Option(None, "--watch-dir"),
    wait_hours: float = typer.Option(0.0, "--wait-hours"),
    dedup: str = typer.Option("hardlink", "--dedup"),
    layout: str = typer.Option("albums", "--layout"),
    no_open: bool = typer.Option(False, "--no-open"),
) -> None:
    """Open Facebook DYI, watch for export zip, normalize into -o."""
    _run_fetch_wizard(
        "facebook",
        out,
        watch_dir,
        wait_hours,
        layout,
        dedup,
        open_browser=not no_open,
    )


@instagram_app.command("fetch")
def instagram_fetch(
    out: Path = typer.Option(..., "-o", "--out", help="Output directory"),
    watch_dir: Optional[Path] = typer.Option(None, "--watch-dir"),
    wait_hours: float = typer.Option(0.0, "--wait-hours"),
    dedup: str = typer.Option("hardlink", "--dedup"),
    layout: str = typer.Option("albums", "--layout"),
    no_open: bool = typer.Option(False, "--no-open"),
) -> None:
    """Open Instagram data download, watch for zip, normalize into -o."""
    _run_fetch_wizard(
        "instagram",
        out,
        watch_dir,
        wait_hours,
        layout,
        dedup,
        open_browser=not no_open,
    )


@instagram_app.command("experimental-pull")
def instagram_experimental_pull() -> None:
    """Not implemented in v1 — official export assist only."""
    console.print(
        "[bold red]WARNING[/bold red]: Unofficial Instagram pullers violate ToS, "
        "break often, and are [bold]not[/bold] the default path.\n"
        "Use [cyan]reclaim instagram fetch[/cyan] (official export) instead.\n"
        "This command is reserved for a future opt-in experiment."
    )
    _exit(ExitCode.USAGE)


# ── normalize / inspect / status ───────────────────────────────────────


@app.command("normalize")
def normalize_cmd(
    input_path: Path = typer.Argument(..., metavar="INPUT"),
    out: Path = typer.Option(..., "-o", "--out"),
    source: Optional[str] = typer.Option(
        None,
        "--source",
        help="google_photos|google_takeout|facebook|instagram (auto-detect if omitted)",
    ),
    dedup: str = typer.Option("hardlink", "--dedup"),
    layout: str = typer.Option("albums", "--layout"),
) -> None:
    """Unpack/normalize an export zip or folder into OUT."""
    stats, code = normalize(
        input_path,
        out,
        source=source,
        layout=_parse_layout(layout),
        dedup=_parse_dedup(dedup),
        progress=lambda m: console.print(m),
    )
    console.print(
        f"normalize: {stats.extracted} extracted, "
        f"{stats.hardlinked_exact} hardlinked_exact, "
        f"{stats.skipped_existing} skipped_existing, "
        f"{stats.errors} errors"
    )
    console.print(f"output: {out.expanduser().resolve()}")
    _exit(code)


@app.command("inspect")
def inspect_cmd(
    input_path: Path = typer.Argument(..., metavar="INPUT"),
) -> None:
    """Describe an export zip or folder without writing output."""
    report = inspect_input(input_path)
    console.print(f"path:    {report.path}")
    console.print(f"kind:    {report.kind}")
    console.print(f"source:  {report.source_guess or '(unknown)'}")
    console.print(f"media:   {report.media_count}")
    if report.zip_parts:
        console.print(f"parts:   {', '.join(report.zip_parts)}")
    for n in report.notes:
        console.print(f"note:    {n}")
    if report.sample_paths:
        console.print("samples:")
        for s in report.sample_paths:
            console.print(f"  {s}")


@app.command("status")
def status_cmd() -> None:
    """Show pending watches and config location."""
    console.print(f"config: {config_dir()}")
    jobs = list_jobs()
    pending = [j for j in jobs if j.status in ("waiting", "ready")]
    if not pending:
        console.print("No pending export watches.")
    else:
        table = Table(title="Pending jobs")
        table.add_column("id")
        table.add_column("platform")
        table.add_column("output")
        table.add_column("watch_dir")
        table.add_column("message")
        for j in pending:
            table.add_row(j.id, j.platform, j.output, j.watch_dir, j.message[:60])
        console.print(table)

    done = [j for j in jobs if j.status not in ("waiting", "ready")]
    if done:
        console.print(f"{len(done)} finished/failed job file(s) under {config_dir() / 'jobs'}")


@app.command("clear-job")
def clear_job(
    job_id: str = typer.Argument(..., help="Pending job id from reclaim status"),
) -> None:
    """Remove a pending job record."""
    if delete_job(job_id):
        console.print(f"Deleted job {job_id}")
    else:
        console.print(f"No job {job_id}")
        _exit(ExitCode.USAGE)


@app.callback()
def main(
    version: bool = typer.Option(False, "--version", help="Show version and exit"),
) -> None:
    if version:
        console.print(__version__)
        raise typer.Exit(0)


if __name__ == "__main__":
    app()
