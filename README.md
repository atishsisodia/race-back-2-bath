# Race Back 2 Bath – spectator site

A public live page for spectators that sits alongside your Traccar server:

- **Map** with every pair's position and the route they've taken so far
- **Live standings**: finished pairs ranked by time, then everyone else ranked by miles to campus (straight line)
- **Results**: start time, finish time and total time per pair
- A warning next to any pair that hasn't sent an update for a while
- An optional **privacy delay**, so spectators see positions 10 minutes late while organisers see live positions in Traccar

Spectators only ever talk to this site. It talks to Traccar with a read-only account
and serves one cached feed, so your organiser login stays private and Traccar gets the
same load whether 5 or 500 people are watching.

Only Python 3.9+ is needed (already on almost every Linux server). There are no packages to install.

---

## 1. Set up Traccar for the race

Menu names can differ slightly between Traccar versions.

1. **Group:** Settings → Groups → add a group called `Race`. Put every pair's device in it
   (edit each device and set its Group). Name each device after the pair, e.g. "Team Pulteney",
   because that name is what spectators see.
2. **Geofences:** draw two geofences on the map (Settings → Geofences):
   - `Finish`: a small circle around your finish desk on campus, about 100–250 m.
     Keep it small so a pair isn't "finished" while still walking across campus.
   - `Start`: a circle of about 300 m around the drop-off point.
3. **Link the geofences to the group** (Settings → Groups → Race → Connections → Geofences).
   ⚠️ Traccar only creates enter/exit events for geofences linked to a device or its group.
   If you skip this, no start or finish times will appear.
4. **Read-only feed user:** Settings → Users → add a user such as `spectator-feed@yourdomain`.
   Tick **Readonly** but leave **Disable reports** unticked, because the site uses reports to read routes and events. Link it only to the `Race` group and the two geofences
   (Users → that user → Connections). This keeps test phones and organiser devices off the public map.

## 2. Install the site

```bash
sudo mkdir -p /opt/race-back-2-bath
sudo cp -r server.py static /opt/race-back-2-bath/
sudo cp config.example.env /opt/race-back-2-bath/config.env
sudo nano /opt/race-back-2-bath/config.env      # fill in Traccar login, start time, etc.

sudo cp race-tracker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now race-tracker
sudo journalctl -u race-tracker -f               # watch for errors
curl localhost:8090/healthz                      # "ok" means it can reach Traccar
```

Then put it on the web over HTTPS. `nginx-example.conf` shows a setup with
`race.yourdomain` for spectators and `track.yourdomain` for the Traccar dashboard,
with a free certificate from `certbot --nginx`.

## 3. How times are worked out

| | Comes from | Fallback |
|---|---|---|
| **Start** | First time the pair leaves the `Start` geofence | `RACE_START` from config |
| **Finish** | First time the pair enters the `Finish` geofence after starting | none (pair keeps racing) |
| **Time** | Finish minus start | |

Phones report about once a minute, so GPS times can be up to a minute late.
**Record official finish times at the finish desk too.** If they differ, use
the overrides file below. The page tells spectators that official results are confirmed by organisers.

## 4. Manual corrections (overrides.json)

Create `/opt/race-back-2-bath/overrides.json` to correct anything on the day.
It's re-read on every update, so there's no need to restart. Keys are the device **name** or **identifier**:

```json
{
  "Team Pulteney": { "finish": "2026-11-28T11:42:10+00:00", "note": "Official time from finish desk" },
  "Two Dons":      { "status": "withdrawn", "note": "Picked up safely at Chippenham" },
  "rb2b-07":       { "start": "2026-11-28T09:12:00+00:00" }
}
```

`status` can be `withdrawn` or `dnf`. Times need a timezone (`+00:00` in GMT, `+01:00` in BST).
Make sure the file stays valid JSON. If it can't be read, the site ignores it and logs an error.

## 5. Try it without phones

`tools/mock_traccar.py` pretends to be a Traccar server with six pairs racing from
Marlborough to Bath. It's useful for checking the page or showing it off before the event:

```bash
python3 tools/mock_traccar.py &
TRACCAR_URL=http://127.0.0.1:8082 TRACCAR_EMAIL=x TRACCAR_PASSWORD=x python3 server.py
# open http://127.0.0.1:8090
```

## Settings reference

| Setting | Default | Meaning |
|---|---|---|
| `DELAY_MINUTES` | 0 | Spectators see positions, starts and finishes this many minutes late |
| `POLL_SECONDS` | 15 | How often the site asks Traccar for new data |
| `STALE_MINUTES` | 10 | Show a ⚠ warning after this long without an update |
| `TRAIL_MIN_METRES` | 150 | Route points closer than this are thinned out to keep the page light |
| `FINISH_LAT/LNG` | Uni of Bath | Used for "miles to go" if there's no Finish geofence |

For safety monitoring, organisers should use **Traccar itself** (live and undelayed, with
offline alerts and battery levels). This site is for spectators.
