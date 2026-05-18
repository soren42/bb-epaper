"""bb-epaper: FastAPI service that serves rendered e-paper frames and config UI."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from config import Config, VALID_PAGES
from data import (
    CC_INDICES,
    Mover,
    Quote,
    WATCHLIST,
    fetch_movers,
    fetch_quotes,
)
from renderer import (
    image_to_packed_1bpp,
    image_to_png_bytes,
    render_cc,
    render_watch,
)

app = FastAPI(title="bb-epaper")
CFG = Config.load()


@dataclass
class _Cache:
    watch_quotes: list[Quote] = field(default_factory=list)
    cc_indices: list[Quote] = field(default_factory=list)
    gainers: list[Mover] = field(default_factory=list)
    losers: list[Mover] = field(default_factory=list)
    fetched_at: float = 0.0
    fetch_error: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


CACHE = _Cache()


@dataclass
class _DeviceState:
    last_seen: float = 0.0
    ip: str | None = None
    rssi: int | None = None
    fw: str | None = None
    last_page: str | None = None


DEVICE = _DeviceState()


async def _refresh_cache(force: bool = False) -> None:
    """Refetch all market data if cache is older than data_ttl_seconds. Single-flight."""
    async with CACHE.lock:
        if not force and time.time() - CACHE.fetched_at < CFG.data_ttl_seconds and CACHE.watch_quotes:
            return
        try:
            wq, idx, movers = await asyncio.gather(
                fetch_quotes(WATCHLIST),
                fetch_quotes(CC_INDICES),
                fetch_movers(6),
            )
            CACHE.watch_quotes = wq
            CACHE.cc_indices = idx
            CACHE.gainers, CACHE.losers = movers
            CACHE.fetched_at = time.time()
            CACHE.fetch_error = None
        except Exception as e:
            CACHE.fetch_error = f"{type(e).__name__}: {e}"


def _current_page_index() -> int:
    """Time-driven page rotation. Stable per refresh slot so device gets consistent pages."""
    if not CFG.pages:
        return 0
    slot = int(time.time() // max(CFG.refresh_seconds, 1))
    return slot % len(CFG.pages)


def _render_page(page: str, page_idx: int, total: int, status: str = "") -> Any:
    if page == "watch":
        return render_watch(CACHE.watch_quotes, page_idx, total, status)
    if page == "cc":
        return render_cc(CACHE.cc_indices, CACHE.gainers, CACHE.losers, page_idx, total, status)
    raise HTTPException(400, f"unknown page: {page}")


def _status_string() -> str:
    age = int(time.time() - CACHE.fetched_at) if CACHE.fetched_at else -1
    err = CACHE.fetch_error
    if err:
        return f"ERR {err[:30]}"
    return f"OK | {CFG.refresh_seconds // 60}m"


@app.on_event("startup")
async def _startup() -> None:
    # Warm the cache so first /frame.bin call is fast.
    await _refresh_cache(force=True)


@app.get("/epaper/frame.bin")
async def frame_bin() -> Response:
    """48,000-byte packed 1bpp framebuffer for the current rotation page."""
    await _refresh_cache()
    idx = _current_page_index()
    page_name = CFG.pages[idx]
    img = _render_page(page_name, idx, len(CFG.pages), _status_string())
    raw = image_to_packed_1bpp(img)
    return Response(
        content=raw,
        media_type="application/octet-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Epaper-Page": page_name,
            "X-Epaper-Page-Index": str(idx),
            "X-Epaper-Page-Total": str(len(CFG.pages)),
            "X-Epaper-Next-Refresh": str(CFG.refresh_seconds),
            "X-Epaper-Data-Age": str(int(time.time() - CACHE.fetched_at)) if CACHE.fetched_at else "-1",
        },
    )


@app.get("/epaper/frame.png")
async def frame_png(page: str | None = None) -> Response:
    """PNG preview for the web UI. Pass ?page=watch|cc to force; else current rotation."""
    await _refresh_cache()
    if page is None:
        idx = _current_page_index()
        page = CFG.pages[idx]
    else:
        if page not in VALID_PAGES:
            raise HTTPException(400, f"unknown page: {page}")
        idx = CFG.pages.index(page) if page in CFG.pages else 0
    img = _render_page(page, idx, len(CFG.pages), _status_string())
    return Response(content=image_to_png_bytes(img), media_type="image/png", headers={"Cache-Control": "no-store"})


class ConfigIn(BaseModel):
    refresh_seconds: int = Field(ge=60, le=3600)
    pages: list[str] = Field(min_length=1)


@app.get("/epaper/config")
def get_config() -> dict[str, Any]:
    return {
        "refresh_seconds": CFG.refresh_seconds,
        "pages": CFG.pages,
        "data_ttl_seconds": CFG.data_ttl_seconds,
        "valid_pages": sorted(VALID_PAGES),
    }


@app.post("/epaper/config")
def post_config(c: ConfigIn) -> dict[str, Any]:
    for p in c.pages:
        if p not in VALID_PAGES:
            raise HTTPException(400, f"unknown page: {p}")
    CFG.refresh_seconds = c.refresh_seconds
    CFG.pages = c.pages
    CFG.save()
    return get_config()


class HeartbeatIn(BaseModel):
    fw: str | None = None
    rssi: int | None = None
    page: str | None = None


@app.post("/epaper/heartbeat")
def heartbeat(h: HeartbeatIn, request: Request) -> dict[str, Any]:
    DEVICE.last_seen = time.time()
    DEVICE.ip = request.client.host if request.client else None
    DEVICE.fw = h.fw
    DEVICE.rssi = h.rssi
    DEVICE.last_page = h.page
    return {"refresh_seconds": CFG.refresh_seconds, "now": DEVICE.last_seen}


@app.get("/epaper/status")
def status() -> dict[str, Any]:
    return {
        "config": get_config(),
        "device": {
            "last_seen": DEVICE.last_seen,
            "seen_ago_seconds": int(time.time() - DEVICE.last_seen) if DEVICE.last_seen else None,
            "ip": DEVICE.ip,
            "rssi": DEVICE.rssi,
            "fw": DEVICE.fw,
            "last_page": DEVICE.last_page,
        },
        "data": {
            "fetched_at": CACHE.fetched_at,
            "age_seconds": int(time.time() - CACHE.fetched_at) if CACHE.fetched_at else None,
            "error": CACHE.fetch_error,
            "watch_count": len(CACHE.watch_quotes),
        },
        "current_page_index": _current_page_index(),
        "current_page": CFG.pages[_current_page_index()] if CFG.pages else None,
    }


_INDEX_HTML = """<!doctype html>
<meta charset=utf-8>
<title>bb-epaper</title>
<style>
  :root { color-scheme: dark; }
  body { font: 13px/1.4 ui-monospace, Menlo, Consolas, monospace; background:#0a0a0a; color:#f4d35e; margin:0; padding:24px; }
  h1 { color:#ff8c00; font-size:22px; margin:0 0 4px; letter-spacing:.18em; }
  .sub { color:#888; font-size:11px; letter-spacing:.18em; margin-bottom:24px; }
  .grid { display:grid; grid-template-columns: minmax(0,2fr) minmax(0,1fr); gap:24px; }
  .panel { border:1px solid #333; padding:14px; background:#111; }
  .panel h2 { color:#ff8c00; font-size:11px; letter-spacing:.22em; margin:0 0 10px; }
  .preview img { width:100%; image-rendering:pixelated; border:1px solid #333; background:#fff; }
  .preview .tabs { display:flex; gap:1px; margin-bottom:6px; }
  .preview .tabs button { flex:1; background:#222; color:#f4d35e; border:1px solid #333; padding:6px; cursor:pointer; font:inherit; }
  .preview .tabs button.active { background:#ff8c00; color:#000; }
  label { display:block; color:#aaa; font-size:11px; letter-spacing:.18em; margin:10px 0 4px; }
  input[type=number] { background:#000; color:#f4d35e; border:1px solid #333; padding:6px 8px; font:inherit; width:120px; }
  .row { display:flex; gap:12px; align-items:center; }
  button.primary { background:#ff8c00; color:#000; border:0; padding:8px 16px; font:inherit; font-weight:bold; letter-spacing:.15em; cursor:pointer; }
  button.primary:hover { background:#f4d35e; }
  .kv { display:grid; grid-template-columns: 130px 1fr; gap:6px 12px; font-size:12px; }
  .kv b { color:#aaa; font-weight:normal; letter-spacing:.1em; }
  .ok { color:#22ee22; } .err { color:#ff3b3b; } .muted { color:#666; }
  .pages-pick { display:flex; gap:8px; flex-wrap:wrap; }
  .pages-pick label { display:flex; align-items:center; gap:6px; background:#000; border:1px solid #333; padding:6px 10px; cursor:pointer; margin:0; }
</style>

<h1>BB-EPAPER</h1>
<div class=sub>COMPANION DISPLAY · 800×480 MONO</div>

<div class=grid>
  <div class="panel preview">
    <h2>LIVE PREVIEW</h2>
    <div class=tabs>
      <button data-page="" class=active>CURRENT</button>
      <button data-page="watch">WATCH</button>
      <button data-page="cc">CC</button>
      <button onclick="reload()">⟳ REFRESH</button>
    </div>
    <img id=preview alt="">
  </div>

  <div>
    <div class=panel>
      <h2>CONFIG</h2>
      <label>REFRESH (SECONDS) — DEVICE WILL FETCH THIS OFTEN</label>
      <input type=number id=refresh min=60 max=3600 step=30>
      <label>PAGE ROTATION</label>
      <div class=pages-pick>
        <label><input type=checkbox value=watch> WATCH (22 ASSETS)</label>
        <label><input type=checkbox value=cc> COMMAND CENTER</label>
      </div>
      <div class=row style="margin-top:16px">
        <button class=primary onclick="saveConfig()">SAVE</button>
        <span id=savemsg class=muted></span>
      </div>
    </div>

    <div class=panel style="margin-top:16px">
      <h2>DEVICE STATUS</h2>
      <div class=kv id=status>
        <b>—</b><span>loading…</span>
      </div>
    </div>
  </div>
</div>

<script>
let currentPage = "";

function reload() {
  const img = document.getElementById("preview");
  const q = currentPage ? `?page=${currentPage}` : "";
  img.src = "/epaper/frame.png" + q + "&_=" + Date.now();
}

document.querySelectorAll(".tabs button[data-page]").forEach(b => {
  b.onclick = () => {
    document.querySelectorAll(".tabs button[data-page]").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
    currentPage = b.dataset.page;
    reload();
  };
});

async function loadConfig() {
  const c = await fetch("/epaper/config").then(r => r.json());
  document.getElementById("refresh").value = c.refresh_seconds;
  document.querySelectorAll(".pages-pick input").forEach(i => {
    i.checked = c.pages.includes(i.value);
  });
}

async function saveConfig() {
  const refresh = +document.getElementById("refresh").value;
  const pages = [...document.querySelectorAll(".pages-pick input:checked")].map(i => i.value);
  const msg = document.getElementById("savemsg");
  msg.textContent = "saving…"; msg.className = "muted";
  try {
    const r = await fetch("/epaper/config", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({refresh_seconds: refresh, pages})
    });
    if (!r.ok) throw new Error(await r.text());
    msg.textContent = "saved"; msg.className = "ok";
    setTimeout(reload, 200);
  } catch (e) {
    msg.textContent = "error: " + e.message; msg.className = "err";
  }
}

async function loadStatus() {
  const s = await fetch("/epaper/status").then(r => r.json());
  const d = s.device, dat = s.data;
  const seen = d.seen_ago_seconds == null ? "never" : `${d.seen_ago_seconds}s ago`;
  const seenClass = d.seen_ago_seconds == null ? "muted" : (d.seen_ago_seconds < s.config.refresh_seconds * 1.5 ? "ok" : "err");
  const age = dat.age_seconds == null ? "—" : `${dat.age_seconds}s ago`;
  document.getElementById("status").innerHTML = `
    <b>LAST SEEN</b>     <span class=${seenClass}>${seen}</span>
    <b>DEVICE IP</b>     <span>${d.ip || "—"}</span>
    <b>RSSI</b>          <span>${d.rssi != null ? d.rssi + " dBm" : "—"}</span>
    <b>FIRMWARE</b>      <span>${d.fw || "—"}</span>
    <b>LAST PAGE</b>     <span>${d.last_page || "—"}</span>
    <b>DATA AGE</b>      <span>${age} ${dat.error ? `<span class=err>${dat.error}</span>` : ""}</span>
    <b>CURRENT PAGE</b>  <span>${s.current_page} (${s.current_page_index + 1}/${s.config.pages.length})</span>
  `;
}

loadConfig();
reload();
loadStatus();
setInterval(loadStatus, 5000);
setInterval(reload, 30000);
</script>
"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _INDEX_HTML
