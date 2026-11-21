import re
import html
import time
from typing import List, Optional

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
        return str(value).strip()


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
        return str(value).strip()


class ScoreResponse(BaseModel):
    status: str
    title: str
    score: str
    run_rate: str = NOT_FOUND
    current_batsmen: List[Batsman]
    current_bowler: Bowler

    @field_validator("title", "score", "run_rate", mode="before")
    @classmethod
    def validate_fields(cls, value: Optional[str]) -> str:
        if not value or not str(value).strip():
            return NOT_FOUND
        return str(value).strip()

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
    version="0.0.3",
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

    @staticmethod
    def clean(text: str) -> str:
        if not text:
            return NOT_FOUND
        return html.escape(" ".join(text.split()))

    @classmethod
    def default_batsmen(cls) -> List[Batsman]:
        return [Batsman(), Batsman()]

    @classmethod
    def extract_batsman_from_json(cls, key: str, raw_html: str) -> Optional[Batsman]:
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
            else NOT_FOUND
        )
        return Batsman(
            name=cls.clean(name_m.group(1)),
            score=score_val,
        )

    @classmethod
    def extract_batsmen(cls, og_title: str, raw_html: str) -> List[Batsman]:
        batsmen: List[Batsman] = []

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
    def extract_run_rate(cls, page_text: str, raw_html: str) -> str:
        json_match = re.search(
            r'\\?"currentRunRate\\?"\s*:\s*([0-9]+(?:\.[0-9]+)?)',
            raw_html,
        )
        if json_match:
            return f"CRR: {json_match.group(1)}"

        text_match = re.search(
            r"\bCRR\s*:\s*([0-9]+(?:\.[0-9]+)?)",
            page_text,
            re.IGNORECASE,
        )
        if text_match:
            return f"CRR: {text_match.group(1)}"

        return NOT_FOUND

    @classmethod
    def extract_bowler(
        cls, soup: BeautifulSoup, page_text: str, raw_html: str
    ) -> Bowler:
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
                return Bowler(
                    name=cls.clean(name_m.group(1)),
                    over=f"{ovs_m.group(1)} over" if ovs_m else NOT_FOUND,
                    runs=runs_m.group(1) if runs_m else NOT_FOUND,
                    wicket=wkts_m.group(1) if wkts_m else NOT_FOUND,
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
                over=f"{overs} over",
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
            og_title = og_tag.get("content", "") if og_tag else ""

            score = NOT_FOUND

            score_match = re.search(
                r"([A-Z]{2,4})\s+(\d+)/(\d+)\s*\(([\d.]+)\)",
                og_title,
            )

            if score_match:
                team, runs, wickets, overs = score_match.groups()
                score = f"{team} {runs}/{wickets} ({overs})"

            page_text = cls.clean(soup.get_text(" ", strip=True))
            batsmen = cls.extract_batsmen(og_title, raw_html)
            run_rate = cls.extract_run_rate(page_text, raw_html)
            current_bowler = cls.extract_bowler(soup, page_text, raw_html)

            return ScoreResponse(
                status="success",
                title=title,
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