# Manual book audit — 2026-08-23

Everything in the wallet that is **not** a crypto up/down fleet market. The fleet has had ~12h of
operator attention; this book has had none. Snapshot taken 2026-08-23 ~09:00Z (05:00 ET).
Wallet `0x7c0d6ffecf0ee95d0c1c3c351626d89bbf21835b`. **Read-only audit — nothing was placed or
cancelled.** Every action below is a recommendation.

Sources: `pmt positions --json`, `pmt orders --json`, `pmt balance`, `pmt pnl`, `pmt book`,
`pmt scan diligence`, gamma `/markets` + `/events`, CLOB `/prices-history` + `/markets`,
data-api `/activity` (full non-updown history back to 2026-05-27), Binance BTCUSDT, MLB StatsAPI.

---

## 0. Headline

Three things matter, in order:

1. **`nato-x-russia-military-clash-by-august-31-2026` has a LIVE DISPUTED UMA RESOLUTION.**
   `umaResolutionStatus: "disputed"`, `umaResolutionStatuses: ["proposed","disputed"]`. It is the
   only sibling in the event with a proposal on it. This is **$1,117.86 — 34.7% of the manual book
   and 23.0% of the whole account** — sitting on a market whose outcome may be decided by a UMA DVM
   vote rather than by the calendar. It was entered yesterday at 0.9616 and marks at 0.959: the
   position has **negative edge against its own current market price**, and `pmt scan diligence`'s
   own verdict on it is *"sharps are on YES (5.9 vs 4.9) @ 0.04 — the technicality side."*
   Risk/reward is +$44.61 vs −$1,117.86 over 9 days.

2. **A position went 68 days unmonitored while its thesis inverted.**
   `will-mike-mazzei-win-the-2026-oklahoma-governor-republican-primary-election` NO 50 @ 0.98.
   The June 16 primary went to a **runoff**, the market's `endDate` still reads `2026-06-16` but the
   *event* moved to **2026-08-25**, and Mazzei went from ~2c to **78c**. The line is −$38.17 (−78%)
   and **resolves in 2 days**. Nothing in session memory mentions it.

3. **Session memory is materially wrong about the largest position.** Memory records
   "NATO-Russia-clash NO ~92". The real fill was **1,162.47 shares / $1,117.86** across six fills.
   Memory captured the first two fills (82.22 + 10 = 92.22) and never updated. Twelve other live
   manual positions are absent from memory entirely. See §6.

---

## 1. Position table — live manual book

21 live legs, $3,217.59 cost, $3,204.33 mark, **−$13.26 unrealized**.

| # | Market | Side | Size | Avg | Cur | Cost | Mark | Unreal | @res | Resolves |
|---|---|---|---|---:|---:|---:|---:|---:|---:|---|
| 1 | nato-x-russia-military-clash-by-august-31-2026 | No | 1162.47 | 0.9616 | 0.9590 | 1117.86 | 1114.81 | −3.05 | +44.61 | 2026-09-01 |
| 2 | will-bitcoin-dip-to-70k-in-august-2026 | No | 490 | 0.8200 | 0.8050 | 401.80 | 394.45 | −7.35 | +88.20 | 2026-09-01 |
| 3 | israel-x-hamas-ceasefire-cancelled-by-december-31 | No | 301 | 0.8600 | 0.8650 | 258.86 | 260.37 | +1.50 | +42.14 | 2027-01-01 |
| 4 | will-50-74-ships-transit-the-strait-of-hormuz-…-aug-17-23 | No | 265 | 0.9430 | 0.9585 | 249.90 | 254.00 | +4.11 | +15.10 | **2026-08-23** |
| 5 | will-trump-resign-by-december-31-2026 | No | 210 | 0.9500 | 0.9635 | 199.50 | 202.34 | +2.83 | +10.50 | 2026-12-31 |
| 6 | gta-6-launch-postponed-again | No | 214 | 0.9200 | 0.8850 | 196.88 | 189.39 | −7.49 | +17.12 | 2026-11-19 |
| 7 | will-inflation-reach-more-than-10-in-2026 | No | 105 | 0.9523 | 0.9790 | 99.99 | 102.80 | +2.80 | +5.01 | 2026-12-31 |
| 8 | will-bitcoin-replace-sha-256-before-2027 | No | 105 | 0.9513 | 0.9765 | 99.89 | 102.53 | +2.64 | +5.11 | 2026-12-31 |
| 9 | will-bitcoin-dip-to-40000-by-december-31-2026 | No | 129 | 0.7699 | 0.9250 | 99.33 | 119.33 | **+20.00** | +29.67 | 2027-01-01 |
| 10 | will-left-be-said-…-joe-rogan-…-week-of-august-24 | Yes | 100 | 0.9399 | 0.9550 | 94.00 | 95.50 | +1.50 | +6.00 | **2026-08-30** |
| 11 | xi-jinping-out-before-2027 | No | 100 | 0.9300 | 0.9575 | 93.00 | 95.75 | +2.75 | +7.00 | 2026-12-31 |
| 12 | openai-announces-it-has-achieved-agi-before-2027 | No | 100 | 0.9200 | 0.9250 | 92.00 | 92.50 | +0.50 | +8.00 | 2026-12-31 |
| 13 | will-alphabet-be-the-third-largest-company-…-august-31 | Yes | 69 | 0.9699 | 0.9710 | 66.93 | 67.00 | +0.07 | +2.07 | **2026-08-31** |
| 14 | will-mike-mazzei-win-…-oklahoma-governor-rep-primary | No | 50 | 0.9800 | 0.2165 | 49.00 | 10.82 | **−38.17** | +1.00 | **2026-08-25** |
| 15 | will-chinda-kingsley-ogundu-win-…-rivers-state-guber | Yes | 45 | 0.5588 | 0.6850 | 25.15 | 30.82 | +5.67 | +19.85 | 2027-02-06 |
| 16 | will-venezuela-become-51st-state | No | 25 | 0.9760 | 0.9765 | 24.40 | 24.41 | +0.01 | +0.60 | 2026-12-31 |
| 17 | will-russia-invade-another-country-in-2026 | No | 23 | 0.8700 | 0.9450 | 20.01 | 21.73 | +1.73 | +2.99 | 2026-12-31 |
| 18 | sudan-civil-war-ceasefire-by-december-31-2026 | No | 15 | 0.9200 | 0.8950 | 13.80 | 13.43 | −0.38 | +1.20 | 2027-01-01 |
| 19 | lol-hle1-t1-2026-08-23-game2 | T1 | 20 | 0.4900 | 0.5150 | 9.80 | 10.30 | +0.50 | +10.20 | **TODAY, in play** |
| 20 | new-coronavirus-pandemic-in-2026 | Yes | 59.49 | 0.0631 | 0.0440 | 3.76 | 2.62 | −1.14 | +55.73 | 2026-12-31 |
| 21 | will-vladimir-padrino-lpez-be-the-leader-of-venezuela | Yes | 20 | 0.0870 | 0.0020 | 1.74 | 0.04 | −1.70 | +18.26 | 2026-12-31 |
|  | **TOTAL (live)** |  |  |  |  | **3217.59** | **3204.33** | **−13.26** | **+390.37** |  |

`@res` = incremental gain if that leg resolves in our favour. If every live leg won, the book pays
$3,607.96 — **+12.1% on cost**, most of it concentrated in legs that cannot all win (the Chinda,
coronavirus and Padrino tails are where the % is, and they're $30 combined).

### Resolved but not yet redeemed (3 legs)

| Market | Side | Size | Cost | Redeem value | Outcome |
|---|---|---:|---:|---:|---|
| will-bitcoin-reach-80k-on-august-22-2026 | Yes | 120.02 | 2.66 | **$0.00** | resolved No — BTC never touched 80k |
| lol-sen-fly-2026-08-22-total-games-2pt5 | Over | 100 | 43.55 | **$0.00** | resolved Under — SEN swept 2-0 |
| lol-sen-fly-2026-08-22-game2 | FlyQuest | 43 | 16.34 | **$0.00** | resolved Sentinels |
|  |  |  | **62.55** | **$0.00** |  |

CLOB `/markets` confirms `closed: true`, `winner: false` on all three of our tokens.
**Redeeming frees $0.00.** There is no locked capital to recover anywhere in the book — it's
housekeeping only (clears three dead rows out of `pmt positions`).

---

## 2. Resting orders — staleness check

Six live GTC SELLs. **All six are sells of tokens we already hold, so `locked` cash is $0.00** —
they tie up *shares*, not USDC. Every one of them commits 100% of its position's shares, so any
emergency exit on those five markets requires a cancel first.

| ID | Market | Sz | Ask | Book context | Age | Verdict |
|---|---|---:|---:|---|---|---|
| `0x85cb3662` | israel-x-hamas NO | 301 | 0.88 | **at top of book** (0.88 level holds 393sh incl. ours); best bid 0.85, mid 0.865 | 18h | **Sane, but underprices the hold.** Fill = +$6.02 (2.3%). Holding to Dec 31 = +$42.14. The flip captures 14% of the hold value. TTL 2026-10-22 already set — keep it. |
| `0x6c454795` | chinda YES | 25 | 0.79 | best ask 0.69; **$332 of ladder ahead of us**; mid 0.685 | 18h | **Out of reach.** 10c above the touch. Re-ladder. |
| `0x78ce4e0e` | chinda YES | 20 | 0.79 | same | 19h | same |
| `0x29ca71ca` | left-JRE YES | 100 | 0.98 | sits at 0.98 (that level *is* ours); 0.979 above 50sh; best bid 0.931, spread 4.8c, book liq $206 | 18h | **Marginal.** Fill = $98 vs $100 held to Aug 30. Giving up 2c on a near-certain event in a $206-deep book. |
| `0xdf1d7335` | xi-jinping NO | 100 | 0.98 | NO best ask 0.958, NO best bid 0.957; needs YES→0.02 against a 117,863sh bid wall at 0.042 | **96 days** | **Stale, no TTL.** Caps a $100 payout at $98. Only fills when the position is already de-risked. |
| `0x0ce9b089` | coronavirus YES | 59 | 0.085 | **above every displayed ask** (0.049/0.050/0.063/0.064/0.070); best bid 0.039 | **97 days** | **Dead.** Would need the price to more than double. Position marks $2.62. |

No resolution-language drift found in any market carrying a resting order — all six descriptions
read exactly as the thesis assumed (Israel/Hamas requires a definitive cancellation announcement,
not a violation; Xi and coronavirus are plain year-end binaries). The drift risk in this book is
elsewhere: **Mazzei**, where the *market* stayed the same but the *world* moved to a runoff.

---

## 3. Deep dives on the flagged lines

### 3.1 NATO x Russia — disputed UMA proposal (**the audit's top finding**)

```
market   nato-x-russia-military-clash-by-august-31-2026
cid      0xcd6c66b11ed8fdbdc1183fb761cf22959c6b481e843a715a7a9f53e22b90d420
uma      umaResolutionStatus  = "disputed"
         umaResolutionStatuses = ["proposed", "disputed"]
siblings Oct 31 → uma=None    Dec 31 → uma=None    (Mar/Jun/Dec-2025 → resolved NO)
```

Only the August-31 sibling carries a proposal. Price history explains why: on **2026-08-20 the NO
side crashed 0.973 → 0.66 intraday** (YES to 0.34) and recovered to 0.96 by the 21st. `pmt scan
diligence` surfaces the trigger in the comment stream —

> 2026-08-21 Tezcatlipoca: *Politico: Romania's Ministry of National Defense initially said the
> uncrewed vessel was carrying explosives, but later clarified it was a maritime drone.*

That clarification is exactly the hinge in the resolution text. The description **excludes**
interception of one-way attack/loitering munitions aimed at a third party, but **includes**
"shooting down UAVs which are not munitions." Reclassifying the object from munition to drone moves
it from the excluded branch to the qualifying branch. Someone proposed a resolution on that basis
and it was disputed.

**Counter-evidence that it is probably a premature/bad proposal, not a real Yes:** the Oct-31 and
Dec-31 siblings sit at 0.15 and 0.255 YES. If a qualifying clash had actually occurred before Aug 31
those later brackets would be pinned at ~1.00, because they cover the same window plus more. They
aren't. The market does not believe a clash happened.

**But the trade is still wrong-way-round on price:**

- Entered 0.9616 (six fills, 2026-08-21 22:08–22:22Z), marks 0.959 → **we paid above the current mid**.
- Break-even implied prob 96.16%; market says 95.9%. Negative edge at entry, still negative.
- Payoff: +$44.61 (+4.0% over 9 days) vs −$1,117.86.
- Concentration: 34.7% of the manual book, 23.0% of the account.
- pmt's own smart-money read: **YES side scores 5.94, NO side 4.93** — sharps are opposite us.
- Sibling precedent is genuinely good for us (Dec-2025, Mar-31, Jun-30 all resolved NO), and the
  three-month tail is why the base rate is comfortable. That is the case *for* the position.

The problem is not the thesis; the thesis is fine. The problem is 4c of upside, a −96c tail, a live
oracle dispute that can decide it off-calendar, and no price edge to compensate.

**Recommendation: trim to roughly half.** Sell ~550–600 NO into the 0.956/0.958 bid (≈$525–575 back,
~−$3 slippage vs cost) and let the rest ride to Aug 31. That keeps the sibling-precedent edge, cuts
account concentration from 23% to ~12%, and takes the dispute out of the "one bad DVM vote"
category. Cutting the whole thing at 0.956 for ~−$7 is also defensible; adding is not.

### 3.2 Mike Mazzei — the 68-day blind spot

```
position   NO 50 @ 0.9800  →  cur 0.2165   cost $49.00  mark $10.82   −$38.17 (−77.9%)
market     endDate 2026-06-16  (stale — the primary date, not the resolution date)
event      oklahoma-governor-republican-primary-winner  endDate 2026-08-25
field      Mazzei 0.7835   Drummond 0.2250   all others ≤0.0005
```

The description always said "including any potential second round or run-off." The June 16 primary
produced no majority; Mazzei and Drummond advanced; Oklahoma's runoff primary is **Tuesday
2026-08-25**. Price history shows the market already at 0.82 by 2026-07-15, so the inversion
happened at/just after the June primary — this line has been ~−$38 for **over two months** with no
review, and it took a full-book audit to surface it.

Book: NO best bid 0.214 (YES best ask 0.786), $128.94 of depth at the touch. Exiting 50 NO is
trivial and gets ~$10.70.

EV is a wash — 21.65% × $50 = $10.82 held, ~$10.70 sold. **Recommendation: hold the 2 days.**
There is no edge in either direction and selling pays the spread. The real action item is the
process one: the *reason* to know about this today is that it resolves Monday.

### 3.3 BTC — the touch structure settled, and the open BTC book

BTC spot **$76,470**, 24h range 75,546 – 77,548.

The August ladder is internally coherent: ↓75,000 at 0.755, ↓72,500 at 0.41, ↓70,000 at 0.195,
↓67,500 at 0.095, ↓65,000 at 0.0445. Our ↓70,000 NO at 0.805 is priced fairly against its own ladder
— no edge, but a clean +21.9% carry over 8 days if 70k holds.

**Watch it.** BTC held 76k–79k through Aug 22 (both touch legs proved it) and has since broken
below 76k overnight. The market prices a 75.5% chance the 75k level goes. 75k → 70k is another 6.7%.
$401.80 is the second-largest line in the book and it is drifting the wrong way (0.82 → 0.805 in
17 hours).

The ↓40,000 NO (129 @ 0.7699 → 0.925) is the book's best open line at **+$20.00 / +20.1%**, with
$29.67 more to Dec 31. Nothing to do.

### 3.4 Strait of Hormuz — resolves today, well-positioned

Bucket field for the Aug 17–23 week: `<25` 0.325, `25-49` **0.61**, `50-74` 0.0415, `75-99` 0.0065,
`100+` 0.005. Our NO on the 50-74 bucket at 0.9585 is on the right side of a market that expects
under 50 transits. +$15.10 (6.0%) to collect.

**Timing caveat worth calendaring:** resolution keys off IMF Portwatch publication, and the
description allows the market to sit open **up to 14 calendar days** if data is late, plus a
3-day correction window for data-integrity issues. Do not expect cash on Aug 24.

### 3.5 LoL — HLE vs T1, live right now

HLE won Game 1 (game-1 market pinned 0.9995). Series market: **HLE 0.735 / T1 0.265** — T1 must win
Game 2 to force Game 3. Our Game 2 T1 20sh @ 0.49 marks 0.515–0.525. Position is $9.80. Entered
08:27Z today, ~30 min before this snapshot — session memory's guess that it "may have resolved" was
premature. If Game 2 is not completed the market resolves 50-50; postponement backstop is
2026-09-06.

---

## 4. Settled since the last look — realized outcomes

### BTC daily-touch structure (placed 2026-08-22 17:58–18:01Z, settled 04:00Z Aug 23)

**BTC never touched 79,000 or 80,000, and never dipped to 76,000, during the Aug 22 ET day.**
All three legs worked.

| Leg | Entry | Cash out | Cash in | Net |
|---|---|---:|---:|---:|
| ↓76,000 NO 10 @ 0.8641 | 17:58Z | 8.72 | 10.00 (redeem 04:12Z) | **+1.28** |
| ↑79,000 NO 473 @ 0.9500 | 18:01Z | 450.92 | 473.00 (redeem 04:11Z) | **+22.08** |
| ↑80,000 YES 565 @ 0.0221 | 17:59Z | 13.37 | 15.13 (sold 444.98 @ 0.034, 19:21Z); 120.02 left worth $0 | **+1.76** |
| **Structure** |  | **473.01** | **498.13** | **+25.12 (+5.31% in ~10h)** |

The 80k leg is the interesting one: bought as a 2.2c tail, scalped 79% of it at 0.034 within 82
minutes for a small profit, left the stub to expire. Correct handling — the stub is one of the three
$0 redeemables.

**Adjacent discretionary BTC trades the same day (not part of the structure):**
up-or-down 10am ET Down 239 @ 0.8195 → **+40.66**; above-76,800 11am ET NO 291 @ 0.1519 → **−46.81**;
up-or-down 1pm ET Down 5 @ 0.75 → **+1.18**. Net **−4.97**.

### MLB (MLB StatsAPI confirmed)

| Game | Position | Result | Net |
|---|---|---|---:|
| mlb-tor-nyy-2026-08-21 | Yankees 410 @ 0.9045 ($370.85) | won | **+39.15** |
| mlb-tor-nyy-2026-08-22 | Yankees 43 @ ~0.4537 ($19.51) | TOR 4 – NYY 3, **lost** | **−19.51** |
| mlb-nym-cws-2026-08-22 | Mets 139 @ 0.7203 ($100.12) | NYM 10 – CWS 5, **won** | **+38.88** |
| mlb-min-sd-2026-08-22 | Twins 112 @ 0.8950 ($100.23) | SD 7 – MIN 5, **lost** | **−100.23** |
| **Aug 22 slate** |  | 1-2 | **−80.86** |
| **Aug 21+22 combined** |  | 2-2 | **−41.71** |

Session memory listed the Mets and Twins legs but **omitted the Aug-22 Yankees leg entirely**
(a third, losing, ML bet).

### LoL — Sentinels vs FlyQuest, Aug 22

Sentinels swept 2-0. Both legs were bets on the series going long / FlyQuest bouncing back:

| Leg | Cost | Result | Net |
|---|---:|---|---:|
| Total Games Over 2.5, 100 @ 0.4478 | 44.78 | Under | **−44.78** |
| Game 2 FlyQuest, 43 @ 0.3919 | 16.85 | Sentinels | **−16.85** |
| **Total** | **61.63** |  | **−61.63** |

Both still sit unredeemed at $0.

### Other manual closes in the last 36h

| Market | Action | Net |
|---|---|---:|
| hantavirus-pandemic-in-2026 | SELL 1295 NO @ 0.965 → $1,247.49 | position closed |
| anthropic-vs-openai-higher-valuation-on-december-31 | SELL 232 Anthropic @ 0.92 → $212.76 | position closed |
| will-donald-trump-win-the-nobel-peace-prize-in-2026 | SELL 108 NO @ 0.977 → $105.39 | position closed |
| will-april-be-the-best-month-for-bitcoin-in-2026 | BUY 547.76 @ 0.94 → SELL 547.0 @ 0.95 | **+2.60** round trip |
| gta-6-launch-postponed-again | BUY 428 @ 0.92 → SELL 214 @ 0.91 | **−3.02** on the half sold |
| will-left-be-said-… (JRE) | BUY 302 @ 0.94 → SELL 202 @ 0.97 | **+5.82** on the two-thirds sold |

---

## 5. Totals

```
Cash (USDC)                                   $1,649.14   (locked $0.00 — all resting orders are SELLs)
Positions at mark                             $3,204.33
Account total                                 $4,853.47

MANUAL book — capital deployed (cost)         $3,280.14
  of which live legs                          $3,217.59   (66.3% of account)
  of which resolved-but-unredeemed            $62.55      (dead, $0 recoverable)
MANUAL book — mark                            $3,204.33
MANUAL book — unrealized P&L                  -$75.81
  live legs only                              -$13.26
  dead legs already written to zero           -$62.55

FLEET (updown) open at snapshot               $267.94 cost / $0.00 mark   [excluded from this audit]

Redeemable but unredeemed, manual             3 legs, $0.00 recoverable
Redeemable but unredeemed, fleet              3 legs, $0.00 recoverable
LOCKED CAPITAL RECOVERABLE RIGHT NOW          $0.00
```

Concentration: NATO 34.7% of manual / 23.0% of account. Top 3 lines (NATO, BTC-70k, Israel-Hamas)
= 55.6% of manual. Directionally the book is one big bet: **18 of 21 live legs are "the unusual
thing will not happen"** — long NO / long the status quo. That is a coherent style with a fat left
tail; the NATO dispute is exactly what that tail looks like when it twitches.

Account P&L context (`pmt pnl`): 1d −$684.79, 7d −$623.85, all-time −$29.94 mark-to-market. The 1d
number is almost entirely the updown fleet ($2,211 of worthless expiries in 24h), **not** this book.

---

## 6. Session memory vs. reality

| Memory said | Reality | Δ |
|---|---|---|
| NATO-Russia clash NO **~92** | **1,162.47 sh / $1,117.86** (six fills, 22:08–22:22Z Aug 21) | Memory caught the first 2 of 6 fills. Off by 12.6×. Largest position in the book. |
| Israel-Hamas 301 NO @ ~0.86, ask 301 @ 0.88, `0x85cb3662` | Exact match | ✓ |
| Chinda 45 YES @ ~0.5588, asks 45 @ 0.79 | Exact match (two orders, 25 + 20) | ✓ |
| BTC ↑79k NO 473 @ 0.95, ↓76k NO 10 @ 0.864 | Exact match, both redeemed at full value | ✓ |
| BTC ↑80k YES **565** | 565 bought, **444.98 sold at 0.034**, 120.02 stub left | Memory missed the scalp |
| MLB: Mets 139, Twins 112 @ 0.90 | Mets 139 @ 0.7203, Twins 112 @ 0.8950, **plus Yankees 43 @ ~0.4537 (lost)** | Third leg missing |
| LoL T1 game-2 "may have resolved" | Bought **08:27Z today**, still in play, HLE leads 1-0 | Not stale — brand new |
| GTA 6 NO 214, OpenAI-AGI NO 100, Alphabet YES 69, Trump-resign NO 210 | All confirmed | ✓ |
| Coronavirus SELL 59 @ 0.085 | Confirmed, `0x0ce9b089`, **97 days old** | Age not tracked |
| — | **12 live legs absent from memory:** Mazzei NO 50, BTC-70k NO 490, Hormuz NO 265, BTC-40k NO 129, inflation NO 105, SHA-256 NO 105, Xi NO 100, JRE-"Left" YES 100, Venezuela-51st NO 25, Russia-invade NO 23, Padrino YES 20, Sudan NO 15 | $1,326.19 of cost basis unrecorded |
| — | **2 resting orders absent from memory:** Xi `0xdf1d7335` (96d), JRE `0x29ca71ca` | |
| — | **2 LoL legs absent:** SEN/FLY total-games Over 100, game-2 FlyQuest 43 (both lost, −$61.63) | |

The pattern: memory is accurate for whatever was traded in the last few hours of a session and
silently drops everything older. The Mazzei line is what that costs — a −78% position nobody looked
at for 68 days.

---

## 7. Recommendations, ranked by urgency

| # | When | Action | Why |
|---|---|---|---|
| 1 | **Now** | **Trim NATO NO to ~550–600 sh** — sell ~550–600 into the 0.956/0.958 bid | Live disputed UMA proposal on the only sibling that has one; entered above current mid (no edge); 4c upside vs 96c tail; 23% of account; pmt diligence puts sharps on YES |
| 2 | **Today** | Watch `will-50-74-ships-transit-the-strait-of-hormuz…` — resolves today, but Portwatch lag allows the market to stay open up to 14 days | $249.90 with +$15.10 to collect; don't mistake the lag for a stuck market |
| 3 | **Today** | Let LoL T1 Game 2 run — no action | $9.80, in play, HLE leads 1-0. 50-50 resolution if Game 2 doesn't complete |
| 4 | **Today** | **Redeem the 3 dead manual legs** (80k-YES stub, SEN/FLY Over, SEN/FLY game-2) | Frees $0.00 — pure housekeeping, but clears three zero rows from `pmt positions` so the next audit starts clean |
| 5 | **By Mon Aug 25** | **Mazzei NO 50 — hold through the runoff.** Do not add | EV is a wash ($10.82 held vs $10.70 sold); selling just pays the spread. Log the runoff date |
| 6 | **This week** | **Trim or stop-plan BTC-70k NO 490** | $401.80, 2nd-largest line; BTC broke 76k overnight, market gives 75.5% to a 75k touch; drifted 0.82 → 0.805 in 17h. No edge vs the ladder, so size is the only lever |
| 7 | **This week** | **Re-ladder Chinda:** cancel `0x78ce4e0e` (20 @ 0.79) and re-post nearer 0.72–0.73; leave `0x6c454795` (25 @ 0.79) as the stretch | Current asks sit 10c above the touch with $332 of ladder ahead — unreachable. Position is +$5.67; take some of it |
| 8 | **This week** | **Cancel `0x0ce9b089`** (coronavirus 59 @ 0.085) | 97 days old, priced above every displayed ask, will never fill. Either let the $2.62 stub ride to Dec 31 as a free option or re-post at ~0.055 to actually clear it |
| 9 | **This week** | **Decide on `0xdf1d7335`** (Xi NO 100 @ 0.98) | 96 days old, no TTL. It caps a $100 payout at $98 and only fills once the position is already de-risked. Either cancel and hold to Dec 31, or accept it as a free lottery ask — but log the decision |
| 10 | **Before Aug 30** | **Reconsider `0x29ca71ca`** (JRE 100 @ 0.98) | Giving up 2c on a near-certain event in a $206-deep book. Holding to Aug 30 pays $100 |
| 11 | **Optional** | **Raise the Israel-Hamas ask** from 0.88 toward 0.90–0.92 | 0.88 captures 14% of hold value (+$6.02 vs +$42.14). Only 5sh sit at 0.92 and 5 at 0.93 — the ladder above 0.90 is thin. Keep the 2026-10-22 TTL either way |
| 12 | **Process** | **Write the full manual book into session memory** — all 21 live legs and 6 order IDs with ages | 12 legs / $1,326 of cost basis were invisible to memory. Mazzei is what that costs |
| 13 | **Housekeeping** | Padrino YES 20 @ 0.002 ($0.04) is unsellable — below `orderMinSize`, effectively zero | Note it and forget it; it clears at Dec 31 |

---

## 8. Attention calendar

**Resolving within 7 days — $402.70 of cost basis:**

| Date | Market | Cost | Note |
|---|---|---:|---|
| **2026-08-23 (today)** | Hormuz 50-74 ships, week of Aug 17 | 249.90 | Portwatch-dependent; up to 14d publication window |
| **2026-08-23 (today)** | LoL HLE vs T1 Game 2 | 9.80 | In play now; 50-50 if not completed |
| **2026-08-25** | Oklahoma GOP gubernatorial **RUNOFF** → Mazzei NO | 49.00 | Mazzei 78% favourite; expect −$49 |
| **2026-08-30** | JRE "Left" said, week of Aug 24 | 94.00 | Real risk is the "no qualifying episode" branch, not the word |

**Day 8–9 — another $1,586.59, i.e. 61.6% of the manual book resolves inside 10 days:**

| Date | Market | Cost |
|---|---|---:|
| 2026-08-31 | Alphabet 3rd-largest by market cap | 66.93 |
| 2026-08-31 / 09-01 03:59Z | **NATO x Russia clash by Aug 31** | 1,117.86 |
| 2026-09-01 04:00Z | BTC dip to $70k in August | 401.80 |

**TTLs to set (beyond the existing Israel-Hamas 2026-10-22):**

| TTL | Target | Rationale |
|---|---|---|
| 2026-08-25 | Mazzei NO 50 — resolution check | Runoff day; the position's real deadline, not its stale `endDate` of 2026-06-16 |
| 2026-08-30 | Order `0x29ca71ca` (JRE) — cancel if unfilled | Market resolves that day; a live ask past resolution is noise |
| 2026-08-31 | NATO trim decision — final review before resolution | If not trimmed at #1, force the decision before the deadline |
| 2026-09-06 | LoL HLE/T1 postponement backstop | Only fires if Game 2 never completes |
| 2026-09-06 | Hormuz — escalate if still unresolved | 14-day Portwatch window from Aug 23 |
| 2026-09-30 | Order `0xdf1d7335` (Xi, 96d old) — cancel-or-keep review | First TTL this order has ever had |
| 2026-09-30 | Order `0x0ce9b089` (coronavirus, 97d old) — cancel-or-keep review | Same |
| 2026-11-19 | GTA 6 launch date / market resolution | $196.88, currently −$7.49 |
| **Recurring** | **Full manual-book audit, monthly** | The Mazzei line proves that "no news" is not the same as "no change" — the market's own `endDate` field lied for 68 days |
