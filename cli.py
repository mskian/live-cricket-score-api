#!/usr/bin/env python3

import argparse
import json
import re
import sys
import time
from typing import Any, Dict, List, Optional

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

    @staticmethod
    def clean(text: str) -> str:
        return " ".join(text.split()) if text else ""

    @staticmethod
    def not_found() -> str:
        return "score not found"

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
    def extract_batsman_from_json(
        cls, key: str, raw_html: str
    ) -> Optional[Dict[str, str]]:
        block_match = re.search(
            rf'\\?"{key}\\?"\s*:\s*\{{([^}}]*)\}}',
            raw_html,
        )
        if not block_match:
            return None

        block = block_match.group(1)
        name_m = re.search(
            r'\\?"batName\\?"\s*:\s*\\?"([^"\\]+)\\?"', block
        )
        runs_m = re.search(
            r'\\?"batRuns\\?"\s*:\s*([0-9]+)', block
        )
        balls_m = re.search(
            r'\\?"batBalls\\?"\s*:\s*([0-9]+)', block
        )

        if not name_m:
            return None

        score_val = (
            f"{runs_m.group(1)}({balls_m.group(1)})"
            if runs_m and balls_m
            else cls.not_found()
        )
        return {
            "name": cls.clean(name_m.group(1)) or cls.not_found(),
            "score": score_val,
        }

    @classmethod
    def extract_batsmen(
        cls, og_title: str, raw_html: str
    ) -> List[Dict[str, str]]:
        batsmen: List[Dict[str, str]] = []

        for key in ("batsmanStriker", "batsmanNonStriker"):
            player = cls.extract_batsman_from_json(key, raw_html)
            if player:
                batsmen.append(player)

        if len(batsmen) < 2 and og_title:
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
    def extract_run_rate(cls, full_text: str, raw_html: str) -> str:
        json_match = re.search(
            r'\\?"currentRunRate\\?"\s*:\s*([0-9]+(?:\.[0-9]+)?)',
            raw_html,
        )
        if json_match:
            return f"CRR: {json_match.group(1)}"

        text_match = re.search(
            r"\bCRR\s*:\s*([0-9]+(?:\.[0-9]+)?)",
            full_text,
            re.IGNORECASE,
        )
        if text_match:
            return f"CRR: {text_match.group(1)}"

        return cls.not_found()

    @classmethod
    def extract_bowler(
        cls, soup: BeautifulSoup, full_text: str, raw_html: str
    ) -> Dict[str, str]:
        block_match = re.search(
            r'\\?"bowlerStriker\\?"\s*:\s*\{([^}]*)\}',
            raw_html,
        )
        if block_match:
            block = block_match.group(1)
            name_m = re.search(
                r'\\?"bowlName\\?"\s*:\s*\\?"([^"\\]+)\\?"', block
            )
            ovs_m = re.search(
                r'\\?"bowlOvs\\?"\s*:\s*([0-9]+(?:\.[0-9]+)?)', block
            )
            runs_m = re.search(
                r'\\?"bowlRuns\\?"\s*:\s*([0-9]+)', block
            )
            wkts_m = re.search(
                r'\\?"bowlWkts\\?"\s*:\s*([0-9]+)', block
            )

            if name_m:
                return {
                    "name": cls.clean(name_m.group(1)) or cls.not_found(),
                    "over": f"{ovs_m.group(1)} over" if ovs_m else cls.not_found(),
                    "runs": runs_m.group(1) if runs_m else cls.not_found(),
                    "wicket": wkts_m.group(1) if wkts_m else cls.not_found(),
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
                "over": f"{overs} over",
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

            score: Any = cls.not_found()

            score_match = re.search(
                r"([A-Z]{2,4})\s+(\d+)/(\d+)\s*\(([\d.]+)\)",
                og_title,
            )

            if score_match:
                team, runs, wickets, overs = score_match.groups()
                score = {
                    "team": team,
                    "runs": int(runs),
                    "wickets": int(wickets),
                    "overs": float(overs),
                    "display": f"{team} {runs}/{wickets} ({overs})",
                }

            batsmen = cls.extract_batsmen(og_title, raw_html)
            run_rate = cls.extract_run_rate(full_text, raw_html)
            bowler = cls.extract_bowler(soup, full_text, raw_html)

            return {
                "status": "success",
                "title": title or cls.not_found(),
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