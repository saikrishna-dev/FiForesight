# backend/services.py
import re
import json
import logging
import httpx
import pandas as pd
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional
import newrelic.agent

try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
    logging.getLogger(__name__).info(
        "[YFINANCE] ✓ yfinance package loaded successfully"
    )
except ImportError:
    yf = None  # type: ignore
    YFINANCE_AVAILABLE = False
    logging.getLogger(__name__).warning(
        "[YFINANCE] ✗ yfinance not installed — run: pip install yfinance"
    )

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS
from config import Config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Input validation — prevents Flux/InfluxQL injection via user-supplied tags
# ---------------------------------------------------------------------------

_SAFE_TAG_RE  = re.compile(r"^[A-Za-z0-9._:\-]+$")
_VALID_SIM_ENV = frozenset({"local", "preview", "live"})
_UUID_RE       = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _validate_tag(value: str, field: str = "tag") -> str:
    """
    Strict validation for values that are interpolated into Flux queries.
    Rejects any character outside [A-Za-z0-9._:-] to prevent query injection
    (e.g. a symbol like `AAPL" or r._field == "`).
    """
    if not isinstance(value, str) or not _SAFE_TAG_RE.match(value):
        raise ValueError(f"Invalid {field}: {value!r}")
    if len(value) > 32:
        raise ValueError(f"{field} too long: {value!r}")
    return value


_ALLOWED_MODELS = frozenset({
    "prophet", "sarima", "rf",
    # per-horizon ensemble accuracy trackers (d1 reuses same slot as per-model)
    "ensemble_d1", "ensemble_d2", "ensemble_d3", "ensemble_d4", "ensemble_d5",
})


def _validate_model(model: str) -> str:
    if model not in _ALLOWED_MODELS:
        raise ValueError(f"Invalid model: {model!r}")
    return model


# ---------------------------------------------------------------------------
# InfluxDB Service  —  stores and retrieves full OHLCV data
# ---------------------------------------------------------------------------

class InfluxService:
    def __init__(self):
        logger.info(
            f"[INFLUXDB] Initialising client — url={Config.INFLUXDB_URL}, "
            f"org={Config.INFLUXDB_ORG}, bucket={Config.INFLUXDB_BUCKET}, "
            f"token_set={'yes' if Config.INFLUXDB_TOKEN else 'NO — writes will fail'}"
        )
        self.client = InfluxDBClient(
            url=Config.INFLUXDB_URL,
            token=Config.INFLUXDB_TOKEN,
            org=Config.INFLUXDB_ORG,
        )
        self.write_api = self.client.write_api(write_options=SYNCHRONOUS)
        self.query_api = self.client.query_api()
        logger.info("[INFLUXDB] ✓ Client ready (write_api=SYNCHRONOUS)")

    # --- Write a single live close price (used for intraday snapshots) -------
    def write_price(self, symbol: str, price: float):
        try:
            _validate_tag(symbol, "symbol")
        except ValueError as e:
            logger.error(f"[INFLUXDB] write_price rejected — {e}")
            return
        logger.info(f"[INFLUXDB] write_price — symbol={symbol}, price=${price:.2f}")
        try:
            p = (
                Point("market_data")
                .tag("symbol", symbol)
                .field("close", float(price))
                .time(datetime.now(timezone.utc), WritePrecision.NS)
            )
            self.write_api.write(
                bucket=Config.INFLUXDB_BUCKET, org=Config.INFLUXDB_ORG, record=p
            )
            logger.info(
                f"[INFLUXDB] ✓ write_price — {symbol} @ ${price:.2f} stored "
                f"(measurement=market_data, field=close)"
            )
        except Exception as e:
            logger.error(f"[INFLUXDB] ✗ write_price error for {symbol}: {e}")

    # --- Write a full OHLCV batch (from yfinance) ----------------------------
    def write_ohlcv_batch(self, symbol: str, df: pd.DataFrame):
        """
        df must have a DatetimeIndex (UTC) and columns: Open, High, Low, Close, Volume.
        InfluxDB Cloud has a 30-day retention policy — rows older than 29 days are skipped.
        """
        try:
            _validate_tag(symbol, "symbol")
        except ValueError as e:
            logger.error(f"[INFLUXDB] write_ohlcv_batch rejected — {e}")
            return
        cutoff = datetime.now(timezone.utc) - timedelta(days=29)

        logger.info(
            f"[INFLUXDB] write_ohlcv_batch — symbol={symbol}, df_rows={len(df)}, "
            f"retention_cutoff={cutoff.date()} (last 29 days)"
        )

        points  = []
        skipped = 0
        for ts, row in df.iterrows():
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            if ts < cutoff:
                skipped += 1
                continue   # outside retention window
            p = (
                Point("market_data")
                .tag("symbol", symbol)
                .field("open",   float(row.get("Open",   row.get("open",   0))))
                .field("high",   float(row.get("High",   row.get("high",   0))))
                .field("low",    float(row.get("Low",    row.get("low",    0))))
                .field("close",  float(row.get("Close",  row.get("close",  0))))
                .field("volume", float(row.get("Volume", row.get("volume", 0))))
                .time(ts, WritePrecision.NS)
            )
            points.append(p)

        logger.info(
            f"[INFLUXDB] Batch analysis — total_rows={len(df)}, "
            f"skipped_outside_retention={skipped}, queued_to_write={len(points)}"
        )

        if not points:
            logger.warning(
                f"[INFLUXDB] write_ohlcv_batch — 0 points to write for {symbol} "
                f"(all {skipped} rows were outside the 29-day retention window)"
            )
            return

        try:
            self.write_api.write(
                bucket=Config.INFLUXDB_BUCKET,
                org=Config.INFLUXDB_ORG,
                record=points,
            )
            logger.info(
                f"[INFLUXDB] ✓ write_ohlcv_batch — wrote {len(points)} OHLCV rows for {symbol} "
                f"(skipped {skipped} rows outside retention)"
            )
        except Exception as e:
            logger.error(f"[INFLUXDB] ✗ write_ohlcv_batch error for {symbol}: {e}")

    # --- Query stored OHLCV history ------------------------------------------
    def query_history(self, symbol: str, days: int = 365) -> list:
        """
        Returns list of dicts with keys: _time, open, high, low, close, volume
        """
        try:
            _validate_tag(symbol, "symbol")
        except ValueError as e:
            logger.error(f"[INFLUXDB] query_history rejected — {e}")
            return []
        days = max(1, int(days))
        logger.info(
            f"[INFLUXDB] query_history — symbol={symbol}, range=last {days}d, "
            f"measurement=market_data"
        )
        query = f"""
        from(bucket: "{Config.INFLUXDB_BUCKET}")
          |> range(start: -{days}d)
          |> filter(fn: (r) => r["_measurement"] == "market_data" and r["symbol"] == "{symbol}")
          |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
          |> sort(columns: ["_time"], desc: false)
        """
        try:
            tables = self.query_api.query(query)
            rows   = [r.values for t in tables for r in t.records]

            out = []
            for row in rows:
                out.append({
                    "_time":  row.get("_time"),
                    "open":   float(row.get("open",   row.get("close", 0)) or 0),
                    "high":   float(row.get("high",   row.get("close", 0)) or 0),
                    "low":    float(row.get("low",    row.get("close", 0)) or 0),
                    "close":  float(row.get("close",  0) or 0),
                    "volume": float(row.get("volume", 0) or 0),
                })

            if out:
                times    = [r["_time"] for r in out if r["_time"] is not None]
                date_min = min(times).date() if times else "?"
                date_max = max(times).date() if times else "?"
                logger.info(
                    f"[INFLUXDB] ✓ query_history — {len(out)} rows returned for {symbol} | "
                    f"date range: {date_min} → {date_max} | "
                    f"close: ${out[0]['close']:.2f} (oldest) → ${out[-1]['close']:.2f} (latest)"
                )
            else:
                logger.warning(
                    f"[INFLUXDB] query_history — 0 rows returned for {symbol} "
                    f"(symbol not in DB or range too narrow)"
                )
            return out

        except Exception as e:
            logger.error(f"[INFLUXDB] ✗ query_history error for {symbol}: {e}")
            return []

    def has_recent_data(self, symbol: str, within_hours: int = 20) -> bool:
        """Return True if we already have fresh data for today (avoids re-fetching)."""
        try:
            _validate_tag(symbol, "symbol")
        except ValueError as e:
            logger.error(f"[INFLUXDB] has_recent_data rejected — {e}")
            return False
        within_hours = max(1, int(within_hours))
        logger.info(
            f"[INFLUXDB] has_recent_data — symbol={symbol}, "
            f"checking last {within_hours}h ..."
        )
        query = f"""
        from(bucket: "{Config.INFLUXDB_BUCKET}")
          |> range(start: -{within_hours}h)
          |> filter(fn: (r) => r["_measurement"] == "market_data" and r["symbol"] == "{symbol}")
          |> count()
          |> limit(n: 1)
        """
        try:
            tables = self.query_api.query(query)
            for t in tables:
                for r in t.records:
                    count = r.get_value() or 0
                    if count > 0:
                        logger.info(
                            f"[INFLUXDB] Cache HIT — {count} record(s) found for {symbol} "
                            f"within last {within_hours}h → yfinance fetch will be SKIPPED"
                        )
                        return True
                    else:
                        logger.info(
                            f"[INFLUXDB] Cache MISS — 0 records for {symbol} "
                            f"within last {within_hours}h → yfinance fetch required"
                        )
                        return False
        except Exception as e:
            logger.error(f"[INFLUXDB] ✗ has_recent_data error for {symbol}: {e}")

        logger.info(
            f"[INFLUXDB] Cache MISS — no records returned for {symbol} "
            f"(query returned empty) → yfinance fetch required"
        )
        return False

    # ── Simulation state persistence ─────────────────────────────────────────
    # Each simulation is stored as a separate time-series (env + sim_id tags).
    # Soft-delete: write an empty-string tombstone so `last()` filters it out.
    # One InfluxDB point per save/delete — negligible storage footprint.

    @staticmethod
    def _validate_sim_env(env: str) -> str:
        if env not in _VALID_SIM_ENV:
            raise ValueError(f"Invalid simulation env: {env!r}")
        return env

    @staticmethod
    def _validate_sim_id(sim_id: str) -> str:
        if not _UUID_RE.match(sim_id.lower()):
            raise ValueError(f"sim_id must be a UUID v4, got: {sim_id!r}")
        return sim_id.lower()

    def write_simulation_state(self, sim_id: str, env: str, state: dict) -> bool:
        try:
            env    = self._validate_sim_env(env)
            sim_id = self._validate_sim_id(sim_id)
            point  = (
                Point("simulation_state")
                .tag("env",    env)
                .tag("sim_id", sim_id)
                .field("state_json", json.dumps(state))
            )
            self.write_api.write(
                bucket=Config.INFLUXDB_BUCKET,
                org=Config.INFLUXDB_ORG,
                record=point,
            )
            logger.info("[INFLUXDB] simulation_state saved: sim_id=%s env=%s", sim_id, env)
            return True
        except Exception as exc:
            logger.error("[INFLUXDB] write_simulation_state failed: %s", exc)
            return False

    def query_simulation_states(self, env: str) -> List[dict]:
        """Return all live (non-deleted) simulations for the given env, newest first."""
        try:
            env = self._validate_sim_env(env)
            query = f"""
from(bucket: "{Config.INFLUXDB_BUCKET}")
  |> range(start: -365d)
  |> filter(fn: (r) => r._measurement == "simulation_state")
  |> filter(fn: (r) => r.env == "{env}")
  |> filter(fn: (r) => r._field == "state_json")
  |> last()
  |> filter(fn: (r) => r._value != "")
  |> sort(columns: ["_time"], desc: true)
"""
            tables = self.query_api.query(query, org=Config.INFLUXDB_ORG)
            results: List[dict] = []
            for table in tables:
                for record in table.records:
                    try:
                        state = json.loads(record.get_value())
                        results.append(state)
                    except Exception as exc:
                        logger.debug(
                            "[INFLUXDB] skipping unparseable simulation_state record: %s",
                            exc,
                        )
            return results
        except Exception as exc:
            logger.error("[INFLUXDB] query_simulation_states failed: %s", exc)
            return []

    def delete_simulation_state(self, sim_id: str, env: str) -> bool:
        """Soft-delete: write an empty-string tombstone so `last()` filters it out."""
        try:
            env    = self._validate_sim_env(env)
            sim_id = self._validate_sim_id(sim_id)
            point  = (
                Point("simulation_state")
                .tag("env",    env)
                .tag("sim_id", sim_id)
                .field("state_json", "")
            )
            self.write_api.write(
                bucket=Config.INFLUXDB_BUCKET,
                org=Config.INFLUXDB_ORG,
                record=point,
            )
            logger.info("[INFLUXDB] simulation_state deleted: sim_id=%s env=%s", sim_id, env)
            return True
        except Exception as exc:
            logger.error("[INFLUXDB] delete_simulation_state failed: %s", exc)
            return False


# ---------------------------------------------------------------------------
# ForecastStore  —  RL feedback loop: record forecasts, resolve outcomes,
#                   maintain per-model accuracy for calibrated weight blending
# ---------------------------------------------------------------------------

class ForecastStore:
    """
    Three InfluxDB measurements:
      forecast_record  — per-model day-1 preds + ensemble preds + weights
      price_outcome    — actual market closes (written at resolution time)
      model_accuracy   — running day-1 MAE per model (prophet / sarima / rf)

    On every /predict request:
      1. write_forecast_record  — records what was predicted
      2. resolve_past_forecasts (background) — matches old forecasts against
         actual closes, updates model_accuracy
      3. query_model_accuracy  — returns historical MAE used to blend weights
    """

    def __init__(self, influx_service: "InfluxService"):
        self._svc = influx_service

    # ── Writes ────────────────────────────────────────────────────────────────

    def write_forecast_record(
        self,
        symbol: str,
        last_price: float,
        prophet_d1: Optional[float],
        sarima_d1: Optional[float],
        rf_d1: Optional[float],
        w_prophet: float,
        w_sarima: float,
        w_rf: float,
        ensemble_preds: List[float],  # 5-element list (one per forecast day)
        d1_high: Optional[float] = None,
        d1_low: Optional[float] = None,
    ) -> None:
        try:
            _validate_tag(symbol, "symbol")
        except ValueError as e:
            logger.error(f"[RL] write_forecast_record rejected — {e}")
            return
        p_str = f"{prophet_d1:.2f}" if prophet_d1 is not None else "N/A"
        s_str = f"{sarima_d1:.2f}"  if sarima_d1  is not None else "N/A"
        r_str = f"{rf_d1:.2f}"      if rf_d1       is not None else "N/A"
        try:
            logger.info(
                f"[RL] write_forecast_record — {symbol} @ last_price=${last_price:.2f} | "
                f"p_d1={p_str}, s_d1={s_str}, r_d1={r_str} | "
                f"w=[{w_prophet:.3f},{w_sarima:.3f},{w_rf:.3f}]"
            )
            p = (
                Point("forecast_record")
                .tag("symbol", symbol)
                .field("last_price", float(last_price))
                .field("p_d1", float(prophet_d1) if prophet_d1 is not None else -1.0)
                .field("s_d1", float(sarima_d1)  if sarima_d1  is not None else -1.0)
                .field("r_d1", float(rf_d1)       if rf_d1       is not None else -1.0)
                .field("w_p",  float(w_prophet))
                .field("w_s",  float(w_sarima))
                .field("w_r",  float(w_rf))
            )
            for i, pred in enumerate(ensemble_preds[:5], 1):
                p = p.field(f"e_d{i}", float(pred))
            if d1_high is not None:
                p = p.field("e_d1_high", float(d1_high))
            if d1_low is not None:
                p = p.field("e_d1_low", float(d1_low))
            p = p.time(datetime.now(timezone.utc), WritePrecision.NS)
            self._svc.write_api.write(
                bucket=Config.INFLUXDB_BUCKET, org=Config.INFLUXDB_ORG, record=p
            )
            logger.info(f"[RL] ✓ forecast_record written for {symbol}")
        except Exception as e:
            logger.error(f"[RL] ✗ write_forecast_record error for {symbol}: {e}")

    def write_price_outcome(
        self, symbol: str, outcome_dt: datetime, actual_close: float
    ) -> None:
        try:
            _validate_tag(symbol, "symbol")
        except ValueError as e:
            logger.error(f"[RL] write_price_outcome rejected — {e}")
            return
        try:
            p = (
                Point("price_outcome")
                .tag("symbol", symbol)
                .field("actual_close", float(actual_close))
                .time(outcome_dt, WritePrecision.NS)
            )
            self._svc.write_api.write(
                bucket=Config.INFLUXDB_BUCKET, org=Config.INFLUXDB_ORG, record=p
            )
            logger.info(
                f"[RL] ✓ price_outcome written — {symbol} {outcome_dt.date()} = ${actual_close:.2f}"
            )
        except Exception as e:
            logger.error(f"[RL] ✗ write_price_outcome error for {symbol}: {e}")

    def mark_forecast_resolved(
        self, symbol: str, record_time: datetime, horizon: int = 1
    ) -> None:
        """
        Write a resolution marker for a given forecast_record.
        `horizon` is the highest ensemble day resolved so far (1–5).
        Writing the same timestamp overwrites the previous value in InfluxDB,
        so the stored value always reflects the max horizon resolved.
        """
        try:
            _validate_tag(symbol, "symbol")
        except ValueError as e:
            logger.error(f"[RL] mark_forecast_resolved rejected — {e}")
            return
        try:
            p = (
                Point("forecast_resolution")
                .tag("symbol", symbol)
                .field("resolved", int(horizon))
                .time(record_time, WritePrecision.NS)
            )
            self._svc.write_api.write(
                bucket=Config.INFLUXDB_BUCKET, org=Config.INFLUXDB_ORG, record=p
            )
        except Exception as e:
            logger.error(f"[RL] ✗ mark_forecast_resolved error for {symbol}: {e}")

    def query_resolved_timestamps(
        self, symbol: str, days: int = 30
    ) -> Dict[object, int]:
        """
        Return {forecast_record_time: max_horizon_resolved} for all resolved records.
        horizon=1 means d1 done, horizon=5 means fully resolved.
        Old records written with resolved=1 (boolean) continue to work correctly.
        """
        try:
            _validate_tag(symbol, "symbol")
        except ValueError as e:
            logger.error(f"[RL] query_resolved_timestamps rejected — {e}")
            return {}
        days = max(1, int(days))
        query = f"""
        from(bucket: "{Config.INFLUXDB_BUCKET}")
          |> range(start: -{days}d)
          |> filter(fn: (r) => r["_measurement"] == "forecast_resolution" and r["symbol"] == "{symbol}")
          |> keep(columns: ["_time", "_value"])
        """
        try:
            tables = self._svc.query_api.query(query)
            out: Dict[object, int] = {}
            for t in tables:
                for r in t.records:
                    tv  = r.values.get("_time")
                    val = r.values.get("_value", 0) or 0
                    if tv is not None:
                        out[tv] = max(out.get(tv, 0), int(val))
            return out
        except Exception as e:
            logger.error(f"[RL] ✗ query_resolved_timestamps error for {symbol}: {e}")
            return {}

    def write_model_accuracy(
        self, symbol: str, model: str, mae: float, sample_count: int
    ) -> bool:
        """Write updated EMA-MAE stats for one model/horizon key.

        Returns True on success, False on any validation or InfluxDB failure
        so callers can gate downstream operations (e.g. mark_forecast_resolved)
        on confirmed writes.
        """
        try:
            _validate_tag(symbol, "symbol")
            _validate_model(model)
        except ValueError as e:
            logger.error(f"[RL] write_model_accuracy rejected — {e}")
            return False
        try:
            p = (
                Point("model_accuracy")
                .tag("symbol", symbol)
                .tag("model", model)
                .field("mae_d1", float(mae))
                .field("sample_count", int(sample_count))
                .time(datetime.now(timezone.utc), WritePrecision.NS)
            )
            self._svc.write_api.write(
                bucket=Config.INFLUXDB_BUCKET, org=Config.INFLUXDB_ORG, record=p
            )
            logger.info(
                f"[RL] ✓ model_accuracy updated — {symbol}/{model}: MAE=${mae:.3f}, n={sample_count}"
            )
            return True
        except Exception as e:
            logger.error(f"[RL] ✗ write_model_accuracy error for {symbol}/{model}: {e}")
            return False

    # ── Queries ───────────────────────────────────────────────────────────────

    def query_forecast_records(self, symbol: str, days: int = 10) -> List[dict]:
        """Returns forecast record rows from the last N days."""
        try:
            _validate_tag(symbol, "symbol")
        except ValueError as e:
            logger.error(f"[RL] query_forecast_records rejected — {e}")
            return []
        days = max(1, int(days))
        query = f"""
        from(bucket: "{Config.INFLUXDB_BUCKET}")
          |> range(start: -{days}d)
          |> filter(fn: (r) => r["_measurement"] == "forecast_record" and r["symbol"] == "{symbol}")
          |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
          |> sort(columns: ["_time"], desc: false)
        """
        try:
            tables = self._svc.query_api.query(query)
            rows = [r.values for t in tables for r in t.records]
            logger.info(
                f"[RL] query_forecast_records — {symbol}: {len(rows)} records in last {days}d"
            )
            return rows
        except Exception as e:
            logger.error(f"[RL] ✗ query_forecast_records error for {symbol}: {e}")
            return []

    def query_price_outcomes(self, symbol: str, days: int = 15) -> Dict[object, float]:
        """Returns {date: actual_close} from price_outcome measurement."""
        try:
            _validate_tag(symbol, "symbol")
        except ValueError as e:
            logger.error(f"[RL] query_price_outcomes rejected — {e}")
            return {}
        days = max(1, int(days))
        query = f"""
        from(bucket: "{Config.INFLUXDB_BUCKET}")
          |> range(start: -{days}d)
          |> filter(fn: (r) => r["_measurement"] == "price_outcome" and r["symbol"] == "{symbol}")
          |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
        """
        try:
            tables = self._svc.query_api.query(query)
            result: Dict[object, float] = {}
            for t in tables:
                for r in t.records:
                    t_val = r.values.get("_time")
                    close = r.values.get("actual_close", 0.0)
                    if t_val is not None:
                        result[t_val.date()] = float(close)
            logger.info(
                f"[RL] query_price_outcomes — {symbol}: {len(result)} outcomes in last {days}d"
            )
            return result
        except Exception as e:
            logger.error(f"[RL] ✗ query_price_outcomes error for {symbol}: {e}")
            return {}

    def query_model_accuracy(self, symbol: str, lookback_days: int = 90) -> Dict[str, dict]:
        """
        Returns most-recent per-model accuracy record.
        { "prophet": {"mae": float, "samples": int}, "sarima": {...}, "rf": {...} }
        """
        try:
            _validate_tag(symbol, "symbol")
        except ValueError as e:
            logger.error(f"[RL] query_model_accuracy rejected — {e}")
            return {}
        lookback_days = max(1, int(lookback_days))
        result: Dict[str, dict] = {}
        for model in ("prophet", "sarima", "rf"):
            _validate_model(model)
            query = f"""
            from(bucket: "{Config.INFLUXDB_BUCKET}")
              |> range(start: -{lookback_days}d)
              |> filter(fn: (r) =>
                  r["_measurement"] == "model_accuracy"
                  and r["symbol"] == "{symbol}"
                  and r["model"] == "{model}")
              |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
              |> sort(columns: ["_time"], desc: true)
              |> limit(n: 1)
            """
            try:
                tables = self._svc.query_api.query(query)
                for t in tables:
                    for r in t.records:
                        vals = r.values
                        result[model] = {
                            "mae":     float(vals.get("mae_d1",      999.0)),
                            "samples": int(vals.get("sample_count", 0)),
                        }
            except Exception as e:
                logger.warning(
                    f"[RL] query_model_accuracy error for {symbol}/{model}: {e}"
                )
        logger.info(f"[RL] query_model_accuracy — {symbol}: {result}")
        return result

    def query_ensemble_mae(self, symbol: str, lookback_days: int = 90) -> Dict[str, dict]:
        """
        Returns most-recent per-horizon ensemble accuracy.
        { "ensemble_d1": {"mae": float, "samples": int}, ..., "ensemble_d5": {...} }
        Empty dict or missing keys = no data yet for that horizon.
        """
        try:
            _validate_tag(symbol, "symbol")
        except ValueError as e:
            logger.error(f"[RL] query_ensemble_mae rejected — {e}")
            return {}
        lookback_days = max(1, int(lookback_days))
        result: Dict[str, dict] = {}
        for model in ("ensemble_d1", "ensemble_d2", "ensemble_d3", "ensemble_d4", "ensemble_d5"):
            query = f"""
            from(bucket: "{Config.INFLUXDB_BUCKET}")
              |> range(start: -{lookback_days}d)
              |> filter(fn: (r) =>
                  r["_measurement"] == "model_accuracy"
                  and r["symbol"] == "{symbol}"
                  and r["model"] == "{model}")
              |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
              |> sort(columns: ["_time"], desc: true)
              |> limit(n: 1)
            """
            try:
                tables = self._svc.query_api.query(query)
                for t in tables:
                    for r in t.records:
                        vals = r.values
                        result[model] = {
                            "mae":     float(vals.get("mae_d1", 999.0)),
                            "samples": int(vals.get("sample_count", 0)),
                        }
            except Exception as e:
                logger.warning(f"[RL] query_ensemble_mae error for {symbol}/{model}: {e}")
        logger.info(f"[RL] query_ensemble_mae — {symbol}: {result}")
        return result


# ---------------------------------------------------------------------------
# YFinance Service  —  free historical OHLCV + fundamentals
# ---------------------------------------------------------------------------

class YFinanceService:
    """
    Wraps yfinance for:
      - Full OHLCV history (1d bars, up to 2 years)
      - Company fundamentals (P/E, market cap, 52w range, dividend yield)
      - Current/last known price
    yfinance is completely free — no API key needed.
    """

    @staticmethod
    def _to_yf_symbol(symbol: str) -> str:
        """Convert SerpAPI-style 'NVDA:NASDAQ' → 'NVDA' for yfinance."""
        converted = symbol.split(":")[0].upper()
        if converted != symbol:
            logger.info(f"[YFINANCE] Symbol normalised: '{symbol}' → '{converted}'")
        return converted

    @newrelic.agent.function_trace()
    def fetch_history(self, symbol: str, period: str = "2y") -> pd.DataFrame:
        """
        Fetch daily OHLCV for `period` (e.g. '2y', '1y', '6mo').
        Returns a DataFrame with DatetimeIndex (UTC) and columns:
        Open, High, Low, Close, Volume.
        Returns empty DataFrame on failure.
        """
        if not YFINANCE_AVAILABLE:
            logger.error("[YFINANCE] ✗ fetch_history SKIPPED — yfinance not installed")
            return pd.DataFrame()

        ticker = self._to_yf_symbol(symbol)
        logger.info(
            f"[YFINANCE] fetch_history — ticker={ticker}, period={period}, "
            f"interval=1d, auto_adjust=True, timeout=20s"
        )

        try:
            df = yf.download(
                ticker,
                period=period,
                interval="1d",
                progress=False,
                auto_adjust=True,
                timeout=20,
            )

            if df.empty:
                logger.warning(
                    f"[YFINANCE] ✗ fetch_history — empty response for {ticker} "
                    f"(ticker may be invalid or delisted)"
                )
                return pd.DataFrame()

            # Flatten MultiIndex columns if present (yfinance ≥0.2 quirk)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
                df = df.loc[:, ~df.columns.duplicated(keep='first')]

            df.index = pd.to_datetime(df.index)
            if df.index.tz is None:
                df.index = df.index.tz_localize("UTC")
            else:
                df.index = df.index.tz_convert("UTC")

            keep = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
            df   = df[keep].dropna(subset=["Close"])

            close_col = df['Close']
            logger.info(
                f"[YFINANCE] ✓ fetch_history — {len(df)} rows for {ticker} | "
                f"date range: {df.index.min().date()} → {df.index.max().date()} | "
                f"close: ${float(close_col.iloc[0]):.2f} (oldest) → "
                f"${float(close_col.iloc[-1]):.2f} (latest) | "
                f"columns: {list(df.columns)}"
            )
            return df

        except Exception as e:
            logger.error(f"[YFINANCE] ✗ fetch_history error for {ticker}: {e}")
            return pd.DataFrame()

    @newrelic.agent.function_trace()
    def fetch_info(self, symbol: str) -> dict:
        """
        Returns a dict of fundamentals:
        market_cap, pe_ratio, dividend_yield, prev_close, range_52w, current_price
        """
        if not YFINANCE_AVAILABLE:
            logger.warning("[YFINANCE] fetch_info SKIPPED — yfinance not installed")
            return {}

        ticker = self._to_yf_symbol(symbol)
        logger.info(f"[YFINANCE] fetch_info — ticker={ticker}")

        try:
            info       = yf.Ticker(ticker).info
            prev_close = info.get("previousClose") or info.get("regularMarketPreviousClose")
            high52     = info.get("fiftyTwoWeekHigh")
            low52      = info.get("fiftyTwoWeekLow")
            range_52w  = f"{low52:.2f} - {high52:.2f}" if high52 and low52 else "N/A"
            current    = (
                info.get("currentPrice")
                or info.get("regularMarketPrice")
                or info.get("previousClose")
                or 0.0
            )
            result = {
                "current_price":   float(current),
                "market_cap":      info.get("marketCap",      "N/A"),
                "pe_ratio":        info.get("trailingPE",     "N/A"),
                "dividend_yield":  info.get("dividendYield",  "N/A"),
                "prev_close":      prev_close or "N/A",
                "range_52w":       range_52w,
                "short_name":      info.get("shortName",  ticker),
                "sector":          info.get("sector",     "N/A"),
                "currency":        info.get("currency",   "USD"),
            }
            logger.info(
                f"[YFINANCE] ✓ fetch_info — {ticker}: "
                f"price=${result['current_price']:.2f}, "
                f"market_cap={result['market_cap']}, "
                f"pe={result['pe_ratio']}, "
                f"div_yield={result['dividend_yield']}, "
                f"52w={result['range_52w']}, "
                f"sector={result['sector']}, "
                f"currency={result['currency']}"
            )
            return result

        except Exception as e:
            logger.error(f"[YFINANCE] ✗ fetch_info error for {ticker}: {e}")
            return {}

    @newrelic.agent.function_trace()
    def get_live_price(self, symbol: str) -> float:
        """Fast path to get the latest price from yfinance."""
        if not YFINANCE_AVAILABLE:
            logger.warning("[YFINANCE] get_live_price SKIPPED — yfinance not installed")
            return 0.0

        ticker = self._to_yf_symbol(symbol)
        logger.info(f"[YFINANCE] get_live_price — ticker={ticker}")

        try:
            t     = yf.Ticker(ticker)
            price = (
                t.info.get("currentPrice")
                or t.info.get("regularMarketPrice")
                or t.info.get("previousClose")
                or 0.0
            )
            price_f = float(price)
            if price_f > 0:
                logger.info(f"[YFINANCE] ✓ get_live_price — {ticker} = ${price_f:.2f}")
            else:
                logger.warning(
                    f"[YFINANCE] get_live_price — {ticker} returned 0.0 "
                    f"(currentPrice/regularMarketPrice/previousClose all missing)"
                )
            return price_f

        except Exception as e:
            logger.error(f"[YFINANCE] ✗ get_live_price error for {ticker}: {e}")
            return 0.0


# ---------------------------------------------------------------------------
# Data Cleaner  —  gap-fill, outlier removal, normalise
# ---------------------------------------------------------------------------

class DataCleaner:
    """
    Cleans a raw OHLCV DataFrame before it goes to InfluxDB or the models.
    """

    @staticmethod
    def clean(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            logger.warning("[CLEANER] clean() — input DataFrame is empty, returning as-is")
            return df

        input_rows = len(df)
        logger.info(
            f"[CLEANER] clean() — input: {input_rows} rows | "
            f"close range: ${float(df['Close'].min()):.2f} – ${float(df['Close'].max()):.2f}"
        )

        df = df.copy()
        df.sort_index(inplace=True)

        # 1. Drop rows where Close is zero or NaN
        df = df[df["Close"].notna() & (df["Close"] > 0)]
        after_nan_drop = len(df)
        nan_removed    = input_rows - after_nan_drop
        if nan_removed:
            logger.info(f"[CLEANER] NaN/zero Close rows removed: {nan_removed}")
        else:
            logger.info("[CLEANER] NaN/zero check — 0 rows removed (all Close values valid)")

        if df.empty:
            logger.warning("[CLEANER] DataFrame empty after NaN/zero drop — returning empty")
            return df

        # 2. Remove outliers: Close values > 4σ from rolling 30-day median
        rolling_med = df["Close"].rolling(30, min_periods=5, center=True).median()
        rolling_std = df["Close"].rolling(30, min_periods=5, center=True).std()
        upper = rolling_med + 4 * rolling_std
        lower = rolling_med - 4 * rolling_std
        mask  = (df["Close"] >= lower) & (df["Close"] <= upper)
        removed = int((~mask).sum())
        if removed:
            logger.info(
                f"[CLEANER] Outlier rows removed (>4σ from 30d rolling median): {removed}"
            )
        else:
            logger.info(
                "[CLEANER] Outlier check — 0 rows removed (all within 4σ rolling bands)"
            )
        df = df[mask]
        after_outlier = len(df)

        if df.empty:
            logger.warning("[CLEANER] DataFrame empty after outlier removal — returning empty")
            return df

        # 3. Reindex to business-day frequency and forward-fill gaps.
        #    IMPORTANT: Only price columns (Open/High/Low/Close) are
        #    forward-filled. Volume is LEFT AS NaN on synthetic rows and then
        #    set to 0 — forward-filling volume copies real traded volume onto
        #    non-trading days, which biases Prophet/SARIMAX/RF (they'd see
        #    repeated high-volume bars that never actually happened).
        #    Downstream (main.py) replaces zero-volume rows with a rolling
        #    mean so the synthetic markers don't contaminate normalisation.
        try:
            full_idx = pd.bdate_range(
                start=df.index.min(), end=df.index.max(), freq="B"
            )
            full_idx = full_idx.tz_localize("UTC") if full_idx.tz is None else full_idx

            price_cols = [c for c in ("Open", "High", "Low", "Close") if c in df.columns]
            has_volume = "Volume" in df.columns

            df = df.reindex(full_idx)
            if price_cols:
                df[price_cols] = df[price_cols].ffill()
            if has_volume:
                # Mark synthetic rows with volume=0 (not ffill).
                df["Volume"] = df["Volume"].fillna(0.0)

            after_ffill = len(df)
            synthetic   = after_ffill - after_outlier
            logger.info(
                f"[CLEANER] Gap-fill (bdate_range ffill) — "
                f"rows before={after_outlier}, after={after_ffill} "
                f"(+{synthetic} synthetic rows; price ffill, volume=0 on synthetics)"
            )
        except Exception as e:
            logger.warning(
                f"[CLEANER] bdate_range reindex FAILED ({e}) — gap-fill SKIPPED, "
                f"proceeding with {after_outlier} rows"
            )

        if df.empty:
            logger.warning("[CLEANER] DataFrame empty after gap-fill — returning empty")
            return df

        # 4. Ensure High >= Close >= Low (yfinance adjusted data can drift)
        if "High" in df.columns and "Low" in df.columns:
            df["High"] = df[["High", "Close"]].max(axis=1)
            df["Low"]  = df[["Low",  "Close"]].min(axis=1)
            logger.info("[CLEANER] OHLC integrity enforced — High≥Close≥Low corrected")

        logger.info(
            f"[CLEANER] ✓ clean() complete — output: {len(df)} rows "
            f"(from {input_rows} input, net delta={len(df) - input_rows:+d})"
        )
        return df

    @staticmethod
    def to_history_list(df: pd.DataFrame) -> list:
        """Convert cleaned DataFrame → list of dicts used by main.py and models."""
        if df.empty:
            logger.warning("[CLEANER] to_history_list() — empty DataFrame, returning []")
            return []

        logger.info(f"[CLEANER] to_history_list() — converting {len(df)} rows to dicts")
        out = []
        for ts, row in df.iterrows():
            out.append({
                "_time":  ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts,
                "open":   float(row.get("Open",   row.get("Close", 0))),
                "high":   float(row.get("High",   row.get("Close", 0))),
                "low":    float(row.get("Low",    row.get("Close", 0))),
                "close":  float(row.get("Close",  0)),
                "volume": float(row.get("Volume", 0)),
            })

        if out:
            logger.info(
                f"[CLEANER] ✓ to_history_list() — {len(out)} dict rows produced | "
                f"close: ${out[0]['close']:.2f} (oldest) → ${out[-1]['close']:.2f} (latest)"
            )
        else:
            logger.warning("[CLEANER] to_history_list() — 0 rows produced from non-empty df")
        return out


# ---------------------------------------------------------------------------
# SerpAPI Service  —  live news & trending tickers
# ---------------------------------------------------------------------------

class SerpService:
    @staticmethod
    def clean_price(price_str):
        if price_str is None:
            return 0.0
        if isinstance(price_str, (int, float)):
            return float(price_str)
        cleaned = re.sub(r"[^\d.-]", "", str(price_str))
        try:
            return float(cleaned)
        except ValueError:
            return 0.0

    async def fetch_data(self, query: str) -> dict:
        """
        Query SerpAPI Google Finance for news and trending market data.
        Returns a dict with keys:
          - news_results : list of news articles
          - markets      : dict of category → list of tickers
        Falls back to empty structure if the API key is missing or the call fails.
        """
        if not Config.SERP_API_KEY:
            logger.info(
                "[SERP] API key not set — news/trending fetch SKIPPED "
                "(returning empty news_results and markets)"
            )
            return {"news_results": [], "markets": {}}

        logger.info(
            f"[SERP] fetch_data — query='{query}', engine=google_finance, timeout=25s"
        )
        params = {
            "engine":  "google_finance",
            "q":       query,
            "api_key": Config.SERP_API_KEY,
        }
        try:
            async with httpx.AsyncClient(timeout=25.0) as client:
                resp = await client.get("https://serpapi.com/search", params=params)
                resp.raise_for_status()
                data = resp.json()

            news_count    = len(data.get("news_results", []))
            market_cats   = list(data.get("markets", {}).keys())
            market_total  = sum(
                len(v) for v in data.get("markets", {}).values() if isinstance(v, list)
            )
            logger.info(
                f"[SERP] ✓ fetch_data — query='{query}' | "
                f"news_articles={news_count} | "
                f"market_categories={market_cats} | "
                f"trending_tickers={market_total}"
            )
            return {
                "news_results": data.get("news_results", []),
                "markets":      data.get("markets",      {}),
            }

        except Exception as e:
            logger.error(f"[SERP] ✗ fetch_data error for '{query}': {e}")
            return {"news_results": [], "markets": {}}


# ---------------------------------------------------------------------------
# Sentiment Service  —  VADER-based news headline scoring
# ---------------------------------------------------------------------------

try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer as _VADER
    _VADER_AVAILABLE = True
except ImportError:
    _VADER = None  # type: ignore
    _VADER_AVAILABLE = False
    logging.getLogger(__name__).warning(
        "[SENTIMENT] vaderSentiment not installed — run: pip install vaderSentiment"
    )


class SentimentService:
    """
    VADER-based sentiment scoring for news headlines.
    VADER (Valence Aware Dictionary and sEntiment Reasoner) is purpose-built
    for short financial/social text and requires no external API or model download.

    score_headlines() returns:
      compound      : float [-1, 1]  — aggregate sentiment
      label         : str            — 'Bullish' | 'Bearish' | 'Neutral'
      scores        : list[dict]     — per-headline scores
      headline_count: int
    """

    def __init__(self):
        self._analyzer = _VADER() if _VADER_AVAILABLE else None
        if self._analyzer:
            logger.info("[SENTIMENT] ✓ VADER SentimentIntensityAnalyzer ready")
        else:
            logger.warning("[SENTIMENT] VADER unavailable — sentiment scoring disabled")

    def score_headlines(self, headlines: List[str]) -> dict:
        empty = {"compound": 0.0, "label": "Neutral", "scores": [], "headline_count": 0}
        if not self._analyzer or not headlines:
            return empty

        scores = []
        for h in headlines:
            vs = self._analyzer.polarity_scores(h)
            scores.append({"text": h[:120], "compound": round(vs["compound"], 4)})

        compounds = [s["compound"] for s in scores]
        avg = sum(compounds) / len(compounds)
        label = "Bullish" if avg >= 0.05 else "Bearish" if avg <= -0.05 else "Neutral"

        logger.info(
            f"[SENTIMENT] ✓ scored {len(scores)} headlines — "
            f"compound={avg:.4f} ({label})"
        )
        return {
            "compound":       round(avg, 4),
            "label":          label,
            "scores":         scores,
            "headline_count": len(scores),
        }


# ---------------------------------------------------------------------------
# Analyst Jury  —  3 personas, each pinned to a different model & provider
#
#   LLAMA-4-SCOUT → Groq meta-llama/llama-4-scout-17b-16e-instruct (free tier) — Macro & Risk lens
#   LLAMA-70B → Groq llama-3.3-70b-versatile    (14,400 RPD free) — Growth lens
#   QWEN3-32B → Groq qwen/qwen3-32b             (free tier)       — Quant lens
#
#   _ai_note uses Groq llama-3.3-70b independently (header note, not jury)
# ---------------------------------------------------------------------------

ANALYST_PERSONAS = [
    {
        "id":          "LLAMA-4-SCOUT",
        "avatar":      "L4",
        "title":       "Macro & Risk Lens",
        "model_label": "Groq · Llama 4 Scout",
        "provider":    "groq",
        "api_model":   "meta-llama/llama-4-scout-17b-16e-instruct",
        "color":       "#94a3b8",
        "system": (
            "You are a macro-economic and risk analyst running on Meta Llama 4 Scout. "
            "Your lens is downside scenarios, warning signals, and risk-adjusted positioning. "
            "Analyse the provided market data and news results. "
            "Identify systemic risks, fundamental red flags, and external headwinds "
            "that could materially impact this asset. "
            "Flag RSI extremes, negative trend slope, elevated volatility, or concerning forecast skew. "
            "Examine BB band squeeze/expansion, proximity to support/resistance, whether volume confirms "
            "or contradicts the move, and any fundamental red flags. "
            "Focus on what could go wrong, not growth upside. "
            "Be specific, actionable, and advisory — not generic. "
            "Conclude with a clear rating (Strong Sell / Sell / Hold) and your recommended risk stance."
        ),
    },
    {
        "id":          "LLAMA-70B",
        "avatar":      "70B",
        "title":       "Growth Lens",
        "model_label": "Groq · llama-3.3-70b",
        "provider":    "groq",
        "api_model":   "llama-3.3-70b-versatile",
        "color":       "#00f2ff",
        "system": (
            "You are LLAMA-70B, a fundamental growth equity analyst running on Llama 3.3 70B. "
            "Your lens is momentum, corporate catalysts, and upside potential. "
            "Analyse the provided market data and SERP news results. "
            "Identify fundamental tailwinds, earnings momentum, product or sector catalysts, "
            "and breakout potential. Correlate positive news flow with price momentum and volume trends. "
            "Reference MACD momentum, SMA50/200 positioning, RSI strength, and forecast confidence. "
            "Ignore macro doomsday scenarios — focus on this asset's specific growth trajectory "
            "and why it has the potential to outperform. "
            "Be specific, actionable, and advisory — not generic. "
            "Conclude with a clear rating (Strong Buy / Buy / Hold) and an upside price target or range."
        ),
    },
    {
        "id":          "QWEN3-32B",
        "avatar":      "QW",
        "title":       "Quant Lens",
        "model_label": "Groq · Qwen3 32B",
        "provider":    "groq",
        "api_model":   "qwen/qwen3-32b",
        "max_tokens":  2048,
        "color":       "#10b981",
        "system": (
            "You are QWEN3-32B, a quantitative signal analyst running on Alibaba Qwen3 32B. "
            "You operate purely on technical indicators and statistical signals — no macro bias, no news sentiment. "
            "Analyse the provided OHLCV data and indicator outputs. "
            "Interpret RSI regime, MACD histogram crossover state, Bollinger Band width and price position, "
            "SMA50/200 crossover proximity and % distance, annualised volatility, "
            "and volume confirmation of the trend. Cite the forecast confidence label explicitly. "
            "Map each signal to a clear probabilistic implication. Be precise and data-first. "
            "Conclude with a clear quantitative rating (Accumulate / Hold / Distribute) "
            "and identify specific technical entry and exit levels where applicable."
        ),
    },
]


NOTE_PROMPT_SUFFIX = (
    "\n\nRespond ONLY with valid JSON in exactly this format (no extra text):\n"
    '{"rating": "<Strong Buy|Buy|Hold|Sell|Strong Sell|Low Risk|Medium Risk|High Risk|Accumulate|Distribute>", '
    '"note": "<3-4 advisory sentences, up to 420 chars, with specific signals and implications>", '
    '"confidence": <integer 10-95>}'
)


class AnalystJuryService:
    """
    Routes analyst persona calls to the correct provider API.

    Routing logic:
      - provider == 'groq'  → Groq OpenAI-compatible endpoint (all 3 personas)

    Each persona is pinned to its own model with no cross-provider fallback,
    ensuring each analyst genuinely reflects its own model's reasoning.
    """

    GROQ_BASE_URL = "https://api.groq.com/openai/v1/chat/completions"

    # ------------------------------------------------------------------
    # Internal: generic OpenAI-compatible POST (Groq)
    # ------------------------------------------------------------------
    @newrelic.agent.function_trace()
    async def _call_openai_compatible(
        self,
        base_url: str,
        api_key: str,
        model: str,
        system: str,
        user: str,
        provider_label: str,
        max_tokens: int = 320,
    ) -> str:
        """
        Generic OpenAI-compatible POST (Groq).
        Raises httpx.HTTPStatusError on non-2xx so callers can inspect status codes.
        """
        if not api_key:
            raise ValueError(f"{provider_label} API key not configured")

        logger.info(
            f"[{provider_label}] POST → {base_url} | "
            f"model={model}, max_tokens={max_tokens}, temperature=0.65, "
            f"system_chars={len(system)}, user_chars={len(user)}"
        )

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type":  "application/json",
        }
        body = {
            "model":       model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            "max_tokens":  max_tokens,
            "temperature": 0.65,
        }
        async with httpx.AsyncClient(timeout=35.0) as client:
            resp = await client.post(base_url, json=body, headers=headers)
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"].strip()

        logger.info(
            f"[{provider_label}] ✓ Response — model={model}, "
            f"response_chars={len(content)}, "
            f"http_status={resp.status_code}"
        )
        return content

    # ------------------------------------------------------------------
    # Internal: provider-specific wrapper
    # ------------------------------------------------------------------

    async def call_groq(
        self, model: str, system: str, user: str, max_tokens: int = 320
    ) -> str:
        """Public entry point for a single Groq chat completion."""
        return await self._call_groq(model, system, user, max_tokens)

    async def _call_groq(
        self, model: str, system: str, user: str, max_tokens: int = 320
    ) -> str:
        logger.info(
            f"[GROQ] _call_groq — model={model}, max_tokens={max_tokens}"
        )
        result = await self._call_openai_compatible(
            base_url=self.GROQ_BASE_URL,
            api_key=Config.GROQ_API_KEY,
            model=model,
            system=system,
            user=user,
            provider_label="GROQ",
            max_tokens=max_tokens,
        )
        logger.info(
            f"[GROQ] ✓ _call_groq complete — model={model}, "
            f"response_chars={len(result)}"
        )
        return result

    # ------------------------------------------------------------------
    # Internal: robust structured response parser
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_analyst_response(raw: str, persona_id: str = "?") -> dict:
        import json

        logger.info(
            f"[JURY/{persona_id}] _parse_analyst_response — "
            f"raw_chars={len(raw)}, "
            f"has_think_block={'yes' if '<think>' in raw else 'no'}"
        )

        # Strip reasoning-model thinking blocks (Qwen3 emits <think>...</think>)
        if "<think>" in raw:
            if "</think>" in raw:
                cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
                logger.info(
                    f"[JURY/{persona_id}] <think> block stripped — "
                    f"remaining_chars={len(cleaned)}"
                )
            else:
                # Truncated mid-think — no JSON output yet; fall through to regex fallback
                cleaned = ""
                logger.warning(
                    f"[JURY/{persona_id}] Truncated <think> block (no </think>) — "
                    f"model ran out of tokens mid-reasoning, cleaned=''"
                )
        else:
            cleaned = raw.strip()

        # Strip markdown code fences
        cleaned = re.sub(r"```(?:json)?\s*", "", cleaned).strip()
        cleaned = cleaned.replace("```", "").strip()

        # ── Primary: brace-depth JSON extraction ─────────────────────────────
        try:
            start = cleaned.index("{")
            depth, end = 0, -1
            for i, ch in enumerate(cleaned[start:], start):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
            if end != -1:
                parsed = json.loads(cleaned[start: end + 1])
                if "rating" in parsed:
                    logger.info(
                        f"[JURY/{persona_id}] ✓ Parse path: PRIMARY (brace-depth JSON) — "
                        f"rating={parsed.get('rating')}, confidence={parsed.get('confidence')}"
                    )
                    return parsed
        except (ValueError, json.JSONDecodeError):
            logger.debug(
                f"[JURY/{persona_id}] Primary brace-depth parse failed — "
                f"trying secondary full-string parse"
            )

        # ── Secondary: full cleaned string JSON parse ─────────────────────────
        try:
            parsed = json.loads(cleaned)
            if "rating" in parsed:
                logger.info(
                    f"[JURY/{persona_id}] ✓ Parse path: SECONDARY (full-string JSON) — "
                    f"rating={parsed.get('rating')}, confidence={parsed.get('confidence')}"
                )
                return parsed
        except (json.JSONDecodeError, TypeError):
            logger.debug(
                f"[JURY/{persona_id}] Secondary full-string JSON parse failed — "
                f"falling back to plain-text field extraction"
            )

        # ── Last resort: regex field extraction from plain text ───────────────
        logger.warning(
            f"[JURY/{persona_id}] ⚠ Parse path: LAST-RESORT (regex plain-text extraction) — "
            f"both JSON parse paths failed"
        )
        rating = "Hold"
        for candidate in [
            "Strong Buy", "Strong Sell",
            "Accumulate", "Distribute",
            "Low Risk", "Medium Risk", "High Risk",
            "Buy", "Sell", "Hold",
        ]:
            if candidate.lower() in cleaned.lower():
                rating = candidate
                break

        conf_match = re.search(r"(\d{1,3})\s*%", cleaned)
        confidence = int(conf_match.group(1)) if conf_match else 60
        note       = re.sub(r"\{.*?\}", "", cleaned, flags=re.DOTALL).strip()[:420]

        result = {
            "rating":     rating,
            "note":       note,
            "confidence": min(max(confidence, 10), 95),
        }
        logger.info(
            f"[JURY/{persona_id}] Last-resort result — "
            f"rating={result['rating']}, confidence={result['confidence']}"
        )
        return result

    # ------------------------------------------------------------------
    # Public: dispatch one persona and return a structured verdict
    # ------------------------------------------------------------------
    @newrelic.agent.function_trace()
    async def get_analyst_verdict(self, persona: dict, market_ctx: str) -> dict:
        """
        Dispatches a single analyst persona to its assigned provider and model.
        All 3 personas use Groq. Provider field reserved for future expansion.
        Returns a fully structured verdict dict ready for the API response.
        """
        user_prompt = market_ctx + NOTE_PROMPT_SUFFIX
        model_used  = persona["api_model"]
        max_tok     = persona.get("max_tokens", 320)
        raw         = ""

        logger.info(
            f"[JURY/{persona['id']}] ── Dispatching — "
            f"provider={persona['provider']}, model={model_used}, "
            f"max_tokens={max_tok}, title='{persona['title']}' ──"
        )
        logger.info(
            f"[JURY/{persona['id']}] Context sent — "
            f"market_ctx_chars={len(market_ctx)}, total_prompt_chars={len(user_prompt)}"
        )

        try:
            if persona["provider"] == "groq":
                raw = await self._call_groq(model_used, persona["system"], user_prompt, max_tok)
            else:
                raise ValueError(f"Unknown provider: {persona['provider']}")

            logger.info(
                f"[JURY/{persona['id']}] ✓ Raw response received — "
                f"model={model_used}, raw_chars={len(raw)}"
            )

        except Exception as e:
            logger.error(
                f"[JURY/{persona['id']}] ✗ FAILED — model={model_used}, error={e} | "
                f"FALLBACK: returning Hold/25 default verdict"
            )
            raw = (
                '{"rating": "Hold", '
                '"note": "Model unavailable — no verdict at this time.", '
                '"confidence": 25}'
            )
            model_used = "error"

        parsed = self._parse_analyst_response(raw, persona_id=persona["id"])

        verdict = {
            "id":          persona["id"],
            "avatar":      persona["avatar"],
            "title":       persona["title"],
            "model_label": persona["model_label"],
            "color":       persona["color"],
            "rating":      parsed.get("rating",     "Hold"),
            "note":        parsed.get("note",        "No available analysis at this time."),
            "confidence":  parsed.get("confidence",  50),
            "model":       model_used,
        }
        logger.info(
            f"[JURY/{persona['id']}] Final verdict — "
            f"rating={verdict['rating']}, confidence={verdict['confidence']}%, "
            f"model={verdict['model']}, note_chars={len(verdict['note'])}"
        )
        return verdict
