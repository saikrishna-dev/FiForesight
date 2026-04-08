# backend/main.py
import asyncio
import logging
import traceback
from datetime import datetime, timezone
from typing import List

import uvicorn
from fastapi import FastAPI, HTTPException, Body, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from config import Config, SanitizeHttpxFilter
from models import (
    calculate_rsi, calculate_rsi_series, run_ensemble_forecast,
    calculate_macd, calculate_bollinger_bands, calculate_sma_series,
    calculate_support_resistance,
)
from services import (
    DataCleaner, AnalystJuryService, InfluxService,
    ANALYST_PERSONAS, NOTE_PROMPT_SUFFIX,
    SerpService, YFinanceService,
)

# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Sanitize sensitive parameters in logs
logging.getLogger("httpx").addFilter(SanitizeHttpxFilter())

app = FastAPI(title="FiForesight Quantum Engine")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    tb = traceback.format_exc()
    logger.error(f"Unhandled exception on {request.method} {request.url}:\n{tb}")
    return JSONResponse(
        status_code=500,
        content={"detail": "An internal server error occurred."},
    )


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------
influx_svc       = InfluxService()
serp_svc         = SerpService()
yf_svc           = YFinanceService()
analyst_jury_svc = AnalystJuryService()


# ---------------------------------------------------------------------------
# Response schema
# ---------------------------------------------------------------------------

class PredictionResponse(BaseModel):
    symbol:       str
    currentPrice: str
    rsi:          str
    prediction:   dict
    analystNote:  str
    confidence:   str
    history:      List[dict]
    forecastDays: List[dict]
    modelStats:   dict
    metrics:      dict
    news:         List[dict]
    trending:     List[dict]
    indicators:   dict
    lastUpdated:  str
    juryAnalysts: List[dict]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_market_cap(cap) -> str:
    try:
        v = float(cap)
        if v >= 1e12:
            return f"${v/1e12:.2f}T"
        if v >= 1e9:
            return f"${v/1e9:.2f}B"
        if v >= 1e6:
            return f"${v/1e6:.2f}M"
        return f"${v:,.0f}"
    except Exception:
        return str(cap) if cap else "N/A"


def _fmt_pct(val) -> str:
    try:
        return f"{float(val)*100:.2f}%"
    except Exception:
        return "N/A"


async def _ai_note(symbol: str, closes: List[float], rsi: float, forecast: dict) -> str:
    """
    Header analyst note via Groq llama-3.3-70b (14,400 RPD free, no quota issues).
    Replaces Gemini which has a 20 RPD free-tier limit — too scarce for per-request use.
    Falls back to the ensemble model's own note if Groq is unavailable.
    """
    recent    = closes[-20:]
    price_str = ", ".join(f"{p:.2f}" for p in recent)
    system = (
        "You are a concise quantitative financial analyst. "
        "Respond in 2 plain-text sentences only — no markdown, no bullet points, no JSON."
    )
    prompt = (
        f"Symbol: {symbol} | RSI: {rsi:.1f}\n"
        f"Recent 20-day closes: {price_str}\n"
        f"5-day ensemble forecast: high=${forecast['high']:.2f}, low=${forecast['low']:.2f} "
        f"[Confidence: {forecast.get('conf', 'low')}]\n"
        f"Write a concise 2-sentence analyst note covering the price outlook and the key risk."
    )
    try:
        raw = await analyst_jury_svc._call_groq(
            "llama-3.3-70b-versatile", system, prompt
        )
        return raw.strip()
    except Exception as e:
        logger.warning(f"Groq analyst note failed: {e}")
        return forecast["note"]


async def _run_analyst_jury(
    symbol: str,
    closes: List[float],
    rsi: float,
    forecast: dict,
    stats: dict,
    info: dict,
    macd_data: dict,
    bb_data: dict,
    sma50: List,
    sma200: List,
    historical_prices: list,
    sr_levels: dict,
    *,
    news_task=None,
) -> List[dict]:
    """
    Run all 3 analyst personas concurrently via AnalystJuryService.
      - KIMI-K2    → Groq moonshotai/kimi-k2-instruct  (macro & risk lens, Moonshot AI)
      - LLAMA-70B  → Groq llama-3.3-70b-versatile    (growth lens, Meta)
      - QWEN3-32B  → Groq qwen/qwen3-32b             (quant lens, Alibaba)

    All provider routing and response parsing are handled inside
    AnalystJuryService — no per-provider branching needed here.
    """

    # ------------------------------------------------------------------
    # Build shared market context string
    # ------------------------------------------------------------------

    def _last(series):
        """Last non-None value from a series."""
        for v in reversed(series):
            if v is not None:
                return v
        return None

    price     = closes[-1]
    recent    = closes[-10:]
    price_str = ", ".join(f"{p:.2f}" for p in recent)

    # Indicator snapshots
    cur_macd   = _last(macd_data["macd"])
    cur_signal = _last(macd_data["signal"])
    cur_hist   = _last(macd_data["hist"])
    cur_upper  = _last(bb_data["upper"])
    cur_middle = _last(bb_data["middle"])
    cur_lower  = _last(bb_data["lower"])
    cur_sma50  = _last(sma50)
    cur_sma200 = _last(sma200)

    # Derived labels
    macd_label = (
        ("Bullish" if cur_macd > cur_signal else "Bearish")
        if cur_macd is not None and cur_signal is not None else "N/A"
    )
    bb_pos_str = (
        f"{(price - cur_lower) / (cur_upper - cur_lower) * 100:.1f}% of band"
        if cur_upper and cur_lower and cur_upper != cur_lower else "N/A"
    )
    sma50_pct  = f"{(price - cur_sma50)  / cur_sma50  * 100:+.2f}%"  if cur_sma50  is not None else "N/A"
    sma200_pct = f"{(price - cur_sma200) / cur_sma200 * 100:+.2f}%"  if cur_sma200 is not None else "N/A"

    # Volume trend
    vols   = [float(p.get("volume", 0)) for p in historical_prices if p.get("volume", 0) > 0]
    vol10  = sum(vols[-10:]) / len(vols[-10:]) if len(vols) >= 10 else None
    vol30  = sum(vols[-30:]) / len(vols[-30:]) if len(vols) >= 30 else None
    vol_line = (
        f"10d avg {vol10/1e6:.2f}M vs 30d avg {vol30/1e6:.2f}M "
        f"({(vol10-vol30)/vol30*100:+.1f}%)"
        if vol10 and vol30 else "N/A"
    )

    # Support / resistance (top 2 each)
    sup_str = " / ".join(f"${v:.2f}" for v in sr_levels.get("support",    [])[:2]) or "N/A"
    res_str = " / ".join(f"${v:.2f}" for v in sr_levels.get("resistance", [])[:2]) or "N/A"

    # Fundamentals
    pe_str  = str(info.get("pe_ratio", "N/A"))
    cap_str = _fmt_market_cap(info.get("market_cap"))
    rng_str = info.get("range_52w", "N/A")
    sec_str = info.get("sector",    "N/A")
    div_str = (
        _fmt_pct(info.get("dividend_yield"))
        if info.get("dividend_yield") not in (None, "N/A") else "N/A"
    )

    # News headlines — opportunistic: only if serp_task already finished
    news_block = ""
    if news_task is not None and news_task.done() and not news_task.cancelled():
        try:
            headlines = news_task.result().get("news_results", [])[:4]
            if headlines:
                lines = []
                for h in headlines:
                    src = (
                        h.get("source", {}).get("name", "")
                        if isinstance(h.get("source"), dict)
                        else str(h.get("source", ""))
                    )
                    lines.append(f"  - {h.get('title', '')} [{src}]")
                news_block = "Recent news:\n" + "\n".join(lines) + "\n"
        except Exception:
            pass

    # Formatted indicator lines
    macd_line = (
        f"MACD: {cur_macd:.4f} | Signal: {cur_signal:.4f} | Hist: {cur_hist:.4f} [{macd_label}]"
        if cur_macd is not None and cur_signal is not None and cur_hist is not None
        else "MACD: N/A"
    )
    bb_line = (
        f"BB: Upper={cur_upper:.2f} Mid={cur_middle:.2f} Lower={cur_lower:.2f} | Price: {bb_pos_str}"
        if cur_upper is not None else "BB: N/A"
    )
    # Each arm is only formatted if its value actually exists
    sma50_str  = f"SMA50: {cur_sma50:.2f} ({sma50_pct})"    if cur_sma50  is not None else "SMA50: N/A"
    sma200_str = f"SMA200: {cur_sma200:.2f} ({sma200_pct})" if cur_sma200 is not None else "SMA200: N/A"
    sma_line   = f"{sma50_str} | {sma200_str}"


    ctx = (
        f"Symbol: {symbol} | Price: ${price:.2f} | RSI: {rsi:.1f} | Sector: {sec_str}\n"
        f"Fundamentals: PE={pe_str} | Cap={cap_str} | Div={div_str} | 52w={rng_str}\n"
        f"10-day closes: {price_str}\n"
        f"Forecast 5d → High: ${forecast['high']:.2f}, Low: ${forecast['low']:.2f} "
        f"[Confidence: {forecast.get('conf', 'low')}]\n"
        f"Volatility: {stats.get('ann_volatility_pct', 0):.1f}% ann | "
        f"Slope: {stats.get('trend_slope', 0):.4f}/day | "
        f"vs SMA20: {stats.get('price_vs_sma20_pct', 0):+.2f}%\n"
        f"{macd_line}\n"
        f"{bb_line}\n"
        f"{sma_line}\n"
        f"Volume: {vol_line}\n"
        f"Support: {sup_str} | Resistance: {res_str}\n"
        f"{news_block}"
    )

    # ------------------------------------------------------------------
    # Dispatch all personas concurrently — AnalystJuryService routes
    # to Groq (LLAMA-70B, QWEN-32B) or xAI (XAI-GROK) internally
    # ------------------------------------------------------------------
    results = await asyncio.gather(
        *[analyst_jury_svc.get_analyst_verdict(persona, ctx) for persona in ANALYST_PERSONAS],
        return_exceptions=True,
    )

    verdicts = []
    for r, persona in zip(results, ANALYST_PERSONAS):
        if isinstance(r, Exception):
            logger.warning(f"Analyst {persona['id']} failed: {r}")
            verdicts.append({
                "id":          persona["id"],
                "avatar":      persona["avatar"],
                "title":       persona["title"],
                "model_label": persona["model_label"],
                "color":       persona["color"],
                "rating":      "Hold",
                "note":        "Analysis unavailable.",
                "confidence":  30,
                "model":       "error",
            })
        else:
            verdicts.append(r)

    return verdicts


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/debug")
async def debug():
    """Checks every service dependency — hit this in the browser to diagnose 500s."""
    from models import MODELS_AVAILABLE
    from services import YFINANCE_AVAILABLE

    results: dict = {
        "yfinance_installed":   YFINANCE_AVAILABLE,
        "ml_models_available":  MODELS_AVAILABLE,
        "serp_api_key_set":     bool(Config.SERP_API_KEY),
        "groq_key_set":         bool(Config.GROQ_API_KEY),
        "influxdb_token_set":   bool(Config.INFLUXDB_TOKEN),
    }

    # Test InfluxDB connectivity
    try:
        influx_svc.has_recent_data("DEBUG_TEST")
        results["influxdb_reachable"] = True
    except Exception as e:
        results["influxdb_reachable"] = False
        results["influxdb_error"]     = str(e)

    # Test yfinance (quick 5-day fetch)
    if YFINANCE_AVAILABLE:
        try:
            import yfinance as _yf
            df = await asyncio.to_thread(
                lambda: _yf.download(
                    "AAPL", period="5d", interval="1d",
                    progress=False, auto_adjust=True, timeout=10,
                )
            )
            results["yfinance_reachable"] = not df.empty
            results["yfinance_rows"]      = int(len(df))
        except Exception as e:
            results["yfinance_reachable"] = False
            results["yfinance_error"]     = str(e)

    return results


@app.post("/predict", response_model=PredictionResponse)
async def predict(payload: dict = Body(...)):
    symbol = payload.get("data", "SPY").upper()
    now    = datetime.now(timezone.utc)

    # 1. Fetch OHLCV history
    if not influx_svc.has_recent_data(symbol):
        df = await asyncio.to_thread(yf_svc.fetch_history, symbol, "2y")
        if not df.empty:
            try:
                df = DataCleaner.clean(df)
            except Exception as e:
                logger.warning(f"DataCleaner.clean failed for {symbol}: {e}")
                df = df.__class__()
            if not df.empty:
                await asyncio.to_thread(influx_svc.write_ohlcv_batch, symbol, df)

    historical_prices: list = await asyncio.to_thread(influx_svc.query_history, symbol)

    # Fallback: yfinance directly if InfluxDB has insufficient history
    if not historical_prices or len(historical_prices) < 60:
        df = await asyncio.to_thread(yf_svc.fetch_history, symbol, "2y")
        if not df.empty:
            try:
                df = DataCleaner.clean(df)
            except Exception as e:
                logger.warning(f"DataCleaner.clean fallback failed for {symbol}: {e}")
                df = df.__class__()
            if not df.empty:
                historical_prices = DataCleaner.to_history_list(df)

    if not historical_prices:
        raise HTTPException(status_code=404, detail=f"No data found for {symbol}.")

    historical_prices.sort(key=lambda x: x["_time"])

    # 2. Live price
    live_price = await asyncio.to_thread(yf_svc.get_live_price, symbol)
    if live_price > 0:
        historical_prices.append({
            "_time": now, "close": live_price,
            "open":  live_price, "high": live_price,
            "low":   live_price, "volume": 0.0,
        })
    else:
        live_price = float(historical_prices[-1]["close"])

    # 3. Fundamentals
    info = await asyncio.to_thread(yf_svc.fetch_info, symbol)
    metrics = {
        "market_cap": _fmt_market_cap(info.get("market_cap")),
        "pe_ratio":   str(info.get("pe_ratio", "N/A")),
        "yield":      _fmt_pct(info.get("dividend_yield")) if info.get("dividend_yield") not in (None, "N/A") else "N/A",
        "prev_close": str(info.get("prev_close", "N/A")),
        "range_52w":  info.get("range_52w", "N/A"),
        "sector":     info.get("sector",    "N/A"),
        "currency":   info.get("currency",  "USD"),
    }

    # 4. Analytics & ensemble forecast
    closes   = [float(p["close"]) for p in historical_prices]
    rsi      = calculate_rsi(closes)
    forecast = run_ensemble_forecast(closes, symbol)

    # 4b. Technical indicators (full series, sliced to chart window later)
    macd_data = calculate_macd(closes)
    bb_data   = calculate_bollinger_bands(closes)
    sma50     = calculate_sma_series(closes, 50)
    sma200    = calculate_sma_series(closes, 200)

    # Fire news fetch concurrently — started now, awaited after jury
    serp_task = asyncio.create_task(serp_svc.fetch_data(symbol))

    # Support/resistance — computed before jury so levels can be cited
    sr_levels = calculate_support_resistance(closes)

    # 5. AI analyst note + jury (concurrent)
    note, jury = await asyncio.gather(
        _ai_note(symbol, closes, rsi, forecast),
        _run_analyst_jury(
            symbol, closes, rsi, forecast,
            forecast.get("stats", {}),
            info, macd_data, bb_data, sma50, sma200,
            historical_prices, sr_levels,
            news_task=serp_task,
        ),
    )

    # 6. News + trending via SerpAPI
    news: list     = []
    trending: list = []
    try:
        serp_data = await serp_task
        news = [
            {
                "title":     n.get("title",  ""),
                "link":      n.get("link",   ""),
                "source":    (
                    n.get("source", {}).get("name", "")
                    if isinstance(n.get("source"), dict)
                    else str(n.get("source", ""))
                ),
                "thumbnail": n.get("thumbnail", ""),
                "date":      n.get("date",   ""),
            }
            for n in serp_data.get("news_results", [])[:8]
        ]
        for category, items in serp_data.get("markets", {}).items():
            if isinstance(items, list):
                for t in items[:3]:
                    trending.append({
                        "symbol":   t.get("symbol") or t.get("name", "N/A"),
                        "name":     t.get("name",  ""),
                        "price":    str(t.get("price", "N/A")),
                        "change":   str(t.get("price_change_percentage", "0%")),
                        "category": category,
                    })
    except Exception as e:
        logger.warning(f"SerpAPI fetch failed: {e}")

    # 7. Background price snapshot
    asyncio.create_task(asyncio.to_thread(influx_svc.write_price, symbol, live_price))

    # 8. Chart history (last 90 trading days) with indicator slices
    total       = len(historical_prices)
    slice_start = max(0, total - 90)
    history = []
    for idx, p in enumerate(historical_prices[slice_start:], start=slice_start):
        history.append({
            "date":        p["_time"].strftime("%m/%d") if hasattr(p["_time"], "strftime") else str(p["_time"])[:10],
            "price":       round(float(p["close"]),                 2),
            "open":        round(float(p.get("open",  p["close"])), 2),
            "high":        round(float(p.get("high",  p["close"])), 2),
            "low":         round(float(p.get("low",   p["close"])), 2),
            "volume":      round(float(p.get("volume", 0)),         0),
            "bb_upper":    bb_data["upper"][idx],
            "bb_middle":   bb_data["middle"][idx],
            "bb_lower":    bb_data["lower"][idx],
            "sma50":       sma50[idx],
            "sma200":      sma200[idx],
            "macd":        macd_data["macd"][idx],
            "macd_signal": macd_data["signal"][idx],
            "macd_hist":   macd_data["hist"][idx],
        })

    # RSI series — last 90 points aligned to chart window
    rsi_full   = calculate_rsi_series(closes)
    rsi_series = rsi_full[slice_start:]

    return PredictionResponse(
        symbol       = symbol,
        currentPrice = f"{live_price:.2f}",
        rsi          = f"{rsi:.2f}",
        prediction   = {
            "highRange": f"{forecast['high']:.2f}",
            "lowRange":  f"{forecast['low']:.2f}",
            "trend":     "Bullish" if rsi > 50 else "Bearish",
        },
        analystNote  = note,
        confidence   = forecast["conf"],
        history      = history,
        forecastDays = forecast["forecast_days"],
        modelStats   = forecast.get("stats", {}),
        metrics      = metrics,
        news         = news,
        trending     = trending,
        indicators   = {
            "rsi_series": rsi_series,
            "support":    sr_levels["support"],
            "resistance": sr_levels["resistance"],
        },
        lastUpdated  = now.isoformat(),
        juryAnalysts = jury,
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=Config.PORT)
