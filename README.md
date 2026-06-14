# TRIMARC

Traffic data from Kentucky's TRIMARC project in Louisville (https://www.trimarc.org/)

This repo is a [git scraper](https://simonwillison.net/series/git-scraping/): a
GitHub Actions workflow fetches the TRIMARC RSS feed every 15 minutes and records
it into a CSV, so the git history becomes an archive of incident and construction
notices over time — including notices that eventually roll off the live feed.

## Data

- [`data/trimarc.csv`](data/trimarc.csv) — the dataset.
- [`data/trimarcrss.xml`](data/trimarcrss.xml) — the raw feed snapshot (source of truth).

### CSV columns

| column | meaning |
| --- | --- |
| `incident_id` | The leading number in the feed item's title — TRIMARC's stable id for the notice. Used to track a notice across updates. |
| `title` | Item title. |
| `link` | Item link (note: every item uses the same `https://www.trimarc.org`, so it isn't a unique identifier). |
| `description` | Item description / details. |
| `pubDate` | The feed's publish date for the item (RFC 822). Can be months old. |
| `first_seen` | UTC timestamp (`YYYY-MM-DDTHH:MM:SSZ`) of when **this scraper** first captured this version. |

### Update policy: append-only, full history

The CSV is **append-only**. A row is written the first time an incident is seen,
and a *new* row is appended every time any tracked field (`title`, `link`,
`description`, `pubDate`) changes. Old rows are never edited or deleted, so the
file preserves the full edit history of each notice — e.g. a construction notice
that gets an "UPDATE (date) …" revision will appear as multiple rows sharing one
`incident_id`.

Note on `first_seen`: it's capture time, not the feed's `pubDate`. The rows from
the very first scraper run all share that run's timestamp, even for notices the
feed had published long before.

## Running locally

No dependencies — standard library only.

```sh
python3 scrape.py
```

It prints what it did and writes to `data/` only when something changed.

## How it runs on GitHub

[`.github/workflows/scrape.yml`](.github/workflows/scrape.yml) runs `scrape.py`
on a `*/15 * * * *` schedule (and on manual dispatch from the Actions tab), then
commits and pushes `data/` only if it changed. Scheduled runs can be delayed
under GitHub load, and GitHub pauses schedules after 60 days with no repo
activity — neither matters here since the scraper commits whenever the feed
changes.

The workflow grants `contents: write` so the built-in `GITHUB_TOKEN` can push. If
the push step ever fails with a 403, set **Settings → Actions → General →
Workflow permissions** to **Read and write permissions**.
