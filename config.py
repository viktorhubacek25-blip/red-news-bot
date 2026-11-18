# -*- coding: utf-8 -*-
"""
Nastavení Red News bota.
Tady se ladí všechno: zdroje, klíčová slova, práh důležitosti, kalendář.
Po úpravě stačí soubor uložit (commitnout) na GitHubu – další běh už jede s novým nastavením.
"""
from urllib.parse import quote_plus

# ---------------------------------------------------------------------------
# OBECNÉ
# ---------------------------------------------------------------------------
TIMEZONE = "Europe/Prague"

# Minimální skóre, od kterého se headline pošle do Telegramu.
# Vyšší = méně zpráv, jen to největší. Doporučený rozsah 8–12.
SCORE_THRESHOLD = 10

# Denní pojistka proti zahlcení: po DAILY_SOFT_CAP zprávách za den projdou už jen zprávy se skóre
# alespoň SCORE_THRESHOLD_AFTER_CAP.
DAILY_SOFT_CAP = 10
SCORE_THRESHOLD_AFTER_CAP = 13

# Neposílat články/squawky s výsledky dat (výsledek přijde jednou jako 📊 zpráva z kalendáře)
SKIP_DATA_RELEASES = True

# Zprávy starší než tohle se ignorují (hodiny).
MAX_AGE_HOURS = 3

# Kolik headlinů max poslat v jednom běhu jako samostatné zprávy.
# Když jich je víc, spojí se do jedné souhrnné zprávy (ochrana proti spamu).
MAX_SEPARATE_MESSAGES_PER_RUN = 3

# Podobnost titulků (0–1), nad kterou se zpráva bere jako duplikát už poslané.
DUPLICATE_SIMILARITY = 0.55
DUPLICATE_WINDOW_HOURS = 8

# Tichý režim: v těchto hodinách (místní čas) chodí zprávy bez zvuku.
# None = vypnuto. Příklad: (0, 7) = od půlnoci do 7:00 potichu.
QUIET_HOURS = None

# Zobrazovat náhled odkazu pod zprávou?
LINK_PREVIEW = False

# Překlad headlinů (bezplatný Google Translate, záloha MyMemory).
# None = posílat anglicky. Při chybě překladu se pošle originál.
TRANSLATE_TO = "cs"
# Přidat pod český překlad i původní anglický titulek (malým písmem)?
SHOW_ORIGINAL = False

# ---------------------------------------------------------------------------
# ZDROJE HEADLINŮ
# trust = bonus ke skóre za důvěryhodnost zdroje
# ---------------------------------------------------------------------------
_TOP_SITES = "(site:reuters.com OR site:bloomberg.com OR site:wsj.com OR site:ft.com OR site:cnbc.com OR site:apnews.com)"


def _gnews(query):
    q = quote_plus(f"{query} {_TOP_SITES} when:2h")
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


def gnews_range(query, after, before):
    """Google News za konkrétní období (after/before = 'YYYY-MM-DD')."""
    q = quote_plus(f"{query} {_TOP_SITES} after:{after} before:{before}")
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


SOURCES = [
    # Rychlé "squawk" headliny – hlavní zdroj breaking zpráv a výsledků dat
    {"name": "FinancialJuice", "url": "https://www.financialjuice.com/feed.ashx?xy=rss", "trust": 1},
    {"name": "investingLive", "url": "https://investinglive.com/feed/news", "trust": 1},
    # Oficiální zdroje centrálních bank
    {"name": "Federal Reserve", "url": "https://www.federalreserve.gov/feeds/press_all.xml", "trust": 3},
    {"name": "ECB", "url": "https://www.ecb.europa.eu/rss/press.html", "trust": 2},
    # Trumpovy příspěvky z Truth Social (archiv trumpstruth.org) – projdou jen ty o trzích
    {"name": "Trump (Truth Social)", "url": "https://trumpstruth.org/feed", "trust": 0, "trump": True},
    # Google News filtrovaný jen na top agentury (Reuters, Bloomberg, WSJ, FT, CNBC, AP)
    {"name": "GN Centrální banky", "url": _gnews('(Fed OR FOMC OR Powell OR "rate cut" OR "rate hike" OR ECB OR "Bank of Japan")'), "trust": 0, "google": True},
    {"name": "GN Makro data", "url": _gnews('(CPI OR inflation OR payrolls OR "jobs report" OR GDP OR PCE OR "retail sales")'), "trust": 0, "google": True},
    {"name": "GN Komodity", "url": _gnews('(oil OR crude OR OPEC OR gold OR Brent)'), "trust": 0, "google": True},
    {"name": "GN Trhy & geopolitika", "url": _gnews('(tariffs OR sanctions OR "Treasury yields" OR "dollar" OR "Wall Street" OR selloff)'), "trust": 0, "google": True},
]

# Bonus za původního vydavatele (u Google News se pozná z tagu <source>)
PUBLISHER_TRUST = {
    "reuters": 2,
    "bloomberg": 2,
    "the wall street journal": 2,
    "wsj": 2,
    "financial times": 2,
    "associated press": 1,
    "ap news": 1,
    "cnbc": 1,
}

# ---------------------------------------------------------------------------
# SKÓROVÁNÍ – klíčová slova (regex, bez ohledu na velikost písmen)
# ---------------------------------------------------------------------------
# Tyto vzory pošlou zprávu VŽDY (skóre 99) – zásadní události.
MUST_SEND = [
    r"\bFOMC statement\b",
    r"(^|: )(the )?Fed(eral Reserve)? (raises|cuts|hikes|lowers|holds|leaves|keeps)\b.*\brates?\b",
    r"\bFed(eral Reserve)? (decision|rate decision)\b",
    r"\bemergency (rate )?(cut|hike|meeting)\b",
    r"\bECB (raises|cuts|hikes|lowers|holds|leaves|keeps)\b",
    r"\bmonetary policy decisions?\b",
    r"\bOPEC\+? (agrees|decides|cuts|raises|boosts|extends)\b",
    r"\bTrump\b.*\b(announces?|imposes?|slaps?|raises?|hikes?|threatens?|orders?)\b.*\btariffs?\b",
    r"\b\d{2,3}\s?(%|percent) tariffs?\b",
]

# (váha, [vzory]) – každý zasažený vzor přidá váhu, max. 2× váha za skupinu
KEYWORD_GROUPS = [
    # Tier A – zásadní pro USD / indexy / komodity
    (6, [
        r"\bFOMC\b", r"\bFederal Open Market Committee\b", r"\bPowell\b",
        r"\brate (cut|hike)s?\b", r"\b(cut|hike|raise|lower)s? (interest )?rates\b", r"\bbasis points?\b|\b\d{2,3} ?bps?\b",
        r"\bnon-?farm\b", r"\bpayrolls?\b", r"\bCPI\b", r"\bcore PCE\b", r"\bPCE\b",
        r"\b(consumer|producer|wholesale|import) prices\b|\bjobs report\b|\bjob growth\b",
        r"\bOPEC\+?\b", r"\btariffs?\b", r"\bsanctions?\b", r"\bintervention\b",
        r"\bdowngrades?\b", r"\bdefault\b", r"\bgovernment shutdown\b",
        r"\binvasion\b|\binvades\b", r"\bmissiles?\b|\bairstrikes?\b", r"\bceasefire\b",
        r"\bStrait of Hormuz\b", r"\bnuclear\b",
        # Námořní doprava / útoky (ropa, Hormuz, Rudé moře)
        # tanker/loď jen v souvislosti s ropou nebo konfliktem (ne nehody říčních lodí apod.)
        r"^(?=.*\b(Hormuz|Red Sea|Iran\w*|IRGC|Houthis?|oil|crude|attack\w*|mines?|strikes?|struck|seiz\w*|UKMTO|Gulf|Oman\w*|navy|fire|explosion)\b).*\b(tankers?|vessels?)\b|\bUKMTO\b|\bRed Sea\b",
        # „mine“ jen jako námořní mina, ne důl / těžařská firma
        r"\battack(s|ed)?\b|\b(sea|naval) mines?\b|\bhit(s|ting)? (a |another )?mines?\b|\bmines? (in|near|off|east|west|north|south)\b|\bmin(ed|ing) (the )?(strait|waters|sea)\b|\bexplosions?\b|\bdrone strikes?\b|\bseiz(e|ed|es|ure)\b",
    ]),
    # Tier B – důležitá makro data a centrální banky
    (4, [
        r"\bGDP\b", r"\bretail sales\b", r"\bISM\b", r"\bPMI\b", r"\bjobless claims\b",
        r"\bJOLTS\b", r"\bPPI\b", r"\binflation\b", r"\bunemployment rate\b",
        r"\bECB\b", r"\bLagarde\b", r"\bBOJ\b|\bBank of Japan\b", r"\bUeda\b",
        r"\bPBOC\b", r"\bBOE\b|\bBank of England\b", r"\bSNB\b",
        # Fed speakers – jeden vzor, aby se samotné jméno nepočítalo dvakrát
        r"\bFed's\b|\bFed (chair|governor|president)\b|\bWaller\b|\bJefferson\b|\bBowman\b|\bBarr\b|\bLogan\b|\bDaly\b|\bGoolsbee\b|\bHammack\b|\bMusalem\b|\bKashkari\b|\bSchmid\b|\bMiran\b|\bBostic\b|\bHarker\b",
        r"\bTreasury (yields?|auction|refunding)\b", r"\bBessent\b",
        r"\bEIA\b.*\b(crude|inventor)", r"\bcrude (oil )?inventor(y|ies)\b",
    ]),
    # Tier C – aktiva (samo o sobě nestačí, ale přidá)
    (2, [
        r"\bdollar\b|\bDXY\b|\bUSD\b|\bgreenback\b",
        r"\bS&P\b|\bS&P 500\b|\bNasdaq\b|\bDow\b|\bRussell\b|\bVIX\b|\bWall Street\b|\bstocks?\b|\bequities\b",
        r"\boil\b|\bcrude\b|\bBrent\b|\bWTI\b|\bgold\b|\bsilver\b|\bcopper\b|\bnatural gas\b",
        r"\byields?\b|\b10-year\b|\bTreasur(y|ies)\b|\bbonds?\b",
        r"\byuan\b|\byen\b|\beuro\b",
    ]),
    # Signály "pohybu" / breaking
    (3, [
        r"\bbreaking\b", r"\burgent\b", r"\bflash\b", r"\bjust in\b",
        r"\bplunges?\b|\bplummets?\b|\bsoars?\b|\bsurges?\b|\bcrash(es)?\b|\btumbles?\b|\bspikes?\b",
        r"\brecord (high|low)\b", r"\bhighest since\b|\blowest since\b",
        r"\bhalt(s|ed)?\b", r"\bunexpected(ly)?\b|\bsurprise\b",
    ]),
    # Směr měnové politiky (v kombinaci s Fedem/ECB apod.)
    (3, [r"\bcuts?\b|\bhikes?\b|\beasing\b|\btightening\b|\bhawkish\b|\bdovish\b|\bpause\b"]),
    # Trump/Bílý dům – samo slabé, v kombinaci s tarify/Fedem silné
    (2, [r"\bTrump\b", r"\bWhite House\b", r"\bXi\b|\bBeijing\b", r"\bPutin\b|\bKremlin\b"]),
    # Blízký východ (ropa)
    (3, [r"\bIran\b|\bIsrael\b|\bHouthis?\b|\bSaudi\b"]),
]

# Zpráva projde jen když se týká USA / dolaru / US indexů / komodit (ostatní měny jen v sobotním souhrnu).
# None = vypnout tento filtr.
REQUIRE_RELEVANCE = (
    r"(?-i:\bUS\b|\bU\.S\.)|\bUnited States\b|\bAmerica(n)?\b|\bFed\b|\bFed's\b|\bFOMC\b|\bFederal Reserve\b"
    r"|\bPowell\b|\bWarsh\b|\bTreasur(y|ies)\b|\bBessent\b|\bTrump\b|\bWhite House\b|\bWashington\b"
    r"|\bCongress\b|\bSenate\b|\bdollar\b|\bDXY\b|\bgreenback\b|\bWall Street\b|\bS&P\b|\bNasdaq\b"
    r"|\bDow\b|\bVIX\b|\bnon-?farm\b|\bPCE\b|\bISM\b|\bjobless claims\b"
    r"|\boil\b|\bcrude\b|\bBrent\b|\bWTI\b|\bOPEC\b|\bgold\b|\bsilver\b|\bcopper\b|\bnatural gas\b"
    r"|\bEIA\b|\bHormuz\b|\btankers?\b|\bIran\b|\bRed Sea\b|\btariffs?\b|\bWaller\b|\bJefferson\b"
    r"|\bBowman\b|\bBarr\b|\bLogan\b|\bDaly\b|\bGoolsbee\b|\bHammack\b|\bMusalem\b|\bKashkari\b"
    r"|\bSchmid\b|\bMiran\b|\bBostic\b|\bHarker\b|\bWilliams\b|\bCollins\b"
)

# Headline s výsledkem dat ("US CPI 3.1% vs 3.0% expected")
DATA_RELEASE_PATTERN = r"(\bactual\b|\bvs\.?\b|\bversus\b).*(\bexp(ected)?\b|\best(imate)?\b|\bforecast\b|\bconsensus\b|\bprior\b|\bprev(ious)?\b)"
DATA_RELEASE_BONUS = 3

# Bonus pro Trumpovy příspěvky z Truth Social (pošlou se jen ty, které obsahují tržní témata)
TRUMP_BONUS = 4
# Bonus, když headline odpovídá červené události z kalendáře (±60 min od vydání)
CALENDAR_MATCH_BONUS = 5

# Tyto vzory skóre výrazně sníží (šum, analýzy, reklama, krypto…)
NEGATIVE_PATTERNS = [
    r"\bpreview\b", r"\bweek ahead\b", r"\bwhat to (watch|expect)\b", r"\bopinion\b", r"\bcolumn\b",
    r"\bpodcast\b", r"\bvideo\b", r"\bwatch:\b", r"\bsponsored\b", r"\bbrokers?\b", r"\bwrap\b",
    r"\brecap\b", r"\bexplainer\b", r"\bnewsletter\b", r"\btechnical analysis\b", r"\btradeCompass\b",
    r"\bbitcoin\b|\bcrypto\b|\bethereum\b", r"\bhow to\b", r"\bcertified\b", r"\bIPO\b",
    r"\bstocks? to (buy|watch)\b", r"\bdividend\b", r"\bmortgage rates? today\b",
    r"\benforcement action\b", r"\bterminat(es|ion) of enforcement\b", r"\bcommunity reinvestment\b",
    r"\bapplication\b", r"\bjob opening\b",
]
NEGATIVE_PENALTY = 12

# Pokračující témata: po odeslání zprávy na téma dalších N hodin nic dalšího k tématu
# (kromě MUST_SEND). Agentury o jednom příběhu píšou pořád nové titulky, slovní
# deduplikace je nepozná. (název, regex, hodin)
TOPIC_COOLDOWNS = [
    ("Hormuz / tankery / Rudé moře",
     r"\bHormuz\b|\btankers?\b|\bvessels?\b|\bUKMTO\b|\bRed Sea\b|\bHouthis?\b|\b(sea|naval) mines?\b|\bhit(s|ting)? (a |another )?mines?\b",
     12),
]

# ---------------------------------------------------------------------------
# ŠTÍTKY AKTIV v Telegram zprávě
# ---------------------------------------------------------------------------
ASSET_TAGS = [
    ("💵 USD", r"\bdollar\b|\bDXY\b|\bUSD\b|\bFed\b|\bFOMC\b|\bPowell\b|\bTreasur|\byields?\b|(?-i:\bUS\b)|\bCPI\b|\bpayrolls?\b|\bPCE\b|\bnon-?farm\b"),
    ("📈 Indexy", r"\bS&P\b|\bNasdaq\b|\bDow\b|\bstocks?\b|\bequit|\bWall Street\b|\bindex\b|\bindices\b|\bVIX\b|\bDAX\b|\bNikkei\b"),
    ("🛢 Komodity", r"\boil\b|\bcrude\b|\bBrent\b|\bWTI\b|\bOPEC\b|\bgold\b|\bsilver\b|\bcopper\b|\bnatural gas\b|\bEIA\b"),
]

# ---------------------------------------------------------------------------
# EKONOMICKÝ KALENDÁŘ (ForexFactory – červené události)
# ---------------------------------------------------------------------------
CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CALENDAR_REFRESH_MINUTES = 60  # feed má limit na počet stažení, nestahovat častěji

# Měny, jejichž červené události chceš (USD + hlavní složky DXY + Čína)
# – denní přehledy, upozornění a výsledky: jen DAILY_CURRENCIES
# – sobotní souhrn: všechny CALENDAR_CURRENCIES
CALENDAR_CURRENCIES = ["USD", "EUR", "JPY", "CNY"]
DAILY_CURRENCIES = ["USD"]
# Po zveřejnění červené události poslat výsledek (skutečnost vs. odhad)
SEND_RESULTS = True
CALENDAR_IMPACTS = ["High"]
# Události, které chceš vždy, i když nejsou červené (regex na název)
CALENDAR_ALWAYS = [r"Crude Oil Inventories", r"OPEC"]

# Svátky / bank holidays (banky zavřené = nižší likvidita) – pro tyto měny
HOLIDAY_CURRENCIES = ["USD", "EUR", "GBP", "JPY", "CNY", "CHF", "CAD", "AUD"]

# Ranní přehled dne (místní čas). None = vypnuto.
DAILY_SUMMARY_HOUR = 7
# Večerní přehled – události na zítřek (místní čas). None = vypnuto.
EVENING_SUMMARY_HOUR = 20
# Ve večerním přehledu (po–pá) i denní výsledky všech aktiv z MARKETS
EVENING_MARKETS = True
# V pondělí přidat do ranního přehledu výhled celého týdne
WEEKLY_OUTLOOK_ON_MONDAY = True

# Upozornění X minut před událostí
REMIND_MINUTES_BEFORE = 15

# ---------------------------------------------------------------------------
# TÝDENNÍ SOUHRN (sobota)
# ---------------------------------------------------------------------------
WEEKLY_REPORT = True
WEEKLY_REPORT_WEEKDAY = 5   # 0 = pondělí … 5 = sobota
WEEKLY_REPORT_HOUR = 9      # místní čas

# AI komentář přes Claude API – použije se jen když je v GitHub Secrets ANTHROPIC_API_KEY.
# Bez klíče přijdou jen čísla + hlavní zprávy týdne ke každému aktivu.
WEEKLY_AI_MODEL = "claude-sonnet-5-5"

# Pro souhrn se navíc dohledají titulky top agentur za celý týden (i to, co bot přes týden nezachytil)
WEEKLY_NEWS_QUERIES = [
    '(Fed OR FOMC OR Powell OR "rate cut" OR "rate hike" OR ECB OR "Bank of Japan")',
    '(CPI OR PPI OR inflation OR payrolls OR "jobs report" OR GDP OR PCE OR "retail sales")',
    '(oil OR crude OR OPEC OR gold OR Brent OR copper OR "natural gas")',
    '(tariffs OR sanctions OR "Treasury yields" OR dollar OR "Wall Street" OR stocks)',
]

# Sledovaná aktiva: (skupina, [(název, Yahoo symbol, Stooq symbol, typ)])
# typ "price" = změna v %, "yield" = výnos, změna v bazických bodech
MARKETS = [
    ("💵 Dolar", [
        ("DXY", "DX-Y.NYB", "dx.f", "price"),
        ("EUR/USD", "EURUSD=X", "eurusd", "price"),
        ("USD/JPY", "JPY=X", "usdjpy", "price"),
        ("US 10Y výnos", "^TNX", "10yusy.b", "yield"),
    ]),
    ("📈 Indexy", [
        ("S&P 500", "^GSPC", "^spx", "price"),
        ("Nasdaq 100", "^NDX", "^ndx", "price"),
        ("Dow Jones", "^DJI", "^dji", "price"),
        ("DAX", "^GDAXI", "^dax", "price"),
    ]),
    ("🛢 Komodity", [
        ("Ropa WTI", "CL=F", "cl.f", "price"),
        ("Ropa Brent", "BZ=F", "cb.f", "price"),
        ("Zlato", "GC=F", "gc.f", "price"),
        ("Stříbro", "SI=F", "si.f", "price"),
        ("Měď", "HG=F", "hg.f", "price"),
        ("Zemní plyn", "NG=F", "ng.f", "price"),
    ]),
]

# Blok „💰 Kolik $ přibylo / ubylo“ (odhad změny hodnoty trhu v dolarech)
# Ráno (v DAILY_SUMMARY_HOUR) přijde samostatně za předchozí obchodní den, s konečnými čísly.
MARKET_VALUE_MORNING = True
MARKET_VALUE_MORNING_DAYS = [1, 2, 3, 4, 5]  # 0 = pondělí … út–so = za po–pá
MARKET_VALUE_EVENING = True  # ve 20:00 (po–pá) samostatná zpráva 💰 za dnešek, průběžná čísla
# název aktiva (jako v MARKETS): (typ, hodnota, započítat do součtu, poznámka)
#   "cap" = tržní kapitalizace indexu v USD → změna = kapitalizace × denní změna %
#           (přibližné hodnoty k 11/2026, stačí jednou za čas aktualizovat)
#   "qty" = množství komodity v jednotkách ceny → změna = množství × změna ceny
#           (počítá se z aktuální ceny, aktualizovat není potřeba)
# Nasdaq 100 a Dow jsou z velké části uvnitř S&P 500 → do součtu se nepočítají.
# Ropa WTI chybí záměrně, aby se ropa nepočítala dvakrát (bere se Brent).
MARKET_VALUE = {
    "S&P 500":    ("cap", 70e12, True, ""),
    "Nasdaq 100": ("cap", 40e12, False, "v S&P"),
    "Dow Jones":  ("cap", 19e12, False, "v S&P"),
    "Zlato":      ("qty", 7.21e9, True, "zásoby"),         # trojské unce (WGC: ~224 200 t)
    "Stříbro":    ("qty", 56e9, True, "zásoby"),           # trojské unce (~1,75 mil. t)
    "Ropa Brent": ("qty", 38.3e9, True, "roční produkce"),  # barely (~105 mil. barelů/den)
    "Měď":        ("qty", 50.7e9, True, "roční produkce"),  # libry (~23 mil. t/rok)
    "Zemní plyn": ("qty", 39e9, True, "roční produkce USA"),  # MMBtu (Henry Hub ~ US produkce)
}
