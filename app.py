import re
import html
from typing import Any, Dict, List, Optional, Tuple

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
    version="0.1.0",
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
    response.headers["Content-Language"] = "en"

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
        "Accept": (
            "text/html,"
            "application/xhtml+xml,"
            "application/xml;q=0.9,"
            "*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
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

    INVALID_PLAYER_NAMES = {
        "view profile",
        "view match performance",
        "batter",
        "batters",
        "batsman",
        "batsmen",
        "bowler",
        "bowlers",
        "scorecard",
        "commentary",
        "squads",
        "highlights",
        "full commentary",
        "partnership",
        "last wicket",
        "key stats",
        "null",
        "none",
        "n/a",
        "score not found",
    }

    HOMEPAGE_TITLES = (
        "live cricket score, schedule, latest news",
        "cricbuzz.com",
    )

    NAV_NOISE_MARKERS = (
        "matches ",
        "rankings",
        "more matches",
        "live scores",
        "schedule",
        "archives",
    )

    UI_NOISE_RE = re.compile(
        r"View\s+match\s+performance|View\s+profile|Stats",
        re.IGNORECASE,
    )

    @staticmethod
    def clean(text: Any) -> str:
        if text is None:
            return NOT_FOUND
        unescaped = html.unescape(" ".join(str(text).split()))
        sanitized = re.sub(r"[<>]", "", unescaped).strip()
        return sanitized if sanitized else NOT_FOUND

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
    def extract_json_object(text: str, key: str) -> Optional[str]:
        pattern = re.compile(rf'"{re.escape(key)}"\s*:\s*\{{')
        for match in pattern.finditer(text):
            start = match.end() - 1
            depth = 0
            in_string = False
            escape = False
            end = -1
            limit = min(len(text), start + 40000)

            for idx in range(start, limit):
                ch = text[idx]
                if in_string:
                    if escape:
                        escape = False
                    elif ch == "\\":
                        escape = True
                    elif ch == '"':
                        in_string = False
                else:
                    if ch == '"':
                        in_string = True
                    elif ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            end = idx + 1
                            break

            if end > start + 2:
                return text[start:end]
        return None

    @classmethod
    def extract_miniscore_scope(cls, norm_html: str) -> str:
        obj = cls.extract_json_object(norm_html, "miniscore")
        if obj:
            return f'"miniscore":{obj}'
        match = re.search(r'"miniscore"\s*:\s*\{', norm_html)
        if not match:
            return norm_html
        start = match.start()
        return norm_html[start : start + 30000]

    @staticmethod
    def format_overs_value(raw_overs: Any) -> str:
        if raw_overs is None:
            return ""
        cleaned = str(raw_overs).strip()
        if cleaned.endswith(".0"):
            return cleaned[:-2]
        return cleaned

    @staticmethod
    def is_valid_overs(raw_overs: str) -> bool:
        return bool(re.fullmatch(r"\d+(?:\.[0-5])?", raw_overs.strip()))

    @classmethod
    def clean_player_name(cls, raw_name: str) -> str:
        if not raw_name:
            return NOT_FOUND
        name = cls.UI_NOISE_RE.sub("", raw_name)
        name = re.sub(r"\s*\((?:c|wk|c\s*&\s*wk|sub)\)\s*", " ", name, flags=re.IGNORECASE)
        name = name.replace("*", " ")
        name = cls.clean(name)
        if name == NOT_FOUND:
            return NOT_FOUND
        name = name.strip(" -|,•:")
        return name if cls.is_valid_player_name(name) else NOT_FOUND

    @classmethod
    def is_valid_player_name(cls, name: str) -> bool:
        if not name:
            return False
        normalized = name.strip()
        lower_val = normalized.lower()
        if len(normalized) < 2 or len(normalized) > 48:
            return False
        if lower_val in cls.INVALID_PLAYER_NAMES:
            return False
        if any(marker in lower_val for marker in ("view profile", "match performance", "http://", "https://")):
            return False
        if not re.search(r"[A-Za-zÀ-ÿ]", normalized):
            return False
        if re.search(r"\d", normalized):
            return False
        return True

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
    def is_homepage_response(cls, final_url: str, match_id: str, title: str) -> bool:
        lower_title = title.lower()
        if any(marker in lower_title for marker in cls.HOMEPAGE_TITLES):
            return True
        if f"/{match_id}" not in final_url:
            return True
        return False

    @classmethod
    def is_valid_match_status(cls, candidate: str) -> bool:
        if not candidate:
            return False
        normalized = candidate.strip()
        lower_val = normalized.lower()
        if len(normalized) < 4 or len(normalized) > 160:
            return False
        if lower_val in cls.INVALID_STATUS_VALUES:
            return False
        if normalized.isdigit():
            return False
        if "http://" in lower_val or "https://" in lower_val:
            return False
        if any(marker in lower_val for marker in cls.NAV_NOISE_MARKERS):
            return False
        if lower_val.count(" vs ") >= 2:
            return False
        return True

    @classmethod
    def default_batsmen(cls) -> List[Batsman]:
        return [Batsman(), Batsman()]

    @classmethod
    def build_score_from_innings(
        cls,
        innings: List[Dict[str, Any]],
        active_id: Optional[int],
        live_runs: Optional[int] = None,
        live_wkts: Optional[int] = None,
        live_overs: Optional[str] = None,
    ) -> Tuple[str, Optional[int], Optional[str]]:
        if not innings:
            return NOT_FOUND, None, None

        ordered = sorted(innings, key=lambda item: int(item.get("innings_id") or 0))
        current = dict(
            next(
                (item for item in ordered if item.get("innings_id") == active_id),
                ordered[-1],
            )
        )

        if live_runs is not None:
            current["runs"] = live_runs
        if live_wkts is not None:
            current["wickets"] = live_wkts
        if live_overs:
            current["overs"] = cls.format_overs_value(live_overs)

        team = str(current.get("team") or "").strip()
        if not team:
            return NOT_FOUND, None, None

        runs = int(current.get("runs") or 0)
        wickets = int(current.get("wickets") or 0)
        overs = cls.format_overs_value(current.get("overs") or "")
        declared = bool(current.get("declared"))

        curr_runs_str = f"{runs}/{wickets}d" if declared else f"{runs}/{wickets}"
        curr_display = f"{curr_runs_str} ({overs})" if overs else curr_runs_str

        prev_same_team = [
            item
            for item in ordered
            if str(item.get("team") or "").lower() == team.lower()
            and int(item.get("innings_id") or 0) < int(current.get("innings_id") or 0)
        ]

        if prev_same_team:
            prev = prev_same_team[-1]
            prev_runs = int(prev.get("runs") or 0)
            prev_wkts = int(prev.get("wickets") or 0)
            prev_decl = bool(prev.get("declared"))
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

    @classmethod
    def extract_innings_list(cls, scope_html: str) -> List[Dict[str, Any]]:
        best: List[Dict[str, Any]] = []

        for list_match in re.finditer(
            r'"inningsScoreList"\s*:\s*\[(.*?)\]',
            scope_html,
            re.DOTALL,
        ):
            innings: List[Dict[str, Any]] = []
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
    def extract_title(cls, soup: BeautifulSoup, norm_html: str) -> str:
        raw_title = soup.title.get_text(strip=True) if soup.title else ""
        cleaned = re.sub(
            r"^Cricket commentary\s*\|\s*",
            "",
            raw_title,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r"\s*\|\s*Cricbuzz\.com\s*$",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        title = cls.clean(cleaned)
        if title != NOT_FOUND:
            return title

        header_obj = cls.extract_json_object(norm_html, "matchHeader")
        if header_obj:
            desc_m = re.search(r'"matchDescription"\s*:\s*"([^"]+)"', header_obj)
            series_m = re.search(r'"seriesName"\s*:\s*"([^"]+)"', header_obj)
            parts = []
            if desc_m:
                parts.append(cls.decode_json_string(desc_m.group(1)))
            if series_m:
                parts.append(cls.decode_json_string(series_m.group(1)))
            if parts:
                return cls.clean(", ".join(parts))

        return NOT_FOUND

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

            live_runs: Optional[int] = None
            live_wkts: Optional[int] = None
            bat_team_obj = cls.extract_json_object(miniscore_html, "batTeam")
            if bat_team_obj:
                bt_str_score = re.search(
                    r'"teamScore"\s*:\s*"(\d+)[/-](\d+)"', bat_team_obj
                )
                if bt_str_score:
                    live_runs = int(bt_str_score.group(1))
                    live_wkts = int(bt_str_score.group(2))
                else:
                    bt_runs = re.search(r'"teamScore"\s*:\s*(\d+)', bat_team_obj)
                    bt_wkts = re.search(r'"teamWkts"\s*:\s*(\d+)', bat_team_obj)
                    if bt_runs:
                        live_runs = int(bt_runs.group(1))
                        if bt_wkts:
                            live_wkts = int(bt_wkts.group(1))

            live_overs_m = re.search(
                r'"miniscore"\s*:\s*\{.{0,1500}?"overs"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)"?',
                miniscore_html,
                re.DOTALL,
            )
            live_overs = live_overs_m.group(1) if live_overs_m else None

            return cls.build_score_from_innings(
                innings,
                active_id,
                live_runs=live_runs,
                live_wkts=live_wkts,
                live_overs=live_overs,
            )

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
            r"\b([A-Z][A-Za-z0-9]{1,5})\s+(?:(\d+(?:[/-]\d+)?\s*d?)\s*&\s*)?(\d+)[/-](\d+)\s*(d)?\s*\(([\d.]+)\)\s*CRR",
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
        block = cls.extract_json_object(scope_html, key)
        if not block:
            return None

        name_m = re.search(r'"(?:batName|name|nickName)"\s*:\s*"([^"]+)"', block)
        if not name_m:
            return None

        name = cls.clean_player_name(cls.decode_json_string(name_m.group(1)))
        if name == NOT_FOUND:
            return None

        runs_m = re.search(r'"(?:batRuns|runs)"\s*:\s*"?(\d+)"?', block)
        balls_m = re.search(r'"(?:batBalls|balls)"\s*:\s*"?(\d+)"?', block)

        runs_val = runs_m.group(1) if runs_m else "0"
        balls_val = balls_m.group(1) if balls_m else "0"
        return Batsman(name=name, score=f"{runs_val}({balls_val})")

    @classmethod
    def extract_batsmen_from_dom(cls, soup: BeautifulSoup) -> List[Batsman]:
        batsmen: List[Batsman] = []
        existing: set = set()

        batter_header = soup.find(
            string=re.compile(r"^\s*Batter(?:s)?\s*$", re.IGNORECASE)
        )
        if not batter_header:
            return batsmen

        bowler_header = batter_header.find_next(
            string=re.compile(r"^\s*Bowler(?:s)?\s*$", re.IGNORECASE)
        )

        for link in batter_header.find_all_next(
            "a", href=re.compile(r"/profiles/"), limit=10
        ):
            if bowler_header and bowler_header in link.find_all_previous(string=True, limit=40):
                break

            name = cls.clean_player_name(link.get_text(" ", strip=True))
            if name == NOT_FOUND or name.lower() in existing:
                continue

            row_text = ""
            node = link.parent
            for _ in range(3):
                if node is None:
                    break
                candidate_text = cls.UI_NOISE_RE.sub(
                    "", " ".join(node.get_text(" ", strip=True).split())
                )
                if re.search(r"\b\d+\s+\d+\s+\d+\s+\d+", candidate_text):
                    row_text = candidate_text
                    break
                node = node.parent

            score_val = NOT_FOUND
            if row_text:
                after_name = row_text.split(name, 1)[-1]
                stat_m = re.search(
                    r"(\d+)\s+(\d+)\s+\d+\s+\d+\s+\d+(?:\.\d+)?", after_name
                )
                if not stat_m:
                    stat_m = re.search(r"(\d+)\s+(\d+)", after_name)
                if stat_m:
                    score_val = f"{stat_m.group(1)}({stat_m.group(2)})"

            batsmen.append(Batsman(name=name, score=score_val))
            existing.add(name.lower())
            if len(batsmen) == 2:
                break

        return batsmen

    @classmethod
    def extract_batsmen_from_text(cls, page_text: str) -> List[Batsman]:
        batsmen: List[Batsman] = []
        existing: set = set()

        cleaned_text = cls.UI_NOISE_RE.sub("", page_text)
        section_m = re.search(
            r"Batter(?:s)?\s+R\s+B\s+4s\s+6s\s+SR\s+(.*?)(?:Bowler(?:s)?\s+O\s+M\s+R\s+W|Key\s+Stats|Partnership|Last\s+Wicket|Recent|$)",
            cleaned_text,
            re.IGNORECASE,
        )
        if not section_m:
            return batsmen

        row_pattern = re.compile(
            r"([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ.'\- ]{1,42}?)\s*(?:\([^)]*\))?\s*\*?\s+(\d+)\s+(\d+)\s+\d+\s+\d+\s+\d+(?:\.\d+)?"
        )
        for match in row_pattern.finditer(section_m.group(1)):
            name = cls.clean_player_name(match.group(1))
            if name == NOT_FOUND or name.lower() in existing:
                continue
            batsmen.append(Batsman(name=name, score=f"{match.group(2)}({match.group(3)})"))
            existing.add(name.lower())
            if len(batsmen) == 2:
                break

        return batsmen

    @classmethod
    def extract_batsmen(
        cls,
        soup: BeautifulSoup,
        og_title: str,
        page_text: str,
        miniscore_html: str,
        norm_html: str,
    ) -> List[Batsman]:
        batsmen: List[Batsman] = []
        existing: set = set()

        def append_unique(players: List[Batsman]) -> None:
            for player in players:
                if player.name == NOT_FOUND:
                    continue
                key_name = player.name.lower()
                if key_name not in existing:
                    batsmen.append(player)
                    existing.add(key_name)
                if len(batsmen) == 2:
                    return

        for key in ("batsmanStriker", "batsmanNonStriker"):
            player = cls.extract_batsman_from_json(key, miniscore_html)
            if not player and miniscore_html is not norm_html:
                player = cls.extract_batsman_from_json(key, norm_html)
            if player:
                append_unique([player])

        if len(batsmen) < 2:
            append_unique(cls.extract_batsmen_from_dom(soup))

        if len(batsmen) < 2:
            append_unique(cls.extract_batsmen_from_text(page_text))

        if len(batsmen) < 2 and og_title:
            batsman_match = re.search(r"\(([^()]*\d+\(\d+\)[^()]*)\)\s*\|", og_title)
            if not batsman_match:
                batsman_match = re.search(r"\((.*?)\)\s*\|", og_title)
            if batsman_match:
                players = re.findall(
                    r"([A-Za-zÀ-ÿ\s.'-]+)\s+(\d+\(\d+\))",
                    batsman_match.group(1),
                )
                for raw_name, score_value in players:
                    name = cls.clean_player_name(raw_name)
                    if name != NOT_FOUND:
                        append_unique([Batsman(name=name, score=cls.clean(score_value))])

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
                val = float(json_fallback.group(1))
                if val > 0:
                    return f"CRR: {val:.2f}"
            except ValueError:
                return f"CRR: {json_fallback.group(1)}"

        return NOT_FOUND

    @classmethod
    def extract_bowler_from_json(cls, key: str, scope_html: str) -> Optional[Bowler]:
        block = cls.extract_json_object(scope_html, key)
        if not block:
            return None

        name_m = re.search(r'"(?:bowlName|name|nickName)"\s*:\s*"([^"]+)"', block)
        if not name_m:
            return None

        name = cls.clean_player_name(cls.decode_json_string(name_m.group(1)))
        if name == NOT_FOUND:
            return None

        ovs_m = re.search(r'"(?:bowlOvs|overs)"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)"?', block)
        runs_m = re.search(r'"(?:bowlRuns|runs)"\s*:\s*"?(\d+)"?', block)
        wkts_m = re.search(r'"(?:bowlWkts|wickets)"\s*:\s*"?(\d+)"?', block)

        overs_clean = cls.format_overs_value(ovs_m.group(1)) if ovs_m else ""
        has_valid_overs = bool(overs_clean and cls.is_valid_overs(overs_clean))

        overs_val = f"{overs_clean} over" if has_valid_overs else NOT_FOUND
        runs_val = runs_m.group(1) if runs_m else ("0" if has_valid_overs else NOT_FOUND)
        wkts_val = (
            wkts_m.group(1)
            if wkts_m and 0 <= int(wkts_m.group(1)) <= 10
            else ("0" if has_valid_overs else NOT_FOUND)
        )

        return Bowler(
            name=name,
            over=overs_val,
            runs=runs_val,
            wicket=wkts_val,
        )

    @classmethod
    def extract_bowler_from_dom(cls, soup: BeautifulSoup) -> Optional[Bowler]:
        bowler_header = soup.find(
            string=re.compile(r"^\s*Bowler(?:s)?\s*$", re.IGNORECASE)
        )
        if not bowler_header:
            return None

        candidates: List[Tuple[bool, Bowler]] = []
        for link in bowler_header.find_all_next(
            "a", href=re.compile(r"/profiles/"), limit=6
        ):
            raw_link_text = link.get_text(" ", strip=True)
            name = cls.clean_player_name(raw_link_text)
            if name == NOT_FOUND:
                continue

            is_striker = "*" in raw_link_text
            row_text = ""
            node = link.parent
            for _ in range(3):
                if node is None:
                    break
                candidate_text = cls.UI_NOISE_RE.sub(
                    "", " ".join(node.get_text(" ", strip=True).split())
                )
                if re.search(r"\b\d+(?:\.\d+)?\s+\d+\s+\d+\s+\d+", candidate_text):
                    row_text = candidate_text
                    if "*" in candidate_text:
                        is_striker = True
                    break
                node = node.parent

            overs_val = NOT_FOUND
            runs_val = NOT_FOUND
            wkts_val = NOT_FOUND
            if row_text:
                after_name = row_text.split(name, 1)[-1]
                stat_m = re.search(
                    r"(\d+(?:\.[0-5])?)\s+\d+\s+(\d+)\s+(\d+)(?:\s+\d+(?:\.\d+)?)?",
                    after_name,
                )
                if stat_m:
                    overs_clean = cls.format_overs_value(stat_m.group(1))
                    if cls.is_valid_overs(overs_clean) and int(stat_m.group(3)) <= 10:
                        overs_val = f"{overs_clean} over"
                        runs_val = stat_m.group(2)
                        wkts_val = stat_m.group(3)

            candidates.append(
                (
                    is_striker,
                    Bowler(
                        name=name,
                        over=overs_val,
                        runs=runs_val,
                        wicket=wkts_val,
                    ),
                )
            )
            if is_striker and overs_val != NOT_FOUND:
                return candidates[-1][1]

        for is_striker, bowler in candidates:
            if is_striker:
                return bowler

        for _, bowler in candidates:
            if bowler.over != NOT_FOUND:
                return bowler

        return candidates[0][1] if candidates else None

    @classmethod
    def extract_bowler_from_text(cls, page_text: str) -> Optional[Bowler]:
        cleaned_text = cls.UI_NOISE_RE.sub("", page_text)
        section_m = re.search(
            r"Bowler(?:s)?\s+O\s+M\s+R\s+W\s+ECO\s+(.*?)(?:Key\s+Stats|Partnership|Last\s+Wicket|Recent|Commentary|$)",
            cleaned_text,
            re.IGNORECASE,
        )
        if not section_m:
            return None

        row_pattern = re.compile(
            r"([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ.'\- ]{1,42}?)\s*(?:\([^)]*\))?\s*(\*)?\s+(\d+(?:\.[0-5])?)\s+\d+\s+(\d+)\s+(\d+)(?:\s+\d+(?:\.\d+)?)?"
        )
        first_bowler: Optional[Bowler] = None
        for match in row_pattern.finditer(section_m.group(1)):
            name = cls.clean_player_name(match.group(1))
            if name == NOT_FOUND:
                continue
            is_striker = bool(match.group(2))
            overs_clean = cls.format_overs_value(match.group(3))
            runs_val = match.group(4)
            wkts_val = match.group(5)
            if not cls.is_valid_overs(overs_clean) or int(wkts_val) > 10:
                continue
            bowler = Bowler(
                name=name,
                over=f"{overs_clean} over",
                runs=runs_val,
                wicket=wkts_val,
            )
            if is_striker:
                return bowler
            if first_bowler is None:
                first_bowler = bowler

        return first_bowler

    @classmethod
    def extract_bowler(
        cls, soup: BeautifulSoup, page_text: str, miniscore_html: str, norm_html: str
    ) -> Bowler:
        bowler = cls.extract_bowler_from_json("bowlerStriker", miniscore_html)
        if not bowler and miniscore_html is not norm_html:
            bowler = cls.extract_bowler_from_json("bowlerStriker", norm_html)
        if bowler:
            return bowler

        dom_bowler = cls.extract_bowler_from_dom(soup)
        if dom_bowler and dom_bowler.over != NOT_FOUND:
            return dom_bowler

        text_bowler = cls.extract_bowler_from_text(page_text)
        if text_bowler:
            return text_bowler

        non_striker = cls.extract_bowler_from_json("bowlerNonStriker", miniscore_html)
        if non_striker:
            return non_striker

        if dom_bowler:
            return dom_bowler

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
        candidate_urls = (
            f"https://www.cricbuzz.com/live-cricket-scores/{match_id}/",
            f"https://www.cricbuzz.com/live-cricket-scores/{match_id}/match",
            f"https://www.cricbuzz.com/live-cricket-scores/{match_id}",
        )

        try:
            async with httpx.AsyncClient(
                timeout=10.0,
                follow_redirects=True,
            ) as client:
                last_status_error: Optional[httpx.HTTPStatusError] = None

                for url in candidate_urls:
                    try:
                        response = await client.get(url, headers=cls.HEADERS)
                        response.raise_for_status()
                    except httpx.HTTPStatusError as exc:
                        last_status_error = exc
                        continue

                    raw_html = response.text
                    if not raw_html or len(raw_html) < 500:
                        continue

                    norm_html = cls.normalize_json_html(raw_html)
                    miniscore_html = cls.extract_miniscore_scope(norm_html)
                    soup = BeautifulSoup(raw_html, "lxml")

                    title = cls.extract_title(soup, norm_html)
                    if cls.is_homepage_response(str(response.url), match_id, title):
                        continue

                    og_tag = soup.find("meta", property="og:title")
                    og_title = html.unescape(og_tag.get("content", "")) if og_tag else ""

                    for noise_tag in soup.find_all(
                        ["header", "nav", "footer", "script", "style", "noscript"]
                    ):
                        noise_tag.decompose()

                    page_text = cls.clean(soup.get_text(" ", strip=True))

                    score, live_runs, live_overs = cls.extract_score_and_context(
                        og_title, page_text, miniscore_html, norm_html
                    )
                    match_status = cls.extract_match_status(
                        soup, page_text, miniscore_html, norm_html
                    )
                    batsmen = cls.extract_batsmen(
                        soup, og_title, page_text, miniscore_html, norm_html
                    )
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

                if last_status_error is not None:
                    raise last_status_error

                raise APIError(404, "score data unavailable")

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