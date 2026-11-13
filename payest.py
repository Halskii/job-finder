"""An estimated pay range for a posting that states none, from the U.S. Bureau of Labor Statistics wage survey (OEWS).

The BLS publishes, once a year, the 10th, 25th, 50th, 75th and 90th percentile annual wage of every occupation, for the whole country,
for each state and for each metropolitan area, through a free public API (https://www.bls.gov/developers/). This module
  1. maps the job title to a BLS occupation ("Backend Engineer" -> Software Developers) and to a level (entry, mid, senior),
  2. maps the job's place to a metro area, else its state, else the country,
  3. asks the API once for the figures (without a key it allows 25 requests a day, so a free key in BLS_API_KEY raises that to 500) and
     keeps them in the `bls_wages` table for a year, since the survey only changes once a year,
  4. picks the band that goes with the level: entry 10th to 25th percentile, mid 25th to 75th, senior 50th to 90th.
It is an estimate of base wages for the occupation in that place, not what the employer will pay, and it is shown as such. Nothing here
calls a paid service."""
from __future__ import annotations

import os
import re
import threading
import time

import db
import sources

API_V1 = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
API_V2 = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
PERCENTILES = (("p10", "11"), ("p25", "12"), ("p50", "13"), ("p75", "14"), ("p90", "15"))   # BLS data types: annual wage at each percentile
MAX_AGE_DAYS = 365           # the survey comes out once a year
BLOCKED_HOURS = 6            # after the API says its daily limit is reached, it is not asked again for this long
BANDS = {"entry": ("p10", "p25"), "mid": ("p25", "p75"), "senior": ("p50", "p90")}
BAND_NAMES = {"p10": "10th", "p25": "25th", "p50": "50th", "p75": "75th", "p90": "90th"}


class Unavailable(Exception):
    """The BLS cannot be asked right now. `reason` is 'limit' (its daily limit) or 'error'."""

    def __init__(self, reason: str, message: str = ""):
        super().__init__(message or reason)
        self.reason = reason


# ----------------------------------------------------------------------------- title -> occupation and level
# (pattern on the lowercase title, BLS occupation code, name shown). The first match wins, so the specific ones come first.
OCCUPATIONS = [
    (r"(engineering|software|technology|it|data|development|platform) (manager|director)|head of (engineering|data|software)", "113021", "computer and information systems managers"),
    (r"data scien|machine learning scientist|research scientist", "152051", "data scientists"),
    (r"security (engineer|analyst|architect)|cyber|infosec|appsec|devsecops", "151212", "information security analysts"),
    (r"\bsdet\b|quality assurance|\bqa\b|test (engineer|analyst|automation)|software (test|quality)|engineer in test", "151253", "software quality assurance analysts and testers"),
    (r"database architect|data architect", "151243", "database architects"),
    (r"\bdba\b|database administrator", "151242", "database administrators"),
    (r"network (engineer|architect)", "151241", "computer network architects"),
    (r"network (admin|support)|system(s)? admin|sysadmin", "151244", "network and computer systems administrators"),
    (r"help ?desk|it support|desktop support|technical support|support specialist", "151232", "computer user support specialists"),
    (r"web developer", "151254", "web developers"),
    (r"(business|systems|it) analyst|systems integration|business systems", "151211", "computer systems analysts"),
    (r"sales engineer|solutions engineer|solutions architect|technical solutions|customer engineer", "419031", "sales engineers"),
    (r"technical writer|documentation (engineer|specialist)", "273042", "technical writers"),
    (r"software|developer|programmer|devops|\bsre\b|site reliability|platform engineer|cloud engineer|backend|back-end|front.?end|full.?stack|"
     r"\bmobile\b|\bios\b|android|machine learning|\bml\b|\bai\b|data engineer|\betl\b|integration engineer|automation engineer|embedded|firmware|"
     r"application engineer|infrastructure engineer|systems engineer|solutions developer", "151252", "software developers"),
]
_OCCUPATIONS = [(re.compile(p), code, name) for p, code, name in OCCUPATIONS]
SENIOR = {"senior", "sr", "staff", "principal", "lead", "distinguished", "iii", "iv", "3", "4", "architect"}
ENTRY = {"junior", "jr", "associate", "entry", "graduate", "apprentice", "i", "1"}


def occupation_for(title: str) -> tuple[str, str] | None:
    """(BLS occupation code, name) for a job title, or None when it is not a kind of job the BLS table here covers."""
    t = (title or "").lower()
    for pattern, code, name in _OCCUPATIONS:
        if pattern.search(t):
            return code, name
    return None


def level_of(title: str) -> str:
    """'entry', 'mid' or 'senior', from the words and numerals of the title (Engineer I, Junior, Senior, Staff, III ...)."""
    words = re.findall(r"[a-z0-9]+", (title or "").lower())
    if any(w in SENIOR for w in words):
        return "senior"
    if any(w in ENTRY for w in words):
        return "entry"
    return "mid"


# ----------------------------------------------------------------------------- place -> area
STATES = {
    "AL": ("01", "Alabama"), "AK": ("02", "Alaska"), "AZ": ("04", "Arizona"), "AR": ("05", "Arkansas"), "CA": ("06", "California"),
    "CO": ("08", "Colorado"), "CT": ("09", "Connecticut"), "DE": ("10", "Delaware"), "DC": ("11", "District of Columbia"), "FL": ("12", "Florida"),
    "GA": ("13", "Georgia"), "HI": ("15", "Hawaii"), "ID": ("16", "Idaho"), "IL": ("17", "Illinois"), "IN": ("18", "Indiana"), "IA": ("19", "Iowa"),
    "KS": ("20", "Kansas"), "KY": ("21", "Kentucky"), "LA": ("22", "Louisiana"), "ME": ("23", "Maine"), "MD": ("24", "Maryland"),
    "MA": ("25", "Massachusetts"), "MI": ("26", "Michigan"), "MN": ("27", "Minnesota"), "MS": ("28", "Mississippi"), "MO": ("29", "Missouri"),
    "MT": ("30", "Montana"), "NE": ("31", "Nebraska"), "NV": ("32", "Nevada"), "NH": ("33", "New Hampshire"), "NJ": ("34", "New Jersey"),
    "NM": ("35", "New Mexico"), "NY": ("36", "New York"), "NC": ("37", "North Carolina"), "ND": ("38", "North Dakota"), "OH": ("39", "Ohio"),
    "OK": ("40", "Oklahoma"), "OR": ("41", "Oregon"), "PA": ("42", "Pennsylvania"), "RI": ("44", "Rhode Island"), "SC": ("45", "South Carolina"),
    "SD": ("46", "South Dakota"), "TN": ("47", "Tennessee"), "TX": ("48", "Texas"), "UT": ("49", "Utah"), "VT": ("50", "Vermont"),
    "VA": ("51", "Virginia"), "WA": ("53", "Washington"), "WV": ("54", "West Virginia"), "WI": ("55", "Wisconsin"), "WY": ("56", "Wyoming"),
}
_STATE_BY_NAME = {name.lower(): abbr for abbr, (_, name) in STATES.items()}

# (metro area code, name shown, "city ST" places in it). The code is the metro's CBSA code, which is how the BLS names metro areas.
METROS = [
    (35620, "the New York area", "new york ny, brooklyn ny, manhattan ny, queens ny, bronx ny, jersey city nj, hoboken nj, newark nj, white plains ny, stamford ct"),
    (31080, "the Los Angeles area", "los angeles ca, santa monica ca, culver city ca, irvine ca, anaheim ca, long beach ca, pasadena ca, burbank ca, el segundo ca, playa vista ca, glendale ca"),
    (16980, "the Chicago area", "chicago il, naperville il, schaumburg il, oak brook il, evanston il, rosemont il, deerfield il"),
    (19100, "the Dallas-Fort Worth area", "dallas tx, fort worth tx, plano tx, irving tx, frisco tx, richardson tx, arlington tx, addison tx, mckinney tx, allen tx, lewisville tx, coppell tx"),
    (26420, "the Houston area", "houston tx, sugar land tx, the woodlands tx, katy tx, spring tx"),
    (47900, "the Washington, DC area", "washington dc, arlington va, reston va, mclean va, herndon va, mclean va, tysons va, alexandria va, bethesda md, rockville md, mclean va, chantilly va, fairfax va"),
    (37980, "the Philadelphia area", "philadelphia pa, king of prussia pa, wilmington de, malvern pa, conshohocken pa"),
    (33100, "the Miami area", "miami fl, fort lauderdale fl, boca raton fl, west palm beach fl, doral fl"),
    (12060, "the Atlanta area", "atlanta ga, alpharetta ga, marietta ga, duluth ga, norcross ga, sandy springs ga, kennesaw ga"),
    (14460, "the Boston area", "boston ma, cambridge ma, waltham ma, burlington ma, somerville ma, lexington ma, bedford ma, marlborough ma, nashua nh"),
    (38060, "the Phoenix area", "phoenix az, scottsdale az, tempe az, chandler az, mesa az, gilbert az, glendale az, peoria az"),
    (41860, "the San Francisco area", "san francisco ca, oakland ca, berkeley ca, redwood city ca, menlo park ca, south san francisco ca, san mateo ca, pleasanton ca, emeryville ca, walnut creek ca, fremont ca, hayward ca, foster city ca"),
    (41940, "the San Jose / Silicon Valley area", "san jose ca, santa clara ca, sunnyvale ca, mountain view ca, palo alto ca, cupertino ca, milpitas ca, los gatos ca, campbell ca, los altos ca"),
    (40140, "the Riverside area", "riverside ca, ontario ca, temecula ca"),
    (19820, "the Detroit area", "detroit mi, southfield mi, troy mi, dearborn mi, auburn hills mi, warren mi, farmington hills mi"),
    (42660, "the Seattle area", "seattle wa, bellevue wa, redmond wa, kirkland wa, renton wa, tacoma wa, issaquah wa, everett wa"),
    (33460, "the Minneapolis area", "minneapolis mn, saint paul mn, st paul mn, bloomington mn, eden prairie mn, plymouth mn"),
    (41740, "the San Diego area", "san diego ca, carlsbad ca, la jolla ca"),
    (45300, "the Tampa area", "tampa fl, st petersburg fl, clearwater fl"),
    (19740, "the Denver area", "denver co, aurora co, lakewood co, englewood co, centennial co, broomfield co, littleton co"),
    (12580, "the Baltimore area", "baltimore md, columbia md, towson md"),
    (41180, "the St. Louis area", "st louis mo, saint louis mo, st. louis mo, clayton mo"),
    (36740, "the Orlando area", "orlando fl, lake mary fl, winter park fl"),
    (16740, "the Charlotte area", "charlotte nc, concord nc, fort mill sc, huntersville nc"),
    (41700, "the San Antonio area", "san antonio tx, new braunfels tx"),
    (38900, "the Portland area", "portland or, beaverton or, hillsboro or, vancouver wa"),
    (40900, "the Sacramento area", "sacramento ca, roseville ca, folsom ca, rancho cordova ca"),
    (38300, "the Pittsburgh area", "pittsburgh pa"),
    (12420, "the Austin area", "austin tx, round rock tx, cedar park tx, georgetown tx, pflugerville tx, san marcos tx"),
    (29820, "the Las Vegas area", "las vegas nv, henderson nv"),
    (17140, "the Cincinnati area", "cincinnati oh, mason oh, blue ash oh"),
    (28140, "the Kansas City area", "kansas city mo, kansas city ks, overland park ks, olathe ks, lenexa ks"),
    (18140, "the Columbus area", "columbus oh, dublin oh, westerville oh"),
    (26900, "the Indianapolis area", "indianapolis in, carmel in, fishers in"),
    (17460, "the Cleveland area", "cleveland oh, independence oh, beachwood oh"),
    (34980, "the Nashville area", "nashville tn, franklin tn, brentwood tn, murfreesboro tn"),
    (47260, "the Virginia Beach area", "virginia beach va, norfolk va, chesapeake va"),
    (39300, "the Providence area", "providence ri, warwick ri"),
    (33340, "the Milwaukee area", "milwaukee wi, brookfield wi"),
    (27260, "the Jacksonville area", "jacksonville fl"),
    (36420, "the Oklahoma City area", "oklahoma city ok, edmond ok"),
    (39580, "the Raleigh area", "raleigh nc, cary nc, morrisville nc, apex nc"),
    (20500, "the Durham-Chapel Hill area", "durham nc, chapel hill nc"),
    (32820, "the Memphis area", "memphis tn"),
    (40060, "the Richmond area", "richmond va, glen allen va"),
    (31140, "the Louisville area", "louisville ky"),
    (41620, "the Salt Lake City area", "salt lake city ut, murray ut, draper ut, south jordan ut, lehi ut, sandy ut"),
    (13820, "the Birmingham area", "birmingham al"),
    (15380, "the Buffalo area", "buffalo ny, amherst ny"),
    (25540, "the Hartford area", "hartford ct, west hartford ct, farmington ct"),
    (40380, "the Rochester area", "rochester ny"),
    (46060, "the Tucson area", "tucson az"),
    (46140, "the Tulsa area", "tulsa ok"),
    (36540, "the Omaha area", "omaha ne"),
    (10740, "the Albuquerque area", "albuquerque nm"),
    (14260, "the Boise area", "boise id, meridian id"),
    (14500, "the Boulder area", "boulder co, louisville co"),
    (31540, "the Madison area", "madison wi"),
    (24860, "the Greenville area", "greenville sc"),
    (17820, "the Colorado Springs area", "colorado springs co"),
    (26620, "the Huntsville area", "huntsville al"),
    (11460, "the Ann Arbor area", "ann arbor mi"),
    (19780, "the Des Moines area", "des moines ia, west des moines ia"),
    (16700, "the Charleston area", "charleston sc"),
    (39900, "the Reno area", "reno nv"),
]
_METRO_OF: dict[tuple[str, str], tuple[int, str]] = {}
for _code, _label, _places in METROS:
    for _place in _places.split(", "):
        _city, _st = _place.rsplit(" ", 1)
        _METRO_OF.setdefault((_city, _st.upper()), (_code, _label))

NATIONAL = ("N", "0000000", "the United States")


def _clean_city(text: str) -> str:
    """'St. Louis (Hybrid)' -> 'st louis'."""
    text = re.sub(r"\(.*?\)", " ", text.lower())
    text = re.sub(r"\b(remote|hybrid|onsite|on-site|metro|area|greater)\b", " ", text)
    return " ".join(re.sub(r"[^a-z ]", " ", text.replace(".", "")).split())


def _state_of(text: str) -> str | None:
    t = re.sub(r"[^a-z ]", "", text.lower()).strip()
    if t.upper() in STATES:
        return t.upper()
    return _STATE_BY_NAME.get(t)


def area_chain(location: str) -> list[tuple[str, str, str]]:
    """[(BLS area type, area code, name shown)] from the most specific place to the whole country, for a posting's place such as
    'Austin, Texas, United States', 'Cupertino, CA; Austin, TX' (the first place counts) or 'Remote'. A place the table does not know
    still gets its state's figures, and a place with no state gets the national ones."""
    first = re.sub(r"\(.*?\)", " ", re.split(r"[;|]| / ", location or "")[0])      # '(Hybrid)' and the like say nothing about the place
    parts = [p.strip() for p in first.split(",") if p.strip()]
    chain: list[tuple[str, str, str]] = []
    city = _clean_city(parts[0]) if parts else ""
    state = next((s for s in (_state_of(p) for p in parts[1:]) if s), None) or (_state_of(parts[0]) if len(parts) == 1 else None)
    if city and state and (city, state) in _METRO_OF:
        code, label = _METRO_OF[(city, state)]
        chain.append(("M", f"00{code}", label))
    if state:
        chain.append(("S", f"{STATES[state][0]}00000", STATES[state][1]))
    chain.append(NATIONAL)
    return chain


# ----------------------------------------------------------------------------- the BLS and the cache
def series_id(area_type: str, area: str, occupation: str, datatype: str) -> str:
    """OE + U (not seasonally adjusted) + area type + area code (7) + industry (000000: all) + occupation (6) + data type (2)."""
    return f"OEU{area_type}{area}000000{occupation}{datatype}"


def _number(value) -> int | None:
    try:
        number = int(float(str(value).replace(",", "")))
    except ValueError:
        return None
    return number if number > 0 else None


def query_bls(series: list[str], key: str | None = None) -> dict[str, tuple[int | None, int | None]]:
    """{series id: (year, annual wage or None)} from the BLS API for the latest year. Raises Unavailable when it cannot be used."""
    body: dict = {"seriesid": series, "latest": True}
    url = API_V1
    if key:
        body["registrationkey"], url = key, API_V2
    try:
        response = sources._session().post(url, json=body, timeout=sources.TIMEOUT)
        response.raise_for_status()
        data = response.json()
    except (OSError, ValueError) as e:       # requests' errors are OSErrors
        raise Unavailable("error", f"the BLS did not answer ({type(e).__name__})") from None
    if data.get("status") != "REQUEST_SUCCEEDED":
        message = " ".join(str(m) for m in data.get("message") or [])
        raise Unavailable("limit" if "threshold" in message.lower() else "error", message[:200])
    out: dict[str, tuple[int | None, int | None]] = {}
    for s in (data.get("Results") or {}).get("series") or []:
        rows = s.get("data") or []
        out[s.get("seriesID", "")] = (_number(rows[0].get("year")), _number(rows[0].get("value"))) if rows else (None, None)
    return out


_lock = threading.Lock()


def _cached(conn, occupation: str, areas, now: float) -> dict:
    """{(area type, area code): row} for the areas already held and not older than a year."""
    out = {}
    for area_type, area, _ in areas:
        row = conn.execute("SELECT * FROM bls_wages WHERE occupation=? AND area_type=? AND area=?", (occupation, area_type, area)).fetchone()
        if row and now - row["fetched"] < MAX_AGE_DAYS * 86400:
            out[(area_type, area)] = row
    return out


def _fetch_missing(conn, occupation: str, areas, now: float, query) -> None:
    key = os.environ.get("BLS_API_KEY") or None
    blocked = db.get_meta(conn, "bls_blocked_until")
    if blocked and now < float(blocked):
        raise Unavailable("limit", "the BLS daily limit was reached; try later")
    ids = {(t, a): [series_id(t, a, occupation, dt) for _, dt in PERCENTILES] for t, a, _ in areas}
    try:
        got = (query or query_bls)([sid for group in ids.values() for sid in group], key)
    except Unavailable as e:
        if e.reason == "limit":
            db.set_meta(conn, "bls_blocked_until", str(now + BLOCKED_HOURS * 3600))
        raise
    for (t, a), group in ids.items():
        cells = [got.get(sid, (None, None)) for sid in group]
        year = next((y for y, v in cells if v is not None and y), None)
        values = [v for _, v in cells]
        conn.execute(
            "INSERT INTO bls_wages (occupation, area_type, area, year, p10, p25, p50, p75, p90, fetched) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT (occupation, area_type, area) DO UPDATE SET year=excluded.year, p10=excluded.p10, p25=excluded.p25, "
            "p50=excluded.p50, p75=excluded.p75, p90=excluded.p90, fetched=excluded.fetched",
            (occupation, t, a, year, *values, now))
    conn.commit()


def _band(row, level: str) -> tuple[str, str] | None:
    """The two percentiles to show for this level, falling back to the middle half when the survey withholds one of them."""
    for low, high in (BANDS[level], BANDS["mid"]):
        if row[low] and row[high] and row[low] <= row[high]:
            return low, high
    return None


def _round(n: int) -> int:
    return int(round(n / 1000.0)) * 1000


def estimate(conn, title: str, location: str, now: float | None = None, query=None) -> dict:
    """{"ok": True, "low", "high", "area", "occupation", "level", "band", "year"} for a job, or {"ok": False, "reason": ...} where reason is
    'no_occupation' (a kind of job the table does not cover), 'no_data' (the survey has no figures for it) or 'limit' / 'error' (the BLS
    cannot be asked now). `query` replaces the call to the BLS (tests)."""
    found = occupation_for(title)
    if not found:
        return {"ok": False, "reason": "no_occupation"}
    occupation, occupation_name = found
    level = level_of(title)
    areas = area_chain(location)
    now = time.time() if now is None else now
    with _lock:                                  # two jobs opened together ask the BLS once
        rows = _cached(conn, occupation, areas, now)
        if len(rows) < len(areas):
            try:
                _fetch_missing(conn, occupation, [a for a in areas if (a[0], a[1]) not in rows], now, query)
            except Unavailable as e:
                # figures already held for a broader area are still worth showing, so only give up when there are none
                rows = _cached(conn, occupation, areas, now)
                if not any(_band(r, level) for r in rows.values()):
                    return {"ok": False, "reason": e.reason}
            else:
                rows = _cached(conn, occupation, areas, now)
    for area_type, area, label in areas:
        row = rows.get((area_type, area))
        band = _band(row, level) if row else None
        if band:
            return {"ok": True, "low": _round(row[band[0]]), "high": _round(row[band[1]]), "area": label, "occupation": occupation_name,
                    "level": level, "band": f"{BAND_NAMES[band[0]]} to {BAND_NAMES[band[1]]} percentile", "year": row["year"]}
    return {"ok": False, "reason": "no_data"}
