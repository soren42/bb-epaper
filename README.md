# bb-epaper — moved

This project now lives in **[Solarian-Terminal](https://github.com/soren42/Solarian-Terminal)**,
alongside the Solarian dashboard, the feed service and the Robinhood bridge.

| What was here | Where it is now |
| --- | --- |
| `server.py`, `renderer.py`, `data.py`, `config.py`, `CONTROLS.html` | `services/epaper/` |
| `firmware/bb-epaper/` | `firmware/bb-epaper/` |
| `bb-epaper.service` | `deploy/bb-epaper.service` |

The watchlist this service reads is `shared/watchlist.json` in that repo; it is
no longer referenced by absolute path across repositories.

This repository is archived. Its history was preserved in the move, so
`git log services/epaper/` there reaches back to these commits.
