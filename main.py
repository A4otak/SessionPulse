import asyncio
import csv
import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.prompt import Confirm, IntPrompt
from rich.table import Table

from checker import AccountResult, check_single_account
from proxy_parser import parse_proxy

console = Console()

SESSIONS_DIR = Path("sessions")
RESULT_DIR = Path("result")
PROXIES_FILE = Path("proxies.txt")


def init_workspace() -> None:
    SESSIONS_DIR.mkdir(exist_ok=True)
    RESULT_DIR.mkdir(exist_ok=True)
    if not PROXIES_FILE.exists():
        PROXIES_FILE.write_text(
            "# По одному прокси на строку:\n"
            "# socks5://user:pass@ip:port\n"
            "# http://user:pass@ip:port\n"
            "# ip:port:user:pass\n",
            encoding="utf-8",
        )


def load_proxies() -> List[Tuple[Optional[Tuple], str]]:
    proxies: List[Tuple[Optional[Tuple], str]] = []
    if not PROXIES_FILE.exists():
        return proxies

    for line in PROXIES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            parsed = parse_proxy(line)
            if parsed:
                masked = line.split("@")[-1] if "@" in line else line
                proxies.append((parsed, masked))
        except Exception as e:
            console.print(f"[yellow]Пропущен битый прокси '{line}': {e}[/yellow]")
    return proxies


def save_account_files(session_path: Path, status: str, move_files: bool) -> None:
    target_dir = RESULT_DIR / status
    target_dir.mkdir(parents=True, exist_ok=True)

    json_path = session_path.with_suffix(".json")
    op = shutil.move if move_files else shutil.copy2

    if session_path.exists():
        op(str(session_path), str(target_dir / session_path.name))
    if json_path.exists():
        op(str(json_path), str(target_dir / json_path.name))


def export_csv(results: List[AccountResult]) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = RESULT_DIR / f"report_{timestamp}.csv"
    fieldnames = [
        "filename",
        "status",
        "phone",
        "user_id",
        "username",
        "full_name",
        "dc_id",
        "premium",
        "has_2fa",
        "twofa_pass",
        "spambot_info",
        "proxy_used",
    ]
    with open(report_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        for item in results:
            writer.writerow(item.to_dict())
    return report_path


def print_summary(results: List[AccountResult], report_path: Path) -> None:
    counts = {
        "valid_clean": 0,
        "spamblock_temp": 0,
        "spamblock_perm": 0,
        "frozen": 0,
        "dead": 0,
        "proxy_error": 0,
    }
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1

    table = Table(title="Итоги проверки сессий Telegram", show_lines=True)
    table.add_column("Категория", style="bold")
    table.add_column("Кол-во", justify="center")
    table.add_column("Папка сохранения", style="dim")

    table.add_row("[green]Валид (чистые)[/green]", str(counts["valid_clean"]), "result/valid_clean/")
    table.add_row("[yellow]Временный спамблок[/yellow]", str(counts["spamblock_temp"]), "result/spamblock_temp/")
    table.add_row("[red]Вечный спамблок[/red]", str(counts["spamblock_perm"]), "result/spamblock_perm/")
    table.add_row("[magenta]Заморожены[/magenta]", str(counts["frozen"]), "result/frozen/")
    table.add_row("[bright_black]Мёртвые / бан[/bright_black]", str(counts["dead"]), "result/dead/")
    table.add_row("[cyan]Ошибка прокси[/cyan]", str(counts["proxy_error"]), "result/proxy_error/")

    console.print()
    console.print(table)
    console.print(f"\n[bold green]CSV-отчёт сохранён:[/bold green] {report_path}")


async def run() -> None:
    init_workspace()

    console.print(
        Panel.fit(
            "[bold cyan]SessionPulse v1.0[/bold cyan]\n"
            "[dim]Асинхронный чекер и сортировщик пар .session + .json (Telethon)[/dim]",
            border_style="cyan",
        )
    )

    sessions = sorted(SESSIONS_DIR.glob("*.session"))
    if not sessions:
        console.print(
            "\n[yellow]Папка 'sessions/' пуста![/yellow]\n"
            "Закинь туда файлы [bold].session[/bold] (и [bold].json[/bold], если есть) и перезапусти скрипт."
        )
        return

    proxies = load_proxies()
    console.print(f"Найдено сессий: [bold green]{len(sessions)}[/bold green]")
    console.print(
        f"Загружено прокси: [bold]{len(proxies)}[/bold] "
        f"({'работа через прокси' if proxies else '[yellow]прямое подключение без прокси[/yellow]'})"
    )

    threads = IntPrompt.ask("\nКоличество потоков", default=3)
    threads = max(1, min(threads, 30))
    check_spam = Confirm.ask("Проверять статус спамблока через @SpamBot?", default=True)
    move_files = Confirm.ask("Перемещать файлы (Да) или копировать (Нет)?", default=False)

    semaphore = asyncio.Semaphore(threads)
    results: List[AccountResult] = []

    status_colors = {
        "valid_clean": "green",
        "spamblock_temp": "yellow",
        "spamblock_perm": "red",
        "frozen": "magenta",
        "dead": "bright_black",
        "proxy_error": "cyan",
    }

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task_id = progress.add_task("Проверка аккаунтов...", total=len(sessions))

        async def worker(idx: int, session_file: Path) -> None:
            async with semaphore:
                if proxies:
                    proxy_tuple, proxy_label = proxies[idx % len(proxies)]
                else:
                    proxy_tuple, proxy_label = None, "direct"

                res = await check_single_account(
                    session_path=session_file,
                    proxy_tuple=proxy_tuple,
                    proxy_label=proxy_label,
                    check_spam=check_spam,
                )
                # Даём Windows 0.2с освободить дескриптор SQLite-файла .session
                await asyncio.sleep(0.2)
                save_account_files(session_file, res.status, move_files=move_files)
                results.append(res)

                color = status_colors.get(res.status, "white")
                prem_tag = " [gold1][PREM][/gold1]" if res.premium else ""
                twofa_tag = " [blue][2FA][/blue]" if res.has_2fa else ""
                console.print(
                    f"[{color}][{res.status.upper()}][/{color}] "
                    f"{res.filename} | {res.phone} ({res.username}) | DC{res.dc_id}"
                    f"{prem_tag}{twofa_tag} -> {res.spambot_info}"
                )
                progress.advance(task_id)

        await asyncio.gather(*(worker(i, s) for i, s in enumerate(sessions)))

    report_path = export_csv(results)
    print_summary(results, report_path)


if __name__ == "__main__":
    asyncio.run(run())