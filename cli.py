#!/usr/bin/env python3

import argparse
import html
import json
import re
import sys
import time
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
        "Origin": "https://www.cricbuzz.com",
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

    @staticmethod
    def clean(text: str) -> str:
        if not text:
            return ""
        return html.unescape(" ".join(text.split())).strip()

    @staticmethod
    def not_found() -> str:
        return "score not found"

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
    def build_url(cls, match_id: str) -> str:
        return f"{cls.BASE_URL}{match_id}/?_={time.time_ns()}"

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
    def extract_score_and_context(
        cls, og_title: str, full_text: str, miniscore_html: str, norm_html: str
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

            return cls.clean(score_str) or cls.not_found(), runs, overs or None

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
            r"\b([A-Z]{2,5})\s+(?:(\d+(?:[/-]\d+)?\s*d?)\s*&\s*)?(\d+)[/-](\d+)\s*(d)?\s*\(([\d.]+)\)\s*CRR",
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
        cls, soup: BeautifulSoup, full_text: str, miniscore_html: str, norm_html: str
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

        state_match = re.search(
            r'"stateTitle"\s*:\s*"([^"]+)"',
            norm_html,
        )
        if state_match:
            candidate = cls.decode_json_string(state_match.group(1))
            if cls.is_valid_match_status(candidate):
                return cls.clean(candidate)

        return cls.not_found()

    @classmethod
    def extract_batsman_from_json(
        cls, key: str, scope_html: str
    ) -> Optional[Dict[str, str]]:
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

        return {
            "name": cls.clean(cls.decode_json_string(name_m.group(1))) or cls.not_found(),
            "score": score_val,
        }

    @classmethod
    def extract_batsmen(
        cls, og_title: str, miniscore_html: str, norm_html: str
    ) -> List[Dict[str, str]]:
        batsmen: List[Dict[str, str]] = []

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
                existing_names = {b["name"].lower() for b in batsmen}
                for name, score_value in players:
                    cleaned_name = cls.clean(name)
                    if cleaned_name and cleaned_name.lower() not in existing_names:
                        batsmen.append(
                            {
                                "name": cleaned_name,
                                "score": cls.clean(score_value) or cls.not_found(),
                            }
                        )
                        existing_names.add(cleaned_name.lower())
                    if len(batsmen) == 2:
                        break

        while len(batsmen) < 2:
            batsmen.append(cls.default_batsman())

        return batsmen[:2]

    @classmethod
    def extract_run_rate(
        cls,
        full_text: str,
        miniscore_html: str,
        norm_html: str,
        runs: Optional[int] = None,
        overs: Optional[str] = None,
    ) -> str:
        text_matches = re.findall(
            r"\bCRR\s*:?\s*([0-9]+(?:\.[0-9]+)?)",
            full_text,
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

        return cls.not_found()

    @classmethod
    def extract_bowler(
        cls, soup: BeautifulSoup, full_text: str, miniscore_html: str, norm_html: str
    ) -> Dict[str, str]:
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
                    else cls.not_found()
                )
                runs_val = runs_m.group(1) if runs_m else ("0" if has_bowled else cls.not_found())
                wkts_val = wkts_m.group(1) if wkts_m else ("0" if has_bowled else cls.not_found())
                return {
                    "name": cls.clean(cls.decode_json_string(name_m.group(1))) or cls.not_found(),
                    "over": overs_val,
                    "runs": runs_val,
                    "wicket": wkts_val,
                }

        cleaned_text = re.sub(
            r"View match performance|View profile",
            "",
            full_text,
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
            return {
                "name": cls.clean(name) or cls.not_found(),
                "over": f"{cls.format_overs_value(overs)} over",
                "runs": runs,
                "wicket": wickets,
            }

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
                    bowler = cls.default_bowler()
                    bowler["name"] = cls.clean(name) or cls.not_found()
                    return bowler

        return cls.default_bowler()

    @classmethod
    def extract_match_data(cls, match_id: str) -> Dict[str, Any]:
        url = cls.build_url(match_id)

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
            response = requests.get(
                url,
                headers=cls.HEADERS,
                timeout=10,
                allow_redirects=True,
            )
            response.raise_for_status()

            raw_html = response.text
            norm_html = cls.normalize_json_html(raw_html)
            miniscore_html = cls.extract_miniscore_scope(norm_html)
            soup = BeautifulSoup(raw_html, "lxml")
            full_text = cls.clean(soup.get_text(" ", strip=True))

            raw_title = (
                soup.title.get_text(strip=True)
                if soup.title
                else ""
            )

            title = cls.clean(
                re.sub(
                    r"^Cricket commentary\s*\|\s*",
                    "",
                    raw_title,
                    flags=re.IGNORECASE,
                )
            )

            og_title_tag = soup.find("meta", property="og:title")
            og_title = cls.clean(
                og_title_tag.get("content", "")
                if og_title_tag
                else ""
            )

            score, live_runs, live_overs = cls.extract_score_and_context(
                og_title, full_text, miniscore_html, norm_html
            )
            match_status = cls.extract_match_status(
                soup, full_text, miniscore_html, norm_html
            )
            batsmen = cls.extract_batsmen(og_title, miniscore_html, norm_html)
            run_rate = cls.extract_run_rate(
                full_text, miniscore_html, norm_html, live_runs, live_overs
            )
            bowler = cls.extract_bowler(soup, full_text, miniscore_html, norm_html)

            return {
                "status": "success",
                "title": title or cls.not_found(),
                "match_status": match_status,
                "score": score,
                "run_rate": run_rate,
                "current_batsmen": batsmen,
                "current_bowler": bowler,
            }

        except requests.Timeout:
            default_response["error"] = "request timeout"
            return default_response

        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else "unknown"
            default_response["error"] = f"http error ({status_code}): score data unavailable"
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