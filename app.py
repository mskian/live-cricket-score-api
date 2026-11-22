import re
import html
import time
from typing import Dict, List, Optional, Tuple

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import (
    JSONResponse,
    PlainTextResponse,
    HTMLResponse,
)
from pydantic import BaseModel, ValidationError, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException


NOT_FOUND = "score not found"
REQUEST_TIMEOUT = "request timeout"
INVALID_MATCH_ID = "invalid score id"


class APIError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message


class Batsman(BaseModel):
    name: str = NOT_FOUND
    score: str = NOT_FOUND

    @field_validator("name", "score", mode="before")
    @classmethod
    def validate_non_empty(cls, value: Optional[str]) -> str:
        if not value or not str(value).strip():
            return NOT_FOUND
        return html.unescape(str(value).strip())


class Bowler(BaseModel):
    name: str = NOT_FOUND
    over: str = NOT_FOUND
    runs: str = NOT_FOUND
    wicket: str = NOT_FOUND

    @field_validator("name", "over", "runs", "wicket", mode="before")
    @classmethod
    def validate_non_empty(cls, value: Optional[str]) -> str:
        if not value or not str(value).strip():
            return NOT_FOUND
        return html.unescape(str(value).strip())


class ScoreResponse(BaseModel):
    status: str
    title: str
    match_status: str = NOT_FOUND
    score: str
    run_rate: str = NOT_FOUND
    current_batsmen: List[Batsman]
    current_bowler: Bowler

    @field_validator("title", "match_status", "score", "run_rate", mode="before")
    @classmethod
    def validate_fields(cls, value: Optional[str]) -> str:
        if not value or not str(value).strip():
            return NOT_FOUND
        return html.unescape(str(value).strip())

    @field_validator("current_batsmen")
    @classmethod
    def validate_batsmen_count(cls, value: List[Batsman]) -> List[Batsman]:
        batsmen = list(value[:2])
        while len(batsmen) < 2:
            batsmen.append(Batsman())
        return batsmen


class MatchValidator(BaseModel):
    score: str

    @field_validator("score")
    @classmethod
    def validate_match_id(cls, value: str) -> str:
        value = value.strip()

        if not value:
            raise ValueError(INVALID_MATCH_ID)

        if not value.isdigit():
            raise ValueError("score id must contain digits only")

        if len(value) < 4:
            raise ValueError("score id must be at least 4 digits")

        if len(value) > 20:
            raise ValueError("score id too long")

        return value


app = FastAPI(
    title="Score API",
    version="0.0.7",
    description="Live Cricket Score JSON API",
    docs_url=None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)

    response.headers["Cache-Control"] = (
        "no-store, no-cache, must-revalidate, "
        "proxy-revalidate, max-age=0"
    )
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["Surrogate-Control"] = "no-store"

    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"

    response.headers["Strict-Transport-Security"] = (
        "max-age=31536000"
    )

    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "connect-src 'self' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "img-src 'self' data: https://fastapi.tiangolo.com; "
        "object-src 'none'; "
        "frame-ancestors 'none';"
    )

    return response


class ScoreService:
    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(X11; Linux x86_64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/146.0.0.0 "
            "Safari/537.36"
        ),
        "Referer": "https://www.cricbuzz.com/",
        "Origin": "https://www.cricbuzz.com",
        "Cache-Control": "no-cache, no-store, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
        "Connection": "close",
        "Accept": (
            "text/html,"
            "application/xhtml+xml,"
            "application/xml;q=0.9,"
            "*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }

    STATUS_CLASS_PATTERN = re.compile(
        r"(?:cb-text-(?:live|complete|stump|rain|abandon|delay|preview|inprogress|lunch|tea|inningsbreak)|"
        r"text-cb(?:Text)?(?:Live|Complete|Preview|Stump|Rain|Abandon|Delay))",
        re.IGNORECASE,
    )

    INVALID_STATUS_VALUES = {
        "success",
        "error",
        "ok",
        "true",
        "false",
        "null",
        "none",
        "live",
        "complete",
        "upcoming",
        "preview",
        "in progress",
    }

    @staticmethod
    def clean(text: str) -> str:
        if not text:
            return NOT_FOUND
        cleaned = html.unescape(" ".join(text.split())).strip()
        return cleaned if cleaned else NOT_FOUND

    @staticmethod
    def decode_json_string(value: str) -> str:
        decoded = value.replace("\\/", "/").replace('\\"', '"')
        decoded = re.sub(
            r"\\u([0-9a-fA-F]{4})",
            lambda m: chr(int(m.group(1), 16)),
            decoded,
        )
        return html.unescape(" ".join(decoded.split())).strip()

    @staticmethod
    def normalize_json_html(raw_html: str) -> str:
        return re.sub(r'\\+"', '"', raw_html)

    @staticmethod
    def extract_miniscore_scope(norm_html: str) -> str:
        match = re.search(r'"miniscore"\s*:\s*\{', norm_html)
        if not match:
            return norm_html
        start = match.start()
        return norm_html[start : start + 25000]

    @staticmethod
    def format_overs_value(raw_overs: str) -> str:
        cleaned = raw_overs.strip()
        if cleaned.endswith(".0"):
            return cleaned[:-2]
        return cleaned

    @staticmethod
    def compute_run_rate(runs: int, overs_str: str) -> Optional[str]:
        try:
            parts = overs_str.strip().split(".")
            completed = int(parts[0])
            balls = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            if balls >= 6:
                return None
            total_overs = completed + (balls / 6.0)
            if total_overs <= 0:
                return None
            return f"{runs / total_overs:.2f}"
        except Exception:
            return None

    @classmethod
    def is_valid_match_status(cls, candidate: str) -> bool:
        if not candidate:
            return False
        normalized = candidate.strip()
        if len(normalized) < 4 or len(normalized) > 160:
            return False
        if normalized.lower() in cls.INVALID_STATUS_VALUES:
            return False
        if normalized.isdigit():
            return False
        if "http://" in normalized.lower() or "https://" in normalized.lower():
            return False
        return True

    @classmethod
    def default_batsmen(cls) -> List[Batsman]:
        return [Batsman(), Batsman()]

    @classmethod
    def extract_innings_list(cls, scope_html: str) -> List[Dict[str, object]]:
        best: List[Dict[str, object]] = []

        for list_match in re.finditer(
            r'"inningsScoreList"\s*:\s*\[(.*?)\]',
            scope_html,
            re.DOTALL,
        ):
            innings: List[Dict[str, object]] = []
            for obj_match in re.finditer(r"\{([^{}]*)\}", list_match.group(1)):
                block = obj_match.group(1)
                team_m = re.search(
                    r'"(?:batTeamName|batTeamSName)"\s*:\s*"([^"]+)"', block
                )
                if not team_m:
                    continue

                innings_id_m = re.search(r'"inningsId"\s*:\s*(\d+)', block)
                score_m = re.search(r'"score"\s*:\s*(\d+)', block)
                wkts_m = re.search(r'"wickets"\s*:\s*(\d+)', block)
                overs_m = re.search(
                    r'"overs"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)"?', block
                )
                decl_m = re.search(
                    r'"isDeclared"\s*:\s*(true|false)', block, re.IGNORECASE
                )

                innings.append(
                    {
                        "innings_id": int(innings_id_m.group(1))
                        if innings_id_m
                        else len(innings) + 1,
                        "team": cls.decode_json_string(team_m.group(1)),
                        "runs": int(score_m.group(1)) if score_m else 0,
                        "wickets": int(wkts_m.group(1)) if wkts_m else 0,
                        "overs": cls.format_overs_value(overs_m.group(1))
                        if overs_m
                        else "",
                        "declared": bool(
                            decl_m and decl_m.group(1).lower() == "true"
                        ),
                    }
                )

            if len(innings) > len(best):
                best = innings

        best.sort(key=lambda item: int(item["innings_id"]))
        return best

    @classmethod
    def extract_score_and_context(
        cls, og_title: str, page_text: str, miniscore_html: str, norm_html: str
    ) -> Tuple[str, Optional[int], Optional[str]]:
        innings = cls.extract_innings_list(miniscore_html)
        if not innings and miniscore_html is not norm_html:
            innings = cls.extract_innings_list(norm_html)

        if innings:
            active_id_m = re.search(
                r'"miniscore"\s*:\s*\{.{0,1200}?"inningsId"\s*:\s*(\d+)',
                miniscore_html,
                re.DOTALL,
            )
            active_id = int(active_id_m.group(1)) if active_id_m else None

            current = dict(
                next(
                    (item for item in innings if item["innings_id"] == active_id),
                    innings[-1],
                )
            )

            bat_team_m = re.search(
                r'"batTeam"\s*:\s*\{([^}]*)\}', miniscore_html
            )
            if bat_team_m:
                bt_block = bat_team_m.group(1)
                bt_str_score = re.search(
                    r'"teamScore"\s*:\s*"(\d+)[/-](\d+)"', bt_block
                )
                if bt_str_score:
                    current["runs"] = int(bt_str_score.group(1))
                    current["wickets"] = int(bt_str_score.group(2))
                else:
                    bt_runs = re.search(r'"teamScore"\s*:\s*(\d+)', bt_block)
                    bt_wkts = re.search(r'"teamWkts"\s*:\s*(\d+)', bt_block)
                    if bt_runs:
                        current["runs"] = int(bt_runs.group(1))
                        if bt_wkts:
                            current["wickets"] = int(bt_wkts.group(1))

            live_overs_m = re.search(
                r'"miniscore"\s*:\s*\{.{0,1500}?"overs"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)"?',
                miniscore_html,
                re.DOTALL,
            )
            if live_overs_m:
                current["overs"] = cls.format_overs_value(live_overs_m.group(1))

            team = str(current["team"])
            runs = int(current["runs"])
            wickets = int(current["wickets"])
            overs = str(current["overs"])
            declared = bool(current["declared"])

            curr_runs_str = f"{runs}/{wickets}d" if declared else f"{runs}/{wickets}"
            curr_display = f"{curr_runs_str} ({overs})" if overs else curr_runs_str

            prev_same_team = [
                item
                for item in innings
                if str(item["team"]).lower() == team.lower()
                and int(item["innings_id"]) < int(current["innings_id"])
            ]

            if prev_same_team:
                prev = prev_same_team[-1]
                prev_runs = int(prev["runs"])
                prev_wkts = int(prev["wickets"])
                prev_decl = bool(prev["declared"])
                if prev_decl:
                    prev_str = f"{prev_runs}/{prev_wkts}d"
                elif prev_wkts >= 10:
                    prev_str = str(prev_runs)
                else:
                    prev_str = f"{prev_runs}/{prev_wkts}"
                score_str = f"{team} {prev_str} & {curr_display}"
            else:
                score_str = f"{team} {curr_display}"

            return cls.clean(score_str), runs, overs or None

        score_pattern = re.compile(
            r"\b([A-Z][A-Za-z0-9]{1,5})\s+(?:(\d+(?:[/-]\d+)?\s*d?)\s*&\s*)?(\d+)(?:[/-](\d+))?\s*(d)?\s*\(([\d.]+)\)"
        )

        if og_title:
            og_head = html.unescape(og_title).split("|")[0]
            multi_match = score_pattern.findall(og_head)
            if multi_match:
                team, prev_inn, runs_s, wkts_s, decl_s, overs_s = multi_match[-1]
                overs_clean = cls.format_overs_value(overs_s)
                curr_part = f"{runs_s}/{wkts_s}" if wkts_s else runs_s
                if decl_s:
                    curr_part = f"{curr_part}d"
                prev_clean = prev_inn.strip().replace("-", "/")
                if prev_clean:
                    score_str = f"{team} {prev_clean} & {curr_part} ({overs_clean})"
                else:
                    score_str = f"{team} {curr_part} ({overs_clean})"
                return cls.clean(score_str), int(runs_s), overs_clean

        text_crr_match = re.search(
            r"\b([A-Z]{2,5})\s+(?:(\d+(?:[/-]\d+)?\s*d?)\s*&\s*)?(\d+)[/-](\d+)\s*(d)?\s*\(([\d.]+)\)\s*CRR",
            page_text,
        )
        if text_crr_match:
            team, prev_inn, runs_s, wkts_s, decl_s, overs_s = text_crr_match.groups()
            overs_clean = cls.format_overs_value(overs_s)
            curr_part = f"{runs_s}/{wkts_s}d" if decl_s else f"{runs_s}/{wkts_s}"
            prev_clean = prev_inn.strip().replace("-", "/") if prev_inn else ""
            if prev_clean:
                score_str = f"{team} {prev_clean} & {curr_part} ({overs_clean})"
            else:
                score_str = f"{team} {curr_part} ({overs_clean})"
            return cls.clean(score_str), int(runs_s), overs_clean

        text_any_match = score_pattern.search(page_text[:1500])
        if text_any_match:
            team, prev_inn, runs_s, wkts_s, decl_s, overs_s = text_any_match.groups()
            overs_clean = cls.format_overs_value(overs_s)
            curr_part = f"{runs_s}/{wkts_s}" if wkts_s else runs_s
            if decl_s:
                curr_part = f"{curr_part}d"
            prev_clean = prev_inn.strip().replace("-", "/") if prev_inn else ""
            if prev_clean:
                score_str = f"{team} {prev_clean} & {curr_part} ({overs_clean})"
            else:
                score_str = f"{team} {curr_part} ({overs_clean})"
            return cls.clean(score_str), int(runs_s), overs_clean

        return NOT_FOUND, None, None

    @classmethod
    def extract_match_status(
        cls, soup: BeautifulSoup, page_text: str, miniscore_html: str, norm_html: str
    ) -> str:
        json_patterns = (
            r'"matchScoreDetails"\s*:\s*\{[^{}]*?"customStatus"\s*:\s*"([^"]+)"',
            r'"miniscore"\s*:\s*\{.{0,2000}?"(?:custStatus|customStatus|status)"\s*:\s*"([^"]+)"',
            r'"matchHeader"\s*:\s*\{.{0,2000}?"status"\s*:\s*"([^"]+)"',
            r'"(?:customStatus|custStatus)"\s*:\s*"([^"]+)"',
        )

        for target in (miniscore_html, norm_html):
            for pattern in json_patterns:
                match = re.search(pattern, target, re.DOTALL)
                if match:
                    candidate = cls.decode_json_string(match.group(1))
                    if cls.is_valid_match_status(candidate):
                        return cls.clean(candidate)

        for tag in soup.find_all(["div", "span", "p"], class_=cls.STATUS_CLASS_PATTERN):
            candidate = " ".join(tag.get_text(" ", strip=True).split())
            if cls.is_valid_match_status(candidate):
                return cls.clean(candidate)

        text_patterns = (
            r"(Day\s+\d+\s*:\s*(?:Stumps|Lunch|Tea|Dinner|Innings Break|Rain|Bad Light|Play Stopped|Session \d+|\d+(?:st|nd|rd)\s+Session)[^|•\n]{0,80})",
            r"(Match\s+(?:abandoned|drawn|tied|delayed|starts|cancelled|suspended)[^|•\n]{0,80})",
            r"([A-Za-z0-9 .'-]{2,40}\s+won\s+by\s+[^|•\n]{2,60})",
            r"([A-Za-z0-9 .'-]{2,40}\s+opt(?:ed)?\s+to\s+(?:bat|bowl)[^|•\n]{0,40})",
            r"([A-Za-z0-9 .'-]{2,40}\s+(?:lead|trail|need)\s+by\s+[^|•\n]{2,60})",
            r"([A-Za-z0-9 .'-]{2,40}\s+need\s+\d+\s+runs?[^|•\n]{0,60})",
            r"((?:Rain|Wet outfield|Bad light|Toss delayed|Play delayed|Play stopped|No result)[^|•\n]{0,80})",
        )

        for pattern in text_patterns:
            match = re.search(pattern, page_text, re.IGNORECASE)
            if match:
                candidate = re.split(
                    r"\s+(?:Batter|Bowler|CRR|REQ|Partnership|Last wicket|Commentary|Scorecard)\b",
                    match.group(1),
                    maxsplit=1,
                    flags=re.IGNORECASE,
                )[0].strip(" -|•")
                if cls.is_valid_match_status(candidate):
                    return cls.clean(candidate)

        state_match = re.search(
            r'"stateTitle"\s*:\s*"([^"]+)"',
            norm_html,
        )
        if state_match:
            candidate = cls.decode_json_string(state_match.group(1))
            if cls.is_valid_match_status(candidate):
                return cls.clean(candidate)

        return NOT_FOUND

    @classmethod
    def extract_batsman_from_json(cls, key: str, scope_html: str) -> Optional[Batsman]:
        block_match = re.search(
            rf'"{key}"\s*:\s*\{{([^}}]*)\}}',
            scope_html,
        )
        if not block_match:
            return None

        block = block_match.group(1)
        name_m = re.search(r'"batName"\s*:\s*"([^"]+)"', block)
        if not name_m:
            return None

        runs_m = re.search(r'"batRuns"\s*:\s*([0-9]+)', block)
        balls_m = re.search(r'"batBalls"\s*:\s*([0-9]+)', block)

        if runs_m or balls_m:
            runs_val = runs_m.group(1) if runs_m else "0"
            balls_val = balls_m.group(1) if balls_m else "0"
            score_val = f"{runs_val}({balls_val})"
        else:
            score_val = "0(0)"

        return Batsman(
            name=cls.clean(cls.decode_json_string(name_m.group(1))),
            score=score_val,
        )

    @classmethod
    def extract_batsmen(
        cls, og_title: str, miniscore_html: str, norm_html: str
    ) -> List[Batsman]:
        batsmen: List[Batsman] = []

        for key in ("batsmanStriker", "batsmanNonStriker"):
            player = cls.extract_batsman_from_json(key, miniscore_html)
            if not player and miniscore_html is not norm_html:
                player = cls.extract_batsman_from_json(key, norm_html)
            if player:
                batsmen.append(player)

        if len(batsmen) < 2 and og_title:
            batsman_match = re.search(r"\(([^()]*\d+\(\d+\)[^()]*)\)\s*\|", og_title)
            if not batsman_match:
                batsman_match = re.search(r"\((.*?)\)\s*\|", og_title)
            if batsman_match:
                players = re.findall(
                    r"([A-Za-z\s.'-]+)\s+(\d+\(\d+\))",
                    batsman_match.group(1),
                )
                existing_names = {b.name.lower() for b in batsmen}
                for name, score_value in players:
                    cleaned_name = cls.clean(name)
                    if cleaned_name.lower() not in existing_names:
                        batsmen.append(
                            Batsman(
                                name=cleaned_name,
                                score=cls.clean(score_value),
                            )
                        )
                        existing_names.add(cleaned_name.lower())
                    if len(batsmen) == 2:
                        break

        while len(batsmen) < 2:
            batsmen.append(Batsman())

        return batsmen[:2]

    @classmethod
    def extract_run_rate(
        cls,
        page_text: str,
        miniscore_html: str,
        norm_html: str,
        runs: Optional[int] = None,
        overs: Optional[str] = None,
    ) -> str:
        text_matches = re.findall(
            r"\bCRR\s*:?\s*([0-9]+(?:\.[0-9]+)?)",
            page_text,
            re.IGNORECASE,
        )
        for val in text_matches:
            if "." in val:
                return f"CRR: {val}"

        for target in (miniscore_html, norm_html):
            json_matches = re.findall(
                r'"(?:currentRunRate|crr)"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)"?',
                target,
            )
            for val in json_matches:
                if "." in val:
                    return f"CRR: {val}"

        if runs is not None and overs:
            computed = cls.compute_run_rate(runs, overs)
            if computed:
                return f"CRR: {computed}"

        if text_matches:
            try:
                return f"CRR: {float(text_matches[0]):.2f}"
            except ValueError:
                return f"CRR: {text_matches[0]}"

        json_fallback = re.search(
            r'"(?:currentRunRate|crr)"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)"?',
            miniscore_html,
        )
        if json_fallback:
            try:
                return f"CRR: {float(json_fallback.group(1)):.2f}"
            except ValueError:
                return f"CRR: {json_fallback.group(1)}"

        return NOT_FOUND

    @classmethod
    def extract_bowler(
        cls, soup: BeautifulSoup, page_text: str, miniscore_html: str, norm_html: str
    ) -> Bowler:
        block_match = re.search(
            r'"bowlerStriker"\s*:\s*\{([^}]*)\}',
            miniscore_html,
        )
        if not block_match and miniscore_html is not norm_html:
            block_match = re.search(
                r'"bowlerStriker"\s*:\s*\{([^}]*)\}',
                norm_html,
            )
        if block_match:
            block = block_match.group(1)

            name_m = re.search(r'"bowlName"\s*:\s*"([^"]+)"', block)
            ovs_m = re.search(r'"bowlOvs"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)"?', block)
            runs_m = re.search(r'"bowlRuns"\s*:\s*([0-9]+)', block)
            wkts_m = re.search(r'"bowlWkts"\s*:\s*([0-9]+)', block)

            if name_m:
                has_bowled = ovs_m is not None
                overs_val = (
                    f"{cls.format_overs_value(ovs_m.group(1))} over"
                    if ovs_m
                    else NOT_FOUND
                )
                runs_val = runs_m.group(1) if runs_m else ("0" if has_bowled else NOT_FOUND)
                wkts_val = wkts_m.group(1) if wkts_m else ("0" if has_bowled else NOT_FOUND)
                return Bowler(
                    name=cls.clean(cls.decode_json_string(name_m.group(1))),
                    over=overs_val,
                    runs=runs_val,
                    wicket=wkts_val,
                )

        cleaned_text = re.sub(
            r"View match performance|View profile",
            "",
            page_text,
            flags=re.IGNORECASE,
        )
        text_match = re.search(
            r"Bowler\s+O\s+M\s+R\s+W\s+ECO\s+"
            r"([A-Za-z.'\- ]+?)\s*\*?\s+"
            r"(\d+(?:\.\d+)?)\s+\d+\s+(\d+)\s+(\d+)",
            cleaned_text,
            re.IGNORECASE,
        )
        if text_match:
            name, overs, runs, wickets = text_match.groups()
            return Bowler(
                name=cls.clean(name),
                over=f"{cls.format_overs_value(overs)} over",
                runs=runs,
                wicket=wickets,
            )

        bowler_header = soup.find(
            string=re.compile(r"^\s*Bowler\s*$", re.IGNORECASE)
        )
        if bowler_header:
            for link in bowler_header.find_all_next(
                "a", href=re.compile(r"/profiles/"), limit=5
            ):
                name = link.get_text(" ", strip=True).rstrip("*").strip()
                if name and name.lower() not in {
                    "view profile",
                    "view match performance",
                }:
                    return Bowler(name=cls.clean(name))

        return Bowler()

    @classmethod
    def format_tree(cls, data: ScoreResponse) -> str:
        batsmen_lines = "\n".join(
            f"│   ├── {player.name} : {player.score}"
            for player in data.current_batsmen
        )

        bowler = data.current_bowler
        bowler_info = (
            f"{bowler.name} "
            f"({bowler.over}, {bowler.runs} runs, {bowler.wicket} wkt)"
        )

        return (
            "🏏 Live Score\n"
            "│\n"
            f"├── Match    : {data.title}\n"
            f"├── Status   : {data.match_status}\n"
            f"├── Score    : {data.score}\n"
            f"├── Run Rate : {data.run_rate}\n"
            f"├── Bowler   : {bowler_info}\n"
            "├── Batsmen\n"
            f"{batsmen_lines}"
        )

    @classmethod
    async def fetch_score(cls, match_id: str) -> ScoreResponse:
        try:
            url = (
                "https://www.cricbuzz.com/live-cricket-scores/"
                f"{match_id}?_={time.time_ns()}"
            )

            async with httpx.AsyncClient(
                timeout=10.0,
                follow_redirects=True,
            ) as client:
                response = await client.get(
                    url,
                    headers=cls.HEADERS,
                )
                response.raise_for_status()

            raw_html = response.text
            norm_html = cls.normalize_json_html(raw_html)
            miniscore_html = cls.extract_miniscore_scope(norm_html)
            soup = BeautifulSoup(raw_html, "lxml")

            title = cls.clean(
                re.sub(
                    r"^Cricket commentary\s*\|\s*",
                    "",
                    soup.title.get_text(strip=True)
                    if soup.title
                    else NOT_FOUND,
                    flags=re.IGNORECASE,
                )
            )

            og_tag = soup.find("meta", property="og:title")
            og_title = html.unescape(og_tag.get("content", "")) if og_tag else ""

            page_text = cls.clean(soup.get_text(" ", strip=True))
            score, live_runs, live_overs = cls.extract_score_and_context(
                og_title, page_text, miniscore_html, norm_html
            )
            match_status = cls.extract_match_status(
                soup, page_text, miniscore_html, norm_html
            )
            batsmen = cls.extract_batsmen(og_title, miniscore_html, norm_html)
            run_rate = cls.extract_run_rate(
                page_text, miniscore_html, norm_html, live_runs, live_overs
            )
            current_bowler = cls.extract_bowler(
                soup, page_text, miniscore_html, norm_html
            )

            return ScoreResponse(
                status="success",
                title=title,
                match_status=match_status,
                score=score,
                run_rate=run_rate,
                current_batsmen=batsmen,
                current_bowler=current_bowler,
            )

        except APIError:
            raise

        except httpx.TimeoutException:
            raise APIError(408, REQUEST_TIMEOUT)

        except httpx.HTTPStatusError:
            raise APIError(404, "score data unavailable")

        except httpx.RequestError:
            raise APIError(502, "failed to reach score provider")

        except Exception:
            raise APIError(500, "failed to process score data")


@app.get("/docs", include_in_schema=False)
async def custom_swagger_docs():
    try:
        swagger_html = get_swagger_ui_html(
            openapi_url=app.openapi_url,
            title="Live Cricket Score API Docs",
            swagger_favicon_url="https://fastapi.tiangolo.com/img/favicon.png",
        )

        content = swagger_html.body.decode("utf-8")

        if "</head>" not in content:
            raise ValueError("Invalid Swagger HTML")

        custom_style = """
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1">
        <style>
            html, body {
                margin: 0;
                padding: 0;
                width: 100%;
                overflow-x: hidden;
                -webkit-text-size-adjust: 100%;
            }

            .swagger-ui {
                width: 100%;
                overflow-x: hidden;
            }

            .swagger-ui .wrapper {
                width: 100%;
                max-width: 100% !important;
                padding: 10px !important;
                box-sizing: border-box;
            }

            .swagger-ui .opblock-summary {
                flex-wrap: wrap !important;
                gap: 6px;
            }

            .swagger-ui .opblock-summary-path {
                white-space: normal !important;
                word-break: break-word !important;
                overflow-wrap: anywhere !important;
                font-size: 14px !important;
                line-height: 1.4;
            }

            .swagger-ui pre,
            .swagger-ui code,
            .swagger-ui .microlight,
            .swagger-ui .highlight-code {
                white-space: pre-wrap !important;
                word-break: break-word !important;
                overflow-wrap: anywhere !important;
                overflow-x: auto !important;
                max-width: 100% !important;
                max-height: 220px !important;
                overflow-y: auto !important;
                box-sizing: border-box;
                font-size: 12px !important;
                line-height: 1.5 !important;
                border-radius: 8px;
            }

            .swagger-ui table {
                display: block;
                width: 100%;
                overflow-x: auto;
            }

            .swagger-ui textarea,
            .swagger-ui input,
            .swagger-ui select {
                width: 100% !important;
                box-sizing: border-box;
                font-size: 16px !important;
            }

            .swagger-ui .btn {
                min-height: 42px !important;
                white-space: normal !important;
            }

            @media (max-width: 768px) {
                .swagger-ui .wrapper {
                    padding: 8px !important;
                }

                .swagger-ui pre,
                .swagger-ui code {
                    max-height: 180px !important;
                    font-size: 11px !important;
                }
            }
        </style>
        """

        content = content.replace(
            "</head>",
            custom_style + "</head>",
        )

        response = HTMLResponse(content=content)

        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"

        return response

    except Exception:
        return HTMLResponse(
            content="""
            <html>
                <head>
                    <meta name="viewport" content="width=device-width, initial-scale=1.0">
                    <title>Docs Error</title>
                </head>
                <body style="font-family:sans-serif;padding:20px;">
                    <h2>Unable to load Swagger docs</h2>
                </body>
            </html>
            """,
            status_code=500,
        )


@app.get("/", response_model=ScoreResponse)
async def root(
    score: Optional[str] = Query(
        None,
        min_length=4,
        max_length=20,
    ),
    text: bool = Query(False),
):
    if score is None:
        return ScoreResponse(
            status="success",
            title="Live Score API",
            match_status=NOT_FOUND,
            score=NOT_FOUND,
            run_rate=NOT_FOUND,
            current_batsmen=ScoreService.default_batsmen(),
            current_bowler=Bowler(),
        )

    try:
        validated = MatchValidator(score=score)
    except ValidationError as exc:
        err_msg = exc.errors()[0].get("msg", INVALID_MATCH_ID)
        err_msg = err_msg.removeprefix("Value error, ")
        return JSONResponse(
            status_code=422,
            content={
                "status": "error",
                "code": 422,
                "message": err_msg,
            },
        )

    result = await ScoreService.fetch_score(validated.score)

    if text:
        return PlainTextResponse(
            ScoreService.format_tree(result)
        )

    return result


@app.exception_handler(APIError)
async def api_error_handler(request: Request, exc: APIError):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "status": "error",
            "code": exc.status_code,
            "message": exc.message,
        },
    )


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(
    request: Request,
    exc: StarletteHTTPException,
):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "status": "error",
            "code": exc.status_code,
            "message": "invalid api route",
        },
    )


@app.exception_handler(Exception)
async def global_error_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={
            "status": "error",
            "code": 500,
            "message": "internal server error",
        },
    )