"""ดึงผลหวยจาก ExpHuay แล้วบันทึกลง results.json

สคริปต์นี้ออกแบบให้รันบน GitHub Actions:
- ใช้เวลาไทย (Asia/Bangkok) เสมอ
- ลอง selector หลายแบบ เผื่อหน้าเว็บเปลี่ยน class
- ไม่ลบข้อมูลเก่าเมื่อหน้าเว็บต้นทางมีปัญหา
- บันทึกสถานะการทำงานไว้ใน _meta เพื่อให้หน้าเว็บแสดงได้
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright


PAGES = {
    "lao": "https://exphuay.com/result/laosdevelops",
    "hanoi_special": "https://exphuay.com/result/xsthm",
    "hanoi_normal": "https://exphuay.com/result/minhngoc",
    "hanoi_vip": "https://exphuay.com/result/mlnhngo",
}

DATA_FILE = Path(__file__).with_name("results.json")
THAI_TZ = timezone(timedelta(hours=7), name="Asia/Bangkok")
MAX_HISTORY_PER_TYPE = 730
THAI_MONTHS = {
    "มกราคม": 1, "กุมภาพันธ์": 2, "มีนาคม": 3, "เมษายน": 4,
    "พฤษภาคม": 5, "มิถุนายน": 6, "กรกฎาคม": 7, "สิงหาคม": 8,
    "กันยายน": 9, "ตุลาคม": 10, "พฤศจิกายน": 11, "ธันวาคม": 12,
}

# Selector แรกคือรูปแบบเดิม ส่วนรายการถัดไปเป็นทางสำรองเมื่อเว็บไซต์เปลี่ยน class
RESULT_SELECTORS = (
    "div.bg-gray-200.text-xl.text-black.font-semibold",
    "[data-result]",
    "[class*='result'] [class*='number']",
    "main [class*='font-semibold']",
)


def empty_data() -> dict:
    return {**{key: [] for key in PAGES}, "_meta": {}}


def load_data() -> dict:
    if not DATA_FILE.exists():
        return empty_data()

    try:
        with DATA_FILE.open(encoding="utf-8") as file:
            data = json.load(file)
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"อ่าน {DATA_FILE.name} ไม่สำเร็จ: {exc}") from exc

    for key in PAGES:
        if not isinstance(data.get(key), list):
            data[key] = []
    if not isinstance(data.get("_meta"), dict):
        data["_meta"] = {}
    return data


def digits_only(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def extract_numbers(page: Page) -> list[str]:
    """คืนค่ากลุ่มตัวเลขที่มองเห็น โดยกรองปี เวลา และข้อความอื่นออก"""
    for selector in RESULT_SELECTORS:
        values = page.locator(selector).all_inner_texts()
        numbers = [digits_only(value) for value in values]
        numbers = [value for value in numbers if 2 <= len(value) <= 6]
        if len(numbers) >= 2:
            return numbers

    # ทางสำรองสุดท้าย: มองหา element ที่มีเฉพาะเลข 2–6 หลัก
    values = page.locator("main div, main span, main p").all_inner_texts()
    candidates = []
    for value in values:
        stripped = value.strip()
        if re.fullmatch(r"\d{2,6}", stripped):
            candidates.append(stripped)

    # ตัดค่าซ้ำโดยรักษาลำดับเดิม
    return list(dict.fromkeys(candidates))


def extract_draw_date(page: Page, fallback: datetime) -> str:
    heading = page.locator("h1").first.inner_text().strip()
    match = re.search(r"(\d{1,2})\s+([ก-๙]+)\s+(\d{4})", heading)
    if not match or match.group(2) not in THAI_MONTHS:
        return fallback.date().isoformat()

    day, month_name, year = match.groups()
    christian_year = int(year) - 543 if int(year) > 2400 else int(year)
    return datetime(christian_year, THAI_MONTHS[month_name], int(day)).date().isoformat()


def scrape_one(page: Page, url: str, now: datetime) -> tuple[list[str], str]:
    page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    page.wait_for_timeout(5_000)

    # บางหน้าดึงผลด้วย JavaScript หลังหน้าเว็บหลักโหลดเสร็จ
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except PlaywrightTimeoutError:
        pass

    numbers = extract_numbers(page)
    if len(numbers) < 2:
        raise RuntimeError(
            f"ไม่พบชุดผลรางวัล (title={page.title()!r}, url={page.url!r})"
        )
    return numbers, extract_draw_date(page, now)


def normalize_result(key: str, numbers: list[str]) -> tuple[str, str, str]:
    """แปลงข้อมูลหน้าเว็บเป็น เลขหลัก, 3 ตัวบน และ 2 ตัวล่าง"""
    if key == "lao":
        # หวยลาวมักแสดงเลขหลัก 6 ตัว แล้วตามด้วย 3 ตัวบน/2 ตัวล่าง
        main = next((n for n in numbers if len(n) == 6), numbers[0])
    else:
        # ฮานอยอาจแสดงเลขหลัก 5 ตัว หรือแสดงเฉพาะ 3 ตัวบนกับ 2 ตัวล่าง
        main = next((n for n in numbers if len(n) == 5), "")

    top3 = next((n for n in numbers if len(n) == 3), main[-3:] if main else "")
    bottom2 = next((n for n in numbers if len(n) == 2), main[-2:] if main else "")

    if not main:
        main = f"{top3}{bottom2}"
    if not (main and top3 and bottom2):
        raise RuntimeError(f"ข้อมูลไม่ครบ: {numbers!r}")
    return main, top3, bottom2


def upsert(
    entries: list[dict],
    draw_date: str,
    now: datetime,
    main: str,
    top3: str,
    bottom2: str,
) -> list[dict]:
    entries = [entry for entry in entries if entry.get("date") != draw_date]
    entries.append(
        {
            "date": draw_date,
            "time": now.strftime("%H:%M"),
            "status": "out",
            "main": main,
            "top3": top3,
            "bottom2": bottom2,
        }
    )
    return sorted(
        entries,
        key=lambda entry: (entry.get("date", ""), entry.get("time", "")),
        reverse=True,
    )[:MAX_HISTORY_PER_TYPE]


def save_data(data: dict) -> None:
    temporary_file = DATA_FILE.with_suffix(".json.tmp")
    with temporary_file.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")
    temporary_file.replace(DATA_FILE)


def main() -> int:
    data = load_data()
    now = datetime.now(THAI_TZ)
    errors: dict[str, str] = {}
    updated: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            locale="th-TH",
            timezone_id="Asia/Bangkok",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/138.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1365, "height": 768},
        )
        page = context.new_page()

        for key, url in PAGES.items():
            try:
                numbers, draw_date = scrape_one(page, url, now)
                main_number, top3, bottom2 = normalize_result(key, numbers)
                data[key] = upsert(data[key], draw_date, now, main_number, top3, bottom2)
                updated.append(key)
                print(f"[{key}] สำเร็จ: {main_number} / {top3} / {bottom2}")
            except Exception as exc:  # เก็บข้อมูลเก่าไว้และรายงานแต่ละหน้า
                errors[key] = str(exc)
                print(f"[{key}] ไม่สำเร็จ: {exc}", file=sys.stderr)

        context.close()
        browser.close()

    data["_meta"] = {
        "last_run": now.isoformat(timespec="seconds"),
        "updated": updated,
        "errors": errors,
    }
    save_data(data)

    if not updated:
        print("ไม่สามารถดึงข้อมูลได้ทุกประเภท", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
