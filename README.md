# text_to_gmap

## Goal
Standardize clinic details from messy CSV input by finding the most likely Google Maps listing for each clinic, and surface consistent clinic identifiers across records.

## Inputs (CSV fields)
- `doctorid`: doctor entry id
- `providerid`: provider entry id
- `doctorname`: name of the doctor (low reliability for matching)
- `providername`: clinic name hand-filled
- `openloop_providername`: clinic picked from a dropdown
- `providerstate`: state of clinic
- `providercity`: city of clinic
- `providerpincode`: pincode of clinic
- `provderaddress`: address filled for clinic (note: field name appears misspelled in source)
- `speciality`: speciality of clinic

## Recommended Matching Strategy
### 1) Normalize and choose the best clinic name
Use a "best available name" to query listings:
1. `openloop_providername` (highest trust)
2. `providername`
3. If both are empty, fall back to a combination of `speciality + city` and skip exact-name matching.

Normalize strings (name + address) by:
- Lowercasing
- Removing punctuation
- Collapsing whitespace
- Expanding common abbreviations (e.g., "Hosp" → "Hospital")
- Removing stop-words (e.g., "clinic", "hospital", "center") only for name similarity (not for final display)

### 2) Candidate retrieval (Google Maps)
Form a query using:
- best available clinic name
- `providerpincode`
- `providercity`

Example query:
```
"Cloudnine Hospital" 110045 Dwarka
```
If no results in the target pincode, broaden:
1. Same city + state
2. Adjacent pincodes (if available)

### 3) Candidate scoring (multi-signal)
Compute a weighted confidence score. Suggested signals:
- **Name similarity** (0–1): fuzzy match between normalized clinic name and listing name.
- **Address similarity** (0–1): fuzzy match between `provderaddress` and listing address.
- **Pincode match** (0 or 1): exact pincode match.
- **City match** (0 or 1): city match after normalization.

Recommended weights (tune as needed):
- Name similarity: 0.45
- Address similarity: 0.35
- Pincode match: 0.15
- City match: 0.05

Score = (0.45 * name_sim) + (0.35 * addr_sim) + (0.15 * pincode_match) + (0.05 * city_match)

### 4) Decision rules
- **Single confident match**: if best candidate score ≥ 0.80 and the next best is at least 0.10 lower, accept it.
- **Multiple possible matches**: if multiple candidates score ≥ 0.70, return all with confidence scores.
- **No match**: if best score < 0.70, stamp “no results found”.

### 5) Output schema per row
For each input row, output:
- `matched_status`: `single_match` | `multi_match` | `no_match`
- `matched_confidence`: score of top candidate (0–1)
- `google_maps_url`: URL of accepted match (if single)
- `google_maps_candidates`: list of URLs + scores (if multi)
- `match_notes`: short reasoning (e.g., "pincode mismatch but address similarity high")

## Handling the Given Example
Input:
- Name: Cloudnine Hospital
- Pincode: 110045
- Address: (user-provided)

Process:
1. Query for “Cloudnine Hospital 110045 Dwarka”.
2. Score all listings in that pincode.
3. If a listing is in 110045 and address similarity is high, return its unique listing URL.
4. If multiple plausible hits exist, return all with scores.
5. If no listing in 110045, return `no results found`.

## Notes
- `doctorname` should not be used as a primary signal; it’s too noisy for clinic matching.
- Consider deduplicating across rows by clustering on accepted `google_maps_url` to identify common clinics.
- Keep raw input for auditability and re-scoring.

## Playwright Automation (Reference Implementation)
A basic Playwright-based matcher is included in `scripts/gmap_matcher.py`. It:
- Reads a CSV file with the fields described above.
- Searches Google Maps per row using the best available clinic name + pincode/city.
- Scores candidates using the multi-signal formula and emits JSONL results.

### Setup
```
pip install -r requirements.txt
playwright install chromium
```

### Run
```
python scripts/gmap_matcher.py --input data/clinics.csv --output output/matches.jsonl --headless
```

### Output
Each output line is a JSON object containing the original row plus:
- `matched_status`
- `matched_confidence`
- `google_maps_url`
- `google_maps_candidates`
- `match_notes`
