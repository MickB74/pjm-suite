# Playbook: Find a Renewables Project's Offtakers and PPA Terms

For any US utility-scale wind or solar project, work out (1) who owns it, (2) who buys the power, (3) how big each PPA is, and (4) what's actually knowable about price. Free public sources only.

Worked example: *Mockingbird Solar Center*, 471 MW, Lamar County TX, owned by Ørsted.

---

## Why the obvious path (FERC EQR) usually fails

Every wholesale power seller with FERC market-based-rate authority files **[FERC Electric Quarterly Reports (EQR)](https://eqrreportviewer.ferc.gov/)** — every transaction with counterparty, MWh, and $/MWh. In principle this is the answer.

In practice, three failure modes:

1. **Corporate vPPAs are not FERC-jurisdictional.** A virtual PPA is a financial swap between the project LLC and a corporate buyer (Bloomberg, Google, UnitedHealth, etc.). The corporate buyer doesn't touch physical power and doesn't file EQR. The physical energy flows into the ISO market at LMP, booked by whichever affiliate the developer uses.
2. **Developer trading affiliates absorb physical volume.** Ørsted's US onshore solar all books through *Orsted US Trading LLC*, aggregating dozens of projects into one filer. Same for many other developers. You lose per-project resolution.
3. **Confidential contracts get redacted.** Even for direct PPAs, big corporate deals often file with `$0.00` and a footnote.

Result: EQR is useful for **bilateral hub prices** and **developers who file per-project** (NextEra is the counter-example — 12 separate filers). For most modern corporate-PPA-funded projects, EQR won't reveal price.

**But it's still the first stop** — because a negative answer there is diagnostic. It tells you the project uses a trading affiliate or vPPA structure, which narrows what the other sources should tell you.

---

## The playbook

### Step 1 — Confirm the project exists in EIA (5 min)

Get plant_id, capacity, entity name, operating status, monthly generation history.

```bash
# EIA v2 API: search plant names
curl -s "https://api.eia.gov/v2/electricity/facility-fuel/facet/plantCode/?api_key=$EIA_KEY" \
  | jq '.response.facets[] | select(.description | test("mockingbird"; "i"))'
```

Then pull monthly generation:

```bash
curl -s "https://api.eia.gov/v2/electricity/facility-fuel/data/?api_key=$EIA_KEY&frequency=monthly&data[0]=generation&facets[plantCode][]=64347&sort[0][column]=period&sort[0][direction]=desc&length=24"
```

Also pull nameplate MW and the *entity name* (this is the LLC to search for in EQR):

```bash
curl -s "https://api.eia.gov/v2/electricity/operating-generator-capacity/data/?api_key=$EIA_KEY&frequency=monthly&data[0]=nameplate-capacity-mw&data[1]=net-summer-capacity-mw&facets[plantid][]=64347&length=5"
```

**Deliverables from Step 1:**
- Plant EIA ID
- Entity name (`Mockingbird Solar Center, LLC`)
- Nameplate MW (471), Summer MW
- Operating date (~April 2024)
- 24-mo generation history (implied capacity factor)
- State + BA

### Step 2 — Check FERC EQR by the LLC name (5 min)

Open [`eqrreportviewer.ferc.gov`](https://eqrreportviewer.ferc.gov/) → Reports → Summary Reports. The **Seller** listbox contains every entity that filed an EQR in the selected quarter (~3,900 filers per recent quarter). Search programmatically via browser JS:

```javascript
const s = document.getElementById(
  'TabContainerReportViewer_TabPanelReporting_TabContainerReports_TabPanelSummaryReports_lbxSellerSum');
const opts = [...s.options].map(o => o.text);
opts.filter(t => /mockingbird/i.test(t));   // exact project LLC
opts.filter(t => /orsted/i.test(t));        // parent
```

Three possible outcomes:

- **Hit on the project LLC** → you can pull that filer's contracts + transactions via Downloads → Selective Filings (email delivery). Best case.
- **Hit only on the parent/trading affiliate** → volumes are aggregated; you can see book-level pricing at ERCOT hubs but can't isolate this project. This is what happened for Mockingbird (only `Orsted US Trading LLC` hits).
- **No hits at all** → project is pre-COD, or all sales go through unmapped affiliates. Rare for operating assets.

### Step 3 — Pivot to developer press releases (10 min)

Search for offtake announcements. The developer names the buyer in every corporate PPA press release (that's the point of them — corporate ESG marketing).

```
"Mockingbird Solar" PPA offtaker
"Mockingbird Solar" Ørsted power purchase
"Mockingbird Solar" fourth OR quartet corporate
```

For each hit, extract: **buyer name, MW, tenor, signed date, product type (PPA vs vPPA)**. Add each to a running roster.

**Key sanity check:** the sum of all MW should approach the nameplate. If it's under, keep searching for the remaining tranche(s). Mockingbird's math:

| Offtaker | MW | Tenor | Signed |
|---|---|---|---|
| UnitedHealth Group | 250 | 15 yr | 2024 |
| Covestro | 90 | 15 yr | 2023 |
| Bloomberg | 80 | 15 yr | Jan 2024 |
| Royal DSM (now DSM-Firmenich) | ~50 | 10 yr | 2021 |
| **Sum** | **~470** | | |

Sum ≈ nameplate → **fully contracted**. If the sum ran short you'd know to keep hunting.

The last-signed tranche's press release usually says "brings the project to fully-contracted status" — a strong signal you have them all. Mockingbird's Bloomberg release said exactly that.

Sourcing quality varies:

- **PRNewswire / developer site**: authoritative but often walled behind bot protection (use browser or WebFetch)
- **Trade press** (pv-magazine, RenewablesNow, NS Energy, Solar Industry, Hart Energy): reliable and usually free
- **Corporate buyer's own newsroom**: authoritative on their side of the deal (UnitedHealth's own release named MW)
- **PPA broker announcements** (Edison Energy, Schneider Electric, 3Degrees): reveal MW when they broker the deal (Edison Energy named Covestro's 90 MW)
- **LinkedIn / analyst blogs**: unreliable, but sometimes surface deals the majors haven't reported

### Step 4 — What's still hidden

**Price is almost never disclosed.** Corporate vPPA press releases give MW, tenor, and ESG framing; the strike stays confidential. To estimate:

- Industry ballpark ranges for the tech + region + vintage. ERCOT solar vPPAs 2021–2024 clustered around **$30–45/MWh** with 2024-signed deals near the upper end.
- Paid databases (S&P Global, LevelTen, Enverus, BloombergNEF) have PPA indices by region, tech, and quarter — sometimes with named deals.
- The offtaker's own SEC disclosures occasionally quantify the total commitment (10-K risk factors, sustainability reports) — divide by expected MWh over the term to back-calculate implied strike.

**The physical settlement point** (hub or resource node) is not always public either. Reasonable defaults for ERCOT: North Hub for most West-TX / North-TX solar; the resource node itself if the developer signs a physical PPA.

### Step 5 — Cross-check with SCED / operational data (optional, ERCOT only)

If the developer's ISO is ERCOT and you want to verify the offtake volumes make sense given actual generation, ERCOT publishes 5-min telemetered net output by resource with a ~60-day lag (`60-Day SCED Disclosure Reports`). Sum monthly, compare to EIA-923 to sanity-check the plant is generating what you think it is. This is what the `ercot-suite` reconcile module does.

PJM: `hrly_dam_awd_gen` from Data Miner 2 gives day-ahead cleared awards by unit, similar shape but PJM-specific.

---

## What to hand back to the requester

A single roster table:

| Field | Where it came from | Confidence |
|---|---|---|
| Plant ID / capacity / status | EIA API | High — official |
| Operating date / gen history | EIA-923 monthly | High — filed data |
| Owner LLC | EIA-860 `entityName` | High |
| Ultimate parent | Developer website / press release | High for majors |
| EQR filer name | FERC EQR seller list | High |
| Offtaker list (name, MW, tenor) | Developer + broker + buyer press releases | High if quartet sums to nameplate |
| PPA strike $/MWh | **Not public** | — |
| Physical settlement point | Sometimes disclosed, else infer | Medium |
| Aggregate ERCOT hub bilateral prices | FERC EQR Orsted US Trading LLC filings | Medium — not project-specific |

Explicitly flag what is **not** knowable (price, exact hedge structure, hourly shape assumptions). Estimate the strike from public comps if the requester needs a number, but tag it as an estimate.

---

## Reusable checklist

```
[ ] EIA plant_id, MW, entity name, operating date, monthly gen
[ ] Search EQR seller listbox for LLC + parent + known aliases
[ ] If no direct EQR hit → note that price is not knowable from public data
[ ] Search "<project name> PPA" + "<project name> offtaker" + variants
[ ] Extract each PPA: buyer, MW, tenor, signed date, PPA/vPPA
[ ] Sum MW; if < nameplate, keep searching for remaining tranches
[ ] Look for "fully contracted" phrasing in last signed release
[ ] Note broker if named (Edison, Schneider, LevelTen, 3Degrees) — they often disclose MW
[ ] Cross-check with buyer's own ESG report / SEC filings
[ ] Optionally: pull ERCOT SCED or PJM DAM-awarded gen for volume sanity check
[ ] Write up: known / not knowable / estimates tagged as such
```

---

## Known developer patterns

Rough rules for whom to expect to file per-project in EQR versus route through an affiliate. Useful upfront so you know where the search will end.

- **Per-project EQR filers** (each asset is its own LLC that files): NextEra, Invenergy, EDF Renewables, Enel Green Power, most utility-owned IPPs.
- **Trading-affiliate aggregators** (one filer books many projects): Ørsted (via `Orsted US Trading LLC`), Shell, BP, ENGIE, some Berkshire Hathaway Energy subs.
- **Mixed** (offshore filed per-project, onshore rolled up): Ørsted offshore (Sunrise Wind, Revolution Wind, South Fork Wind, Block Island each file separately) but onshore does not.

If the developer is in the second bucket, skip straight to Step 3 — EQR will only ever return the parent trading LLC.
