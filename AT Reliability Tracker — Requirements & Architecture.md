# AT Reliability Tracker — Requirements & Architecture

Oct 13, 2026 · @dev

## The project in one paragraph

The tracker answers one question every day: **did Auckland's buses, trains and ferries run on time, and where and when did they not?** It collects AT's live feed every 30 seconds, matches each live update to the published timetable, and turns the result into daily punctuality scores by route and by time of day. Those scores are kept forever so trends (this month vs last month, before vs after a timetable change) can be compared.

**Two data sources:**

- **Timetable (GTFS static)** — the planned schedule. Downloaded as a zip of text files from [AT's GTFS page](https://at.govt.nz/about-us/at-data-sources/general-transit-feed-specification). Changes every few weeks.
- **Live feed (GTFS-realtime)** — where vehicles are and how late they are, right now. From the [AT Realtime API](https://dev-portal.at.govt.nz/realtime-api), free key required, refreshed at least every 30 seconds.

## What's in the data

The timetable in your `gtfs` folder covers **5 Oct 2026 – 7 Feb 2027**: 17 operators, 218 routes (197 bus, 17 ferry, 4 train), about 7,000 stops and 39,000 trips. The biggest file, `stop_times.txt`, has 1.2 million rows (100 MB) — one row per trip per stop.

**Timetable files that matter for punctuality**

| File | What it holds | Rows | Why we need it |
| --- | --- | --- | --- |
| `stop_times.txt` | Planned arrival/departure at every stop of every trip | 1,224,088 | The "should have arrived at" time we compare against |
| `trips.txt` | Each trip, its route, direction and service pattern | 39,289 | Links a live update to its route |
| `routes.txt` | Route number, operator, type (bus/train/ferry) | 218 | The main grouping for reports |
| `stops.txt` | Stop names and coordinates | 7,007 | Stop-level views and maps |
| `calendar.txt` / `calendar_dates.txt` | Which days each service pattern runs, plus holiday exceptions | 146 / 1,091 | Tells us which trips were *meant* to run on a given day |
| `agency.txt` | Operators (e.g. NZ Bus, Go Bus, Fullers360) | 17 | Reporting by operator |
| `shapes.txt` | The path each route draws on a map | 589,496 | Optional — maps only |

Fares, frequencies and transfers files are empty or near-empty, so we can ignore them.

**Live feed (from the AT Realtime API)**

| Feed | What it says | Use in this project |
| --- | --- | --- |
| Trip updates | For a running trip: how many seconds early/late at upcoming stops | Main source of delay |
| Vehicle positions | GPS location, trip and timestamp of each vehicle | Backup check; detecting trips that never ran |
| Service alerts | Cancellations, detours, disruptions | Explaining bad days |
| Combined feed | All three in one response | Simplest to poll |

The API returns JSON by default, or compact Protobuf if asked. It is refreshed at least every 30 seconds ([AT Realtime API](https://dev-portal.at.govt.nz/realtime-api)).

**Confirmed in the dev portal:** the realtime feed is the \*\*Realtime Compat\*\* API (not the GTFS V3 API). Combined feed: \`https://api.at.govt.nz/realtime/legacy/\` (also \`/tripupdates\`, \`/vehiclelocations\`, \`/servicealerts\`). Key goes in the \`Ocp-Apim-Subscription-Key\` header. The Public Transport Dev plan allows 600 calls/minute and 35,000 calls/week, so poll the combined feed once every 30 s (20,160 calls/week), not three feeds separately.

## Functional requirements — what the system must do

Eight "Must" items make a working tracker; the rest add polish. Change any priority with the dropdown.

| ID | The system must… | Priority |
| --- | --- | --- |
| FR-1 | Download the latest GTFS timetable zip once a day and keep each version (so old dates are scored against the timetable that was live then) | Must |
| FR-2 | Call the AT Realtime API every 30 seconds, 24/7, and save every response untouched (the "raw" copy) | Must |
| FR-3 | Skip duplicate updates (the same vehicle and timestamp seen twice) | Must |
| FR-4 | Match each live update to its planned trip and stop using `trip_id` and `stop_id`, and work out the delay in seconds | Must |
| FR-5 | Use AT's own time zone (Pacific/Auckland) and handle trips past midnight (GTFS times like 25:10:00) and daylight-saving changes | Must |
| FR-6 | Label each stop arrival as early (more than 1 min early), on time (1 min early to 5 min late) or late (more than 5 min late) — thresholds kept in one config file | Must |
| FR-7 | Each night, calculate for yesterday: % on time, average delay and worst delays — by route, operator, mode, and hour of day / time band (AM peak, midday, PM peak, evening, night) | Must |
| FR-8 | Add each day's results to a history table that is never overwritten, only appended or corrected | Must |
| FR-9 | Count planned trips that never appeared in the live feed ("missing" or likely cancelled) | Should |
| FR-10 | Re-run any past day on demand (a "backfill") from the saved raw data, giving the same answer every time | Should |
| FR-11 | Show results in a simple dashboard: worst 10 routes this week, trend lines, a heat map of route × hour | Should |
| FR-12 | Attach service alerts to the day they happened, to explain bad days | Could |
| FR-13 | Send a message (email or Slack) if the collector stops receiving data for more than 10 minutes | Could |

The 1-min-early / 5-min-late window is a starting assumption to agree with stakeholders, not an AT-published standard.

## Non-functional requirements — how well it must do it

The hard parts are running non-stop and keeping storage cheap; the targets below are starting points for a solo portfolio project.

| ID | Quality | Target (plain words) |
| --- | --- | --- |
| NFR-1 | **Always on** | Collector captures at least 99% of the 2,880 polls a day (one every 30 s) |
| NFR-2 | **Recovers by itself** | If a call fails or times out, retry a few times, then carry on; a crash restarts automatically |
| NFR-3 | **Fresh enough** | Yesterday's scores ready by 6 am each day |
| NFR-4 | **Repeatable** | Re-running a day gives the same numbers (same raw data + same timetable version = same result) |
| NFR-5 | **Cheap** | Runs on free tiers or a few dollars a month; raw data compressed and stored by date (one folder per day) |
| NFR-6 | **Scales over time** | Storage design still works after a year of data; queries only read the days they need |
| NFR-7 | **Correct** | Daily data-quality checks pass (see Undercurrents) before results are published |
| NFR-8 | **Respects AT's API** | Stays inside the free plan's quota and AT's [terms of use](https://dev-portal.at.govt.nz/api-terms-of-use) |
| NFR-9 | **Secure** | API key never in code or on GitHub |
| NFR-10 | **Easy to understand** | README, a diagram, and one command to run locally — a recruiter can follow it in 5 minutes |

Storage per day is unknown until the collector runs; measure it in week 1 and use that to size the retention rule (e.g. raw files to cheaper storage after 90 days).

## Stakeholders

Upstream people *feed* the pipeline; downstream people *use* what comes out. As a data engineer, you sit in the middle and owe both sides a clear contract.

**Upstream — where the data comes from**

| Who | What they provide | What you need from them |
| --- | --- | --- |
| Auckland Transport (data team) | GTFS timetable and the Realtime API | Stable formats, notice of changes, a working API key |
| Bus, train and ferry operators (NZ Bus, Go Bus, Fullers360, etc.) | GPS units and trip-tracking on vehicles that create the live data | Vehicles logged in to the right trip (otherwise updates can't be matched) |
| AT developer portal / API platform | Keys, quotas, uptime | To know the quota and any outages |

**Downstream — who uses the results**

| Who | What they want to know | How they'd use it |
| --- | --- | --- |
| Commuters and the public | "Is my route reliable at 8 am?" | Pick a route or leave earlier |
| AT planners and network managers | Which routes and time bands are worst, and trending which way | Timetable fixes, bus lanes, extra vehicles |
| Operator contract managers | Each operator's punctuality over time | Performance reviews of operators |
| Journalists, councillors, advocacy groups | Has reliability got better or worse? | Public reporting and accountability |
| Data analysts / scientists (including you) | Clean, joined historical data | Delay prediction models, research |
| Recruiters / hiring managers | Can this person build a real, running pipeline? | Your portfolio |

For a portfolio, the realistic downstream user is a planner or analyst; design the outputs around their questions.

## Architecture — the five stages

Data flows top to bottom: born at AT, pulled in every 30 seconds, stored raw, cleaned and scored nightly, then served to people.

&#91;embedded content: pipeline architecture · 5 stages + undercurrents\]

Two design choices make this robust. **Keep the raw copy forever** (cheaply), so any day can be recalculated if the logic changes. **Split storage by date**, so a nightly job reads only one day instead of the whole history. Start everything on your laptop with DuckDB, then move the collector to an always-on cloud box once it works.

## Undercurrents — the things that run underneath every stage

These aren't a stage of their own; they apply all the way through. Each one, in plain words, for this project:

| Undercurrent | What it means here | Simple first version |
| --- | --- | --- |
| **Security** | Keep the API key secret; only you can write to storage; dashboard is read-only | Key in a `.env` file or cloud secret store, `.env` in `.gitignore`; least-access storage permissions |
| **Data management** | Know what each table means, where it came from, and how long it's kept | A short data dictionary; tag every row with the timetable version and the date it was collected; retention rule for raw files |
| **Data quality** | Catch bad data before it reaches a report | Daily checks: poll count close to 2,880, % of updates matched to a trip, no negative or absurd delays (e.g. over 2 hours), every route present |
| **DataOps / monitoring** | Know quickly when something breaks | Log each poll's status; alert if no data for 10 minutes; a "pipeline health" panel on the dashboard |
| **Orchestration** | Run steps in the right order at the right time | Collector runs continuously; nightly job runs: timetable refresh → transform → checks → publish (start with cron, move to Airflow, Dagster or Prefect) |
| **DevOps** | Ship changes safely and restart automatically | Code on GitHub; Docker container; GitHub Actions runs tests on each push; auto-restart on crash |
| **Software engineering** | Code others can read and trust | Small modules (collect / load / transform), unit tests for delay maths and the past-midnight case, SQL models in dbt |
| **Cost** | Don't get a surprise bill | Compress raw files, partition by date, budget alert on the cloud account |

If you only do three of these well, make them: secret key handling, the data-quality checks, and the no-data alert.

## Next steps — build order

Build the collector first: every day it isn't running is history you can never get back.

- [ ] Sign up on the AT dev portal, get the key, and note the endpoint URLs, header name and quota
- [ ] Write a small script that polls the combined feed every 30 s and saves raw files by date
- [ ] Deploy it somewhere always-on (small cloud VM or container) with auto-restart and the no-data alert
- [ ] Load the GTFS timetable into a database (DuckDB or Postgres to start) with a version tag
- [ ] Write the join + delay calculation for one day, test the past-midnight case
- [ ] Build the nightly daily-metrics job and the history table
- [ ] Add data-quality checks
- [ ] Build the dashboard (worst routes, trends, route × hour heat map)
- [ ] Add backfill, README and the architecture diagram to GitHub

## Sources

- [AT — General Transit Feed Specification](https://at.govt.nz/about-us/at-data-sources/general-transit-feed-specification)
- [AT Realtime API](https://dev-portal.at.govt.nz/realtime-api)
- [AT API terms of use](https://dev-portal.at.govt.nz/api-terms-of-use)
- GTFS files in your `gtfs` folder (feed valid 5 Oct 2026 – 7 Feb 2027)
