#!/usr/bin/env python3
"""Render accurate, self-hosted profile cards using only the Python standard library.

Online: GITHUB_TOKEN=<ephemeral Actions token> python scripts/update_profile.py
Offline: python scripts/update_profile.py --bootstrap-json work/profile-data.json

The bootstrap file contains the unmodified REST user, every public owner repository,
and the GraphQL contributionCalendar (or a full GraphQL response under ``graphql``).
No synthetic contribution cells, achievement claims, or third-party stats services.
"""

import argparse
import datetime as dt
import html
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request


BACKGROUND = "#080D12"
PANEL = "#0D151C"
BORDER = "#20343D"
TEXT = "#EDF8FA"
MUTED = "#819CA5"
ACCENT = "#39E2D1"
LEVELS = ["#172830", "#135E61", "#188D88", "#24B8AA", "#39E2D1"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


class ProfileDataError(Exception):
    """A deliberately short error that does not expose a token or HTTP payload."""


def api_json(url, token=None, payload=None):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "SethBlades-profile", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        # Never print the response body or request headers: they may include secrets.
        raise ProfileDataError("GitHub API returned HTTP " + str(error.code)) from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        raise ProfileDataError("Could not read GitHub API data") from None


def load_online(username, token, as_of):
    if not token:
        raise ProfileDataError("GITHUB_TOKEN is required for the contribution calendar; use --bootstrap-json for an offline run")
    encoded_user = urllib.parse.quote(username, safe="")
    user = api_json("https://api.github.com/users/" + encoded_user, token)
    repositories = []
    page = 1
    while True:
        batch = api_json("https://api.github.com/users/" + encoded_user + "/repos?type=owner&per_page=100&page=" + str(page), token)
        if not isinstance(batch, list):
            raise ProfileDataError("Unexpected repository response")
        repositories.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    start = as_of - dt.timedelta(days=364)
    query = """query ProfileCalendar($login: String!, $from: DateTime!, $to: DateTime!) {
      user(login: $login) {
        contributionsCollection(from: $from, to: $to) {
          contributionCalendar {
            totalContributions
            weeks { contributionDays { date contributionCount contributionLevel } }
          }
        }
      }
    }"""
    graphql = api_json("https://api.github.com/graphql", token, {
        "query": query,
        "variables": {"login": username, "from": start.isoformat() + "T00:00:00Z", "to": as_of.isoformat() + "T23:59:59Z"},
    })
    if graphql.get("errors"):
        raise ProfileDataError("GitHub GraphQL could not return the contribution calendar")
    return {"generated_at": as_of.isoformat(), "user": user, "repositories": repositories, "graphql": graphql}


def nonnegative_int(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProfileDataError("Missing or invalid " + label)
    return value


def normalized_data(payload, username):
    user = payload.get("user")
    repositories = payload.get("repositories")
    if not isinstance(user, dict) or not isinstance(repositories, list):
        raise ProfileDataError("Bootstrap JSON must include user and all public owner repositories")
    if str(user.get("login", "")).casefold() != username.casefold():
        raise ProfileDataError("The user response does not match --username")
    repo_count = nonnegative_int(user.get("public_repos"), "public repository count")
    followers = nonnegative_int(user.get("followers"), "follower count")
    public_repos = []
    for repo in repositories:
        if not isinstance(repo, dict):
            raise ProfileDataError("Invalid repository entry")
        if repo.get("private") is False and str(repo.get("owner", {}).get("login", "")).casefold() == username.casefold():
            public_repos.append(repo)
    # Missing pages must fail, rather than silently undercount stars.
    if len(public_repos) != repo_count:
        raise ProfileDataError("Repository list must contain every public repository owned by the profile")
    stars = sum(nonnegative_int(repo.get("stargazers_count"), "repository star count") for repo in public_repos)
    calendar = payload.get("contribution_calendar")
    if calendar is None:
        try:
            calendar = payload["graphql"]["data"]["user"]["contributionsCollection"]["contributionCalendar"]
        except (KeyError, TypeError):
            raise ProfileDataError("Bootstrap JSON must include contribution_calendar or a GraphQL response") from None
    if not isinstance(calendar, dict) or not isinstance(calendar.get("weeks"), list):
        raise ProfileDataError("Invalid contribution calendar")
    total = nonnegative_int(calendar.get("totalContributions"), "contribution total")
    days = []
    seen_dates = set()
    for week in calendar["weeks"]:
        if not isinstance(week, dict) or not isinstance(week.get("contributionDays"), list):
            raise ProfileDataError("Invalid contribution week")
        for day in week["contributionDays"]:
            if not isinstance(day, dict):
                raise ProfileDataError("Invalid contribution day")
            try:
                date = dt.date.fromisoformat(day["date"])
            except (KeyError, TypeError, ValueError):
                raise ProfileDataError("Invalid contribution date") from None
            if date in seen_dates:
                raise ProfileDataError("Duplicate contribution date")
            seen_dates.add(date)
            count = nonnegative_int(day.get("contributionCount"), "daily contribution count")
            days.append({"date": date, "count": count, "level": day.get("contributionLevel")})
    if not days:
        raise ProfileDataError("A complete real contribution calendar is required")
    days.sort(key=lambda day: day["date"])
    if sum(day["count"] for day in days) != total:
        raise ProfileDataError("Contribution total does not match the supplied daily counts")
    if (days[-1]["date"] - days[0]["date"]).days + 1 != len(days):
        raise ProfileDataError("The contribution calendar contains missing dates")
    generated_at = payload.get("generated_at")
    try:
        generated_date = dt.date.fromisoformat(str(generated_at)[:10])
    except ValueError:
        raise ProfileDataError("generated_at must be an ISO date") from None
    return {"username": username, "repositories": repo_count, "followers": followers, "stars": stars, "contributions": total, "days": days, "generated_at": generated_date}


def escape(value):
    return html.escape(str(value), quote=True)


def svg_start(title, description, height):
    return [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="' + str(height) + '" viewBox="0 0 1000 ' + str(height) + '" role="img" aria-labelledby="title desc">',
        '<title id="title">' + escape(title) + '</title><desc id="desc">' + escape(description) + '</desc>',
        '<defs><linearGradient id="edge" x1="0" y1="0" x2="1" y2="0"><stop stop-color="#39E2D1"/><stop offset="1" stop-color="#39E2D1" stop-opacity="0"/></linearGradient><pattern id="grid" width="24" height="24" patternUnits="userSpaceOnUse"><path d="M24 0H0V24" fill="none" stroke="#173039" stroke-opacity=".17"/></pattern></defs>',
        '<rect x=".5" y=".5" width="999" height="' + str(height - 1) + '" rx="20" fill="' + BACKGROUND + '" stroke="' + BORDER + '"/>',
        '<rect x="1" y="1" width="998" height="' + str(height - 2) + '" rx="20" fill="url(#grid)"/>',
        '<g font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Arial,sans-serif">',
    ]


def stats_svg(data):
    summary = str(data["repositories"]) + " public repositories, " + str(data["followers"]) + " followers, " + str(data["stars"]) + " stars across public owner repositories, and " + str(data["contributions"]) + " GitHub contributions during the calendar period."
    svg = svg_start("SethBlades — GitHub activity", summary, 226)
    svg.extend([
        '<path d="M30 37h20" stroke="' + ACCENT + '" stroke-width="3" stroke-linecap="round"/>',
        '<text x="60" y="42" font-size="12" font-weight="700" letter-spacing="2.4" fill="' + MUTED + '">GITHUB / ACTIVITY</text>',
        '<text x="970" y="42" text-anchor="end" font-size="11" fill="' + MUTED + '">UPDATED ' + data["generated_at"].isoformat() + '</text>',
    ])
    items = [("PUBLIC REPOS", data["repositories"], "publicly visible projects"), ("FOLLOWERS", data["followers"], "people following the work"), ("REPOSITORY STARS", data["stars"], "across public owner repos"), ("CONTRIBUTIONS", data["contributions"], "GitHub calendar · " + str(len(data["days"])) + " days")]
    for index, (label, value, caption) in enumerate(items):
        x = 30 + index * 239
        svg.extend([
            '<rect x="' + str(x) + '" y="64" width="223" height="130" rx="12" fill="' + PANEL + '" stroke="' + BORDER + '"/>',
            '<text x="' + str(x + 18) + '" y="91" font-size="10" font-weight="600" letter-spacing="1.4" fill="' + MUTED + '">' + label + '</text>',
            '<text x="' + str(x + 18) + '" y="145" font-size="42" font-weight="700" letter-spacing="-1.8" fill="' + ACCENT + '">' + format(value, ",") + '</text>',
            '<text x="' + str(x + 18) + '" y="172" font-size="11" fill="' + MUTED + '">' + caption + '</text>',
        ])
    svg.append('</g></svg>\n')
    return "\n".join(svg)


def cell_level(day):
    explicit = {"NONE": 0, "FIRST_QUARTILE": 1, "SECOND_QUARTILE": 2, "THIRD_QUARTILE": 3, "FOURTH_QUARTILE": 4}
    if day["level"] in explicit:
        return explicit[day["level"]]
    count = day["count"]
    return 0 if count == 0 else 1 if count == 1 else 2 if count <= 3 else 3 if count <= 6 else 4


def calendar_svg(data):
    days = data["days"]
    first = days[0]["date"]
    last = days[-1]["date"]
    sunday = first - dt.timedelta(days=(first.weekday() + 1) % 7)
    week_count = ((last - sunday).days // 7) + 1
    pitch = min(16, 858 / week_count)
    size = pitch - 3
    left = 83
    top = 98
    summary = str(data["contributions"]) + " GitHub contributions between " + first.isoformat() + " and " + last.isoformat() + ". Cell colors show actual daily contribution counts."
    svg = svg_start("SethBlades — contribution calendar", summary, 262)
    svg.extend([
        '<path d="M30 37h20" stroke="' + ACCENT + '" stroke-width="3" stroke-linecap="round"/>',
        '<text x="60" y="42" font-size="12" font-weight="700" letter-spacing="2.4" fill="' + MUTED + '">CONSISTENCY / CONTRIBUTIONS</text>',
        '<text x="886" y="43" text-anchor="end" font-size="19" font-weight="700" fill="' + ACCENT + '">' + format(data["contributions"], ",") + '</text>',
        '<text x="897" y="43" font-size="11" fill="' + MUTED + '">contributions</text>',
    ])
    labeled_months = set()
    previous_label_x = -100
    for day in days:
        date = day["date"]
        week_index = (date - sunday).days // 7
        row = (date.weekday() + 1) % 7
        x = left + week_index * pitch
        y = top + row * pitch
        month_key = (date.year, date.month)
        if month_key not in labeled_months and x - previous_label_x > 40:
            svg.append('<text x="' + format(x, ".2f") + '" y="82" font-size="10" fill="' + MUTED + '">' + MONTHS[date.month - 1] + '</text>')
            previous_label_x = x
            labeled_months.add(month_key)
        # A title on every cell makes count and date inspectable without adding UI noise.
        svg.append('<rect x="' + format(x, ".2f") + '" y="' + format(y, ".2f") + '" width="' + format(size, ".2f") + '" height="' + format(size, ".2f") + '" rx="3" fill="' + LEVELS[cell_level(day)] + '"><title>' + date.isoformat() + ': ' + str(day["count"]) + ' contributions</title></rect>')
    for row, label in [(1, "Mon"), (3, "Wed"), (5, "Fri")]:
        svg.append('<text x="30" y="' + str(top + row * pitch + size - 2) + '" font-size="10" fill="' + MUTED + '">' + label + '</text>')
    svg.extend([
        '<path d="M30 226H970" stroke="' + BORDER + '"/>',
        '<text x="30" y="247" font-size="10" fill="' + MUTED + '">' + first.isoformat() + ' — ' + last.isoformat() + ' · GitHub contribution calendar</text>',
        '<text x="814" y="247" font-size="10" fill="' + MUTED + '">Less</text>',
    ])
    for index, color in enumerate(LEVELS):
        svg.append('<rect x="' + str(846 + index * 17) + '" y="237" width="12" height="12" rx="3" fill="' + color + '"/>')
    svg.append('<text x="938" y="247" font-size="10" fill="' + MUTED + '">More</text></g></svg>\n')
    return "\n".join(svg)


def write_if_changed(path, content):
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", default="SethBlades")
    parser.add_argument("--bootstrap-json", type=Path, help="Offline input with real REST and GraphQL data")
    parser.add_argument("--output-dir", type=Path, default=Path("assets"))
    parser.add_argument("--date", type=dt.date.fromisoformat, default=dt.datetime.now(dt.timezone.utc).date(), help="UTC end date for an online query (YYYY-MM-DD)")
    args = parser.parse_args()
    try:
        if args.bootstrap_json:
            with args.bootstrap_json.open(encoding="utf-8-sig") as source:
                payload = json.load(source)
        else:
            payload = load_online(args.username, os.environ.get("GITHUB_TOKEN"), args.date)
        data = normalized_data(payload, args.username)
        changed = []
        for name, content in [("stats-v3.svg", stats_svg(data)), ("contributions-v3.svg", calendar_svg(data))]:
            if write_if_changed(args.output_dir / name, content):
                changed.append(name)
        print("Profile cards updated: " + (", ".join(changed) if changed else "already current"))
    except (ProfileDataError, OSError, json.JSONDecodeError) as error:
        if isinstance(error, ProfileDataError):
            print("Profile update failed: " + str(error), file=sys.stderr)
        else:
            print("Profile update failed: could not read or write the local data files", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
