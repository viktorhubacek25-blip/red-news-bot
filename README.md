# 🔴 Red News bot

Posílá do Telegramu **jen zprávy s velkým dopadem** na dolar, indexy a komodity:

- **Breaking headliny** (Fed, CPI, NFP, OPEC, cla, geopolitika…) z FinancialJuice, investingLive, Fedu, ECB a z Google News filtrovaného na Reuters, Bloomberg, WSJ, FT, CNBC a AP. Každá zpráva dostane skóre a pošle se jen nad prahem. Titulky chodí **přeložené do češtiny**, se zdrojem a odkazem na originál.
- **Ranní přehled** (7:00) červených událostí na dnešek a **večerní přehled** (20:00) na zítřek z kalendáře ForexFactory, včetně 🏦 **bank holidays** hlavních měn. V pondělí ráno a v neděli večer navíc výhled na celý týden.
- **Upozornění 15 minut** před každou červenou událostí.
- Duplikáty z různých zdrojů se slučují. Když toho přijde hodně najednou, dostaneš jednu souhrnnou zprávu.

Běží zdarma na GitHub Actions každých 5 minut. Počítač nemusí být zapnutý.

---

## Nastavení (cca 10 minut)

### 1. Telegram bot
1. V Telegramu otevři **@BotFather**, pošli `/newbot` a zadej jméno a username (musí končit na `bot`).
2. BotFather ti pošle **token** (vypadá jako `123456789:AAH...`). Ulož si ho.
3. Otevři chat se svým novým botem a pošli mu libovolnou zprávu, třeba `ahoj`.
4. V prohlížeči otevři `https://api.telegram.org/bot<TOKEN>/getUpdates` (místo `<TOKEN>` dosaď svůj token).
   Najdi `"chat":{"id":123456789`. To číslo je tvoje **chat ID**.
   *(Když je výsledek prázdný, pošli botovi další zprávu a stránku obnov.)*

### 2. GitHub repozitář
1. Na GitHubu klikni na **New repository** a pojmenuj ho třeba `red-news-bot`.
2. Zvol **Public**. Veřejné repozitáře mají Actions zdarma bez limitu. Soukromé mají limit 2 000 minut měsíčně a bot by ho překročil. Tokeny jsou i ve veřejném repu skryté v Secrets, vidět je jen kód.
3. Klikni na **uploading an existing file** a nahraj `news_bot.py`, `config.py` a `README.md`. Pak **Commit changes**.
4. Workflow: **Add file → Create new file**, jako název napiš přesně `.github/workflows/news.yml`, vlož obsah souboru `news.yml` a dej **Commit**.
   *(Složka `.github` začíná tečkou a ve Finderu nebo Průzkumníku bývá skrytá. Proto je jednodušší soubor vytvořit takhle.)*

### 3. Tajné klíče
**Settings → Secrets and variables → Actions → New repository secret**. Přidej dva:

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | token od BotFathera |
| `TELEGRAM_CHAT_ID` | tvoje chat ID |

### 4. Spuštění a test
1. Záložka **Actions**. Pokud se objeví tlačítko pro povolení workflows, potvrď ho.
2. Vlevo vyber **red-news-bot**, pak **Run workflow** (políčko *test* nech zaškrtnuté) a znovu **Run workflow**.
3. Do minuty ti přijde 🧪 testovací zpráva:
   - ✅/❌ u každého zdroje, tedy co funguje,
   - top headliny posledních 3 hodin se skóre a s tím, co by prošlo filtrem,
   - dnešní červené události.
4. Hotovo. Od teď běží automaticky každých 5 minut. První automatický běh pošle „✅ Red News bot běží“.

---

## Ladění (soubor `config.py`)

Na GitHubu otevři `config.py`, klikni na ✏️, uprav a dej **Commit**. Další běh už jede s novým nastavením.

| Nastavení | Co dělá |
|---|---|
| `SCORE_THRESHOLD = 8` | Práh důležitosti. Výš = méně zpráv (10 = jen to největší), níž = víc. |
| `CALENDAR_CURRENCIES` | Měny pro kalendář (výchozí USD, EUR, JPY, CNY). |
| `CALENDAR_ALWAYS` | Události, které chceš vždy, i když nejsou červené (výchozí ropné zásoby, OPEC). |
| `DAILY_SUMMARY_HOUR = 7` | Kdy přijde ranní přehled (`None` = vypnout). |
| `EVENING_SUMMARY_HOUR = 20` | Kdy přijde večerní přehled na zítřek (`None` = vypnout). |
| `HOLIDAY_CURRENCIES` | Měny, u kterých hlásit svátky (bank holidays). |
| `TRANSLATE_TO = "cs"` | Jazyk překladu titulků (`None` = posílat anglicky). |
| `SHOW_ORIGINAL = False` | `True` = pod českým překladem i původní anglický titulek. |
| `REMIND_MINUTES_BEFORE = 15` | Kolik minut předem upozornit. |
| `QUIET_HOURS = None` | Např. `(0, 7)` = od půlnoci do 7:00 chodí zprávy potichu. |
| `MUST_SEND`, `KEYWORD_GROUPS`, `NEGATIVE_PATTERNS` | Klíčová slova a jejich váhy. |
| `SOURCES` | Zdroje zpráv. Přidat nebo odebrat RSS je jeden řádek. |

**Jak se počítá skóre:** klíčová slova (Fed/CPI/OPEC/cla = +6, makro data a centrální banky = +4, aktiva = +2, pohyby typu „plunges“/„record high“ = +3), výsledek dat „X vs Y expected“ = +3, shoda s právě vycházející červenou událostí = +5, důvěryhodný zdroj = +1 až +3, šum (preview, wrap, krypto, reklama…) = −12. Skóre vidíš u každé zprávy. Když ti něco chodí zbytečně nebo něco chybí, pošli mi to a filtr doladíme.

---

## Dobré vědět
- **Zpoždění:** GitHub spouští naplánované běhy „zhruba“ každých 5 minut, ve špičce i o 5–15 minut později. Na sekundové reakce je potřeba vlastní server. Bot jde přesunout beze změn kódu.
- **Výsledky dat** (skutečná hodnota vs. odhad) chodí jako headline z FinancialJuice nebo investingLive. Kalendářový feed obsahuje jen odhad a předchozí hodnotu.
- **Nefunkční zdroj:** když nějaký zdroj hodinu nefunguje, bot ti jednou pošle ⚠️ a po opravě ✅.
- **Keepalive:** GitHub vypíná naplánované běhy po 60 dnech bez aktivity. Bot proto jednou za 45 dní udělá prázdný commit.
- GitHub Actions jsou primárně určené pro automatizaci softwarových projektů. Malí osobní boti jsou běžní, ale pokud by to někdy GitHub omezil, bot poběží beze změn i na levném VPS.
- **Další krok:** AI hodnocení přes Claude API (cca 1 $ měsíčně) místo klíčových slov, pro výrazně přesnější filtr.

## Lokální test (volitelné)
```bash
python3 news_bot.py --dry-run --test   # nic neposílá, jen vypíše
```
