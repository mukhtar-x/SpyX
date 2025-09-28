# ethical_spyx/core.py
import asyncio
import aiohttp
import re
import json
from pathlib import Path
from urllib.parse import quote_plus
from collections import defaultdict
from typing import List, Dict, Optional, Tuple
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn, MofNCompleteColumn
from rich.table import Table
from rich.text import Text
import getpass
import sys
import logging

console = Console()

# -------------------------
# Load config
# -------------------------
CONFIG_PATH = Path(__file__).parent / "Json/config.json"
if CONFIG_PATH.exists():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        CONFIG = json.load(f)
else:
    CONFIG = {
        "password": "nooneknows",
        "user_agent": "SpyX/2.0",
        "concurrency": 20,
        "timeout": 10
    }

# -------------------------
# Logging
# -------------------------
LOG_PATH = Path(__file__).parent / "spyx.log"
logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# -------------------------
# Authentication
# -------------------------
def authenticate():
    entered = getpass.getpass("Enter SpyX password: ")
    if entered.strip() != CONFIG.get("password", "nooneknows"):
        console.print("Authentication failed. Exiting.", style="bold red")
        logging.warning("Authentication failed.")
        sys.exit(1)
    console.print("Authentication successful!\n", style="bold green")
    logging.info("Authentication successful.")

# -------------------------
# Site-specific rules
# -------------------------
SITE_RULES = {
    "twitter": {"error": r"page doesn.t exist", "exists": r"followers"},
    "instagram": {"error": r"page not found", "exists": r"followers"},
    "github": {"error": r"not found", "exists": r"repositories"},
}

# -------------------------
# Helpers
# -------------------------
def load_sites() -> Dict[str, Dict[str, List[str]]]:
    """
    Load all site categories from sites.json.
    Returns: category -> {site_name: [patterns]}
    """
    path = Path(__file__).parent / "Json/sites.json"
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data

def generate_username_variants(base: str) -> List[str]:
    if not base:
        return []
    base = base.strip()
    variants = [base, base.lower(), base.upper()]
    seen, out = set(), []
    for v in variants:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out

def generate_emails_no_perms(base: str, domains: List[str]) -> List[str]:
    if not base or not domains:
        return []
    base = base.strip()
    if "@" in base:
        return [base]
    return [f"{base}@{d}" for d in domains]

def generate_phone_no_perms(phone: Optional[str]) -> List[str]:
    return [phone.strip()] if phone else []

# -------------------------
# Async request + retries
# -------------------------
async def fetch(session: aiohttp.ClientSession, url: str, base_timeout: int) -> Tuple[int, str, Optional[str]]:
    timeout = base_timeout
    while timeout <= 100:
        try:
            async with session.get(url, timeout=timeout, allow_redirects=True) as resp:
                text = await resp.text(errors="ignore")
                return resp.status, str(resp.url), text
        except asyncio.TimeoutError:
            timeout += 10
        except Exception as e:
            logging.error(f"Fetch error for {url}: {e}")
            return -1, f"error: {e}", None
    return -1, "timeout-100s", None

def analyze_response(status: int, final_url: str, text: Optional[str], site: str = "") -> Tuple[bool, str]:
    if status == -1:
        return False, "network-error"
    if status == 200:
        site_rules = SITE_RULES.get(site.lower(), {})
        if text:
            if "error" in site_rules and re.search(site_rules["error"], text, re.I):
                return False, "error-pattern"
            if "exists" in site_rules and re.search(site_rules["exists"], text, re.I):
                return True, "found-pattern"
            not_found_patterns = [
                "not found", "does not exist", "unavailable",
                "no such user", "could not be found", "page isn.t available"
            ]
            for nf in not_found_patterns:
                if nf in text.lower():
                    return False, f"contains-{nf}"
        if any(k in final_url.lower() for k in ["/login", "/signin"]):
            return False, "redirected-to-login"
        return True, "found"
    if status == 404:
        return False, "not-found"
    if status in [401, 403]:
        return False, "unauthorized"
    if status == 429:
        return False, "rate-limited"
    if status >= 500:
        return False, f"server-error-{status}"
    return False, "fallback"

# -------------------------
# Main orchestrator
# -------------------------
async def check(base: str,
                domains: List[str],
                phone: Optional[str],
                concurrency: int = None,
                timeout: int = None):
    authenticate()

    concurrency = concurrency or CONFIG.get("concurrency", 20)
    timeout = timeout or CONFIG.get("timeout", 10)

    all_categories = load_sites()  # e.g., social, specific, public
    username_tokens = generate_username_variants(base)
    email_tokens = generate_emails_no_perms(base, domains)
    phone_tokens = generate_phone_no_perms(phone)
    tokens = list(dict.fromkeys(username_tokens + email_tokens + phone_tokens))

    if not tokens:
        console.print("No tokens to try (empty base?).", style="bold red")
        logging.warning("No tokens to try. Base: %s", base)
        return

    # Flatten all categories
    all_sites = {site: patterns for category in all_categories.values() for site, patterns in category.items()}
    site_categories = {site: cat for cat, sites in all_categories.items() for site in sites}  # map site->category

    results = defaultdict(list)

    progress = Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        transient=True
    )
    task_id = progress.add_task("Scanning...", total=len(all_sites) * len(tokens))

    semaphore = asyncio.Semaphore(concurrency)
    headers = {"User-Agent": CONFIG.get("user_agent", "SpyX/2.0")}

    async with aiohttp.ClientSession(headers=headers) as session:
        async def job(site: str, pattern: str, token: str):
            async with semaphore:
                try:
                    url = pattern.format(quote_plus(token)) if "?" in pattern or "search" in pattern.lower() else pattern.format(token)
                except Exception:
                    results[site].append({"token": token, "found": False, "reason": "bad-pattern", "url": None})
                    progress.update(task_id, advance=1, description=f"Invalid pattern → {site}")
                    logging.error("Bad pattern for site %s, token %s", site, token)
                    return
                progress.update(task_id, description=f"Trying {site} → {token[:20]}")
                status, final_url, text = await fetch(session, url, timeout)
                found, reason = analyze_response(status, final_url, text, site)
                results[site].append({
                    "token": token,
                    "found": found,
                    "reason": reason,
                    "url": final_url if found else None
                })
                progress.update(task_id, advance=1)

        tasks = [asyncio.create_task(job(site, p, token))
                 for site, patterns in all_sites.items()
                 for token in tokens
                 for p in patterns]

        with progress:
            await asyncio.gather(*tasks)

    # -------------------------
    # Results Table
    # -------------------------
    table = Table(title="SpyX Results", show_header=True, header_style="bold", show_lines=False, box=None)
    table.add_column("Category")
    table.add_column("Site")
    table.add_column("Token")
    table.add_column("Result", justify="center")
    table.add_column("Reason / Link")

    def prettify(e: dict) -> Tuple[Text, Text]:
        if e["found"]:
            return Text("FOUND", style="bold green"), Text(e.get("url", "Exists"), style="blue")
        reason = e["reason"]
        mapping = {
            "not-found": "Not Found",
            "unauthorized": "Unauthorized",
            "rate-limited": "Rate Limited",
            "redirected-to-login": "Login Wall",
            "bad-pattern": "Pattern Error",
            "network-error": "Network Error",
            "fallback": "Unknown",
        }
        if reason.startswith("status-"):
            return Text("FAIL", style="bold red"), Text(f"HTTP {reason.split('-')[1]}", style="red")
        if reason.startswith("server-error"):
            return Text("FAIL", style="bold red"), Text("Server Error", style="red")
        if reason.startswith("contains-"):
            return Text("FAIL", style="bold red"), Text(reason.replace("contains-", "").capitalize(), style="red")
        return Text("FAIL", style="bold red"), Text(mapping.get(reason, reason), style="red")

    # Stats counters
    total_tokens = 0
    total_found = 0
    total_failed = 0
    total_errors = 0

    for site, entries in results.items():
        category = site_categories.get(site, "Unknown")
        seen = {}
        for e in entries:
            key = (site, e["token"])
            if key not in seen or (not seen[key]["found"] and e["found"]):
                seen[key] = e
        for (s, t), e in seen.items():
            icon, reason = prettify(e)
            table.add_row(category, s, t, icon, reason)
            table.add_row("─"*10, "─"*20, "─"*20, "─"*10, "─"*30)

            total_tokens += 1
            if e["found"]:
                total_found += 1
            else:
                total_failed += 1
                if e["reason"] in ["network-error", "rate-limited", "fallback"]:
                    total_errors += 1

    console.clear()
    console.print(table)

    # -------------------------
    # Summary
    # -------------------------
    console.print(f"\n[bold]Summary:[/bold] Total Tokens: {total_tokens} | Found: [green]{total_found}[/green] | Failed: [red]{total_failed}[/red] | Errors: [yellow]{total_errors}[/yellow]")
    logging.info("Scan Summary: Total=%d Found=%d Failed=%d Errors=%d", total_tokens, total_found, total_failed, total_errors)

    return results
