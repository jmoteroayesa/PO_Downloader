"""Search workflow for the local SAP PO Message Explorer UI (no Streamlit dependency).

Metadata filter (ICO + time range) first, then each candidate's first logged
payload version is fetched sequentially and matched locally (case-insensitive
literal). Only matching payloads are written. Payload contents are never logged.
"""

from dataclasses import dataclass, field
import time
from types import SimpleNamespace
from urllib.error import URLError

import sap_po_download as po

DEFAULT_LIMIT = 100


@dataclass
class Outcome:
    candidates: int = 0
    processed: int = 0
    matched: int = 0
    saved_files: list = field(default_factory=list)
    errors: list = field(default_factory=list)  # (message key, reason)
    cancelled: bool = False


def matches(content, needle):
    """Case-insensitive literal match; empty needle matches everything."""
    if not needle:
        return True
    text = content.decode("utf-8", errors="replace")
    return needle.casefold() in text.casefold()


def run_search(client, scenario_id, scenario_name, start, end, output, text="",
               limit=DEFAULT_LIMIT, progress=None, cancelled=None, delay=0.2):
    """Run the search and save matching payloads.

    Raises po.DownloadError if the metadata search fails or reaches `limit`
    (results would be truncated). progress(done, total, outcome) and
    cancelled() are optional callbacks.
    """
    needle = (text or "").strip()
    args = SimpleNamespace(scenario_id=scenario_id, start=start, end=end, limit=limit)
    messages = client.search(args)
    outcome = Outcome(candidates=len(messages))
    if messages:
        output.mkdir(parents=True, exist_ok=True)
    scenario_stem = po.scenario_filename_stem(scenario_name or scenario_id)
    for index, message in enumerate(messages):
        if cancelled and cancelled():
            outcome.cancelled = True
            break
        if index and delay:
            time.sleep(delay)
        key = message.findtext(f"{{{po.DATA}}}messageKey")
        version = po.first_version(message)
        if not key or not version:
            outcome.errors.append((key or "(no key)", "no key or logged payload location"))
        else:
            try:
                payloads = po.xml_payloads(client.logged_bytes(key, version))
                found = [p for p in payloads if matches(p, needle)]
                if found:
                    stem, _ = po.message_stem(message, scenario_stem)
                    for content in found:
                        outcome.saved_files.append(po.save_xml(output, stem, content))
                    outcome.matched += 1
            except (po.DownloadError, OSError, URLError, TimeoutError) as exc:
                outcome.errors.append((key, str(exc)))
        outcome.processed = index + 1
        if progress:
            progress(outcome.processed, outcome.candidates, outcome)
    return outcome
