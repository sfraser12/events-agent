# Events Agent

A personal, non-commercial tool that finds live events worth planning
for around Glasgow and central Scotland.

It pulls listings from public event APIs and venue calendars, matches
them against a household preference profile, and produces a weekly
digest plus alerts when tickets for something interesting are about to
go on sale. Written in Python, runs on a schedule, output is email and
calendar entries only.

Built for personal use by one household. Not a commercial service, no
public web front end, no ticket resale.

## Data sources

- Ticketmaster Discovery API
- Skiddle Events API
- Public venue calendar feeds
- TMDB (film release dates)
