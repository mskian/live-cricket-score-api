#!/usr/bin/env python3

import argparse
import html
import json
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup


class ScoreCLI:
    BASE_URL = "https://www.cricbuzz.com/live-cricket-scores/"

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
    def not_found() -> str:
        return "score not found"

    @staticmethod
    def clean(text: Any) -> str:
        if text is None:
            return ""
        unescaped = html.unescape(" ".join(str(text).split()))
        return re.sub(r"[<>]", "", unescaped).strip()

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

    @staticmethod
    def extract_top_level_scalar(
        obj_str: Optional[str], keys: Tuple[str, ...]
    ) -> Optional[str]:
        if not obj_str or not obj_str.startswith("{"):
            return None

        target_keys = set(keys)
        n = len(obj_str)
        i = 0
        depth = 0
        arr_depth = 0

        while i < n:
            ch = obj_str[i]
            if ch == "{":
                depth += 1
                i += 1
                continue
            if ch == "}":
                depth -= 1
                i += 1
                continue
            if ch == "[":
                arr_depth += 1
                i += 1
                continue
            if ch == "]":
                arr_depth -= 1
                i += 1
                continue

            if ch == '"':
                j = i + 1
                while j < n:
                    if obj_str[j] == "\\":
                        j += 2
                        continue
                    if obj_str[j] == '"':
                        break
                    j += 1
                token = obj_str[i + 1 : j]
                i = j + 1

                if depth == 1 and arr_depth == 0:
                    k = i
                    while k < n and obj_str[k].isspace():
                        k += 1
                    if k < n and obj_str[k] == ":":
                        k += 1
                        while k < n and obj_str[k].isspace():
                            k += 1
                        if token in target_keys and k < n:
                            if obj_str[k] == '"':
                                v_end = k + 1
                                while v_end < n:
                                    if obj_str[v_end] == "\\":
                                        v_end += 2
                                        continue
                                    if obj_str[v_end] == '"':
                                        break
                                    v_end += 1
                                val = obj_str[k + 1 : v_end]
                                if val.lower() != "null":
                                    return val
                            elif obj_str[k] not in "{[":
                                v_end = k
                                while v_end < n and obj_str[v_end] not in ",}] \t\r\n":
                                    v_end += 1
                                val = obj_str[k:v_end]
                                if val.lower() != "null":
                                    return val
                continue

            i += 1

        return None

    @classmethod
    def extract_miniscore_object(cls, norm_html: str) -> Optional[str]:
        return cls.extract_json_object(norm_html, "miniscore")

    @classmethod
    def extract_miniscore_scope(cls, norm_html: str) -> str:
        obj = cls.extract_miniscore_object(norm_html)
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
    def is_valid_player_name(cls, name: str) -> bool:
        if not name:
            return False
        normalized = name.strip()
        lower_val = normalized.lower()
        if len(normalized) < 2 or len(normalized) > 48:
            return False
        if lower_val in cls.INVALID_PLAYER_NAMES:
            return False
        if any(
            marker in lower_val
            for marker in ("view profile", "match performance", "http://", "https://")
        ):
            return False
        if not re.search(r"[A-Za-zÀ-ÿ]", normalized):
            return False
        if re.search(r"\d", normalized):
            return False
        return True

    @classmethod
    def clean_player_name(cls, raw_name: str) -> str:
        if not raw_name:
            return ""
        name = cls.UI_NOISE_RE.sub("", raw_name)
        name = re.sub(
            r"\s*\((?:c|wk|c\s*&\s*wk|sub)\)\s*", " ", name, flags=re.IGNORECASE
        )
        name = name.replace("*", " ")
        name = cls.clean(name).strip(" -|,•:")
        return name if cls.is_valid_player_name(name) else ""

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
    def default_batsman(cls) -> Dict[str, str]:
        return {
            "name": cls.not_found(),
            "score": cls.not_found(),
        }

    @classmethod
    def default_bowler(cls) -> Dict[str, str]:
        return {
            "name": cls.not_found(),
            "over": cls.not_found(),
            "runs": cls.not_found(),
            "wicket": cls.not_found(),
        }

    @classmethod
    def validate_match_id(cls, value: str) -> str:
        value = value.strip()

        if not value:
            raise argparse.ArgumentTypeError("match_id cannot be empty")

        if not value.isdigit():
            raise argparse.ArgumentTypeError("match_id must contain digits only")

        if len(value) < 4:
            raise argparse.ArgumentTypeError("match_id must be at least 4 digits")

        if len(value) > 20:
            raise argparse.ArgumentTypeError("match_id must be at most 20 digits")

        return value

    @classmethod
    def build_candidate_urls(cls, match_id: str) -> Tuple[str, ...]:
        return (
            f"{cls.BASE_URL}{match_id}/",
            f"{cls.BASE_URL}{match_id}/match",
            f"{cls.BASE_URL}{match_id}",
        )

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
            return cls.not_found(), None, None

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
        if live_overs and cls.is_valid_overs(cls.format_overs_value(live_overs)):
            current["overs"] = cls.format_overs_value(live_overs)

        team = str(current.get("team") or "").strip()
        if not team:
            return cls.not_found(), None, None

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

        return cls.clean(score_str) or cls.not_found(), runs, overs or None

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
        if title:
            return title

        header_obj = cls.extract_json_object(norm_html, "matchHeader")
        if header_obj:
            desc_val = cls.extract_top_level_scalar(header_obj, ("matchDescription",))
            series_val = cls.extract_top_level_scalar(header_obj, ("seriesName",))
            parts = []
            if desc_val:
                parts.append(cls.decode_json_string(desc_val))
            if series_val:
                parts.append(cls.decode_json_string(series_val))
            if parts:
                return cls.clean(", ".join(parts))

        return ""

    @classmethod
    def extract_score_and_context(
        cls,
        og_title: str,
        full_text: str,
        miniscore_obj: Optional[str],
        miniscore_html: str,
        norm_html: str,
    ) -> Tuple[str, Optional[int], Optional[str]]:
        innings = cls.extract_innings_list(miniscore_html)
        if not innings and miniscore_html is not norm_html:
            innings = cls.extract_innings_list(norm_html)

        if innings:
            active_id_str = cls.extract_top_level_scalar(miniscore_obj, ("inningsId",))
            active_id = (
                int(active_id_str)
                if active_id_str and active_id_str.isdigit()
                else None
            )

            live_runs: Optional[int] = None
            live_wkts: Optional[int] = None
            bat_team_obj = cls.extract_json_object(miniscore_html, "batTeam")
            if bat_team_obj:
                bt_score_val = cls.extract_top_level_scalar(
                    bat_team_obj, ("teamScore",)
                )
                bt_wkts_val = cls.extract_top_level_scalar(
                    bat_team_obj, ("teamWkts",)
                )
                if bt_score_val:
                    slash_m = re.fullmatch(r"(\d+)[/-](\d+)", bt_score_val.strip())
                    if slash_m:
                        live_runs = int(slash_m.group(1))
                        live_wkts = int(slash_m.group(2))
                    elif bt_score_val.strip().isdigit():
                        live_runs = int(bt_score_val.strip())
                        if bt_wkts_val and bt_wkts_val.strip().isdigit():
                            live_wkts = int(bt_wkts_val.strip())

            top_overs_raw = cls.extract_top_level_scalar(miniscore_obj, ("overs",))
            live_overs: Optional[str] = None
            if top_overs_raw:
                formatted = cls.format_overs_value(top_overs_raw)
                if cls.is_valid_overs(formatted):
                    live_overs = formatted

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
                return cls.clean(score_str) or cls.not_found(), int(runs_s), overs_clean

        text_crr_match = re.search(
            r"\b([A-Z][A-Za-z0-9]{1,5})\s+(?:(\d+(?:[/-]\d+)?\s*d?)\s*&\s*)?(\d+)[/-](\d+)\s*(d)?\s*\(([\d.]+)\)\s*CRR",
            full_text,
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
            return cls.clean(score_str) or cls.not_found(), int(runs_s), overs_clean

        text_any_match = score_pattern.search(full_text[:1500])
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
            return cls.clean(score_str) or cls.not_found(), int(runs_s), overs_clean

        return cls.not_found(), None, None

    @classmethod
    def extract_match_status(
        cls,
        soup: BeautifulSoup,
        full_text: str,
        miniscore_obj: Optional[str],
        miniscore_html: str,
        norm_html: str,
    ) -> str:
        top_status = cls.extract_top_level_scalar(
            miniscore_obj, ("custStatus", "customStatus", "status")
        )
        if top_status:
            candidate = cls.decode_json_string(top_status)
            if cls.is_valid_match_status(candidate):
                return cls.clean(candidate)

        score_details_obj = cls.extract_json_object(miniscore_html, "matchScoreDetails")
        if score_details_obj:
            sd_status = cls.extract_top_level_scalar(
                score_details_obj, ("customStatus", "custStatus", "status")
            )
            if sd_status:
                candidate = cls.decode_json_string(sd_status)
                if cls.is_valid_match_status(candidate):
                    return cls.clean(candidate)

        header_obj = cls.extract_json_object(norm_html, "matchHeader")
        if header_obj:
            hdr_status = cls.extract_top_level_scalar(
                header_obj, ("status", "stateTitle")
            )
            if hdr_status:
                candidate = cls.decode_json_string(hdr_status)
                if cls.is_valid_match_status(candidate):
                    return cls.clean(candidate)

        for tag in soup.find_all(["div", "span", "p"], class_=cls.STATUS_CLASS_PATTERN):
            candidate = cls.clean(tag.get_text(" ", strip=True))
            if cls.is_valid_match_status(candidate):
                return candidate

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
            match = re.search(pattern, full_text, re.IGNORECASE)
            if match:
                candidate = re.split(
                    r"\s+(?:Batter|Bowler|CRR|REQ|Partnership|Last wicket|Commentary|Scorecard)\b",
                    match.group(1),
                    maxsplit=1,
                    flags=re.IGNORECASE,
                )[0].strip(" -|•")
                if cls.is_valid_match_status(candidate):
                    return cls.clean(candidate)

        return cls.not_found()

    @classmethod
    def extract_batsman_from_json(
        cls, key: str, scope_html: str
    ) -> Optional[Dict[str, str]]:
        block = cls.extract_json_object(scope_html, key)
        if not block:
            return None

        raw_name = cls.extract_top_level_scalar(block, ("batName", "name", "nickName"))
        if not raw_name:
            return None

        name = cls.clean_player_name(cls.decode_json_string(raw_name))
        if not name:
            return None

        runs_raw = cls.extract_top_level_scalar(block, ("batRuns", "runs"))
        balls_raw = cls.extract_top_level_scalar(block, ("batBalls", "balls"))

        runs_val = runs_raw if runs_raw and runs_raw.isdigit() else "0"
        balls_val = balls_raw if balls_raw and balls_raw.isdigit() else "0"
        return {
            "name": name,
            "score": f"{runs_val}({balls_val})",
        }

    @classmethod
    def extract_batsmen_from_dom(cls, soup: BeautifulSoup) -> List[Dict[str, str]]:
        batsmen: List[Dict[str, str]] = []
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
            if bowler_header and bowler_header in link.find_all_previous(
                string=True, limit=40
            ):
                break

            name = cls.clean_player_name(link.get_text(" ", strip=True))
            if not name or name.lower() in existing:
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

            score_val = cls.not_found()
            if row_text:
                after_name = row_text.split(name, 1)[-1]
                stat_m = re.search(
                    r"(\d+)\s+(\d+)\s+\d+\s+\d+\s+\d+(?:\.\d+)?", after_name
                )
                if not stat_m:
                    stat_m = re.search(r"(\d+)\s+(\d+)", after_name)
                if stat_m:
                    score_val = f"{stat_m.group(1)}({stat_m.group(2)})"

            batsmen.append({"name": name, "score": score_val})
            existing.add(name.lower())
            if len(batsmen) == 2:
                break

        return batsmen

    @classmethod
    def extract_batsmen_from_text(cls, full_text: str) -> List[Dict[str, str]]:
        batsmen: List[Dict[str, str]] = []
        existing: set = set()

        cleaned_text = cls.UI_NOISE_RE.sub("", full_text)
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
            if not name or name.lower() in existing:
                continue
            batsmen.append(
                {
                    "name": name,
                    "score": f"{match.group(2)}({match.group(3)})",
                }
            )
            existing.add(name.lower())
            if len(batsmen) == 2:
                break

        return batsmen

    @classmethod
    def extract_batsmen(
        cls,
        soup: BeautifulSoup,
        og_title: str,
        full_text: str,
        miniscore_html: str,
        norm_html: str,
    ) -> List[Dict[str, str]]:
        batsmen: List[Dict[str, str]] = []
        existing: set = set()

        def append_unique(players: List[Dict[str, str]]) -> None:
            for player in players:
                if not player["name"] or player["name"] == cls.not_found():
                    continue
                key_name = player["name"].lower()
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
            append_unique(cls.extract_batsmen_from_text(full_text))

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
                    if name:
                        append_unique(
                            [
                                {
                                    "name": name,
                                    "score": cls.clean(score_value) or cls.not_found(),
                                }
                            ]
                        )

        while len(batsmen) < 2:
            batsmen.append(cls.default_batsman())

        return batsmen[:2]

    @classmethod
    def extract_run_rate(
        cls,
        full_text: str,
        miniscore_obj: Optional[str],
        runs: Optional[int] = None,
        overs: Optional[str] = None,
    ) -> str:
        computed = (
            cls.compute_run_rate(runs, overs)
            if runs is not None and overs
            else None
        )

        top_crr = cls.extract_top_level_scalar(
            miniscore_obj, ("currentRunRate", "crr")
        )
        if top_crr:
            try:
                crr_num = float(top_crr)
                if crr_num > 0:
                    if computed is not None:
                        if "." in top_crr and abs(crr_num - float(computed)) <= 0.25:
                            return f"CRR: {crr_num:.2f}"
                        return f"CRR: {computed}"
                    return f"CRR: {crr_num:.2f}"
            except ValueError:
                pass

        if computed is not None:
            return f"CRR: {computed}"

        text_matches = re.findall(
            r"\bCRR\s*:?\s*([0-9]+(?:\.[0-9]+)?)",
            full_text,
            re.IGNORECASE,
        )
        for val in text_matches:
            try:
                num = float(val)
                if num > 0:
                    return f"CRR: {num:.2f}"
            except ValueError:
                continue

        return cls.not_found()

    @classmethod
    def extract_bowler_from_json(
        cls, key: str, scope_html: str
    ) -> Optional[Dict[str, str]]:
        block = cls.extract_json_object(scope_html, key)
        if not block:
            return None

        raw_name = cls.extract_top_level_scalar(block, ("bowlName", "name", "nickName"))
        if not raw_name:
            return None

        name = cls.clean_player_name(cls.decode_json_string(raw_name))
        if not name:
            return None

        ovs_raw = cls.extract_top_level_scalar(block, ("bowlOvs",))
        if not ovs_raw:
            ovs_raw = cls.extract_top_level_scalar(block, ("overs",))
        runs_raw = cls.extract_top_level_scalar(block, ("bowlRuns", "runs"))
        wkts_raw = cls.extract_top_level_scalar(block, ("bowlWkts", "wickets"))

        overs_clean = cls.format_overs_value(ovs_raw) if ovs_raw else ""
        has_valid_overs = bool(overs_clean and cls.is_valid_overs(overs_clean))

        overs_val = f"{overs_clean} over" if has_valid_overs else cls.not_found()
        runs_val = (
            runs_raw
            if runs_raw and runs_raw.isdigit()
            else ("0" if has_valid_overs else cls.not_found())
        )
        wkts_val = (
            wkts_raw
            if wkts_raw and wkts_raw.isdigit() and 0 <= int(wkts_raw) <= 10
            else ("0" if has_valid_overs else cls.not_found())
        )

        return {
            "name": name,
            "over": overs_val,
            "runs": runs_val,
            "wicket": wkts_val,
        }

    @classmethod
    def extract_bowler_from_dom(
        cls, soup: BeautifulSoup
    ) -> Optional[Dict[str, str]]:
        bowler_header = soup.find(
            string=re.compile(r"^\s*Bowler(?:s)?\s*$", re.IGNORECASE)
        )
        if not bowler_header:
            return None

        candidates: List[Tuple[bool, Dict[str, str]]] = []
        for link in bowler_header.find_all_next(
            "a", href=re.compile(r"/profiles/"), limit=6
        ):
            raw_link_text = link.get_text(" ", strip=True)
            name = cls.clean_player_name(raw_link_text)
            if not name:
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

            overs_val = cls.not_found()
            runs_val = cls.not_found()
            wkts_val = cls.not_found()
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

            bowler = {
                "name": name,
                "over": overs_val,
                "runs": runs_val,
                "wicket": wkts_val,
            }
            candidates.append((is_striker, bowler))
            if is_striker and overs_val != cls.not_found():
                return bowler

        for is_striker, bowler in candidates:
            if is_striker:
                return bowler

        for _, bowler in candidates:
            if bowler["over"] != cls.not_found():
                return bowler

        return candidates[0][1] if candidates else None

    @classmethod
    def extract_bowler_from_text(cls, full_text: str) -> Optional[Dict[str, str]]:
        cleaned_text = cls.UI_NOISE_RE.sub("", full_text)
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
        first_bowler: Optional[Dict[str, str]] = None
        for match in row_pattern.finditer(section_m.group(1)):
            name = cls.clean_player_name(match.group(1))
            if not name:
                continue
            is_striker = bool(match.group(2))
            overs_clean = cls.format_overs_value(match.group(3))
            runs_val = match.group(4)
            wkts_val = match.group(5)
            if not cls.is_valid_overs(overs_clean) or int(wkts_val) > 10:
                continue
            bowler = {
                "name": name,
                "over": f"{overs_clean} over",
                "runs": runs_val,
                "wicket": wkts_val,
            }
            if is_striker:
                return bowler
            if first_bowler is None:
                first_bowler = bowler

        return first_bowler

    @classmethod
    def extract_bowler(
        cls, soup: BeautifulSoup, full_text: str, miniscore_html: str, norm_html: str
    ) -> Dict[str, str]:
        bowler = cls.extract_bowler_from_json("bowlerStriker", miniscore_html)
        if not bowler and miniscore_html is not norm_html:
            bowler = cls.extract_bowler_from_json("bowlerStriker", norm_html)
        if bowler:
            return bowler

        dom_bowler = cls.extract_bowler_from_dom(soup)
        if dom_bowler and dom_bowler["over"] != cls.not_found():
            return dom_bowler

        text_bowler = cls.extract_bowler_from_text(full_text)
        if text_bowler:
            return text_bowler

        non_striker = cls.extract_bowler_from_json("bowlerNonStriker", miniscore_html)
        if non_striker:
            return non_striker

        if dom_bowler:
            return dom_bowler

        return cls.default_bowler()

    @classmethod
    def extract_match_data(cls, match_id: str) -> Dict[str, Any]:
        default_response: Dict[str, Any] = {
            "status": "error",
            "title": cls.not_found(),
            "match_status": cls.not_found(),
            "score": cls.not_found(),
            "run_rate": cls.not_found(),
            "current_batsmen": [cls.default_batsman(), cls.default_batsman()],
            "current_bowler": cls.default_bowler(),
        }

        try:
            with requests.Session() as session:
                last_http_error: Optional[requests.HTTPError] = None

                for url in cls.build_candidate_urls(match_id):
                    try:
                        response = session.get(
                            url,
                            headers=cls.HEADERS,
                            timeout=10,
                            allow_redirects=True,
                        )
                        response.raise_for_status()
                    except requests.HTTPError as exc:
                        last_http_error = exc
                        continue

                    raw_html = response.text
                    if not raw_html or len(raw_html) < 500:
                        continue

                    norm_html = cls.normalize_json_html(raw_html)
                    miniscore_obj = cls.extract_miniscore_object(norm_html)
                    miniscore_html = (
                        f'"miniscore":{miniscore_obj}'
                        if miniscore_obj
                        else cls.extract_miniscore_scope(norm_html)
                    )
                    soup = BeautifulSoup(raw_html, "lxml")

                    title = cls.extract_title(soup, norm_html)
                    if cls.is_homepage_response(response.url, match_id, title):
                        continue

                    og_title_tag = soup.find("meta", property="og:title")
                    og_title = cls.clean(
                        og_title_tag.get("content", "") if og_title_tag else ""
                    )

                    for noise_tag in soup.find_all(
                        ["header", "nav", "footer", "script", "style", "noscript"]
                    ):
                        noise_tag.decompose()

                    full_text = cls.clean(soup.get_text(" ", strip=True))

                    score, live_runs, live_overs = cls.extract_score_and_context(
                        og_title, full_text, miniscore_obj, miniscore_html, norm_html
                    )
                    match_status = cls.extract_match_status(
                        soup, full_text, miniscore_obj, miniscore_html, norm_html
                    )
                    batsmen = cls.extract_batsmen(
                        soup, og_title, full_text, miniscore_html, norm_html
                    )
                    run_rate = cls.extract_run_rate(
                        full_text, miniscore_obj, live_runs, live_overs
                    )
                    bowler = cls.extract_bowler(
                        soup, full_text, miniscore_html, norm_html
                    )

                    return {
                        "status": "success",
                        "title": title or cls.not_found(),
                        "match_status": match_status,
                        "score": score,
                        "run_rate": run_rate,
                        "current_batsmen": batsmen,
                        "current_bowler": bowler,
                    }

                if last_http_error is not None:
                    raise last_http_error

                default_response["error"] = "score data unavailable"
                return default_response

        except requests.Timeout:
            default_response["error"] = "request timeout"
            return default_response

        except requests.HTTPError as exc:
            status_code = (
                exc.response.status_code if exc.response is not None else "unknown"
            )
            default_response["error"] = (
                f"http error ({status_code}): score data unavailable"
            )
            return default_response

        except requests.RequestException:
            default_response["error"] = "network error: failed to reach score provider"
            return default_response

        except Exception:
            default_response["error"] = "parse error: failed to process score data"
            return default_response

    @classmethod
    def format_text(cls, data: Dict[str, Any]) -> str:
        score = (
            data["score"]["display"]
            if isinstance(data["score"], dict)
            else data["score"]
        )

        batsmen_list = data.get("current_batsmen") or []
        has_error = "error" in data
        batsmen_branch = "├── Batsmen" if has_error else "└── Batsmen"
        indent = "│   " if has_error else "    "

        if batsmen_list:
            batsmen_lines = []
            for idx, player in enumerate(batsmen_list):
                connector = "└──" if idx == len(batsmen_list) - 1 else "├──"
                batsmen_lines.append(
                    f"{indent}{connector} {player['name']} : {player['score']}"
                )
            batsmen = "\n".join(batsmen_lines)
        else:
            batsmen = f"{indent}└── N/A"

        bowler = data["current_bowler"]
        bowler_info = (
            f"{bowler['name']} "
            f"({bowler['over']}, {bowler['runs']} runs, {bowler['wicket']} wkt)"
        )

        output = (
            "🏏 Live Score\n"
            "│\n"
            f"├── Match    : {data['title']}\n"
            f"├── Status   : {data['match_status']}\n"
            f"├── Score    : {score}\n"
            f"├── Run Rate : {data['run_rate']}\n"
            f"├── Bowler   : {bowler_info}\n"
            f"{batsmen_branch}\n"
            f"{batsmen}"
        )

        if has_error:
            output += f"\n└── Error    : {data['error']}"

        return output

    @staticmethod
    def normalize_argv(argv: List[str]) -> List[str]:
        normalized: List[str] = []
        for arg in argv:
            match = re.fullmatch(r"-{1,2}(\d+)", arg)
            if match:
                normalized.append(match.group(1))
            else:
                normalized.append(arg)
        return normalized

    @classmethod
    def parse_arguments(cls) -> argparse.Namespace:
        parser = argparse.ArgumentParser(
            prog="scorecli",
            usage="scorecli <match_id> [--text | --json]",
            description="Fetch live cricket score in text (tree) or JSON format.",
            formatter_class=argparse.RawTextHelpFormatter,
            epilog=(
                "Examples:\n"
                "  scorecli 161285\n"
                "  scorecli 161285 --text\n"
                "  scorecli 161285 --json\n"
                "  scorecli --161285 --json"
            ),
        )

        parser.add_argument(
            "match_id",
            type=cls.validate_match_id,
            help="numeric Cricbuzz match ID (4 to 20 digits)",
        )

        output_group = parser.add_mutually_exclusive_group()

        output_group.add_argument(
            "--text",
            action="store_true",
            help="show formatted text/tree output (default)",
        )

        output_group.add_argument(
            "--json",
            action="store_true",
            help="show JSON output",
        )

        return parser.parse_args(cls.normalize_argv(sys.argv[1:]))

    @classmethod
    def run(cls) -> int:
        args = cls.parse_arguments()
        data = cls.extract_match_data(args.match_id)

        if args.json:
            print(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            print(cls.format_text(data))

        return 1 if data.get("status") == "error" else 0


if __name__ == "__main__":
    try:
        sys.exit(ScoreCLI.run())
    except KeyboardInterrupt:
        print("\nInterrupted by user", file=sys.stderr)
        sys.exit(130)