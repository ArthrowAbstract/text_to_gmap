#!/usr/bin/env python3
"""Match clinic records to Google Maps listings using Playwright automation.

This script automates Google Maps searches based on clinic name + pincode/city,
collects candidate listings, scores them, and outputs a match decision per row.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable, List, Optional

from playwright.sync_api import sync_playwright

STOP_WORDS = {
    "clinic",
    "hospital",
    "center",
    "centre",
    "medical",
    "health",
    "healthcare",
    "the",
    "and",
    "of",
}

ABBREVIATIONS = {
    "hosp": "hospital",
    "ctr": "center",
    "ctre": "center",
    "med": "medical",
}

PINCODE_RE = re.compile(r"\b\d{5,6}\b")


@dataclass
class Candidate:
    name: str
    address: str
    url: str
    pincode: Optional[str]
    city: Optional[str]


@dataclass
class MatchResult:
    status: str
    confidence: float
    url: Optional[str]
    candidates: List[dict]
    notes: str


def normalize_text(value: str) -> str:
    lowered = re.sub(r"[^a-z0-9\s]", " ", value.lower())
    tokens = []
    for token in lowered.split():
        token = ABBREVIATIONS.get(token, token)
        if token:
            tokens.append(token)
    return " ".join(tokens)


def normalize_name(value: str) -> str:
    tokens = [
        token
        for token in normalize_text(value).split()
        if token not in STOP_WORDS
    ]
    return " ".join(tokens)


def similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def extract_pincode(address: str) -> Optional[str]:
    match = PINCODE_RE.search(address)
    return match.group(0) if match else None


def choose_best_name(row: dict) -> str:
    openloop = (row.get("openloop_providername") or "").strip()
    provider = (row.get("providername") or "").strip()
    if openloop:
        return openloop
    if provider:
        return provider
    speciality = (row.get("speciality") or "").strip()
    city = (row.get("providercity") or "").strip()
    return " ".join(part for part in [speciality, city] if part)


def build_query(row: dict) -> str:
    name = choose_best_name(row)
    pincode = (row.get("providerpincode") or "").strip()
    city = (row.get("providercity") or "").strip()
    return " ".join(part for part in [name, pincode, city] if part)


def score_candidate(row: dict, candidate: Candidate) -> tuple[float, str]:
    raw_name = choose_best_name(row)
    raw_address = (row.get("provderaddress") or "").strip()
    raw_city = (row.get("providercity") or "").strip()
    raw_pincode = (row.get("providerpincode") or "").strip()

    name_sim = similarity(normalize_name(raw_name), normalize_name(candidate.name))
    addr_sim = similarity(normalize_text(raw_address), normalize_text(candidate.address))
    pincode_match = 1.0 if raw_pincode and candidate.pincode == raw_pincode else 0.0
    city_match = 1.0 if raw_city and candidate.city and raw_city.lower() in candidate.city.lower() else 0.0

    score = (0.45 * name_sim) + (0.35 * addr_sim) + (0.15 * pincode_match) + (0.05 * city_match)
    note_parts = []
    if pincode_match == 0.0 and raw_pincode:
        note_parts.append("pincode mismatch")
    if addr_sim < 0.5 and raw_address:
        note_parts.append("low address similarity")
    notes = ", ".join(note_parts) if note_parts else "signals aligned"
    return score, notes


def decide_match(scored: List[tuple[Candidate, float, str]]) -> MatchResult:
    if not scored:
        return MatchResult("no_match", 0.0, None, [], "no candidates")
    scored.sort(key=lambda item: item[1], reverse=True)
    best_candidate, best_score, best_note = scored[0]
    if len(scored) == 1 and best_score >= 0.8:
        return MatchResult("single_match", best_score, best_candidate.url, [], best_note)
    if best_score >= 0.8 and (len(scored) == 1 or best_score - scored[1][1] >= 0.1):
        return MatchResult("single_match", best_score, best_candidate.url, [], best_note)
    multi = [
        {
            "name": candidate.name,
            "address": candidate.address,
            "url": candidate.url,
            "confidence": score,
            "notes": note,
        }
        for candidate, score, note in scored
        if score >= 0.7
    ]
    if multi:
        return MatchResult("multi_match", best_score, None, multi, "multiple candidates")
    return MatchResult("no_match", best_score, None, [], "best score below threshold")


def wait_for_results(page) -> None:
    page.wait_for_timeout(1500)
    page.wait_for_load_state("networkidle")


def get_place_details(page) -> Optional[Candidate]:
    name_el = page.query_selector("h1")
    if not name_el:
        return None
    name = name_el.inner_text().strip()
    address_el = page.query_selector("button[data-item-id='address']")
    address = address_el.inner_text().strip() if address_el else ""
    url = page.url
    pincode = extract_pincode(address)
    city = None
    if address:
        parts = [part.strip() for part in address.split(",") if part.strip()]
        if len(parts) >= 2:
            city = parts[-2]
    return Candidate(name=name, address=address, url=url, pincode=pincode, city=city)


def get_list_results(page, max_results: int) -> List[Candidate]:
    results = []
    cards = page.query_selector_all("div[role='feed'] div[role='article']")
    for card in cards[:max_results]:
        name_el = card.query_selector("a[aria-label]")
        name = name_el.get_attribute("aria-label") if name_el else ""
        address_el = card.query_selector(".W4Efsd span[jsan]")
        address = address_el.inner_text().strip() if address_el else ""
        link_el = card.query_selector("a[aria-label]")
        url = link_el.get_attribute("href") if link_el else page.url
        pincode = extract_pincode(address)
        city = None
        if address:
            parts = [part.strip() for part in address.split(",") if part.strip()]
            if len(parts) >= 2:
                city = parts[-2]
        if name:
            results.append(Candidate(name=name, address=address, url=url, pincode=pincode, city=city))
    return results


def search_maps(page, query: str, max_results: int) -> List[Candidate]:
    page.goto("https://www.google.com/maps", wait_until="domcontentloaded")
    search_box = page.wait_for_selector("input#searchboxinput")
    search_box.fill(query)
    search_box.press("Enter")
    wait_for_results(page)
    place = get_place_details(page)
    if place:
        return [place]
    return get_list_results(page, max_results)


def load_rows(path: Path) -> Iterable[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            yield row


def write_outputs(output_path: Path, rows: Iterable[dict]) -> None:
    with output_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def process_csv(input_path: Path, output_path: Path, max_results: int, headless: bool) -> None:
    processed = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        page = browser.new_page()
        for row in load_rows(input_path):
            query = build_query(row)
            if not query:
                row.update(
                    {
                        "matched_status": "no_match",
                        "matched_confidence": 0.0,
                        "google_maps_url": None,
                        "google_maps_candidates": [],
                        "match_notes": "missing search query",
                    }
                )
                processed.append(row)
                continue
            candidates = search_maps(page, query, max_results)
            scored = []
            for candidate in candidates:
                score, note = score_candidate(row, candidate)
                scored.append((candidate, score, note))
            result = decide_match(scored)
            row.update(
                {
                    "matched_status": result.status,
                    "matched_confidence": round(result.confidence, 3),
                    "google_maps_url": result.url,
                    "google_maps_candidates": result.candidates,
                    "match_notes": result.notes,
                }
            )
            processed.append(row)
            time.sleep(1.0)
        browser.close()
    write_outputs(output_path, processed)


def main() -> None:
    parser = argparse.ArgumentParser(description="Match clinic rows to Google Maps listings.")
    parser.add_argument("--input", required=True, help="Path to input CSV file")
    parser.add_argument("--output", required=True, help="Path to output JSONL file")
    parser.add_argument("--max-results", type=int, default=5, help="Max results per query")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    args = parser.parse_args()

    process_csv(Path(args.input), Path(args.output), args.max_results, args.headless)


if __name__ == "__main__":
    main()
